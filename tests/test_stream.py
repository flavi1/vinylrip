# SPDX-License-Identifier: GPL-3.0-or-later
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

from vinylrip.pcm import PCMParams
from vinylrip.stream import FollowSource, LiveSource, WavServer, wav_header


class StreamTests(unittest.TestCase):
    def test_header(self):
        h = wav_header(PCMParams(96000, 2, "S24_3LE"))
        self.assertEqual((h[:4], h[8:16], len(h)), (b"RIFF", b"WAVEfmt ", 44))
        self.assertEqual(int.from_bytes(h[34:36], "little"), 24)          # bits
        self.assertEqual(int.from_bytes(h[24:28], "little"), 96000)

    def _read(self, url, n):
        with urllib.request.urlopen(url, timeout=5) as r:
            self.assertEqual(r.headers["Content-Type"], "audio/wav")
            return r.read(n)

    def test_follow_file_being_written(self):
        params = PCMParams(8000, 2, "S16_LE")
        with tempfile.TemporaryDirectory() as d:
            raw = os.path.join(d, "face_a.pcm")
            stop = threading.Event()

            def writer():
                with open(raw, "wb") as fh:
                    while not stop.is_set():
                        fh.write(b"\x01\x00" * 800)
                        fh.flush()
                        time.sleep(0.02)
            threading.Thread(target=writer, daemon=True).start()
            srv = WavServer(FollowSource(raw, params, delay_s=0.1), params)
            srv.start()
            try:
                data = self._read(srv.url, 44 + 4000)
            finally:
                srv.stop()
                stop.set()
        self.assertEqual(data[:4], b"RIFF")
        self.assertEqual(data[44:48], b"\x01\x00\x01\x00")

    def test_live_source_shared_by_clients(self):
        params = PCMParams(8000, 1, "S16_LE")
        cmd = [sys.executable, "-c", "import sys,time\nwhile True:\n sys.stdout.buffer.write(b'\\x02\\x00'*400); sys.stdout.flush(); time.sleep(0.02)"]
        srv = WavServer(LiveSource(cmd, params), params)
        srv.start()
        try:
            a = self._read(srv.url, 44 + 2000)
            b = self._read(srv.url, 44 + 2000)
        finally:
            srv.stop()
        self.assertEqual(a[44:46], b"\x02\x00")
        self.assertEqual(b[44:46], b"\x02\x00")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg absent")
    def test_ffmpeg_reads_the_stream(self):
        """Kodi lit les flux avec ffmpeg : l'en-tête de longueur inconnue doit passer."""
        params = PCMParams(48000, 2, "S32_LE")
        cmd = [sys.executable, "-c", "import sys,time\nwhile True:\n sys.stdout.buffer.write(b'\\x00\\x00\\x10\\x00'*9600); sys.stdout.flush(); time.sleep(0.05)"]
        srv = WavServer(LiveSource(cmd, params), params)
        srv.start()
        try:
            r = subprocess.run(["ffmpeg", "-v", "error", "-t", "0.3", "-i", srv.url, "-f", "null", "-"],
                               capture_output=True, text=True, timeout=20)
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=sample_rate,channels,codec_name",
                                    "-of", "csv=p=0", srv.url], capture_output=True, text=True, timeout=20)
        finally:
            srv.stop()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("pcm_s32le,48000,2", probe.stdout)


if __name__ == "__main__":
    unittest.main()
