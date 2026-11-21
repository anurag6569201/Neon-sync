"""Pure WebVTT / SRT caption parser & clean plain-text transcript normalizer."""

from __future__ import annotations

import html
import re

_TIMECODE_RE = re.compile(
    r"^\s*\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3}\s*-->\s*\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3}"
)
_VTT_INLINE_TAG_RE = re.compile(r"<[^>]+>")
_SEQ_NUM_RE = re.compile(r"^\s*\d+\s*$")
_HEADER_PREFIXES = ("WEBVTT", "Kind:", "Language:", "NOTE", "STYLE", "REGION")


def normalize_subtitle_to_transcript(raw_subtitle: str) -> str:
    """Convert raw WebVTT or SRT subtitles into clean, deduplicated plain text."""
    lines: list[str] = []
    in_note_or_style = False

    for raw_line in raw_subtitle.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            in_note_or_style = False
            continue
        if stripped.startswith(("NOTE", "STYLE", "REGION")):
            in_note_or_style = True
            continue
        if in_note_or_style:
            continue
        if stripped.startswith(_HEADER_PREFIXES):
            continue
        if _TIMECODE_RE.match(stripped) or _SEQ_NUM_RE.match(stripped):
            continue

        cleaned = _VTT_INLINE_TAG_RE.sub("", stripped)
        cleaned = html.unescape(cleaned).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        if not cleaned:
            continue

        # Deduplicate rolling auto-generated YouTube captions where line N repeats N-1
        if lines and cleaned == lines[-1]:
            continue
        if lines and cleaned.startswith(lines[-1]) and len(cleaned) > len(lines[-1]):
            lines[-1] = cleaned
            continue
        lines.append(cleaned)

    return "\n".join(lines)
