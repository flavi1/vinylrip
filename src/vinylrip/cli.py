# SPDX-License-Identifier: GPL-3.0-or-later
"""Command line: ``vinylrip devices | record | process | cuts | name | play``."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import __version__, naming
from .capture import (CaptureError, FollowPlayer, Recorder, build_arecord_cmd,
                      bytes_per_minute, check_free_space, listen_pipe)
from .config import Settings, load_settings
from .devices import (Quality, default_quality, find_card, list_capture_cards,
                      probe_hw_params, quality_options)
from .identify import AcoustID, MusicBrainz, OnlineIdentifier
from .job import Job, ProcessError, process
from .pcm import PCMParams, RawPCM
from .split import SplitConfig, analyse


def _emit(args, event: dict) -> None:
    if getattr(args, "json", False):
        print(json.dumps(event, ensure_ascii=False), flush=True)
    else:
        stage = event.get("stage", "")
        extra = {k: v for k, v in event.items() if k != "stage"}
        print(f"[{stage}] {extra}" if extra else f"[{stage}]", flush=True)


def _resolve_card(settings: Settings, wanted: str | None):
    cards = list_capture_cards()
    card = find_card(cards, wanted or settings.device)
    if card is None:
        names = ", ".join(f"{c.card_id} ({c.name})" for c in cards) or "none found"
        raise CaptureError(f"capture device not found; available: {names}")
    return card


def _resolve_quality(settings: Settings, card, wanted: str | None) -> Quality:
    options = quality_options(probe_hw_params(card))
    if not options:
        raise CaptureError("the card reports no usable quality (need S16_LE, S24_3LE or S32_LE, 2 channels)")
    key = wanted or settings.quality
    if key:
        q = Quality.from_key(key)
        if q not in options:
            raise CaptureError(f"{q.label} is not offered by this card")
        return q
    return default_quality(options)


def cmd_devices(args, settings) -> int:
    for c in list_capture_cards():
        print(f"{c.card_id}\t{c.name}\t{c.hw}\tusb={c.usb_id or '-'}")
        if args.qualities:
            try:
                for q in quality_options(probe_hw_params(c)):
                    print(f"    {q.key()}\t{q.label}")
            except RuntimeError as exc:
                print(f"    (probe failed: {exc})")
    return 0


def _wait_enter(prompt: str) -> None:
    input(prompt)


def cmd_record(args, settings) -> int:
    card = _resolve_card(settings, args.device)
    q = _resolve_quality(settings, card, args.quality)
    params = PCMParams(q.rate, q.channels, q.fmt)
    need = int(bytes_per_minute(q) * 30 * args.faces)
    check_free_space(settings.work_dir, max(need, int(settings.min_free_gb * 1e9)))
    job = Job.create(settings.work_dir, params, discs=args.discs)
    print(f"job {job.data['id']} in {job.dir}; quality {q.label}")
    for _ in range(args.faces):
        label, raw, disc = job.next_face_path()
        _wait_enter(f"Face {label} (disc {disc}/{args.discs}): press Enter to start recording… ")
        rec = Recorder(build_arecord_cmd(card, q, raw), raw, params)
        rec.start()
        player = None
        if args.listen:
            player = FollowPlayer(raw, params, settings.listen_device or None, settings.listen_delay_s)
            player.start()
        _wait_enter(f"Recording face {label}. Press Enter to stop… ")
        if player:
            player.stop()
        res = rec.stop()
        print(f"face {label}: {res.seconds:.0f} s, overruns: {res.overruns}")
        if res.overruns:
            print("WARNING: buffer overruns occurred; this face may have gaps. Consider recording it again.")
        job.add_face(label, raw, disc)
    job.data["state"] = "recorded"
    job.save()
    if args.no_process:
        print(f"recorded. process later with: vinylrip process {job.dir}")
        return 0
    return _process(job, args, settings)


def _identifier(settings: Settings):
    if settings.identify_mode == "off" or not settings.acoustid_key:
        return None
    return OnlineIdentifier(MusicBrainz(settings.contact), AcoustID(settings.acoustid_key))


def _chooser(args):
    if not sys.stdin.isatty() or getattr(args, "json", False):
        return None

    def choose(ranked):
        for i, s in enumerate(ranked, start=1):
            r = s.release
            print(f"{i}. {r.artist} - {r.title} ({r.year or '?'}, {len(r.tracks)} tracks) score {s.score:.2f}")
        ans = input("Choose a number, or Enter for none: ").strip()
        return int(ans) - 1 if ans.isdigit() and 1 <= int(ans) <= len(ranked) else None
    return choose


def _process(job: Job, args, settings: Settings) -> int:
    try:
        res = process(job, settings, _identifier(settings), _chooser(args), None, lambda e: _emit(args, e))
    except ProcessError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print("The recordings are untouched. You can finish by hand (Audacity for cutting, Picard for tagging) "
              f"or fix the cause and run: vinylrip process {job.dir}", file=sys.stderr)
        return 2
    _emit(args, {"stage": "done", **{k: v for k, v in res.items() if k != "warnings"}})
    for w in res["warnings"]:
        print(f"warning: {w}", file=sys.stderr)
    return 0


def cmd_process(args, settings) -> int:
    return _process(Job.load(args.job), args, settings)


def cmd_cuts(args, settings) -> int:
    pcm = RawPCM(args.file) if os.path.exists(args.file + ".json") else RawPCM(
        args.file, PCMParams(args.rate, args.channels, args.fmt))
    plan = analyse(pcm, SplitConfig())
    print(json.dumps(plan.to_dict(), indent=2))
    return 0


def cmd_name(args, settings) -> int:
    sample = {"artiste": "Pink Floyd", "artiste_album": "Pink Floyd", "album": "The Wall",
              "annee": 1979, "piste": 3, "titre": "Another Brick in the Wall (Part 1)", "face": "A",
              "disque": 1, "nb_disques": 2, "nb_pistes": 13, "type": "Album"}
    print(naming.render(args.template or settings.template, sample, settings.portable_names))
    return 0


def cmd_play(args, settings) -> int:
    pcm = RawPCM(args.file)
    from .capture import build_aplay_cmd
    import subprocess
    with open(args.file, "rb") as fh:
        return subprocess.run(build_aplay_cmd(pcm.params, settings.listen_device or None), stdin=fh).returncode


def cmd_listen(args, settings) -> int:
    card = _resolve_card(settings, args.device)
    q = _resolve_quality(settings, card, args.quality)
    rec, play = listen_pipe(card, q, settings.listen_device or None)
    try:
        input("Listening. Press Enter to stop… ")
    finally:
        rec.terminate()
        play.terminate()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vinylrip", description=__doc__)
    p.add_argument("--version", action="version", version=f"vinylrip {__version__}")
    p.add_argument("--config", help="path of the TOML configuration")
    p.add_argument("--json", action="store_true", help="progress as JSON lines")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("devices", help="list capture cards")
    d.add_argument("--qualities", action="store_true", help="also probe supported qualities")
    d.set_defaults(fn=cmd_devices)

    r = sub.add_parser("record", help="record faces, then process")
    r.add_argument("--device")
    r.add_argument("--quality", help="rate:fmt:channels, e.g. 192000:S32_LE:2")
    r.add_argument("--faces", type=int, default=2)
    r.add_argument("--discs", type=int, default=1)
    r.add_argument("--listen", action="store_true", help="play while recording")
    r.add_argument("--no-process", action="store_true")
    r.set_defaults(fn=cmd_record)

    pr = sub.add_parser("process", help="process (or resume) a job directory")
    pr.add_argument("job")
    pr.set_defaults(fn=cmd_process)

    c = sub.add_parser("cuts", help="print the silence-based split plan of a raw file")
    c.add_argument("file")
    c.add_argument("--rate", type=int, default=192000)
    c.add_argument("--channels", type=int, default=2)
    c.add_argument("--fmt", default="S32_LE")
    c.set_defaults(fn=cmd_cuts)

    n = sub.add_parser("name", help="preview a naming template with sample values")
    n.add_argument("template", nargs="?")
    n.set_defaults(fn=cmd_name)

    pl = sub.add_parser("play", help="play a raw recording with aplay")
    pl.add_argument("file")
    pl.set_defaults(fn=cmd_play)

    ls = sub.add_parser("listen", help="listen to the turntable without recording")
    ls.add_argument("--device")
    ls.add_argument("--quality")
    ls.set_defaults(fn=cmd_listen)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.config)
        return args.fn(args, settings)
    except (CaptureError, naming.TemplateError, ValueError, RuntimeError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
