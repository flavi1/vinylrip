# SPDX-License-Identifier: GPL-3.0-or-later
"""Pass 1 of track splitting: silence analysis only, no outside information."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np

from .pcm import RawPCM

WIN_S = 0.02        # analysis window
BLOCK_WINS = 5      # robust level = low percentile over 5 windows = 0.1 s
HOP_S = WIN_S * BLOCK_WINS


@dataclass
class SplitConfig:
    strong_gap_s: float = 1.0
    weak_gap_s: float = 0.3
    noise_segment_max_s: float = 15.0
    noise_fraction: float = 0.95
    margin_db: float = 8.0           # threshold above the noise floor
    threshold_db: float | None = None  # absolute override
    head_margin_s: float = 1.0
    tail_margin_s: float = 3.0


@dataclass
class Candidate:
    pos_s: float
    strength: str     # "strong" | "weak"
    length_s: float


@dataclass
class Plan:
    rate: int
    duration_s: float
    floor_db: float
    threshold_db: float
    music_start_s: float
    music_end_s: float
    trim_start_s: float
    trim_end_s: float
    tracks: list = field(default_factory=list)        # [(start_s, end_s)]
    candidates: list = field(default_factory=list)    # [Candidate]
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tracks"] = [list(t) for t in self.tracks]
        return d

    @staticmethod
    def from_dict(d: dict) -> "Plan":
        d = dict(d)
        d["tracks"] = [tuple(t) for t in d["tracks"]]
        d["candidates"] = [Candidate(**c) for c in d["candidates"]]
        return Plan(**d)


def envelope_db(pcm: RawPCM, chunk_s: float = 20.0) -> np.ndarray:
    """RMS level in dBFS per 20 ms window, from the channel mean."""
    p = pcm.params
    w = max(1, int(round(p.rate * WIN_S)))
    chunk_frames = w * int(chunk_s / WIN_S)
    total = pcm.frames
    out = []
    pos = 0
    while pos + w <= total:
        end = min(total, pos + chunk_frames)
        end -= (end - pos) % w
        if end <= pos:
            break
        a = pcm.read_int32(pos, end).astype(np.float64).mean(axis=1) / p.full_scale
        r = np.sqrt((a.reshape(-1, w) ** 2).mean(axis=1))
        out.append(20.0 * np.log10(np.maximum(r, 1e-9)))
        pos = end
    return np.concatenate(out) if out else np.zeros(0)


def robust_levels(db: np.ndarray) -> np.ndarray:
    """Low-percentile level per 0.1 s: clicks and crackles do not lift it."""
    n = len(db) // BLOCK_WINS
    if n == 0:
        return np.zeros(0)
    blocks = db[: n * BLOCK_WINS].reshape(n, BLOCK_WINS)
    return np.percentile(blocks, 30, axis=1)


def _runs(mask: np.ndarray):
    """Yield (start, end_exclusive) of True runs."""
    if len(mask) == 0:
        return
    padded = np.concatenate(([False], mask, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    for s, e in zip(edges[::2], edges[1::2]):
        yield int(s), int(e)


def analyse(pcm: RawPCM, cfg: SplitConfig | None = None) -> Plan:
    cfg = cfg or SplitConfig()
    db = envelope_db(pcm)
    rob = robust_levels(db)
    duration = pcm.duration
    if len(rob) < 10:
        raise ValueError("recording too short to analyse")
    floor = float(np.percentile(rob, 1))
    thr = cfg.threshold_db if cfg.threshold_db is not None else floor + cfg.margin_db
    below = rob < thr
    music = np.flatnonzero(~below)
    if len(music) == 0:
        raise ValueError("no music found: the whole recording is below the silence threshold")
    m0 = float(music[0] * HOP_S)
    m1 = float((music[-1] + 1) * HOP_S)

    smooth = np.convolve(db, np.ones(BLOCK_WINS) / BLOCK_WINS, mode="same")
    cands: list[Candidate] = []
    for s, e in _runs(below):
        length = (e - s) * HOP_S
        if length < cfg.weak_gap_s - 1e-9:
            continue
        ws, we = s * BLOCK_WINS, min(len(smooth), e * BLOCK_WINS)
        quarter = (we - ws) // 4          # cut in the central half of the gap, at its quietest point
        ws, we = ws + quarter, max(ws + quarter + 1, we - quarter)
        pos = (ws + int(np.argmin(smooth[ws:we]))) * WIN_S
        if pos <= m0 or pos >= m1:
            continue  # lead-in / run-out: handled by trimming
        strength = "strong" if length >= cfg.strong_gap_s - 1e-9 else "weak"
        cands.append(Candidate(round(pos, 3), strength, round(length, 3)))

    cuts = [c.pos_s for c in cands if c.strength == "strong"]
    bounds = [m0] + cuts + [m1]
    segs = [[bounds[i], bounds[i + 1]] for i in range(len(bounds) - 1)]
    notes: list[str] = []

    def noise_like(a: float, b: float) -> bool:
        i0, i1 = int(a / HOP_S), max(int(a / HOP_S) + 1, int(b / HOP_S))
        part = rob[i0:i1]
        return len(part) > 0 and float(np.mean(part < thr + 6.0)) >= cfg.noise_fraction

    merged: list[list[float]] = []
    lead = None
    for seg in segs:
        dur = seg[1] - seg[0]
        if dur < cfg.noise_segment_max_s and noise_like(*seg):
            if merged:
                merged[-1][1] = seg[1]
                notes.append(f"merged noise-only segment {seg[0]:.1f}-{seg[1]:.1f}s into previous track")
            else:
                lead = seg
            continue
        if lead is not None:
            seg = [lead[0], seg[1]]
            lead = None
            notes.append("merged leading noise-only segment into next track")
        merged.append(seg)
    if not merged:
        merged = [[m0, m1]]
    tracks = [(round(max(0.0, a), 3), round(min(duration, b), 3)) for a, b in merged]

    trim0 = max(0.0, m0 - cfg.head_margin_s)
    trim1 = min(duration, m1 + cfg.tail_margin_s)
    if tracks:
        tracks[0] = (round(trim0, 3), tracks[0][1])
        tracks[-1] = (tracks[-1][0], round(trim1, 3))
    return Plan(
        rate=pcm.params.rate, duration_s=round(duration, 3),
        floor_db=round(floor, 2), threshold_db=round(float(thr), 2),
        music_start_s=round(m0, 3), music_end_s=round(m1, 3),
        trim_start_s=round(trim0, 3), trim_end_s=round(trim1, 3),
        tracks=tracks, candidates=cands, notes=notes,
    )


def apply_cuts(plan: Plan, cut_positions: list[float], cfg: SplitConfig | None = None) -> list[tuple]:
    """Tracks from a chosen list of internal cut positions (pass 2)."""
    cfg = cfg or SplitConfig()
    bounds = [plan.trim_start_s] + list(cut_positions) + [plan.trim_end_s]
    return [(round(bounds[i], 3), round(bounds[i + 1], 3)) for i in range(len(bounds) - 1)]
