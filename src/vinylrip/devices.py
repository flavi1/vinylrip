# SPDX-License-Identifier: GPL-3.0-or-later
"""ALSA capture devices and the qualities a card really offers."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field

from .pcm import FORMATS

STANDARD_RATES = (44100, 48000, 88200, 96000, 176400, 192000)
# Preference order when picking a default: best first.
_FORMAT_RANK = {"S32_LE": 3, "S24_3LE": 2, "S16_LE": 1}


@dataclass(frozen=True)
class Card:
    index: int
    card_id: str       # short id, stable across re-plugging (e.g. "Device")
    name: str
    device: int
    usb_id: str = ""   # "vendor:product" when known

    @property
    def hw(self) -> str:
        return f"hw:CARD={self.card_id},DEV={self.device}"


@dataclass(frozen=True)
class Quality:
    rate: int
    fmt: str
    channels: int = 2

    @property
    def label(self) -> str:
        bits = FORMATS[self.fmt][1]
        khz = self.rate / 1000
        khz_s = f"{khz:g}"
        return f"{khz_s} kHz / {bits} bits"

    def key(self) -> str:
        return f"{self.rate}:{self.fmt}:{self.channels}"

    @staticmethod
    def from_key(key: str) -> "Quality":
        rate, fmt, ch = key.split(":")
        return Quality(int(rate), fmt, int(ch))


@dataclass
class HwParams:
    formats: list = field(default_factory=list)
    channels: tuple = (0, 0)
    rates: list = field(default_factory=list)       # explicit values
    rate_range: tuple | None = None                   # (min, max)

    def supports_rate(self, rate: int) -> bool:
        if rate in self.rates:
            return True
        return bool(self.rate_range and self.rate_range[0] <= rate <= self.rate_range[1])


_CARD_RE = re.compile(
    r"^card\s+(\d+):\s*(\S+)\s*\[(.*?)\],\s*device\s+(\d+):", re.M
)


def parse_arecord_l(text: str) -> list[Card]:
    cards = []
    for m in _CARD_RE.finditer(text):
        cards.append(Card(int(m.group(1)), m.group(2), m.group(3), int(m.group(4))))
    return cards


def _parse_numbers(s: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", s)]


def parse_hw_params(text: str) -> HwParams:
    """Parse the output of ``arecord --dump-hw-params``."""
    hp = HwParams()
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        key = key.strip().upper()
        rest = rest.strip()
        if key == "FORMAT":
            hp.formats = rest.replace("[", " ").replace("]", " ").split()
        elif key == "CHANNELS":
            nums = _parse_numbers(rest)
            if nums:
                hp.channels = (min(nums), max(nums))
        elif key == "RATE":
            nums = _parse_numbers(rest)
            if rest.startswith("[") and len(nums) == 2 and "," not in rest:
                hp.rate_range = (nums[0], nums[1])
            elif nums:
                hp.rates = nums
    return hp


def quality_options(hp: HwParams, channels: int = 2) -> list[Quality]:
    """Qualities the card offers, best first. Only formats the engine can handle."""
    if not (hp.channels[0] <= channels <= hp.channels[1]):
        return []
    fmts = [f for f in hp.formats if f in FORMATS]
    rates = [r for r in STANDARD_RATES if hp.supports_rate(r)]
    out = [Quality(r, f, channels) for r in rates for f in fmts]
    out.sort(key=lambda q: (q.rate, _FORMAT_RANK[q.fmt]), reverse=True)
    return out


def default_quality(options: list[Quality]) -> Quality | None:
    return options[0] if options else None


def read_usb_id(card_index: int) -> str:
    try:
        with open(f"/proc/asound/card{card_index}/usbid", encoding="utf-8") as fh:
            return fh.read().strip().lower()
    except OSError:
        return ""


def list_capture_cards(runner=None) -> list[Card]:
    run = runner or _run
    cards = parse_arecord_l(run(["arecord", "-l"]))
    return [Card(c.index, c.card_id, c.name, c.device, read_usb_id(c.index)) for c in cards]


def find_card(cards: list[Card], wanted: str) -> Card | None:
    """Match by card id, name substring or ``vendor:product``; empty wanted = no match."""
    if not wanted:
        return None
    w = wanted.strip().lower()
    for c in cards:
        if w in (c.card_id.lower(), c.usb_id.lower()):
            return c
    for c in cards:
        if w in c.name.lower():
            return c
    return None


def probe_hw_params(card: Card, runner=None) -> HwParams:
    """Ask the card what it supports. Opens the device briefly, then times out."""
    cmd = ["arecord", "-D", card.hw, "--dump-hw-params", "-d", "1", "-f", "S16_LE", "/dev/null"]
    run = runner or _run
    return parse_hw_params(run(cmd))


def _run(cmd: list[str]) -> str:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except FileNotFoundError as exc:
        raise RuntimeError(f"{cmd[0]} not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{cmd[0]} timed out") from exc
    # arecord prints the parameter dump on stderr
    return res.stdout + "\n" + res.stderr
