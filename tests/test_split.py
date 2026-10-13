# SPDX-License-Identifier: GPL-3.0-or-later
import os
import tempfile
import unittest

from vinylrip.pcm import RawPCM
from vinylrip.split import Plan, SplitConfig, analyse
from synth import build, three_track_face, write_raw

RATE = 8000


class SplitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def raw(self, x, fmt="S32_LE"):
        p = os.path.join(self.tmp.name, "face.pcm")
        write_raw(p, x, RATE, fmt)
        return RawPCM(p)

    def test_three_tracks_found_at_the_gaps(self):
        x, truth = three_track_face(RATE)
        plan = analyse(self.raw(x))
        self.assertEqual(len(plan.tracks), 3)
        for (a, b), gap in zip(plan.tracks[:2], truth["gap_centers"]):
            self.assertAlmostEqual(b, gap, delta=0.6)
        self.assertAlmostEqual(plan.tracks[1][0], truth["gap_centers"][0], delta=0.6)

    def test_lead_in_and_run_out_trimmed_with_margins(self):
        x, truth = three_track_face(RATE)
        plan = analyse(self.raw(x))
        self.assertAlmostEqual(plan.music_start_s, truth["first_music"], delta=0.3)
        self.assertAlmostEqual(plan.trim_start_s, truth["first_music"] - 1.0, delta=0.3)
        self.assertAlmostEqual(plan.trim_end_s, truth["last_music"] + 3.0, delta=0.3)
        self.assertEqual(plan.tracks[0][0], plan.trim_start_s)
        self.assertEqual(plan.tracks[-1][1], plan.trim_end_s)
        self.assertLess(plan.tracks[-1][1], plan.duration_s)  # run-out groove is dropped

    def test_needle_thump_is_not_music(self):
        x, truth = three_track_face(RATE)
        plan = analyse(self.raw(x))
        self.assertGreater(plan.music_start_s, 3.5)  # the thump sits at 3.0 s

    def test_crackle_in_gaps_does_not_hide_silences(self):
        x, _ = three_track_face(RATE, seed=7)
        plan = analyse(self.raw(x))
        self.assertEqual([c.strength for c in plan.candidates], ["strong", "strong"])

    def test_short_noise_only_segment_is_glued_to_previous_track(self):
        parts = [("silence", 3), ("music", 20, 220), ("silence", 2), ("noise", 4, 10.5),
                 ("silence", 2), ("music", 18, 330), ("silence", 5)]
        x, marks = build(parts, RATE, seed=3)
        plan = analyse(self.raw(x))
        self.assertEqual(len(plan.tracks), 2)
        self.assertTrue(any("noise-only" in n for n in plan.notes))
        cut = plan.tracks[0][1]
        gap2 = [m for m in marks if m[0] == "silence"][2]
        self.assertAlmostEqual(cut, (gap2[1] + gap2[2]) / 2, delta=0.6)
        # everything is kept: no audio between the two tracks is lost
        self.assertEqual(plan.tracks[0][1], plan.tracks[1][0])

    def test_internal_silence_gives_an_extra_candidate_and_track(self):
        x, _ = three_track_face(RATE, internal_gap=1.5)
        plan = analyse(self.raw(x))
        self.assertEqual(len(plan.tracks), 4)  # pass 1 over-splits; pass 2 will fix it
        self.assertEqual(len(plan.candidates), 3)

    def test_works_on_16_and_24_bit_captures(self):
        x, _ = three_track_face(RATE)
        for fmt in ("S16_LE", "S24_3LE"):
            self.assertEqual(len(analyse(self.raw(x, fmt)).tracks), 3, fmt)

    def test_plan_json_roundtrip(self):
        x, _ = three_track_face(RATE)
        plan = analyse(self.raw(x))
        again = Plan.from_dict(plan.to_dict())
        self.assertEqual(again.tracks, plan.tracks)
        self.assertEqual(again.candidates, plan.candidates)

    def test_silence_only_recording_is_an_error(self):
        x, _ = build([("silence", 20)], RATE)
        with self.assertRaises(ValueError):
            analyse(self.raw(x), SplitConfig(threshold_db=-30.0))

    def test_too_short_recording_is_an_error(self):
        x, _ = build([("music", 0.5, 220)], RATE)
        with self.assertRaises(ValueError):
            analyse(self.raw(x))


if __name__ == "__main__":
    unittest.main()
