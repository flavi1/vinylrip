# SPDX-License-Identifier: GPL-3.0-or-later
import io
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from vinylrip import cli
from vinylrip.devices import Card, Quality


class FakeStdin:
    """Entrée qui ne se termine jamais (l'utilisateur n'a pas arrêté l'écoute)."""

    def readline(self):
        import time
        time.sleep(30)
        return ""


class ListenTests(unittest.TestCase):
    def run_listen(self, rec_cmd, extra=()):
        def fake_pipe(card, q, device=None, rec_err=None, play_err=None, **k):
            rec = subprocess.Popen(rec_cmd, stdout=subprocess.PIPE, stderr=rec_err)
            play = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"], stdin=rec.stdout, stderr=play_err)
            rec.stdout.close()
            return rec, play
        args = cli.build_parser().parse_args(["--json", "listen", *extra])
        card, q = Card(1, "Device", "USB Audio CODEC", 0), Quality(48000, "S16_LE", 2)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_resolve_card", return_value=card), \
                mock.patch.object(cli, "_resolve_quality", return_value=q), \
                mock.patch.object(cli, "listen_pipe", fake_pipe), \
                mock.patch.object(sys, "stdin", FakeStdin()), \
                mock.patch.dict(os.environ, {"LC_ALL": "C"}), \
                mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            code = cli.cmd_listen(args, None if False else mock.Mock(listen_device=""))
        return code, out.getvalue(), err.getvalue()

    def test_arecord_failure_ends_listening_with_an_explanation(self):
        code, out, err = self.run_listen([sys.executable, "-c",
                                          "import sys; sys.stderr.write('arecord: main:850: audio open error: Device or resource busy\\n'); sys.exit(1)"])
        self.assertEqual(code, 3)
        ev = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(ev["event"], "error")
        self.assertIn("Device or resource busy", ev["message"])
        self.assertIn("busy", ev["hint"])


if __name__ == "__main__":
    unittest.main()
