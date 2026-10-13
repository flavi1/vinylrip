# SPDX-License-Identifier: GPL-3.0-or-later
"""Reduced-quality copy derived from a verified master, checked by measurement (not bit-exact)."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass

import numpy as np


class ConvertError(RuntimeError):
    pass


def _ffmpeg(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["ffmpeg", "-v", "error", "-y"] + args, capture_output=True)


def reduce_master(master: str, out: str, rate: int, bits: int, dither: str = "triangular") -> str:
    """Resample/requantise `master` into `out`. Returns the resampler used (soxr or swr)."""
    if bits not in (16, 24):
        raise ConvertError("reduced output supports 16 or 24 bits")
    last = ""
    for resampler in ("soxr", "swr"):
        filt = f"aresample=out_sample_rate={rate}:resampler={resampler}"
        if bits == 16:
            filt += f":dither_method={dither}"
        args = ["-i", master, "-map", "0:a", "-map", "0:v?", "-c:v", "copy", "-map_metadata", "0",
                "-af", filt, "-c:a", "flac"]
        args += ["-sample_fmt", "s16"] if bits == 16 else ["-sample_fmt", "s32", "-bits_per_raw_sample", "24"]
        res = _ffmpeg(args + [out])
        if res.returncode == 0:
            return resampler
        last = res.stderr.decode(errors="replace")
    raise ConvertError(last.strip())


def _decode_f32(path: str, rate: int | None) -> np.ndarray:
    args = ["-i", path, "-map", "0:a", "-ac", "2"]
    if rate:
        args += ["-ar", str(rate)]
    res = subprocess.run(["ffmpeg", "-v", "error"] + args + ["-f", "f32le", "pipe:1"], capture_output=True)
    if res.returncode != 0:
        raise ConvertError(res.stderr.decode(errors="replace").strip())
    return np.frombuffer(res.stdout, dtype="<f4").reshape(-1, 2)


def _rms_db(a: np.ndarray) -> float:
    r = float(np.sqrt(np.mean(a.astype(np.float64) ** 2)))
    return 20 * np.log10(max(r, 1e-9))


@dataclass
class ReducedCheck:
    ok: bool
    frames_expected: int
    frames_got: int
    rms_diff_db: float
    clipped_extra: int


def check_reduced(master: str, reduced: str, rate: int, frame_tol: int = 2,
                  rms_tol_db: float = 0.3) -> ReducedCheck:
    """Measure: sample count, level on the common band, no new clipping."""
    ref = _decode_f32(master, rate)
    got = _decode_f32(reduced, None)
    clip = lambda x: int(np.count_nonzero(np.abs(x) >= 0.9999))
    diff = abs(_rms_db(ref) - _rms_db(got))
    extra = clip(got) - clip(ref)
    ok = abs(len(ref) - len(got)) <= frame_tol and diff <= rms_tol_db and extra <= 2
    return ReducedCheck(ok, len(ref), len(got), round(diff, 4), extra)
