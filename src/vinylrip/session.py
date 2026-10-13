# SPDX-License-Identifier: GPL-3.0-or-later
"""A JSON-lines recording session, driven by another program (e.g. the Kodi plugin).

Commands (one JSON object per line on stdin)::

    {"cmd": "start_face"}                  start recording the next face
    {"cmd": "listen", "on": true}          play the recording while it runs (aplay)
    {"cmd": "stop_face"}                   stop the current face and keep it
    {"cmd": "cancel", "delete": true}      abort; delete or keep what was recorded
    {"cmd": "finish"}                      process everything (split, identify, encode…)
    {"cmd": "choose", "index": 0|null}     answer a ``choose_release`` event

Events (one JSON object per line on stdout) carry an ``event`` key: ``ready``, ``recording``,
``face_done``, ``listening``, ``stage``, ``choose_release``, ``done``, ``cancelled``, ``error``.
"""
from __future__ import annotations

import shutil

from .capture import (CaptureError, FollowPlayer, Recorder, build_arecord_cmd,
                      bytes_per_minute, check_free_space)
from .config import Settings
from .devices import Card, Quality
from .job import FACE_LETTERS, Job, ProcessError, process
from .pcm import PCMParams


class Session:
    def __init__(self, settings: Settings, card: Card, quality: Quality, discs: int, emit,
                 get_command, identifier=None, recorder_cls=Recorder, player_cls=FollowPlayer,
                 process_fn=process, cover_fetch=None):
        self.settings, self.card, self.quality, self.discs = settings, card, quality, discs
        self.emit, self.get_command, self.identifier = emit, get_command, identifier
        self.recorder_cls, self.player_cls, self.process_fn = recorder_cls, player_cls, process_fn
        self.cover_fetch = cover_fetch
        self.params = PCMParams(quality.rate, quality.channels, quality.fmt)
        self.job: Job | None = None
        self._rec = None
        self._player = None
        self._current: tuple | None = None
        self.finished = False

    # -- lifecycle -----------------------------------------------------------------
    def open(self) -> bool:
        try:
            need = max(int(bytes_per_minute(self.quality) * 30 * 2 * self.discs),
                       int(self.settings.min_free_gb * 1e9))
            check_free_space(self.settings.work_dir, need)
            self.job = Job.create(self.settings.work_dir, self.params, self.discs)
        except (CaptureError, OSError) as exc:
            self.emit({"event": "error", "message": str(exc), "kept": None})
            return False
        self.emit({"event": "ready", "job": self.job.dir, "quality": self.quality.label,
                   "device": self.card.hw, "discs": self.discs})
        return True

    def handle(self, cmd: dict) -> None:
        name = cmd.get("cmd")
        fn = getattr(self, f"_cmd_{name}", None)
        if fn is None:
            self.emit({"event": "error", "message": f"unknown command {name!r}", "kept": self._kept()})
            return
        try:
            fn(cmd)
        except (CaptureError, ProcessError, ValueError, OSError, RuntimeError) as exc:
            self._stop_player()
            self.emit({"event": "error", "message": str(exc), "kept": self._kept()})

    def close(self) -> None:
        """Stdin closed: never lose a running recording."""
        if self._rec is not None:
            try:
                self._cmd_stop_face({})
            except Exception:
                pass

    def _kept(self) -> str | None:
        return self.job.dir if self.job else None

    # -- commands ----------------------------------------------------------------------
    def _cmd_start_face(self, cmd: dict) -> None:
        if self._rec is not None:
            raise CaptureError("a face is already being recorded")
        if len(self.job.faces) >= len(FACE_LETTERS):
            raise CaptureError("too many faces")
        label, raw, disc = self.job.next_face_path()
        rec = self.recorder_cls(build_arecord_cmd(self.card, self.quality, raw), raw, self.params)
        rec.start()
        self._rec, self._current = rec, (label, raw, disc)
        self.emit({"event": "recording", "face": label, "disc": disc, "path": raw})
        if cmd.get("listen"):
            self._cmd_listen({"on": True})

    def _cmd_listen(self, cmd: dict) -> None:
        if cmd.get("on", True):
            if self._rec is None:
                raise CaptureError("nothing is being recorded")
            if self._player is None:
                self._player = self.player_cls(self._current[1], self.params,
                                               self.settings.listen_device or None, self.settings.listen_delay_s)
                self._player.start()
            self.emit({"event": "listening", "on": True})
        else:
            self._stop_player()
            self.emit({"event": "listening", "on": False})

    def _stop_player(self) -> None:
        if self._player is not None:
            self._player.stop()
            self._player = None

    def _cmd_stop_face(self, cmd: dict) -> None:
        if self._rec is None:
            raise CaptureError("no recording in progress")
        self._stop_player()
        label, raw, disc = self._current
        res = self._rec.stop()
        self._rec, self._current = None, None
        self.job.add_face(label, raw, disc)
        self.emit({"event": "face_done", "face": label, "disc": disc, "seconds": round(res.seconds, 1),
                   "overruns": res.overruns, "errors": res.errors[:3]})

    def _cmd_cancel(self, cmd: dict) -> None:
        if self._rec is not None:
            self._stop_player()
            self._rec.stop()
            self._rec = None
        deleted = bool(cmd.get("delete")) and self.job is not None
        kept = self._kept()
        if deleted:
            shutil.rmtree(self.job.dir, ignore_errors=True)
            kept = None
        self.finished = True
        self.emit({"event": "cancelled", "deleted": deleted, "kept": kept})

    def _cmd_finish(self, cmd: dict) -> None:
        if self._rec is not None:
            raise CaptureError("a face is still being recorded")
        if not self.job.faces:
            raise CaptureError("nothing was recorded")
        self.job.data["state"] = "recorded"
        self.job.save()
        res = self.process_fn(self.job, self.settings, self.identifier, self._choose, self.cover_fetch,
                                  lambda e: self.emit({"event": "stage", **e}))
        self.finished = True
        self.emit({"event": "done", **res})

    def _cmd_choose(self, cmd: dict) -> None:
        pass  # answers are consumed by _choose; a stray one is ignored

    def _choose(self, ranked):
        self.emit({"event": "choose_release", "candidates": [
            {"index": i, "artist": s.release.artist, "album": s.release.title, "year": s.release.year,
             "tracks": len(s.release.tracks), "type": s.release.rtype, "score": round(s.score, 2),
             "mbid": s.release.mbid} for i, s in enumerate(ranked)]})
        while True:
            cmd = self.get_command()
            if cmd is None:
                return None
            if cmd.get("cmd") == "choose":
                idx = cmd.get("index")
                return idx if isinstance(idx, int) and 0 <= idx < len(ranked) else None
