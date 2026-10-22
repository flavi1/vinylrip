# SPDX-License-Identifier: GPL-3.0-or-later
import os
import tempfile
import unittest
from unittest import mock

from vinylrip.job import Job
from vinylrip.pcm import PCMParams
from vinylrip.recovery import attention_text, help_text


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.job = Job.create(self.tmp.name, PCMParams(96000, 2, "S24_3LE"), job_id="j")
        self.job.add_face("A", os.path.join(self.job.dir, "face_a.pcm"), 1)

    def tearDown(self):
        self.tmp.cleanup()

    def test_french_and_english(self):
        with mock.patch.dict(os.environ, {"LC_ALL": "", "LC_MESSAGES": "", "LANG": "fr_FR.UTF-8"}):
            fr = help_text("identify", self.job, [], "network down")
        self.assertIn("Erreur : network down", fr)
        self.assertIn(f"vinylrip process {self.job.dir}", fr)
        self.assertIn("PCM 24 bits signé", fr)
        self.assertIn("96000 Hz", fr)
        with mock.patch.dict(os.environ, {"LC_ALL": "C"}):
            en = help_text("identify", self.job, [], "network down")
        self.assertIn("Signed 24-bit PCM", en)

    def test_each_stage_says_what_was_kept(self):
        with mock.patch.dict(os.environ, {"LC_ALL": "C"}):
            self.assertIn("Nothing was recorded", help_text("open", None, [], "busy"))
            self.assertIn("record that face again", help_text("record", self.job, []))
            self.assertIn("/tmp/x", help_text("encode", self.job, ["/tmp/x"]))
            self.assertIn("flac version", help_text("verify", self.job, ["/tmp/x"]))
            self.assertIn("masters, verified", help_text("convert", self.job, ["/m"]))
            self.assertIn("nothing is ever overwritten", help_text("file", self.job, ["/m"]))

    def test_attention(self):
        with mock.patch.dict(os.environ, {"LC_ALL": "C"}):
            t = attention_text({"faces_to_check": ["B"], "release": None}, self.job)
        self.assertIn("Face(s) B", t)
        self.assertIn("Picard", t)
        self.assertEqual(attention_text({"faces_to_check": [], "release": "mbid"}), "")


if __name__ == "__main__":
    unittest.main()
