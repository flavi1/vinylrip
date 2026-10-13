# SPDX-License-Identifier: GPL-3.0-or-later
"""Recording with arecord and listening with aplay."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

from .devices import Card, Quality
from .pcm import PCMParams, write_sidecar


class CaptureError(RuntimeError):
    pass


def build_arecord_cmd(card: Card, q: Quality, out_path: str) -> list[str]:
    return ["arecord", "-D", card.hw, "-f", q.fmt, "-r", str(q.rate),
            "-c", str(q.channels), "-t", "raw", out_path]


def build_aplay_cmd(params: PCMParams, device: str | None = None) -> list[str]:
    cmd = ["aplay", "-q", "-t", "raw", "-f", params.fmt, "-r", str(params.rate),
           "-c", str(params.channels)]
    if device:
        cmd += ["-D", device]
    return cmd


def bytes_per_minute(q: Quality) -> int:
    from .pcm import FORMATS
    return q.rate * FORMATS[q.fmt][0] * q.channels * 60


def check_free_space(directory: str, min_bytes: int) -> None:
    os.makedirs(directory, exist_ok=True)
    free = shutil.disk_usage(directory).free
    if free < min_bytes:
        raise CaptureError(
            f"not enough free space in {directory}: {free / 1e9:.1f} GB free, "
            f"{min_bytes / 1e9:.1f} GB required"
        )


@dataclass
class RecordingResult:
    path: str
    frames: int
    seconds: float
    overruns: int
    errors: list


class Recorder:
    """Runs arecord, watches its messages, stops it cleanly."""

    def __init__(self, cmd: list[str], out_path: str, params: PCMParams, popen=subprocess.Popen):
        self.cmd, self.out_path, self.params = cmd, out_path, params
        self._popen = popen
        self._proc = None
        self._thread = None
        self.overruns = 0
        self.errors: list[str] = []
        self._t0 = 0.0

    def start(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.out_path)), exist_ok=True)
        try:
            self._proc = self._popen(self.cmd, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, text=True)
        except FileNotFoundError as exc:
            raise CaptureError(f"{self.cmd[0]} not found") from exc
        self._t0 = time.time()
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()

    def _watch(self) -> None:
        for line in self._proc.stderr:
            low = line.lower()
            if "overrun" in low:
                self.overruns += 1
            elif line.strip() and "recording" not in low:
                self.errors.append(line.strip())

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def elapsed(self) -> float:
        return time.time() - self._t0 if self._proc else 0.0

    def stop(self) -> RecordingResult:
        if self._proc is None:
            raise CaptureError("recording was never started")
        if self._proc.poll() is None:
            self._proc.send_signal(signal.SIGINT)
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.terminate()
                self._proc.wait(timeout=5)
        if self._thread:
            self._thread.join(timeout=2)
        size = os.path.getsize(self.out_path) if os.path.exists(self.out_path) else 0
        frames = size // self.params.frame_bytes
        write_sidecar(self.out_path, self.params, frames=frames, overruns=self.overruns,
                      recorded_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        return RecordingResult(self.out_path, frames, frames / self.params.rate,
                               self.overruns, list(self.errors))


class FollowPlayer:
    """Plays a raw file while it is still being written, after an initial delay.

    If playback catches up with the writer it waits until `delay_s` of audio is
    available again instead of stuttering. Failure here never touches the recording.
    """

    def __init__(self, path: str, params: PCMParams, device: str | None = None,
                 delay_s: float = 0.5, popen=subprocess.Popen, block_s: float = 0.1):
        self.path, self.params, self.device = path, params, device
        self.delay_s, self._popen, self.block_s = delay_s, popen, block_s
        self._stop = threading.Event()
        self._thread = None
        self._proc = None
        self.error: str | None = None
        self.rebuffers = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _available(self, pos: int) -> int:
        try:
            return os.path.getsize(self.path) - pos
        except OSError:
            return 0

    def _run(self) -> None:
        fb = self.params.frame_bytes
        delay_b = int(self.delay_s * self.params.rate) * fb
        block_b = max(1, int(self.block_s * self.params.rate)) * fb
        try:
            self._proc = self._popen(build_aplay_cmd(self.params, self.device),
                                     stdin=subprocess.PIPE, stderr=subprocess.PIPE)
            pos = 0
            buffering = True
            with open_when_exists(self.path, self._stop) as fh:
                while not self._stop.is_set():
                    avail = self._available(pos)
                    if buffering:
                        if avail < delay_b:
                            time.sleep(0.02)
                            continue
                        buffering = False
                    if avail < block_b:
                        self.rebuffers += 1
                        buffering = True
                        continue
                    fh.seek(pos)
                    data = fh.read(block_b - block_b % fb)
                    if not data:
                        continue
                    pos += len(data)
                    self._proc.stdin.write(data)
                    self._proc.stdin.flush()
        except (BrokenPipeError, OSError, FileNotFoundError) as exc:
            self.error = str(exc)
        finally:
            self._close_proc()

    def _close_proc(self) -> None:
        if self._proc is not None:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.terminate()
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)


class open_when_exists:
    def __init__(self, path, stop_event):
        self.path, self.stop_event, self.fh = path, stop_event, None

    def __enter__(self):
        while not self.stop_event.is_set():
            try:
                self.fh = open(self.path, "rb")
                return self.fh
            except FileNotFoundError:
                time.sleep(0.05)
        raise OSError("stopped before the file existed")

    def __exit__(self, *exc):
        if self.fh:
            self.fh.close()


def listen_pipe(card: Card, q: Quality, device: str | None = None, popen=subprocess.Popen):
    """`arecord … | aplay …` for listening without recording. Returns (arecord, aplay)."""
    params = PCMParams(q.rate, q.channels, q.fmt)
    rec = popen(["arecord", "-D", card.hw, "-f", q.fmt, "-r", str(q.rate),
                 "-c", str(q.channels), "-t", "raw"], stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL)
    play = popen(build_aplay_cmd(params, device), stdin=rec.stdout,
                 stderr=subprocess.DEVNULL)
    rec.stdout.close()
    return rec, play
