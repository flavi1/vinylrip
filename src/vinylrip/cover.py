# SPDX-License-Identifier: GPL-3.0-or-later
"""Cover art from the Cover Art Archive (MusicBrainz releases)."""
from __future__ import annotations

from .identify import NetworkError, default_fetch

CAA = "https://coverartarchive.org/release/{mbid}/front"


def fetch_cover(mbid: str, fetch=None) -> bytes | None:
    """Front cover bytes, or None when the release has no cover or the network fails."""
    fetch = fetch or default_fetch
    try:
        data = fetch(CAA.format(mbid=mbid), {"User-Agent": "vinylrip"})
    except NetworkError:
        return None
    if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n":
        return data
    return None


def extension_for(data: bytes) -> str:
    return "png" if data[:4] == b"\x89PNG" else "jpg"
