# SPDX-License-Identifier: GPL-3.0-or-later
"""Listening through another player (Kodi…): the sound is served as an endless
WAV over HTTP on 127.0.0.1, instead of being played by aplay.

Useful when that player holds the sound card: it plays the stream with its own
output, and the turntable's capture card is only used by arecord.

- ``LiveSource``: arecord running for the whole session, shared by all clients
  (a capture card cannot be opened twice).
- ``FollowSource``: a raw file being recorded, read from its current end, so
  listening during a recording never touches the capture.
"""
from __future__ import annotations

import os
import queue
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .pcm import FORMATS, PCMParams

PATH = "/vinyl.wav"


def wav_header(params: PCMParams) -> bytes:
    """WAV header of unknown length (sizes at their maximum): read as a stream."""
    bps = FORMATS[params.fmt][0]
    bits = 24 if params.fmt == "S24_3LE" else bps * 8
    block = bps * params.channels
    fmt = struct.pack("<HHIIHH", 1, params.channels, params.rate, params.rate * block, block, bits)
    return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"data" + struct.pack("<I", 0xFFFFFFFF - 36))


class LiveSource:
    """One process (arecord) whose output is copied to every client."""

    def __init__(self, cmd: list[str], params: PCMParams, popen=subprocess.Popen, block_s: float = 0.05, stderr=None):
        self.cmd, self.params, self._popen = cmd, params, popen
        self.stderr = stderr if stderr is not None else subprocess.DEVNULL
        self.ended = threading.Event()
        self.block = max(1, int(block_s * params.rate)) * params.frame_bytes
        self._clients: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._proc = None
        self.error: str | None = None

    def start(self) -> None:
        self._proc = self._popen(self.cmd, stdout=subprocess.PIPE, stderr=self.stderr,
                                 env=dict(os.environ, LC_ALL="C"))
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        try:
            while True:
                data = self._proc.stdout.read(self.block)
                if not data:
                    break
                with self._lock:
                    for q in self._clients:
                        try:
                            q.put_nowait(data)
                        except queue.Full:   # client trop lent : on saute un bloc
                            pass
        except (OSError, ValueError) as exc:
            self.error = str(exc)
        finally:
            self.ended.set()
            with self._lock:
                for q in self._clients:
                    q.put(None)

    def chunks(self, stop: threading.Event):
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._clients.append(q)
        try:
            while not stop.is_set():
                try:
                    data = q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if data is None:
                    return
                yield data
        finally:
            with self._lock:
                self._clients.remove(q)

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()


class FollowSource:
    """Reads a raw file while it is written, starting `delay_s` before its end."""

    def __init__(self, path: str, params: PCMParams, delay_s: float = 0.5, block_s: float = 0.05):
        self.path, self.params = path, params
        fb = params.frame_bytes
        self.delay = int(delay_s * params.rate) * fb
        self.block = max(1, int(block_s * params.rate)) * fb

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def chunks(self, stop: threading.Event):
        fb = self.params.frame_bytes
        while not stop.is_set() and not os.path.exists(self.path):
            time.sleep(0.05)
        if stop.is_set():
            return
        with open(self.path, "rb") as fh:
            size = os.path.getsize(self.path)
            pos = max(0, size - self.delay)
            pos -= pos % fb
            while not stop.is_set():
                avail = os.path.getsize(self.path) - pos
                if avail < self.block:
                    time.sleep(0.02)
                    continue
                fh.seek(pos)
                data = fh.read(self.block)
                pos += len(data)
                yield data


class WavServer:
    """HTTP server on 127.0.0.1 (port chosen by the system unless given)."""

    def __init__(self, source, params: PCMParams, port: int = 0):
        self.source, self.params = source, params
        self._stop = threading.Event()
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def _headers(self):
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()

            def do_HEAD(self):
                self._headers()

            def do_GET(self):
                self._headers()
                try:
                    self.wfile.write(wav_header(server.params))
                    for data in server.source.chunks(server._stop):
                        self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}{PATH}"

    def start(self) -> None:
        self.source.start()
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.source.stop()
