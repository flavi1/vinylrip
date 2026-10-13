# SPDX-License-Identifier: GPL-3.0-or-later
import unittest

from vinylrip import devices

ARECORD_L = """**** List of CAPTURE Hardware Devices ****
card 0: PCH [HDA Intel PCH], device 0: ALC892 Analog [ALC892 Analog]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 2: Device [USB Audio Device], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""

HW = """HW Params of device "hw:CARD=Device,DEV=0":
--------------------
ACCESS:  MMAP_INTERLEAVED RW_INTERLEAVED
FORMAT:  S16_LE S24_3LE S32_LE
SUBFORMAT:  STD
SAMPLE_BITS: [16 32]
FRAME_BITS: [32 64]
CHANNELS: 2
RATE: [44100 192000]
PERIOD_TIME: (125 1000000]
--------------------
"""


class DeviceTests(unittest.TestCase):
    def test_parse_cards(self):
        cards = devices.parse_arecord_l(ARECORD_L)
        self.assertEqual([c.card_id for c in cards], ["PCH", "Device"])
        self.assertEqual(cards[1].index, 2)
        self.assertEqual(cards[1].hw, "hw:CARD=Device,DEV=0")

    def test_find_card_by_id_name_usbid(self):
        cards = [devices.Card(2, "Device", "USB Audio Device", 0, "054c:0dd7"), devices.Card(0, "PCH", "HDA Intel PCH", 0)]
        self.assertEqual(devices.find_card(cards, "device").index, 2)
        self.assertEqual(devices.find_card(cards, "054C:0DD7").index, 2)
        self.assertEqual(devices.find_card(cards, "hda intel").index, 0)
        self.assertIsNone(devices.find_card(cards, ""))
        self.assertIsNone(devices.find_card(cards, "nothing"))

    def test_parse_hw_params(self):
        hp = devices.parse_hw_params(HW)
        self.assertEqual(hp.formats, ["S16_LE", "S24_3LE", "S32_LE"])
        self.assertEqual(hp.channels, (2, 2))
        self.assertEqual(hp.rate_range, (44100, 192000))

    def test_quality_options_best_first_and_only_supported(self):
        opts = devices.quality_options(devices.parse_hw_params(HW))
        self.assertEqual(opts[0], devices.Quality(192000, "S32_LE", 2))
        self.assertEqual(devices.default_quality(opts), opts[0])
        self.assertIn(devices.Quality(44100, "S16_LE", 2), opts)
        self.assertNotIn(devices.Quality(192000, "S32_LE", 1), opts)
        self.assertEqual(len(opts), 6 * 3)

    def test_discrete_rates(self):
        hp = devices.parse_hw_params("FORMAT: S16_LE\nCHANNELS: 2\nRATE: 48000\n")
        self.assertEqual([q.rate for q in devices.quality_options(hp)], [48000])

    def test_unsupported_formats_ignored(self):
        hp = devices.parse_hw_params("FORMAT: FLOAT_LE S24_LE\nCHANNELS: 2\nRATE: 48000\n")
        self.assertEqual(devices.quality_options(hp), [])

    def test_quality_key_roundtrip_and_label(self):
        q = devices.Quality(192000, "S32_LE", 2)
        self.assertEqual(devices.Quality.from_key(q.key()), q)
        self.assertEqual(q.label, "192 kHz / 32 bits")
        self.assertEqual(devices.Quality(44100, "S16_LE").label, "44.1 kHz / 16 bits")

    def test_list_capture_cards_uses_runner(self):
        cards = devices.list_capture_cards(runner=lambda cmd: ARECORD_L)
        self.assertEqual(len(cards), 2)


if __name__ == "__main__":
    unittest.main()
