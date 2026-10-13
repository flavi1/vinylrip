# SPDX-License-Identifier: GPL-3.0-or-later
"""FLAC master encoding and bit-exact verification."""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from dataclasses import dataclass

from .pcm import PCMParams, RawPCM

CHUNK_FRAMES = 1 << 18


class EncodeError(RuntimeError):
    pass


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def flac_version() -> tuple[int, ...] | None:
    if not have("flac"):
        return None
    out = subprocess.run(["flac", "--version"], capture_output=True, text=True).stdout
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", out)
    return tuple(int(x) for x in m.groups() if x is not None) if m else None


def choose_encoder(params: PCMParams, prefer: str = "auto") -> str:
    """'flac' when usable for this bit depth, else 'ffmpeg'."""
    if prefer in ("flac", "ffmpeg"):
        return prefer
    v = flac_version()
    if v and (params.bits < 32 or v >= (1, 4)):
        return "flac"
    if have("ffmpeg"):
        return "ffmpeg"
    raise EncodeError("neither a recent flac (>= 1.4 for 32-bit) nor ffmpeg is available")


def _flac_cmd(p: PCMParams, out: str, tags: dict, picture: str | None) -> list[str]:
    cmd = ["flac", "-s", "-f", "--force-raw-input", "--endian=little", "--sign=signed",
           f"--channels={p.channels}", f"--bps={p.bits}", f"--sample-rate={p.rate}",
           "-8", "--verify", "-o", out]
    for k, v in tags.items():
        cmd.append(f"--tag={k}={v}")
    if picture:
        cmd.append(f"--picture={picture}")
    return cmd + ["-"]


def _ffmpeg_cmd(p: PCMParams, out: str, tags: dict, picture: str | None) -> list[str]:
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", p.ffmpeg_fmt, "-ar", str(p.rate),
           "-ac", str(p.channels), "-i", "pipe:0"]
    if picture:
        cmd += ["-i", picture, "-map", "0:a", "-map", "1:v", "-c:v", "copy",
                "-disposition:v:0", "attached_pic"]
    cmd += ["-c:a", "flac"]
    if p.bits > 16:
        cmd += ["-bits_per_raw_sample", str(p.bits)]
    if p.bits == 32:
        # without this ffmpeg (6.x) silently encodes 32-bit input as 24-bit
        cmd += ["-strict", "experimental"]
    for k, v in tags.items():
        cmd += ["-metadata", f"{k}={v}"]
    return cmd + [out]


def encode_master(pcm: RawPCM, start: int, end: int, out: str, tags: dict | None = None,
                  picture: str | None = None, prefer: str = "auto") -> str:
    """Encode frames [start, end) losslessly. Returns the encoder used."""
    p = pcm.params
    enc = choose_encoder(p, prefer)
    cmd = (_flac_cmd if enc == "flac" else _ffmpeg_cmd)(p, out, tags or {}, picture)
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        pos = start
        while pos < end:
            n = min(CHUNK_FRAMES, end - pos)
            proc.stdin.write(pcm.read_bytes(pos, pos + n))
            pos += n
        proc.stdin.close()
    except BrokenPipeError:
        pass
    err = proc.stderr.read().decode(errors="replace")
    proc.stderr.close()
    if proc.wait() != 0:
        raise EncodeError(f"{enc} failed: {err.strip()}")
    return enc


def decoded_stream(flac_path: str, params: PCMParams, prefer: str = "auto"):
    """Yield the decoded PCM bytes of a FLAC file, in the capture's raw format."""
    if prefer == "flac" or (prefer == "auto" and have("flac")):
        cmd = ["flac", "-d", "-c", "-s", "--force-raw-output", "--endian=little", "--sign=signed", flac_path]
    else:
        cmd = ["ffmpeg", "-v", "error", "-i", flac_path, "-map", "0:a", "-f", params.ffmpeg_fmt, "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        while True:
            block = proc.stdout.read(1 << 20)
            if not block:
                break
            yield block
    finally:
        proc.stdout.close()
    err = proc.stderr.read().decode(errors="replace")
    proc.stderr.close()
    if proc.wait() != 0:
        raise EncodeError(f"decoding {flac_path} failed: {err.strip()}")


@dataclass
class VerifyResult:
    ok: bool
    expected_sha256: str
    decoded_sha256: str
    expected_frames: int
    decoded_frames: int


def verify_master(pcm: RawPCM, start: int, end: int, flac_path: str, prefer: str = "auto") -> VerifyResult:
    """Bit-exact check: SHA-256 and length of the decoded FLAC against the raw segment."""
    expected = pcm.sha256(start, end)
    h = hashlib.sha256()
    total = 0
    for block in decoded_stream(flac_path, pcm.params, prefer):
        h.update(block)
        total += len(block)
    decoded_frames = total // pcm.params.frame_bytes
    got = h.hexdigest()
    return VerifyResult(got == expected and decoded_frames == end - start,
                        expected, got, end - start, decoded_frames)
