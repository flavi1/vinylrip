# SPDX-License-Identifier: GPL-3.0-or-later
import unittest

from vinylrip.align import align
from vinylrip.split import Candidate, Plan, apply_cuts


def plan(cands, start=0.0, end=300.0):
    return Plan(rate=8000, duration_s=end + 5, floor_db=-60, threshold_db=-52, music_start_s=start,
                music_end_s=end, trim_start_s=start - 1, trim_end_s=end + 3,
                candidates=[Candidate(p, s, 2.0 if s == "strong" else 0.5) for p, s in cands])


class AlignTests(unittest.TestCase):
    def test_false_internal_silence_is_dropped(self):
        p = plan([(100, "strong"), (160, "strong"), (220, "strong")])
        a = align(p, [100, 120, 80])
        self.assertEqual(a.cuts, [100.0, 220.0])
        self.assertFalse(a.doubtful)

    def test_weak_candidate_used_for_a_segue(self):
        p = plan([(100, "strong"), (220, "weak")])
        a = align(p, [100, 120, 80])
        self.assertEqual(a.cuts, [100.0, 220.0])

    def test_weak_noise_not_preferred_over_strong_when_equally_good(self):
        p = plan([(100, "strong"), (101, "weak"), (220, "strong")])
        self.assertEqual(align(p, [100, 120, 80]).cuts, [100.0, 220.0])

    def test_speed_offset_is_absorbed(self):
        # platter 1.5 % slow: durations measured longer than the release's
        p = plan([(101.5, "strong"), (223.0, "strong")], end=304.5)
        a = align(p, [100, 120, 80])
        self.assertEqual(a.cuts, [101.5, 223.0])
        self.assertAlmostEqual(a.speed, 1 / 1.015, delta=0.004)
        self.assertFalse(a.doubtful)

    def test_not_enough_candidates(self):
        self.assertIsNone(align(plan([(100, "strong")]), [100, 120, 80]))

    def test_doubtful_when_nothing_fits(self):
        p = plan([(30, "strong"), (60, "strong")])
        a = align(p, [100, 100, 100])
        self.assertTrue(a.doubtful)

    def test_single_track(self):
        a = align(plan([]), [300])
        self.assertEqual(a.cuts, [])
        self.assertFalse(a.doubtful)

    def test_two_tracks(self):
        a = align(plan([(100, "strong"), (200, "strong")]), [100, 200])
        self.assertEqual(a.cuts, [100.0])

    def test_empty_expected(self):
        self.assertIsNone(align(plan([]), []))

    def test_apply_cuts_uses_trim_margins(self):
        p = plan([(100, "strong")], end=200)
        tr = apply_cuts(p, [100.0])
        self.assertEqual(tr, [(-1.0, 100.0), (100.0, 203.0)])


if __name__ == "__main__":
    unittest.main()
