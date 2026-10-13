# SPDX-License-Identifier: GPL-3.0-or-later
import json
import unittest

from vinylrip import identify as ident


def release_json(mbid, title, lengths_ms, numbers=None, fmt='12" Vinyl', year="1979", rtype="Album"):
    numbers = numbers or [str(i + 1) for i in range(len(lengths_ms))]
    return {
        "id": mbid, "title": title, "date": f"{year}-11-30",
        "artist-credit": [{"name": "Artist", "joinphrase": " & "}, {"name": "Band", "joinphrase": ""}],
        "release-group": {"id": "rg-" + mbid, "primary-type": rtype},
        "media": [{"position": 1, "format": fmt, "tracks": [
            {"number": n, "title": f"Song {n}", "length": l, "recording": {"id": f"rec-{n}"}}
            for n, l in zip(numbers, lengths_ms)]}],
    }


class ReleaseTests(unittest.TestCase):
    def test_parse_vinyl_sides(self):
        r = ident.parse_release(release_json("m1", "Album", [200000, 210000, 180000], ["A1", "A2", "B1"]))
        self.assertEqual(r.artist, "Artist & Band")
        self.assertEqual(r.year, 1979)
        self.assertEqual(r.rtype, "Album")
        self.assertTrue(r.has_sides and r.is_vinyl())
        self.assertEqual([t.side for t in r.tracks], ["A", "A", "B"])
        self.assertEqual(r.tracks[0].length_s, 200.0)
        self.assertEqual(r.tracks[0].recording_id, "rec-A1")

    def test_tracks_for_faces_by_letter_and_by_count(self):
        v = ident.parse_release(release_json("m1", "Album", [1000] * 4, ["A1", "A2", "B1", "B2"]))
        by_letter = v.tracks_for_faces([("A", 99), ("B", 99)])
        self.assertEqual([len(x) for x in by_letter], [2, 2])
        # two LPs whose sides restart at A on every disc
        data = release_json("dbl", "Double", [1000] * 8, ["A1", "A2", "B1", "B2", "A1", "A2", "B1", "B2"])
        data["media"] = [dict(data["media"][0], position=1, tracks=data["media"][0]["tracks"][:4]),
                         dict(data["media"][0], position=2, tracks=data["media"][0]["tracks"][4:])]
        dbl = ident.parse_release(data)
        faces = dbl.tracks_for_faces([("A", 2, 1), ("B", 2, 1), ("C", 2, 2), ("D", 2, 2)])
        self.assertEqual([[(t.disc, t.number) for t in f] for f in faces],
                         [[(1, "A1"), (1, "A2")], [(1, "B1"), (1, "B2")], [(2, "A1"), (2, "A2")], [(2, "B1"), (2, "B2")]])
        # sides numbered A..D across the two discs
        data2 = release_json("dbl2", "Double", [1000] * 4, ["A1", "B1", "C1", "D1"])
        data2["media"] = [dict(data2["media"][0], position=1, tracks=data2["media"][0]["tracks"][:2]),
                          dict(data2["media"][0], position=2, tracks=data2["media"][0]["tracks"][2:])]
        faces2 = ident.parse_release(data2).tracks_for_faces([("A", 1, 1), ("B", 1, 1), ("C", 1, 2), ("D", 1, 2)])
        self.assertEqual([f[0].number for f in faces2], ["A1", "B1", "C1", "D1"])
        cd = ident.parse_release(release_json("m2", "Album", [1000] * 5, fmt="CD"))
        self.assertFalse(cd.has_sides)
        self.assertEqual([len(x) for x in cd.tracks_for_faces([("A", 3), ("B", 2)])], [3, 2])

    def test_single_type_and_missing_fields(self):
        r = ident.parse_release({"id": "x", "title": "T", "media": []})
        self.assertEqual((r.artist, r.year, r.rtype, r.tracks), ("Inconnu", None, "Album", []))
        s = ident.parse_release(release_json("s", "Single", [180000, 200000], ["A", "B"], rtype="Single", fmt='7" Vinyl'))
        self.assertEqual(s.rtype, "Single")
        self.assertEqual([t.side for t in s.tracks], ["A", "B"])


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.good = ident.parse_release(release_json("good", "Right", [200000, 210000, 180000], ["A1", "A2", "B1"]))
        self.other = ident.parse_release(release_json("other", "Reissue CD", [200000, 210000, 180000, 90000], fmt="CD"))
        self.wrong = ident.parse_release(release_json("wrong", "Other", [60000, 70000, 80000]))
        self.detected = [201.0, 209.0, 181.0]

    def matches(self):
        return [[ident.Match(0.95, "r1", ["good", "other"])],
                [ident.Match(0.9, "r2", ["good", "other"])],
                [ident.Match(0.9, "r3", ["good"])]]

    def test_best_release_wins_by_votes_and_durations(self):
        ranked = ident.score_releases([self.wrong, self.other, self.good], self.matches(), self.detected)
        self.assertEqual([s.release.mbid for s in ranked][0], "good")
        self.assertEqual(ranked[-1].release.mbid, "wrong")

    def test_choose_best_requires_score_and_margin(self):
        ranked = ident.score_releases([self.good, self.other, self.wrong], self.matches(), self.detected)
        self.assertEqual(ident.choose_best(ranked).release.mbid, "good")
        self.assertIsNone(ident.choose_best(ranked, min_score=0.99))
        self.assertIsNone(ident.choose_best(ranked, margin=0.9))
        self.assertIsNone(ident.choose_best([]))

    def test_duration_fit_prefers_matching_counts_and_lengths(self):
        self.assertGreater(ident.duration_fit(self.good, self.detected), 0.95)
        self.assertLess(ident.duration_fit(self.wrong, self.detected), 0.5)
        self.assertEqual(ident.duration_fit(ident.parse_release({"id": "z", "media": []}), self.detected), 0.5)

    def test_durations_alone_can_decide_without_fingerprints(self):
        ranked = ident.score_releases([self.wrong, self.good], [[], [], []], self.detected)
        self.assertEqual(ranked[0].release.mbid, "good")


