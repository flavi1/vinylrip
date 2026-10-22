# SPDX-License-Identifier: GPL-3.0-or-later
"""Jobs: one session = one disc, or one multi-disc album, processed in resumable stages."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field

from . import naming
from .align import align
from .config import Settings
from .convert import ConvertError, check_reduced, reduce_master
from .cover import extension_for, fetch_cover
from .encode import EncodeError, encode_master, verify_master
from .identify import NetworkError, Release, ToolMissing, TrackInfo, choose_best
from .pcm import PCMParams, RawPCM, pack_s32_to_s24, sidecar_path
from .split import Plan, SplitConfig, analyse, apply_cuts


class ProcessError(RuntimeError):
    """Processing failed at `stage`; `kept` lists partial work left on disk."""

    def __init__(self, message: str, stage: str | None = None, kept: list[str] | None = None):
        super().__init__(message)
        self.stage = stage
        self.kept = list(kept or [])


FACE_LETTERS = "ABCDEFGH"


class Job:
    def __init__(self, directory: str, data: dict):
        self.dir = directory
        self.data = data

    # -- creation / persistence ------------------------------------------------
    @staticmethod
    def create(work_dir: str, params: PCMParams, discs: int = 1, job_id: str | None = None) -> "Job":
        job_id = job_id or time.strftime("%Y%m%d-%H%M%S")
        directory = os.path.join(work_dir, "jobs", job_id)
        os.makedirs(directory, exist_ok=False)
        job = Job(directory, {
            "id": job_id, "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "discs": discs,
            "params": {"rate": params.rate, "channels": params.channels, "fmt": params.fmt},
            "faces": [], "state": "recording", "result": {},
        })
        job.save()
        return job

    @staticmethod
    def load(directory: str) -> "Job":
        with open(os.path.join(directory, "job.json"), encoding="utf-8") as fh:
            return Job(directory, json.load(fh))

    def save(self) -> None:
        tmp = os.path.join(self.dir, "job.json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, os.path.join(self.dir, "job.json"))

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    @property
    def params(self) -> PCMParams:
        p = self.data["params"]
        return PCMParams(p["rate"], p["channels"], p["fmt"])

    @property
    def faces(self) -> list[dict]:
        return self.data["faces"]

    @property
    def discs(self) -> int:
        return self.data["discs"]

    def next_face_path(self) -> tuple[str, str, int]:
        """(label, raw path, disc number) for the next face to record."""
        idx = len(self.faces)
        label = FACE_LETTERS[idx]
        per_disc = 2
        disc = idx // per_disc + 1 if self.discs > 1 else 1
        return label, self.path(f"face_{label.lower()}.pcm"), min(disc, self.discs)

    def add_face(self, label: str, raw: str, disc: int = 1) -> None:
        self.faces.append({"label": label, "disc": disc, "raw": raw, "status": "recorded"})
        self.save()


# -- helpers ------------------------------------------------------------------
def _tags(release: Release | None, album, artist, year, title, number, total, disc, discs, track) -> dict:
    t = {"ARTIST": artist, "ALBUMARTIST": artist, "ALBUM": album, "TITLE": title,
         "TRACKNUMBER": str(number), "TRACKTOTAL": str(total),
         "DISCNUMBER": str(disc), "DISCTOTAL": str(discs)}
    if year:
        t["DATE"] = str(year)
    if release and release.mbid:
        t["MUSICBRAINZ_ALBUMID"] = release.mbid
        if release.group_id:
            t["MUSICBRAINZ_RELEASEGROUPID"] = release.group_id
    if track is not None and track.recording_id:
        t["MUSICBRAINZ_TRACKID"] = track.recording_id
    return t


def _finalize(tmp_root: str, dest_root: str, rels: list[str], multidisc: bool) -> str:
    """Move the album folder out of the temp tree without ever overwriting."""
    album_rel = os.path.dirname(rels[0])
    if multidisc:
        album_rel = os.path.dirname(album_rel)
    os.makedirs(dest_root, exist_ok=True)
    if album_rel:
        dst = naming.unique_dir(os.path.join(dest_root, album_rel))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(os.path.join(tmp_root, album_rel), dst)
    else:
        dst = dest_root
        for rel in rels:
            target = os.path.join(dest_root, rel)
            if os.path.exists(target):
                raise ProcessError(f"{target} already exists; nothing was overwritten")
            shutil.move(os.path.join(tmp_root, rel), target)
    shutil.rmtree(tmp_root, ignore_errors=True)
    return dst


def process(job: Job, settings: Settings, identifier=None, chooser=None,
            cover_fetch=None, on_event=None) -> dict:
    """Split, identify, align, encode, verify, convert and file one job. Returns the result.

    Any failure is raised as ProcessError carrying the stage that failed and the
    partial work kept on disk (see ``recovery.help_text``)."""
    cur = {"stage": "split", "kept": []}
    try:
        return _process(job, settings, identifier, chooser, cover_fetch, on_event, cur)
    except ProcessError as exc:
        if exc.stage is None:
            exc.stage = cur["stage"]
        if not exc.kept:
            exc.kept = list(cur["kept"])
        raise
    except Exception as exc:  # flac, ffmpeg, disque plein, gabarit… : toujours expliqué
        raise ProcessError(str(exc), cur["stage"], list(cur["kept"])) from exc


def _process(job: Job, settings: Settings, identifier, chooser, cover_fetch, on_event, cur: dict) -> dict:
    ev = on_event or (lambda e: None)
    warnings: list[str] = []
    split_cfg = SplitConfig(strong_gap_s=settings.strong_gap_s, weak_gap_s=settings.weak_gap_s,
                            noise_segment_max_s=settings.noise_segment_max_s,
                            head_margin_s=settings.head_margin_s, tail_margin_s=settings.tail_margin_s)
    faces = []
    for f in job.faces:
        pcm = RawPCM(f["raw"])
        ev({"stage": "split", "face": f["label"]})
        plan = analyse(pcm, split_cfg)
        with open(job.path(f"cuts_{f['label']}.json"), "w", encoding="utf-8") as fh:
            json.dump(plan.to_dict(), fh, indent=2)
        faces.append((f, pcm, plan))
        if plan.notes:
            warnings.extend(f"face {f['label']}: {n}" for n in plan.notes)

    # -- identification -------------------------------------------------------
    release: Release | None = None
    ranked = []
    cur["stage"] = "identify"
    if settings.identify_mode != "off" and identifier is not None:
        ev({"stage": "identify"})
        try:
            ranked = identifier.identify([(pcm, plan) for _, pcm, plan in faces])
        except (NetworkError, ToolMissing) as exc:
            warnings.append(f"identification unavailable: {exc}")
        if ranked:
            top = choose_best(ranked, settings.min_score, settings.margin) if settings.identify_mode == "auto" else None
            if top is None and chooser is not None:
                idx = chooser(ranked)
                top = ranked[idx] if idx is not None else None
            elif top is None and settings.identify_mode == "auto" and ranked[0].score >= settings.min_score:
                top = ranked[0]
            release = top.release if top else None
    ev({"stage": "release", "mbid": release.mbid if release else None})

    # -- pass 2: alignment, titles ---------------------------------------------
    face_tracks = release.tracks_for_faces([(f["label"], len(plan.tracks), f["disc"]) for f, _, plan in faces]) \
        if release else [[] for _ in faces]
    entries = []   # one per output track
    disc_counts: dict[int, int] = {}
    for (f, pcm, plan), infos in zip(faces, face_tracks):
        tracks = list(plan.tracks)
        titles: list[TrackInfo | None] = [None] * len(tracks)
        if release and infos and all(t.length_s for t in infos):
            al = align(plan, [t.length_s for t in infos])
            if al is not None and not al.doubtful:
                tracks = apply_cuts(plan, al.cuts, split_cfg)
                titles = list(infos)
                if len(tracks) != len(plan.tracks):
                    warnings.append(f"face {f['label']}: cuts corrected to match the release "
                                    f"({len(plan.tracks)} -> {len(tracks)} tracks)")
            else:
                f["status"] = "to_check"
                warnings.append(f"face {f['label']}: could not align with the release; kept the silence-based cuts")
                if len(infos) == len(tracks):
                    titles = list(infos)
        elif release and infos and len(infos) == len(tracks):
            titles = list(infos)
        for (a, b), info in zip(tracks, titles):
            n = disc_counts.get(f["disc"], 0) + 1
            disc_counts[f["disc"]] = n
            entries.append({"face": f["label"], "disc": f["disc"], "number": n, "info": info,
                            "start": int(round(a * pcm.params.rate)),
                            "end": min(pcm.frames, int(round(b * pcm.params.rate))), "pcm": pcm})
    job.save()

    # -- metadata and naming ----------------------------------------------------
    created = job.data["created"]
    album = release.title if release else f"Vinyle {created[:10]} {created[11:13]}{created[14:16]}"
    artist = release.artist if release else "Inconnu"
    year = release.year if release else None
    rtype = release.rtype if release else "Album"
    multidisc = job.discs > 1
    if release:
        template = settings.multidisc_template if multidisc else settings.template
    else:
        template = "Inconnu/{album}/CD{disque}/Piste {piste:02d}.flac" if multidisc \
            else "Inconnu/{album}/Piste {piste:02d}.flac"
    naming.check_template(template)

    cover = None
    if release and (settings.embed_cover or settings.cover_file):
        cover = (cover_fetch or fetch_cover)(release.mbid)

    params = job.params
    use24 = False
    if settings.master_24bit_if_lossless and params.fmt == "S32_LE" and \
            all(RawPCM(f["raw"]).low_bits_zero() for f in job.faces):
        use24 = True
    reduce_to = None
    cap_bits = 24 if use24 else params.bits
    if settings.wants_reduction(params.rate, cap_bits):
        reduce_to = (settings.output_rate or params.rate, min(settings.output_bits or cap_bits, 24))

    masters_tmp = tempfile.mkdtemp(prefix=".vinylrip-", dir=_ensure(settings.masters_root if reduce_to else settings.resolved_music_root()))
    music_tmp = tempfile.mkdtemp(prefix=".vinylrip-", dir=_ensure(settings.resolved_music_root())) if reduce_to else masters_tmp
    cover_path = None
    if cover and settings.embed_cover:
        cover_path = os.path.join(masters_tmp, f".embed.{extension_for(cover)}")
        with open(cover_path, "wb") as fh:
            fh.write(cover)

    cur["stage"], cur["kept"] = "encode", sorted({masters_tmp, music_tmp})
    rels, ok_all = [], True
    total = len(entries)
    sources = {}
    for f, pcm, _ in faces:
        if use24:
            p24 = pack_s32_to_s24(pcm, job.path(f"face_{f['label'].lower()}.s24.pcm"))
            sources[f["label"]] = p24
        else:
            sources[f["label"]] = pcm
    for i, e in enumerate(entries, start=1):
        info = e["info"]
        title = info.title if info else f"Piste {e['number']:02d}"
        values = {"artiste": artist, "artiste_album": artist, "album": album, "annee": year,
                  "piste": e["number"], "titre": title, "face": e["face"], "disque": e["disc"],
                  "nb_disques": job.discs, "nb_pistes": disc_counts[e["disc"]], "type": rtype}
        rel = naming.render(template, values, settings.portable_names)
        rels.append(rel)
        src = sources[e["face"]]
        tags = _tags(release, album, artist, year, title, e["number"], disc_counts[e["disc"]],
                     e["disc"], job.discs, info)
        out = os.path.join(masters_tmp, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        ev({"stage": "encode", "track": i, "of": total})
        try:
            encode_master(src, e["start"], e["end"], out, tags, cover_path, settings.encoder)
            vr = verify_master(src, e["start"], e["end"], out, settings.encoder)
        except EncodeError as exc:
            raise ProcessError(str(exc), "encode", [masters_tmp]) from exc
        if not vr.ok:
            raise ProcessError(f"verification failed for {rel}: decoded data differs from the capture",
                               "verify", [masters_tmp])
        if reduce_to:
            red = os.path.join(music_tmp, rel)
            os.makedirs(os.path.dirname(red), exist_ok=True)
            ev({"stage": "convert", "track": i, "of": total})
            try:
                reduce_master(out, red, reduce_to[0], reduce_to[1], settings.dither)
                chk = check_reduced(out, red, reduce_to[0])
            except ConvertError as exc:
                raise ProcessError(str(exc), "convert", [masters_tmp]) from exc
            if not chk.ok:
                ok_all = False
                warnings.append(f"{rel}: reduced copy outside tolerance ({chk})")
    if cover_path and os.path.exists(cover_path):
        os.remove(cover_path)

    # cover file next to the tracks
    album_rel = os.path.dirname(rels[0]) if not multidisc else os.path.dirname(os.path.dirname(rels[0]))
    if cover and settings.cover_file:
        for root in {music_tmp}:
            os.makedirs(os.path.join(root, album_rel), exist_ok=True)
            with open(os.path.join(root, album_rel, f"cover.{extension_for(cover)}"), "wb") as fh:
                fh.write(cover)

    # optional archive of each whole face
    if settings.keep_full_face:
        cur["stage"] = "archive"
        arch = os.path.join(job.dir, "archive")
        os.makedirs(arch, exist_ok=True)
        for f, pcm, _ in faces:
            src = sources[f["label"]]
            out = os.path.join(arch, f"face_{f['label'].lower()}.flac")
            encode_master(src, 0, src.frames, out, {"ALBUM": album}, None, settings.encoder)
            if not verify_master(src, 0, src.frames, out, settings.encoder).ok:
                raise ProcessError(f"archive of face {f['label']} failed verification")

    # -- publish ------------------------------------------------------------------
    cur["stage"], cur["kept"] = "file", sorted({masters_tmp, music_tmp})
    final_music = _finalize(music_tmp, settings.resolved_music_root(), rels, multidisc)
    final_masters = final_music
    if reduce_to:
        final_masters = _finalize(masters_tmp, settings.masters_root, rels, multidisc)
    ev({"stage": "filed", "album_dir": final_music})

    to_check = [f["label"] for f in job.faces if f.get("status") == "to_check"]
    deleted_raw = False
    deleted_master = False
    safe = ok_all and not to_check
    if reduce_to and settings.delete_master_after_reduce and safe:
        shutil.rmtree(final_masters, ignore_errors=True)
        _prune_empty(os.path.dirname(final_masters), settings.masters_root)
        deleted_master = True
    if settings.delete_raw and safe:
        for f in job.faces:
            for p in (f["raw"], sidecar_path(f["raw"]), f["raw"].replace(".pcm", ".s24.pcm"),
                      sidecar_path(f["raw"].replace(".pcm", ".s24.pcm"))):
                if os.path.exists(p):
                    os.remove(p)
        deleted_raw = True
    elif not safe:
        warnings.append("raw recordings kept: some faces need checking")

    result = {"album_dir": final_music, "masters_dir": final_masters if reduce_to and not deleted_master else None,
              "album": album, "artist": artist, "year": year, "release": release.mbid if release else None,
              "tracks": len(entries), "faces_to_check": to_check, "deleted_raw": deleted_raw,
              "deleted_master": deleted_master, "warnings": warnings}
    job.data["state"] = "done" if safe else "needs_attention"
    job.data["result"] = result
    job.save()
    return result


def _ensure(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def _prune_empty(path: str, stop: str) -> None:
    """Remove empty directories from `path` upwards, never `stop` itself."""
    stop = os.path.abspath(stop)
    path = os.path.abspath(path)
    while path != stop and path.startswith(stop + os.sep):
        try:
            os.rmdir(path)
        except OSError:
            break
        path = os.path.dirname(path)
