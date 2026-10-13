# SPDX-License-Identifier: GPL-3.0-or-later
"""Identify a record: fingerprints (Chromaprint/AcoustID) and releases (MusicBrainz)."""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field

from . import __version__

_SIDE = re.compile(r"^([A-Za-z])(\d+)?$")


class NetworkError(RuntimeError):
    pass


class ToolMissing(RuntimeError):
    pass


@dataclass
class TrackInfo:
    number: str
    title: str
    length_s: float | None
    disc: int
    position: int
    side: str | None = None
    recording_id: str = ""


@dataclass
class Release:
    mbid: str
    title: str
    artist: str
    year: int | None
    group_id: str
    rtype: str
    formats: list = field(default_factory=list)
    tracks: list = field(default_factory=list)

    @property
    def discs(self) -> int:
        return max((t.disc for t in self.tracks), default=1)

    @property
    def has_sides(self) -> bool:
        return bool(self.tracks) and all(t.side for t in self.tracks)

    def is_vinyl(self) -> bool:
        return any("vinyl" in f.lower() or f.startswith(("7", "10", "12")) for f in self.formats)

    def _letters_repeat_per_disc(self) -> bool:
        by_disc: dict[int, set] = {}
        for t in self.tracks:
            by_disc.setdefault(t.disc, set()).add(t.side)
        sets = list(by_disc.values())
        return len(sets) > 1 and any(sets[0] & other for other in sets[1:])

    def tracks_for_faces(self, faces: list[tuple]) -> list[list[TrackInfo]]:
        """Split release tracks per recorded face: (label, detected count[, disc]).

        Vinyl releases number tracks by side (A1, B2…), either continuing across
        discs (A..D) or restarting on every disc (A, B on each LP): both are handled.
        Without side letters, tracks are cut by the detected counts, in order.
        """
        if self.has_sides:
            per_disc = self._letters_repeat_per_disc()
            seen: dict[int, int] = {}
            out = []
            for face in faces:
                label = face[0].upper()
                disc = face[2] if len(face) > 2 else 1
                idx = seen.get(disc, 0)
                seen[disc] = idx + 1
                if per_disc:
                    letter = "ABCDEFGH"[idx]
                    out.append([t for t in self.tracks if t.disc == disc and t.side == letter])
                else:
                    out.append([t for t in self.tracks if t.side == label])
            return out
        out, i = [], 0
        for face in faces:
            count = face[1]
            out.append(self.tracks[i:i + count])
            i += count
        return out


def parse_release(data: dict) -> Release:
    credit = data.get("artist-credit") or []
    artist = "".join((c.get("name", "") + c.get("joinphrase", "")) for c in credit if isinstance(c, dict)) or "Inconnu"
    date = data.get("date") or ""
    year = int(date[:4]) if date[:4].isdigit() else None
    rg = data.get("release-group") or {}
    rtype = rg.get("primary-type") or "Album"
    formats, tracks = [], []
    for mi, medium in enumerate(data.get("media") or [], start=1):
        formats.append(medium.get("format") or "")
        for ti, tr in enumerate(medium.get("tracks") or [], start=1):
            num = str(tr.get("number") or ti)
            m = _SIDE.match(num)
            length = tr.get("length")
            tracks.append(TrackInfo(
                number=num, title=tr.get("title") or f"Piste {ti:02d}",
                length_s=(length / 1000.0 if length else None),
                disc=int(medium.get("position") or mi), position=ti,
                side=(m.group(1).upper() if m else None),
                recording_id=(tr.get("recording") or {}).get("id", ""),
            ))
    return Release(data.get("id", ""), data.get("title", "Inconnu"), artist, year,
                   rg.get("id", ""), rtype, formats, tracks)


