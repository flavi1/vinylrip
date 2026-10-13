# SPDX-License-Identifier: GPL-3.0-or-later
import io
import os
import tempfile
import time
import unittest

from vinylrip import capture
from vinylrip.devices import Card, Quality
from vinylrip.pcm import PCMParams, read_sidecar


class FakeProc:
    def __init__(self, stderr_text="", on_signal=None):
        self.stderr = io.StringIO(stderr_text)
        self.stdin = io.BytesIO()
        self.returncode = None
        self.signals = []
        self.on_signal = on_signal

    def poll(self):
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)
        self.returncode = 0
        if self.on_signal:
            self.on_signal()

    def wait(self, timeout=None):
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode

    def terminate(self):
        self.returncode = 0


class CaptureTests(unittest.TestCase):
    def test_commands(self):
        card = Card(2, "Device", "USB", 0)
        q = Quality(192000, "S32_LE", 2)
        self.assertEqual(capture.build_arecord_cmd(card, q, "/tmp/a.pcm"),
                         ["arecord", "-D", "hw:CARD=Device,DEV=0", "-f", "S32_LE", "-r", "192000", "-c", "2", "-t", "raw", "/tmp/a.pcm"])
        cmd = capture.build_aplay_cmd(PCMParams(192000, 2, "S32_LE"), "plughw:0,0")
        self.assertEqual(cmd[-2:], ["-D", "plughw:0,0"])
        self.assertIn("S32_LE", cmd)

    def test_bytes_per_minute_matches_the_spec(self):
        self.assertEqual(capture.bytes_per_minute(Quality(192000, "S32_LE", 2)), 92_160_000)

    def test_free_space_check(self):
        with tempfile.TemporaryDirectory() as d:
            capture.check_free_space(d, 1)
            with self.assertRaises(capture.CaptureError):
                capture.check_free_space(d, 10 ** 18)

    def test_recorder_counts_overruns_and_writes_sidecar(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "face_a.pcm")
            params = PCMParams(48000, 2, "S16_LE")

            def write():
                with open(out, "wb") as fh:
                    fh.write(b"\0" * (48000 * 4 * 2))  # 2 s

            proc = FakeProc("Recording raw data 'x'\noverrun!!! (at least 1.2 ms long)\noverrun!!! (at least 3 ms long)\n", write)
            rec = capture.Recorder(["arecord"], out, params, popen=lambda *a, **k: proc)
            rec.start()
            time.sleep(0.05)
            res = rec.stop()
            self.assertEqual(res.overruns, 2)
            self.assertAlmostEqual(res.seconds, 2.0)
            self.assertEqual(read_sidecar(out)["overruns"], 2)
            self.assertEqual(read_sidecar(out)["fmt"], "S16_LE")

    def test_recorder_stop_before_start(self):
        rec = capture.Recorder(["arecord"], "/tmp/x.pcm", PCMParams(48000, 2, "S16_LE"))
        with self.assertRaises(capture.CaptureError):
            rec.stop()

    def test_recorder_missing_binary(self):
        def boom(*a, **k):
            raise FileNotFoundError

        with tempfile.TemporaryDirectory() as d:
            rec = capture.Recorder(["arecord"], os.path.join(d, "a.pcm"), PCMParams(48000, 2, "S16_LE"), popen=boom)
            with self.assertRaises(capture.CaptureError):
                rec.start()

    def test_follow_player_streams_a_growing_file_after_a_delay(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "face.pcm")
            params = PCMParams(8000, 2, "S16_LE")
            proc = FakeProc()
            proc.stdin = type("W", (), {"data": bytearray(), "write": lambda s, b: s.data.extend(b),
                                        "flush": lambda s: None, "close": lambda s: None})()
            player = capture.FollowPlayer(path, params, popen=lambda *a, **k: proc, delay_s=0.2)
            player.start()                     # file does not exist yet: must wait, not crash
            time.sleep(0.1)
            payload = bytes(range(256)) * 80   # 20 480 bytes = 1.28 s at 8 kHz stereo 16-bit
            with open(path, "wb") as fh:
                fh.write(payload[:4000])       # 0.125 s: below the 0.2 s delay
            time.sleep(0.2)
            self.assertEqual(len(proc.stdin.data), 0)
            with open(path, "ab") as fh:
                fh.write(payload[4000:])
            time.sleep(0.4)
            player.stop()
            self.assertGreater(len(proc.stdin.data), 0)
            self.assertEqual(bytes(proc.stdin.data), payload[:len(proc.stdin.data)])
            self.assertIsNone(player.error)

    def test_follow_player_failure_does_not_raise(self):
        def boom(*a, **k):
            raise FileNotFoundError("aplay")

        player = capture.FollowPlayer("/nonexistent/x.pcm", PCMParams(8000, 2, "S16_LE"), popen=boom)
        player.start()
        time.sleep(0.1)
        player.stop()
        self.assertIn("aplay", player.error)


if __name__ == "__main__":
    unittest.main()
