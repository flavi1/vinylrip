# SPDX-License-Identifier: GPL-3.0-or-later
import os
import tempfile
import unittest

from vinylrip import naming


class NamingTests(unittest.TestCase):
    V = {"artiste": "Pink Floyd", "artiste_album": "Pink Floyd", "album": "The Wall", "annee": 1979,
         "piste": 3, "titre": "Another Brick", "face": "A", "disque": 1, "nb_disques": 2,
         "nb_pistes": 13, "type": "Album"}

    def test_default_template(self):
        self.assertEqual(naming.render(naming.DEFAULT_TEMPLATE, self.V),
                         "Pink Floyd/1979 - The Wall/03. Another Brick.flac")

    def test_multidisc_template(self):
        self.assertEqual(naming.render(naming.DEFAULT_MULTIDISC_TEMPLATE, self.V),
                         "Pink Floyd/1979 - The Wall/CD1/03. Another Brick.flac")

    def test_slash_in_value_is_replaced(self):
        v = dict(self.V, titre="AC/DC Medley", artiste_album="AC/DC")
        out = naming.render(naming.DEFAULT_TEMPLATE, v)
        self.assertEqual(out.count("/"), 2)
        self.assertIn("AC-DC", out)

    def test_empty_value_becomes_unknown(self):
        out = naming.render("{artiste_album}/{album}/{piste:02d}.flac", dict(self.V, album=""))
        self.assertEqual(out, "Pink Floyd/Inconnu/03.flac")

    def test_trailing_dots_and_spaces_stripped(self):
        out = naming.render("{album}/{piste:02d}.flac", dict(self.V, album="Etc... "))
        self.assertEqual(out, "Etc/03.flac")

    def test_portable_mode(self):
        out = naming.render("{album}/{piste:02d} {titre}.flac", dict(self.V, titre='Why? "Because": yes'), portable=True)
        self.assertNotRegex(out, r'[?":]')

    def test_unknown_field_rejected(self):
        with self.assertRaises(naming.TemplateError):
            naming.render("{nope}/{titre}.flac", self.V)

    def test_path_traversal_rejected(self):
        for bad in ("../{titre}.flac", "/abs/{titre}.flac"):
            with self.assertRaises(naming.TemplateError):
                naming.render(bad, self.V)

    def test_long_names_truncated_keep_extension(self):
        out = naming.render("{titre}.flac", dict(self.V, titre="é" * 300))
        self.assertTrue(out.endswith(".flac"))
        self.assertLessEqual(len(out.encode()), 255)

    def test_unique_dir_never_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "Album")
            self.assertEqual(naming.unique_dir(p), p)
            os.mkdir(p)
            self.assertEqual(naming.unique_dir(p), p + " (2)")
            os.mkdir(p + " (2)")
            self.assertEqual(naming.unique_dir(p), p + " (3)")


if __name__ == "__main__":
    unittest.main()
