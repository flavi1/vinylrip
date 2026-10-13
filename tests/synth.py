# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic vinyl faces: lead-in, music, gaps with crackle, run-out."""
import numpy as np

from vinylrip.pcm import FORMATS, PCMParams, write_sidecar

SIL = 0.001  # groove noise sigma, about -60 dBFS


def _crackle(x, rate, rng, every_s=0.4):
    n = len(x)
    for pos in range(int(rng.integers(0, int(rate * every_s) + 1)), n - 4, int(rate * every_s)):
        x[pos:pos + 2] += 0.5 * rng.choice([-1, 1])
    return x


def music(sec, rate, f0, rng):
    t = np.arange(int(sec * rate)) / rate
    env = 0.7 + 0.3 * np.sin(2 * np.pi * 0.5 * t)
    x = env * (0.25 * np.sin(2 * np.pi * f0 * t) + 0.1 * np.sin(2 * np.pi * 2.01 * f0 * t + 1))
    return x + rng.normal(0, SIL, len(t))


def silence(sec, rate, rng, db_above=0.0, crackle=True):
    x = rng.normal(0, SIL * 10 ** (db_above / 20), int(sec * rate))
    return _crackle(x, rate, rng) if crackle else x


def build(parts, rate, seed=1):
    """parts: ('music', sec, f0) | ('silence', sec) | ('noise', sec, db_above) | ('thump',)"""
    rng = np.random.default_rng(seed)
    out, marks, t = [], [], 0.0
    for p in parts:
        if p[0] == "music":
            seg = music(p[1], rate, p[2], rng)
        elif p[0] == "silence":
            seg = silence(p[1], rate, rng)
        elif p[0] == "noise":
            seg = silence(p[1], rate, rng, p[2])
        elif p[0] == "thump":
            seg = np.zeros(int(0.004 * rate)) + 0.8
        else:
            raise ValueError(p)
        out.append(seg)
        marks.append((p[0], round(t, 3), round(t + len(seg) / rate, 3)))
        t += len(seg) / rate
    return np.concatenate(out), marks


def write_raw(path, x, rate, fmt="S32_LE", channels=2):
    x = np.clip(x, -1.0, 0.999999)
    nbytes, bits, _ = FORMATS[fmt]
    q = np.round(x * (2 ** (bits - 1))).astype(np.int64)
    q = np.repeat(q[:, None], channels, axis=1)
    if fmt == "S16_LE":
        data = q.astype("<i2").tobytes()
    elif fmt == "S32_LE":
        data = q.astype("<i4").tobytes()
    else:
        u = (q & 0xFFFFFF).astype(np.uint32).reshape(-1)
        data = np.stack([u & 0xFF, (u >> 8) & 0xFF, (u >> 16) & 0xFF], axis=1).astype(np.uint8).tobytes()
    with open(path, "wb") as fh:
        fh.write(data)
    params = PCMParams(rate, channels, fmt)
    write_sidecar(path, params, frames=len(x))
    return params


def three_track_face(rate=8000, seed=1, internal_gap=None):
    """Lead-in, 3 tracks (20/25/15 s) separated by 2 s gaps, run-out. Returns (signal, truth)."""
    t2 = [("music", 25, 330)] if internal_gap is None else \
        [("music", 12, 330), ("silence", internal_gap, 0), ("music", 13, 330)]
    parts = [("silence", 3), ("thump",), ("silence", 1), ("music", 20, 220), ("silence", 2)] + t2 + \
            [("silence", 2), ("music", 15, 440), ("silence", 6)]
    x, marks = build(parts, rate, seed)
    music_marks = [m for m in marks if m[0] == "music"]
    gaps = [m for m in marks if m[0] == "silence" and abs(m[2] - m[1] - 2.0) < 1e-6]
    return x, {"gap_centers": [round((g[1] + g[2]) / 2, 3) for g in gaps], "marks": marks,
               "first_music": music_marks[0][1], "last_music": music_marks[-1][2]}