class ClientTests(unittest.TestCase):
    def test_musicbrainz_user_agent_and_url(self):
        calls = []

        def fetch(url, headers):
            calls.append((url, headers))
            return json.dumps(release_json("abc", "A", [1000])).encode()

        mb = ident.MusicBrainz("me@example.org", fetch=fetch, limiter=ident.RateLimiter(0))
        r = mb.get_release("abc")
        self.assertEqual(r.mbid, "abc")
        url, headers = calls[0]
        self.assertIn("/ws/2/release/abc?", url)
        self.assertIn("fmt=json", url)
        self.assertIn("me@example.org", headers["User-Agent"])
        self.assertIn("vinylrip/", headers["User-Agent"])

    def test_acoustid_lookup_parses_matches(self):
        body = {"status": "ok", "results": [{"id": "x", "score": 0.93, "recordings": [
            {"id": "rec1", "releases": [{"id": "relA"}, {"id": "relB"}]}, {"id": "rec2"}]}]}
        ac = ident.AcoustID("KEY", fetch=lambda u, h: json.dumps(body).encode(), limiter=ident.RateLimiter(0))
        out = ac.lookup("fp", 200)
        self.assertEqual(out[0].release_ids, ["relA", "relB"])
        self.assertEqual(out[1].release_ids, [])
        self.assertAlmostEqual(out[0].score, 0.93)

    def test_acoustid_without_key_is_a_network_error(self):
        with self.assertRaises(ident.NetworkError):
            ident.AcoustID("").lookup("fp", 10)

    def test_rate_limiter_spaces_calls(self):
        now = [0.0]
        sleeps = []
        rl = ident.RateLimiter(1.1, clock=lambda: now[0], sleep=lambda s: (sleeps.append(s), now.__setitem__(0, now[0] + s)))
        rl.wait()
        now[0] += 0.3
        rl.wait()
        self.assertAlmostEqual(sleeps[0], 0.8)

    def test_fpcalc_missing_is_reported(self):
        def runner(*a, **k):
            raise FileNotFoundError

        with self.assertRaises(ident.ToolMissing):
            ident.fingerprint_wav("x.wav", runner=runner)


if __name__ == "__main__":
    unittest.main()
