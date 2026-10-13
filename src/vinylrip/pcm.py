# SPDX-License-Identifier: GPL-3.0-or-later
"""Raw PCM files: parameters, sidecar metadata, exact segment access."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass

import numpy as np

# name -> (bytes per sample, bits, ffmpeg raw format)
FORMATS = {
    "S16_LE": (2, 16, "s16le"),
    "S24_3LE": (3, 24, "s24le"),
    "S32_LE": (4, 32, "s32le"),
}


@dataclass(frozen=True)
class PCMParams:
    rate: int
    channels: int
    fmt: str

    def __post_init__(self):
        if self.fmt not in FORMATS:
            raise ValueError(f"unsupported format {self.fmt!r}; supported: {sorted(FORMATS)}")

    @property
    def sample_bytes(self) -> int:
        return FORMATS[self.fmt][0]

    @property
    def bits(self) -> int:
        return FORMATS[self.fmt][1]

    @property
    def ffmpeg_fmt(self) -> str:
        return FORMATS[self.fmt][2]

    @property
    def frame_bytes(self) -> int:
        return self.sample_bytes * self.channels

    @property
    def full_scale(self) -> float:
        return float(2 ** (self.bits - 1))


def sidecar_path(pcm_path: str) -> str:
    return pcm_path + ".json"


def write_sidecar(pcm_path: str, params: PCMParams, **extra) -> None:
    data = dict(asdict(params), **extra)
    with open(sidecar_path(pcm_path), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def read_sidecar(pcm_path: str) -> dict:
    with open(sidecar_path(pcm_path), encoding="utf-8") as fh:
        return json.load(fh)


class RawPCM:
    """A headerless little-endian PCM file."""

    def __init__(self, path: str, params: PCMParams | None = None):
        self.path = path
        if params is None:
            meta = read_sidecar(path)
            params = PCMParams(meta["rate"], meta["channels"], meta["fmt"])
        self.params = params

    @property
    def frames(self) -> int:
        return os.path.getsize(self.path) // self.params.frame_bytes

    @property
    def duration(self) -> float:
        return self.frames / self.params.rate

    def read_bytes(self, start: int, end: int) -> bytes:
        fb = self.params.frame_bytes
        with open(self.path, "rb") as fh:
            fh.seek(start * fb)
            return fh.read((end - start) * fb)

    def read_int32(self, start: int, end: int) -> np.ndarray:
        """Samples as int32 in their native scale, shape (frames, channels)."""
        end = min(end, self.frames)
        n = max(0, end - start)
        p = self.params
        raw = self.read_bytes(start, start + n)
        if p.fmt == "S16_LE":
            a = np.frombuffer(raw, dtype="<i2").astype(np.int32)
        elif p.fmt == "S32_LE":
            a = np.frombuffer(raw, dtype="<i4").astype(np.int32)
        else:  # S24_3LE
            b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
            a = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
            a = np.where(a & 0x800000, a - 0x1000000, a).astype(np.int32)
        return a.reshape(-1, p.channels)

    def sha256(self, start: int = 0, end: int | None = None, chunk_frames: int = 1 << 20) -> str:
        end = self.frames if end is None else end
        h = hashlib.sha256()
        pos = start
        while pos < end:
            n = min(chunk_frames, end - pos)
            h.update(self.read_bytes(pos, pos + n))
            pos += n
        return h.hexdigest()

    def low_bits_zero(self, bits: int = 8, chunk_frames: int = 1 << 20) -> bool:
        """True if the lowest `bits` bits of every sample are zero (S32_LE only)."""
        if self.params.fmt != "S32_LE":
            return False
        mask = (1 << bits) - 1
        pos = 0
        total = self.frames
        while pos < total:
            a = self.read_int32(pos, min(total, pos + chunk_frames))
            if np.any(a & mask):
                return False
            pos += chunk_frames
        return True


def pack_s32_to_s24(src: RawPCM, dst_path: str, chunk_frames: int = 1 << 20) -> RawPCM:
    """Lossless repack of S32_LE whose low byte is always zero into S24_3LE."""
    if src.params.fmt != "S32_LE":
        raise ValueError("source must be S32_LE")
    dst_params = PCMParams(src.params.rate, src.params.channels, "S24_3LE")
    total = src.frames
    with open(dst_path, "wb") as out:
        pos = 0
        while pos < total:
            end = min(total, pos + chunk_frames)
            raw = np.frombuffer(src.read_bytes(pos, end), dtype=np.uint8).reshape(-1, 4)
            out.write(raw[:, 1:4].tobytes())
            pos = end
    write_sidecar(dst_path, dst_params, frames=total)
    return RawPCM(dst_path, dst_params)
