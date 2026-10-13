# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import os
import tempfile
import unittest

import numpy as np

from vinylrip.pcm import PCMParams, RawPCM, pack_s32_to_s24
from synth import write_raw


class PCMTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def test_unsupported_format(self):
        with self.assertRaises(ValueError):
            PCMParams(48000, 2, "FLOAT_LE")

    def test_frames_and_duration(self):
        x = np.zeros(48000)
        write_raw(self.path("a.pcm"), x, 48000, "S24_3LE")
        pcm = RawPCM(self.path("a.pcm"))
        self.assertEqual(pcm.frames, 48000)
        self.assertAlmostEqual(pcm.duration, 1.0)

    def test_roundtrip_all_formats(self):
        rng = np.random.default_rng(0)
        x = rng.uniform(-0.9, 0.9, 5000)
        for fmt, bits in (("S16_LE", 16), ("S24_3LE", 24), ("S32_LE", 32)):
            write_raw(self.path(f"{fmt}.pcm"), x, 48000, fmt)
            pcm = RawPCM(self.path(f"{fmt}.pcm"))
            a = pcm.read_int32(0, 5000)
            self.assertEqual(a.shape, (5000, 2))
            self.assertTrue(np.array_equal(a[:, 0], a[:, 1]))
            expected = np.round(x * 2 ** (bits - 1)).astype(np.int64)
            self.assertTrue(np.array_equal(a[:, 0].astype(np.int64), expected), fmt)

    def test_negative_24bit_sign_extension(self):
        x = np.array([-0.5, 0.5, -1e-6])
        write_raw(self.path("n.pcm"), x, 48000, "S24_3LE")
        a = RawPCM(self.path("n.pcm")).read_int32(0, 3)[:, 0]
        self.assertLess(a[0], 0)
        self.assertGreater(a[1], 0)
        self.assertLess(a[2], 0)

    def test_sha256_of_segment_matches_bytes(self):
        write_raw(self.path("s.pcm"), np.linspace(-0.5, 0.5, 1000), 48000, "S32_LE")
        pcm = RawPCM(self.path("s.pcm"))
        seg = pcm.read_bytes(100, 400)
        self.assertEqual(pcm.sha256(100, 400, chunk_frames=64), hashlib.sha256(seg).hexdigest())

    def test_low_bits_and_pack_to_24(self):
        x = np.round(np.linspace(-0.5, 0.5, 2000) * 2 ** 23) / 2 ** 23  # exactly 24-bit values
        write_raw(self.path("z.pcm"), x, 48000, "S32_LE")
        pcm = RawPCM(self.path("z.pcm"))
        self.assertTrue(pcm.low_bits_zero())
        p24 = pack_s32_to_s24(pcm, self.path("z24.pcm"))
        self.assertEqual(p24.params.fmt, "S24_3LE")
        self.assertEqual(p24.frames, pcm.frames)
        self.assertTrue(np.array_equal(p24.read_int32(0, 2000), pcm.read_int32(0, 2000) >> 8))

    def test_low_bits_nonzero_detected(self):
        write_raw(self.path("y.pcm"), np.random.default_rng(1).uniform(-0.5, 0.5, 2000), 48000, "S32_LE")
        self.assertFalse(RawPCM(self.path("y.pcm")).low_bits_zero())


if __name__ == "__main__":
    unittest.main()
