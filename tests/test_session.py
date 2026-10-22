# SPDX-License-Identifier: GPL-3.0-or-later
import json, os, shutil, tempfile, unittest, io, contextlib

from vinylrip.config import Settings
from vinylrip.devices import Card, Quality
from vinylrip.session import Session


class FakeRec:
    def __init__(self, cmd, out, params):
        self.out, self.params = out, params

    def start(self):
        with open(self.out, "wb") as f:
            f.write(b"\0" * self.params.frame_bytes * self.params.rate)

    def stop(self):
        from vinylrip.capture import RecordingResult
        return RecordingResult(self.out, self.params.rate, 1.0, 0, [])


class FakePlayer:
    started = stopped = 0

    def __init__(self, *a):
        pass

    def start(self):
        FakePlayer.started += 1

    def stop(self):
        FakePlayer.stopped += 1


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.events = []
        self.cmds = []
        st = Settings(work_dir=self.tmp, min_free_gb=0.0)
        self.processed = []
        self.s = Session(st, Card(1, "Device", "Sony", 0), Quality(48000, "S16_LE", 2), 2,
                         self.events.append, lambda: self.cmds.pop(0) if self.cmds else None,
                         recorder_cls=FakeRec, player_cls=FakePlayer,
                         process_fn=lambda job, *a: {"album_dir": "/x"})

    def names(self):
        return [e["event"] for e in self.events]

    def test_full_flow(self):
        self.assertTrue(self.s.open())
        self.s.handle({"cmd": "start_face", "listen": True})
        self.s.handle({"cmd": "stop_face"})
        self.s.handle({"cmd": "start_face"})
        self.s.handle({"cmd": "stop_face"})
        self.s.handle({"cmd": "finish"})
        self.assertEqual(self.names(), ["ready", "recording", "listening", "face_done",
                                        "recording", "face_done", "done"])
        self.assertEqual([e["face"] for e in self.events if e["event"] == "face_done"], ["A", "B"])
        self.assertTrue(self.s.finished)
        self.assertEqual(FakePlayer.started, FakePlayer.stopped)

    def test_errors(self):
        self.s.open()
        self.s.handle({"cmd": "stop_face"})
        self.s.handle({"cmd": "finish"})
        self.s.handle({"cmd": "bogus"})
        self.assertEqual(self.names()[1:], ["error"] * 3)
        self.assertFalse(self.s.finished)

    def test_processing_error_explains_how_to_resume(self):
        from vinylrip.job import ProcessError

        def boom(job, *a):
            raise ProcessError("flac exploded", "encode", ["/tmp/.vinylrip-x"])
        self.s.process_fn = boom
        self.s.open()
        self.s.handle({"cmd": "start_face"})
        self.s.handle({"cmd": "stop_face"})
        self.s.handle({"cmd": "finish"})
        err = self.events[-1]
        self.assertEqual((err["event"], err["stage"]), ("error", "encode"))
        self.assertIn("/tmp/.vinylrip-x", err["help"])
        self.assertEqual(err["resume"], f"vinylrip process {self.s.job.dir}")
        # Commande refusée : pas d'étape, pas d'explication de reprise.
        self.s.handle({"cmd": "stop_face"})
        self.assertIsNone(self.events[-1]["stage"])
        self.assertNotIn("help", self.events[-1])

    def test_cancel_delete_and_keep(self):
        self.s.open()
        self.s.handle({"cmd": "start_face"})
        self.s.handle({"cmd": "cancel", "delete": True})
        self.assertFalse(os.path.exists(self.s.job.dir))
        self.assertFalse(self.events[-1]["kept"])
        self.s2 = Session(self.s.settings, self.s.card, self.s.quality, 1, self.events.append,
                          lambda: None, recorder_cls=FakeRec, player_cls=FakePlayer)
        self.s2.open()
        self.s2.handle({"cmd": "cancel", "delete": False})
        self.assertTrue(os.path.exists(self.s2.job.dir))

    def test_close_keeps_recording(self):
        self.s.open()
        self.s.handle({"cmd": "start_face"})
        self.s.close()
        self.assertEqual(len(self.s.job.faces), 1)

    def test_choose(self):
        class R: 
            artist, title, year, rtype, mbid, tracks = "a", "b", 1999, "Album", "m", [1]
        class S:
            release, score = R, 0.5
        self.cmds = [{"cmd": "choose", "index": 0}]
        self.assertEqual(self.s._choose([S]), 0)
        self.assertEqual(self.events[-1]["event"], "choose_release")
        self.cmds = [{"cmd": "choose", "index": None}]
        self.assertIsNone(self.s._choose([S]))
        self.cmds = []
        self.assertIsNone(self.s._choose([S]))


if __name__ == "__main__":
    unittest.main()
