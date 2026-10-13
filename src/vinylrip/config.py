# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings, loaded from a TOML file (default ~/.config/vinylrip/config.toml)."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field, fields

from .naming import DEFAULT_MULTIDISC_TEMPLATE, DEFAULT_TEMPLATE

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover (python < 3.11)
    tomllib = None


def xdg_music_dir() -> str:
    try:
        out = subprocess.run(["xdg-user-dir", "MUSIC"], capture_output=True, text=True, timeout=5).stdout.strip()
        if out and out != os.path.expanduser("~"):
            return out
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return os.path.join(os.path.expanduser("~"), "Music")


def default_config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "vinylrip", "config.toml")


@dataclass
class Settings:
    # capture
    device: str = ""                       # card id, name or vendor:product
    quality: str = ""                      # "rate:fmt:channels", empty = best the card offers
    work_dir: str = os.path.join(os.path.expanduser("~"), ".cache", "vinylrip")
    min_free_gb: float = 10.0
    listen_device: str = ""
    listen_delay_s: float = 0.5
    # split
    strong_gap_s: float = 1.0
    weak_gap_s: float = 0.3
    noise_segment_max_s: float = 15.0
    head_margin_s: float = 1.0
    tail_margin_s: float = 3.0
    # identification
    identify_mode: str = "auto"            # auto | manual | off
    min_score: float = 0.55
    margin: float = 0.08
    acoustid_key: str = ""
    contact: str = ""
    # output
    music_root: str = ""                   # empty = xdg MUSIC dir
    masters_root: str = os.path.join(os.path.expanduser("~"), "Archives vinyle")
    template: str = DEFAULT_TEMPLATE
    multidisc_template: str = DEFAULT_MULTIDISC_TEMPLATE
    portable_names: bool = False
    output_rate: int = 0                   # 0 = same as capture
    output_bits: int = 0                   # 0 = same as capture; 16 or 24 otherwise
    dither: str = "triangular"
    master_24bit_if_lossless: bool = False
    delete_raw: bool = True
    delete_master_after_reduce: bool = False
    keep_full_face: bool = False
    embed_cover: bool = True
    cover_file: bool = True
    encoder: str = "auto"                  # auto | flac | ffmpeg

    def resolved_music_root(self) -> str:
        return self.music_root or xdg_music_dir()

    def wants_reduction(self, capture_rate: int, capture_bits: int) -> bool:
        rate = self.output_rate or capture_rate
        bits = self.output_bits or capture_bits
        return rate < capture_rate or bits < capture_bits


def load_settings(path: str | None = None) -> Settings:
    path = path or default_config_path()
    s = Settings()
    if not os.path.exists(path):
        return s
    if tomllib is None:
        raise RuntimeError("python >= 3.11 is required to read the TOML configuration")
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    known = {f.name for f in fields(Settings)}
    flat = {}
    for k, v in data.items():
        if isinstance(v, dict):
            flat.update(v)
        else:
            flat[k] = v
    unknown = sorted(set(flat) - known)
    if unknown:
        raise ValueError(f"unknown settings in {path}: {', '.join(unknown)}")
    for k, v in flat.items():
        setattr(s, k, v)
    return s