def default_fetch(url: str, headers: dict | None = None, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise NetworkError(f"{url}: {exc}") from exc


class RateLimiter:
    """At most one call per `interval` seconds, shared by threads."""

    def __init__(self, interval: float = 1.1, clock=time.monotonic, sleep=time.sleep):
        self.interval, self._clock, self._sleep = interval, clock, sleep
        self._last = None
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            if self._last is not None:
                delay = self._last + self.interval - now
                if delay > 0:
                    self._sleep(delay)
                    now = self._clock()
            self._last = now


class MusicBrainz:
    ROOT = "https://musicbrainz.org/ws/2"

    def __init__(self, contact: str = "", fetch=None, limiter: RateLimiter | None = None):
        ua = f"vinylrip/{__version__} ( {contact or 'https://github.com/flavi1/vinylrip'} )"
        self.headers = {"User-Agent": ua, "Accept": "application/json"}
        self._fetch = fetch or default_fetch
        self.limiter = limiter or RateLimiter()

    def _get(self, path: str, **params) -> dict:
        self.limiter.wait()
        url = f"{self.ROOT}/{path}?" + urllib.parse.urlencode(dict(params, fmt="json"))
        return json.loads(self._fetch(url, self.headers))

    def get_release(self, mbid: str) -> Release:
        data = self._get(f"release/{mbid}", inc="recordings+artist-credits+release-groups+media")
        return parse_release(data)

    def search_release(self, artist: str, album: str, limit: int = 5) -> list[Release]:
        q = f'release:"{album}"' + (f' AND artist:"{artist}"' if artist else "")
        data = self._get("release", query=q, limit=limit)
        return [self.get_release(r["id"]) for r in data.get("releases", [])[:limit]]


@dataclass
class Match:
    score: float
    recording_id: str
    release_ids: list


class AcoustID:
    URL = "https://api.acoustid.org/v2/lookup"

    def __init__(self, api_key: str, fetch=None, limiter: RateLimiter | None = None):
        self.key = api_key
        self._fetch = fetch or default_fetch
        self.limiter = limiter or RateLimiter(0.35)

    def lookup(self, fingerprint: str, duration_s: int) -> list[Match]:
        if not self.key:
            raise NetworkError("no AcoustID application key configured")
        self.limiter.wait()
        q = urllib.parse.urlencode({"client": self.key, "meta": "recordings releaseids",
                                    "duration": int(duration_s), "fingerprint": fingerprint,
                                    "format": "json"})
        data = json.loads(self._fetch(f"{self.URL}?{q}", {}))
        out = []
        for res in data.get("results", []):
            for rec in res.get("recordings", []):
                rel_ids = [r["id"] for r in rec.get("releases", []) if "id" in r]
                out.append(Match(float(res.get("score", 0)), rec.get("id", ""), rel_ids))
        return out


def fingerprint_wav(path: str, runner=subprocess.run) -> tuple[str, int]:
    try:
        res = runner(["fpcalc", "-json", "-length", "120", path], capture_output=True,
                     text=True, timeout=60)
    except FileNotFoundError as exc:
        raise ToolMissing("fpcalc (chromaprint) is not installed") from exc
    if res.returncode != 0:
        raise NetworkError(f"fpcalc failed: {res.stderr.strip()}")
    d = json.loads(res.stdout)
    return d["fingerprint"], int(d["duration"])


def write_excerpt_wav(pcm, start_s: float, end_s: float, out_path: str) -> None:
    """Mono 11.025 kHz excerpt: enough for fingerprinting, small to handle."""
    p = pcm.params
    a, b = int(start_s * p.rate), int(end_s * p.rate)
    data = pcm.read_bytes(a, b)
    res = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", p.ffmpeg_fmt, "-ar", str(p.rate), "-ac", str(p.channels),
         "-i", "pipe:0", "-ac", "1", "-ar", "11025", out_path],
        input=data, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.decode(errors="replace"))


@dataclass
class Scored:
    release: Release
    score: float
    votes: float
    duration_fit: float


def duration_fit(release: Release, detected_s: list[float]) -> float:
    rl = [t.length_s for t in release.tracks]
    if not detected_s or not rl or any(x is None for x in rl):
        return 0.5
    count_fit = 1.0 if len(rl) == len(detected_s) else max(0.0, 1.0 - 0.25 * abs(len(rl) - len(detected_s)))
    tr, td = sum(rl), sum(detected_s)
    total_fit = max(0.0, 1.0 - 3.0 * abs(tr - td) / tr)
    k = min(len(rl), len(detected_s))
    err = sum(abs(rl[i] - detected_s[i]) / rl[i] for i in range(k)) / k
    track_fit = max(0.0, 1.0 - 4.0 * err)
    return 0.4 * count_fit + 0.3 * total_fit + 0.3 * track_fit


def score_releases(releases: list[Release], matches_per_track: list[list[Match]],
                   detected_s: list[float]) -> list[Scored]:
    """Rank releases by agreement with the fingerprints and with the measured durations."""
    n = max(1, sum(1 for m in matches_per_track if m))
    votes: dict[str, float] = defaultdict(float)
    for matches in matches_per_track:
        best: dict[str, float] = {}
        for m in matches:
            for rid in m.release_ids:
                best[rid] = max(best.get(rid, 0.0), m.score)
        for rid, s in best.items():
            votes[rid] += s
    out = []
    for rel in releases:
        v = min(1.0, votes.get(rel.mbid, 0.0) / n)
        fit = duration_fit(rel, detected_s)
        out.append(Scored(rel, 0.55 * v + 0.40 * fit + (0.05 if rel.is_vinyl() else 0.0), v, fit))
    out.sort(key=lambda s: s.score, reverse=True)
    return out


def choose_best(ranked: list[Scored], min_score: float = 0.55, margin: float = 0.08) -> Scored | None:
    if not ranked:
        return None
    top = ranked[0]
    if top.score < min_score:
        return None
    if len(ranked) > 1 and top.score - ranked[1].score < margin:
        return None
    return top


class OnlineIdentifier:
    """Fingerprint a few excerpts per track, vote on releases, score by duration."""

    def __init__(self, mb: MusicBrainz, acoustid: AcoustID, excerpt_s: float = 60.0,
                 max_releases: int = 6):
        self.mb, self.acoustid = mb, acoustid
        self.excerpt_s, self.max_releases = excerpt_s, max_releases

    def identify(self, faces: list) -> list[Scored]:
        """`faces`: list of (RawPCM, Plan). Returns ranked releases (may be empty)."""
        detected = [b - a for _, plan in faces for a, b in plan.tracks]
        per_track: list[list[Match]] = []
        with tempfile.TemporaryDirectory() as tmp:
            for fi, (pcm, plan) in enumerate(faces):
                for ti, (a, b) in enumerate(plan.tracks):
                    mid = (a + b) / 2
                    s = max(a, mid - self.excerpt_s / 2)
                    e = min(b, s + self.excerpt_s)
                    wav = f"{tmp}/f{fi}t{ti}.wav"
                    write_excerpt_wav(pcm, s, e, wav)
                    fp, dur = fingerprint_wav(wav)
                    per_track.append(self.acoustid.lookup(fp, dur))
        counts: dict[str, float] = defaultdict(float)
        for matches in per_track:
            for m in matches:
                for rid in m.release_ids:
                    counts[rid] += m.score
        top_ids = [r for r, _ in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)][: self.max_releases]
        releases = [self.mb.get_release(r) for r in top_ids]
        return score_releases(releases, per_track, detected)
