# SPDX-License-Identifier: GPL-3.0-or-later
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from vinylrip import cli
from vinylrip.config import Settings, load_settings
from vinylrip.devices import Card
from synth import three_track_face, write_raw

HAS_TOML = cli.load_settings.__module__ and __import__("vinylrip.config", fromlist=["tomllib"]).tomllib is not None


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, text):
        p = os.path.join(self.tmp.name, "c.toml")
        with open(p, "w") as fh:
            fh.write(text)
        return p

    def test_defaults_when_file_missing(self):
        s = load_settings(os.path.join(self.tmp.name, "none.toml"))
        self.assertTrue(s.delete_raw)
        self.assertFalse(s.delete_master_after_reduce)
        self.assertEqual(s.template, "{artiste_album}/{annee} - {album}/{piste:02d}. {titre}.flac")

    @unittest.skipUnless(HAS_TOML, "tomllib needs python >= 3.11")
    def test_values_are_loaded_flat_or_in_sections(self):
        p = self.write('device = "Device"\n[output]\ntemplate = "{album}/{piste:02d}.flac"\noutput_rate = 96000\n')
        s = load_settings(p)
        self.assertEqual((s.device, s.template, s.output_rate), ("Device", "{album}/{piste:02d}.flac", 96000))

    @unittest.skipUnless(HAS_TOML, "tomllib needs python >= 3.11")
    def test_unknown_key_is_rejected(self):
        with self.assertRaises(ValueError):
            load_settings(self.write("colour = 'red'\n"))

    def test_music_root_falls_back_to_xdg(self):
        with mock.patch("vinylrip.config.xdg_music_dir", return_value="/home/x/Musique"):
            self.assertEqual(Settings().resolved_music_root(), "/home/x/Musique")
        self.assertEqual(Settings(music_root="/m").resolved_music_root(), "/m")

    def test_xdg_music_dir_uses_the_command(self):
        from vinylrip import config
        fake = mock.Mock(stdout="/home/x/Musique\n")
        with mock.patch("subprocess.run", return_value=fake):
            self.assertEqual(config.xdg_music_dir(), "/home/x/Musique")
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertTrue(config.xdg_music_dir().endswith("Music"))

    def test_wants_reduction(self):
        self.assertFalse(Settings().wants_reduction(192000, 32))
        self.assertTrue(Settings(output_rate=96000).wants_reduction(192000, 32))
        self.assertTrue(Settings(output_bits=24).wants_reduction(192000, 32))
        self.assertFalse(Settings(output_rate=192000).wants_reduction(192000, 32))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_name_preview(self):
        code, out, _ = run(["name"])
        self.assertEqual(code, 0)
        self.assertIn("Pink Floyd/1979 - The Wall/03. ", out)

    def test_name_preview_rejects_bad_template(self):
        code, _, err = run(["name", "{bogus}.flac"])
        self.assertEqual(code, 1)
        self.assertIn("unknown field", err)

    def test_cuts_prints_a_plan(self):
        x, _ = three_track_face(8000)
        raw = os.path.join(self.tmp.name, "f.pcm")
        write_raw(raw, x, 8000, "S32_LE")
        code, out, _ = run(["cuts", raw])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["tracks"]), 3)

    def test_devices_lists_cards(self):
        cards = [Card(2, "Device", "USB Audio Device", 0, "054c:0dd7")]
        with mock.patch.object(cli, "list_capture_cards", return_value=cards):
            code, out, _ = run(["devices"])
        self.assertEqual(code, 0)
        self.assertIn("Device\tUSB Audio Device\thw:CARD=Device,DEV=0", out)

    def test_record_without_the_device_explains(self):
        with mock.patch.object(cli, "list_capture_cards", return_value=[Card(0, "PCH", "HDA", 0)]):
            code, _, err = run(["record", "--device", "Turntable"])
        self.assertEqual(code, 1)
        self.assertIn("capture device not found", err)
        self.assertIn("PCH", err)

    def test_process_reports_errors_without_touching_recordings(self):
        from vinylrip.job import Job
        from vinylrip.pcm import PCMParams
        job = Job.create(os.path.join(self.tmp.name, "work"), PCMParams(8000, 2, "S16_LE"), job_id="x")
        raw = os.path.join(job.dir, "face_a.pcm")
        write_raw(raw, three_track_face(8000)[0][:2000], 8000, "S16_LE")   # 0.25 s: too short to analyse
        job.add_face("A", raw, 1)
        with mock.patch.dict(os.environ, {"LC_ALL": "C", "LANG": "C"}):
            code, _, err = run(["process", job.dir])
        self.assertEqual(code, 2)          # ValueError("recording too short"): explained, with how to resume
        self.assertIn("recording too short", err)
        self.assertIn(f"vinylrip process {job.dir}", err)
        self.assertIn("Audacity", err)
        self.assertIn("Signed 16-bit PCM", err)
        self.assertTrue(os.path.exists(raw))

    def test_parser_requires_a_command(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args([])


if __name__ == "__main__":
    unittest.main()
