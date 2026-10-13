# SPDX-License-Identifier: GPL-3.0-or-later
"""File naming from a user template, e.g. ``{artiste_album}/{annee} - {album}/{piste:02d}. {titre}.flac``."""
from __future__ import annotations

import re
import string

DEFAULT_TEMPLATE = "{artiste_album}/{annee} - {album}/{piste:02d}. {titre}.flac"
DEFAULT_MULTIDISC_TEMPLATE = "{artiste_album}/{annee} - {album}/CD{disque}/{piste:02d}. {titre}.flac"

FIELDS = (
    "artiste", "artiste_album", "album", "annee", "piste", "titre",
    "face", "disque", "nb_disques", "nb_pistes", "type",
)

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_PORTABLE = re.compile(r'[<>:"\\|?*]')


class TemplateError(ValueError):
    pass


def _limit_bytes(text: str, limit: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore")


def sanitize(value: str, portable: bool = False) -> str:
    """Make one value safe as a single path component."""
    text = _CONTROL.sub("", str(value)).replace("/", "-")
    if portable:
        text = _PORTABLE.sub("_", text)
    text = text.strip().rstrip(". ")
    if text in ("", ".", ".."):
        return "Inconnu"
    return text


def check_template(template: str) -> None:
    """Raise TemplateError when the template uses unknown fields or is malformed."""
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise TemplateError(str(exc)) from exc
    for _, field, _, _ in parsed:
        if field is not None and field != "" and field not in FIELDS:
            raise TemplateError(f"unknown field {{{field}}}; known: {', '.join(FIELDS)}")
    if template.startswith("/") or ".." in template.split("/"):
        raise TemplateError("template must be a relative path without '..'")


def render(template: str, values: dict, portable: bool = False) -> str:
    """Render a relative path. Text values are sanitised; numbers keep their format spec."""
    check_template(template)
    clean = {}
    for key in FIELDS:
        v = values.get(key)
        if isinstance(v, int) and not isinstance(v, bool):
            clean[key] = v
        elif v in (None, ""):
            clean[key] = "Inconnu" if key not in ("annee",) else "0000"
        else:
            clean[key] = sanitize(v, portable)
    try:
        out = string.Formatter().vformat(template, (), clean)
    except (KeyError, ValueError, IndexError) as exc:
        raise TemplateError(str(exc)) from exc
    parts = []
    for part in out.split("/"):
        part = part.strip().rstrip(". ") if part not in ("",) else part
        if part == "":
            continue
        if portable:
            part = _PORTABLE.sub("_", part)
        # keep the extension when truncating
        stem, dot, ext = part.rpartition(".")
        if dot and len(ext) <= 5 and stem:
            part = _limit_bytes(stem, 250 - len(ext)) + "." + ext
        else:
            part = _limit_bytes(part, 255)
        parts.append(part)
    if not parts:
        raise TemplateError("template produced an empty path")
    return "/".join(parts)


def unique_dir(path: str) -> str:
    """Return `path`, or `path (2)`, `path (3)`… so that nothing is overwritten."""
    import os
    if not os.path.exists(path):
        return path
    n = 2
    while os.path.exists(f"{path} ({n})"):
        n += 1
    return f"{path} ({n})"
