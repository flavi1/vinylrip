# SPDX-License-Identifier: GPL-3.0-or-later
import os
import shutil
import subprocess
import tempfile
import unittest

import numpy as np

from vinylrip import encode
from vinylrip.convert import check_reduced, reduce_master
from vinylrip.pcm import RawPCM
from synth import write_raw

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
ENC = "flac" if encode.flac_version() else "ffmpeg"


def tone(rate, sec=2.0, amp=0.5, seed=0):
    t = np.arange(int(rate * sec)) / rate
    rng = np.random.default_rng(seed)
    return amp * np.sin(2 * np.pi * 440 * t) + rng.normal(0, 0.01, len(t))


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg required")
class EncodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def p(self, name):
        return os.path.join(self.tmp.name, name)

    def test_master_is_bit_exact_for_every_format(self):
        for fmt in ("S16_LE", "S24_3LE", "S32_LE"):
            write_raw(self.p(f"{fmt}.pcm"), tone(48000), 48000, fmt)
            pcm = RawPCM(self.p(f"{fmt}.pcm"))
            out = self.p(f"{fmt}.flac")
            enc = encode.encode_master(pcm, 1000, 80000, out, {"TITLE": "T"}, prefer=ENC)
            self.assertEqual(enc, ENC)
            res = encode.verify_master(pcm, 1000, 80000, out, prefer=ENC)
            self.assertTrue(res.ok, (fmt, res))
            self.assertEqual(res.decoded_frames, 79000)

    def test_full_32_bit_range_survives(self):
        # real 32-bit content, not just 24 bits padded with zeros
        rng = np.random.default_rng(5)
        x = rng.uniform(-0.8, 0.8, 30000)
        write_raw(self.p("w.pcm"), x, 96000, "S32_LE")
        pcm = RawPCM(self.p("w.pcm"))
        self.assertFalse(pcm.low_bits_zero())
        out = self.p("w.flac")
        encode.encode_master(pcm, 0, pcm.frames, out, prefer=ENC)
        self.assertTrue(encode.verify_master(pcm, 0, pcm.frames, out, prefer=ENC).ok)

    def test_verification_detects_a_changed_sample(self):
        write_raw(self.p("a.pcm"), tone(48000), 48000, "S32_LE")
        pcm = RawPCM(self.p("a.pcm"))
        out = self.p("a.flac")
        encode.encode_master(pcm, 0, pcm.frames, out, prefer=ENC)
        with open(self.p("a.pcm"), "r+b") as fh:       # one bit flipped after encoding
            fh.seek(4000 * 8 + 1)
            b = fh.read(1)
            fh.seek(4000 * 8 + 1)
            fh.write(bytes([b[0] ^ 0x01]))
        res = encode.verify_master(pcm, 0, pcm.frames, out, prefer=ENC)
        self.assertFalse(res.ok)
        self.assertNotEqual(res.expected_sha256, res.decoded_sha256)
        self.assertEqual(res.decoded_frames, res.expected_frames)

    def test_verification_detects_a_truncated_flac(self):
        write_raw(self.p("t.pcm"), tone(48000), 48000, "S16_LE")
        pcm = RawPCM(self.p("t.pcm"))
        out = self.p("t.flac")
        encode.encode_master(pcm, 0, pcm.frames, out, prefer=ENC)
        with open(out, "r+b") as fh:
            fh.truncate(os.path.getsize(out) // 2)
        try:
            res = encode.verify_master(pcm, 0, pcm.frames, out, prefer=ENC)
            self.assertFalse(res.ok)
        except encode.EncodeError:
            pass  # a decoder that refuses the file is just as good

    def test_tags_are_written(self):
        write_raw(self.p("g.pcm"), tone(48000, 1), 48000, "S16_LE")
        pcm = RawPCM(self.p("g.pcm"))
        out = self.p("g.flac")
        encode.encode_master(pcm, 0, pcm.frames, out, {"TITLE": "Un titre", "ALBUM": "Album", "TRACKNUMBER": "3"}, prefer=ENC)
        info = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "default=nw=1", out],
                              capture_output=True, text=True).stdout
        self.assertIn("Un titre", info)
        self.assertRegex(info.lower(), r"(tracknumber|track)=3")

    def test_choose_encoder_rules(self):
        from vinylrip.pcm import PCMParams
        self.assertEqual(encode.choose_encoder(PCMParams(48000, 2, "S16_LE"), "ffmpeg"), "ffmpeg")
        self.assertIn(encode.choose_encoder(PCMParams(48000, 2, "S32_LE")), ("flac", "ffmpeg"))


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg required")
class ConvertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def p(self, name):
        return os.path.join(self.tmp.name, name)

    def master(self, rate=96000, fmt="S32_LE"):
        write_raw(self.p("m.pcm"), tone(rate, 2.0), rate, fmt)
        pcm = RawPCM(self.p("m.pcm"))
        encode.encode_master(pcm, 0, pcm.frames, self.p("m.flac"), prefer=ENC)
        return self.p("m.flac")

    def test_reduce_to_half_rate_24_bits_passes_the_measurements(self):
        m = self.master()
        resampler = reduce_master(m, self.p("r.flac"), 48000, 24)
        self.assertIn(resampler, ("soxr", "swr"))
        chk = check_reduced(m, self.p("r.flac"), 48000)
        self.assertTrue(chk.ok, chk)
        self.assertLessEqual(abs(chk.frames_got - 96000), 2)
        rate = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=sample_rate", "-of", "csv=p=0", self.p("r.flac")],
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(rate, "48000")

    def test_reduce_to_16_bits_with_dither(self):
        m = self.master(48000)
        reduce_master(m, self.p("r16.flac"), 48000, 16)
        self.assertTrue(check_reduced(m, self.p("r16.flac"), 48000).ok)

    def test_a_wrong_copy_fails_the_check(self):
        m = self.master()
        write_raw(self.p("q.pcm"), np.zeros(96000), 48000, "S32_LE")
        q = RawPCM(self.p("q.pcm"))
        encode.encode_master(q, 0, q.frames, self.p("quiet.flac"), prefer=ENC)
        self.assertFalse(check_reduced(m, self.p("quiet.flac"), 48000).ok)

    def test_bad_depth_rejected(self):
        from vinylrip.convert import ConvertError
        with self.assertRaises(ConvertError):
            reduce_master("x.flac", "y.flac", 48000, 32)


if __name__ == "__main__":
    unittest.main()
