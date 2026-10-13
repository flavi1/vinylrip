# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from vinylrip.config import Settings
from vinylrip.encode import flac_version
from vinylrip.identify import NetworkError, Scored, parse_release
from vinylrip.job import Job, ProcessError, process
from vinylrip.pcm import PCMParams
from synth import build, three_track_face, write_raw
from test_identify import release_json

RATE = 8000
ENC = "flac" if flac_version() else "ffmpeg"
HAVE_FFMPEG = shutil.which("ffmpeg") is not None


class FakeIdentifier:
    def __init__(self, releases, fail=None):
        self.releases, self.fail, self.calls = releases, fail, 0

    def identify(self, faces):
        self.calls += 1
        if self.fail:
            raise self.fail
        return [Scored(r, s, 0.9, 0.9) for r, s in self.releases]


def album_release(lengths_a, lengths_b, mbid="mbid-1"):
    n_a = len(lengths_a)
    nums = [f"A{i + 1}" for i in range(n_a)] + [f"B{i + 1}" for i in range(len(lengths_b))]
    return parse_release(release_json(mbid, "Le Disque", [int(x * 1000) for x in lengths_a + lengths_b], nums))


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg required")
class JobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.t = self.tmp.name
        self.settings = Settings(work_dir=f"{self.t}/work", music_root=f"{self.t}/Musique",
                                 masters_root=f"{self.t}/Masters", encoder=ENC, identify_mode="auto")

    def make_job(self, fmt="S32_LE", discs=1, faces=None):
        params = PCMParams(RATE, 2, fmt)
        job = Job.create(self.settings.work_dir, params, discs=discs, job_id="j1")
        for x, label in faces or []:
            _, raw, disc = job.next_face_path()
            write_raw(raw, x, RATE, fmt)
            job.add_face(label, raw, disc)
        return job

    def faces_ab(self, internal_gap=None):
        a, ta = three_track_face(RATE, seed=1, internal_gap=internal_gap)
        b, tb = build([("silence", 3), ("thump",), ("silence", 1), ("music", 18, 262), ("silence", 2),
                       ("music", 22, 392), ("silence", 6)], RATE, seed=2)
        return a, b

    def tree(self, root):
        out = []
        for d, _, files in os.walk(root):
            out += [os.path.relpath(os.path.join(d, f), root) for f in files]
        return sorted(out)

    def test_two_faces_identified_and_filed_with_raw_removed(self):
        a, b = self.faces_ab()
        rel = album_release([20, 25, 15], [18, 22])
        job = self.make_job(faces=[(a, "A"), (b, "B")])
        raws = [f["raw"] for f in job.faces]
        events = []
        res = process(job, self.settings, FakeIdentifier([(rel, 0.9), (album_release([1], [1], "x"), 0.3)]),
                      cover_fetch=lambda mbid: None, on_event=events.append)
        files = self.tree(f"{self.t}/Musique")
        self.assertEqual(files, [f"Artist & Band/1979 - Le Disque/{i:02d}. Song {n}.flac" for i, n in
                                 enumerate(["A1", "A2", "A3", "B1", "B2"], start=1)])
        self.assertEqual(res["tracks"], 5)
        self.assertTrue(res["deleted_raw"])
        self.assertFalse(any(os.path.exists(r) for r in raws))
        self.assertEqual(Job.load(job.dir).data["state"], "done")
        self.assertIn("encode", [e["stage"] for e in events])
        self.assertFalse([n for n in os.listdir(f"{self.t}/Musique") if n.startswith(".vinylrip-")])
        info = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json",
                               f"{self.t}/Musique/Artist & Band/1979 - Le Disque/04. Song B1.flac"],
                              capture_output=True, text=True).stdout
        tags = {k.lower(): v for k, v in json.loads(info)["format"]["tags"].items()}
        self.assertEqual(tags["title"], "Song B1")
        self.assertEqual(tags["album"], "Le Disque")

    def test_false_silence_inside_a_track_is_glued_using_the_release_durations(self):
        a, b = self.faces_ab(internal_gap=1.5)
        rel = album_release([20, 26.5, 15], [18, 22])
        job = self.make_job(faces=[(a, "A"), (b, "B")])
        res = process(job, self.settings, FakeIdentifier([(rel, 0.9)]), cover_fetch=lambda m: None)
        self.assertEqual(res["tracks"], 5)
        self.assertTrue(any("corrected" in w for w in res["warnings"]))
        self.assertEqual(res["faces_to_check"], [])

    def test_mismatching_release_keeps_raw_and_flags_the_face(self):
        a, b = self.faces_ab()
        rel = album_release([100, 100, 100], [100, 100])
        job = self.make_job(faces=[(a, "A"), (b, "B")])
        raws = [f["raw"] for f in job.faces]
        res = process(job, self.settings, FakeIdentifier([(rel, 0.9)]), cover_fetch=lambda m: None)
        self.assertTrue(set(res["faces_to_check"]))
        self.assertFalse(res["deleted_raw"])
        self.assertTrue(all(os.path.exists(r) for r in raws))
        self.assertEqual(Job.load(job.dir).data["state"], "needs_attention")

    def test_unknown_album_when_offline(self):
        a, b = self.faces_ab()
        job = self.make_job(faces=[(a, "A"), (b, "B")])
        res = process(job, self.settings, FakeIdentifier([], fail=NetworkError("offline")), cover_fetch=lambda m: None)
        files = self.tree(f"{self.t}/Musique")
        self.assertEqual(len(files), 5)
        self.assertTrue(all(f.startswith("Inconnu/Vinyle ") and "/Piste " in f for f in files))
        self.assertTrue(any("identification unavailable" in w for w in res["warnings"]))

    def test_manual_mode_asks_the_chooser(self):
        a, b = self.faces_ab()
        r1 = album_release([20, 25, 15], [18, 22], "r1")
        r2 = parse_release(release_json("r2", "Autre Disque", [20000, 25000, 15000, 18000, 22000],
                                        ["A1", "A2", "A3", "B1", "B2"]))
        self.settings.identify_mode = "manual"
        seen = []
        job = self.make_job(faces=[(a, "A"), (b, "B")])
        res = process(job, self.settings, FakeIdentifier([(r1, 0.9), (r2, 0.89)]),
                      chooser=lambda ranked: (seen.append(len(ranked)), 1)[1], cover_fetch=lambda m: None)
        self.assertEqual(seen, [2])
        self.assertEqual(res["album"], "Autre Disque")

    def test_manual_mode_chooser_can_decline(self):
        a, b = self.faces_ab()
        self.settings.identify_mode = "manual"
        job = self.make_job(faces=[(a, "A"), (b, "B")])
        res = process(job, self.settings, FakeIdentifier([(album_release([20, 25, 15], [18, 22]), 0.9)]),
                      chooser=lambda ranked: None, cover_fetch=lambda m: None)
        self.assertIsNone(res["release"])

    def test_reduced_copy_goes_to_music_and_master_is_kept_separately(self):
        a, b = self.faces_ab()
        self.settings.output_rate = 4000
        self.settings.output_bits = 24
        rel = album_release([20, 25, 15], [18, 22])
        job = self.make_job(faces=[(a, "A"), (b, "B")])
        res = process(job, self.settings, FakeIdentifier([(rel, 0.9)]), cover_fetch=lambda m: None)
        self.assertEqual(len(self.tree(f"{self.t}/Musique")), 5)
        self.assertEqual(self.tree(f"{self.t}/Musique"), self.tree(f"{self.t}/Masters"))
        self.assertIsNotNone(res["masters_dir"])
        probe = lambda p: subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=sample_rate", "-of", "csv=p=0", p],
                                         capture_output=True, text=True).stdout.strip()
        first = "Artist & Band/1979 - Le Disque/01. Song A1.flac"
        self.assertEqual(probe(f"{self.t}/Musique/{first}"), "4000")
        self.assertEqual(probe(f"{self.t}/Masters/{first}"), "8000")

    def test_deleting_the_master_is_an_explicit_option(self):
        a, b = self.faces_ab()
        self.settings.output_rate = 4000
        self.settings.output_bits = 24
        self.settings.delete_master_after_reduce = True
        job = self.make_job(faces=[(a, "A"), (b, "B")])
        res = process(job, self.settings, FakeIdentifier([(album_release([20, 25, 15], [18, 22]), 0.9)]),
                      cover_fetch=lambda m: None)
        self.assertTrue(res["deleted_master"])
        self.assertFalse(os.path.exists(f"{self.t}/Masters/Artist & Band"))
        self.assertEqual(len(self.tree(f"{self.t}/Musique")), 5)

    def test_existing_album_is_never_overwritten(self):
        a, b = self.faces_ab()
        rel = album_release([20, 25, 15], [18, 22])
        existing = f"{self.t}/Musique/Artist & Band/1979 - Le Disque"
        os.makedirs(existing)
        with open(f"{existing}/precious.txt", "w") as fh:
            fh.write("keep me")
        job = self.make_job(faces=[(a, "A"), (b, "B")])
        res = process(job, self.settings, FakeIdentifier([(rel, 0.9)]), cover_fetch=lambda m: None)
        with open(f"{existing}/precious.txt") as fh:
            self.assertEqual(fh.read(), "keep me")
        self.assertEqual(res["album_dir"], existing + " (2)")
        self.assertEqual(len(os.listdir(existing + " (2)")), 5)

    def test_cover_is_written_and_embedded(self):
        a, b = self.faces_ab()
        jpeg = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64", "-frames:v", "1",
                               "-f", "mjpeg", "pipe:1"], capture_output=True).stdout
        self.assertTrue(jpeg.startswith(b"\xff\xd8"))
        rel = album_release([20, 25, 15], [18, 22])
        job = self.make_job(faces=[(a, "A"), (b, "B")])
        res = process(job, self.settings, FakeIdentifier([(rel, 0.9)]), cover_fetch=lambda m: jpeg)
        album = res["album_dir"]
        self.assertTrue(os.path.exists(f"{album}/cover.jpg"))
        streams = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0",
                                  f"{album}/01. Song A1.flac"], capture_output=True, text=True).stdout.split()
        self.assertIn("video", streams)
        self.assertEqual(len([f for f in os.listdir(album) if f.endswith(".flac")]), 5)

    def test_double_album_uses_one_folder_per_disc(self):
        a, b = self.faces_ab()
        c, d = self.faces_ab()
        nums = ["A1", "A2", "A3", "B1", "B2"]
        lengths = [20, 25, 15, 18, 22]
        data = release_json("dbl", "Double", [int(x * 1000) for x in lengths * 2], nums * 2)
        data["media"] = [dict(data["media"][0], position=1, tracks=data["media"][0]["tracks"][:5]),
                         dict(data["media"][0], position=2, tracks=data["media"][0]["tracks"][5:])]
        rel = parse_release(data)
        # disc 2 reuses side letters A/B, so the release has no unambiguous letters: counts decide
        job = self.make_job(discs=2, faces=[(a, "A"), (b, "B"), (c, "C"), (d, "D")])
        self.assertEqual([f["disc"] for f in job.faces], [1, 1, 2, 2])
        res = process(job, self.settings, FakeIdentifier([(rel, 0.9)]), cover_fetch=lambda m: None)
        files = self.tree(f"{self.t}/Musique")
        self.assertEqual(len(files), 10)
        self.assertEqual(sum("/CD1/" in f for f in files), 5)
        self.assertEqual(sum("/CD2/" in f for f in files), 5)
        self.assertTrue(any(f.endswith("CD2/05. Song B2.flac") for f in files), files)

    def test_single_with_one_face(self):
        x, _ = build([("silence", 3), ("music", 20, 330), ("silence", 6)], RATE, seed=4)
        rel = parse_release(release_json("sg", "Un Single", [20000], ["A"], fmt='7" Vinyl', rtype="Single"))
        job = self.make_job(faces=[(x, "A")])
        res = process(job, self.settings, FakeIdentifier([(rel, 0.9)]), cover_fetch=lambda m: None)
        self.assertEqual(res["tracks"], 1)
        self.assertEqual(self.tree(f"{self.t}/Musique"), ["Artist & Band/1979 - Un Single/01. Song A.flac"])

    def test_24_bit_master_when_low_bits_are_zero(self):
        a, b = self.faces_ab()
        self.settings.master_24bit_if_lossless = True
        job = self.make_job(faces=[(a, "A"), (b, "B")])
        # make the 32-bit capture exactly 24-bit content
        import numpy as np
        from vinylrip.pcm import RawPCM
        for f in job.faces:
            raw = np.fromfile(f["raw"], dtype="<i4")
            (raw & ~np.int32(0xFF)).astype("<i4").tofile(f["raw"])
        res = process(job, self.settings, FakeIdentifier([(album_release([20, 25, 15], [18, 22]), 0.9)]),
                      cover_fetch=lambda m: None)
        out = f"{res['album_dir']}/01. Song A1.flac"
        bits = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=bits_per_raw_sample", "-of", "csv=p=0", out],
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(bits, "24")

    def test_job_roundtrip_and_face_numbering(self):
        job = self.make_job(discs=2)
        labels = []
        for _ in range(4):
            label, raw, disc = job.next_face_path()
            job.add_face(label, raw, disc)
            labels.append((label, disc))
        self.assertEqual(labels, [("A", 1), ("B", 1), ("C", 2), ("D", 2)])
        again = Job.load(job.dir)
        self.assertEqual(again.faces, job.faces)
        self.assertEqual(again.params, PCMParams(RATE, 2, "S32_LE"))


if __name__ == "__main__":
    unittest.main()
