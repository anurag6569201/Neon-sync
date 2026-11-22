"""Pure WebVTT / SRT / JSON3 caption converter & clean plain-text transcript normalizer."""

from __future__ import annotations

import html
import json
import re

_TIMECODE_RE = re.compile(
    r"^\s*(\d{1,2}:)?(\d{2}):(\d{2})([.,]\d{3})\s*-->\s*(\d{1,2}:)?(\d{2}):(\d{2})([.,]\d{3})"
)
_VTT_INLINE_TAG_RE = re.compile(r"<[^>]+>")
_SEQ_NUM_RE = re.compile(r"^\s*\d+\s*$")
_HEADER_PREFIXES = ("WEBVTT", "Kind:", "Language:", "NOTE", "STYLE", "REGION")


def _fmt_ms(ms: int, *, sep: str = ".") -> str:
    ms = max(0, int(ms))
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{sep}{millis:03d}"


def convert_json3_to_vtt(raw_json: str) -> str:
    """Convert YouTube timedtext JSON3 caption payload into standard WebVTT."""
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        return ""
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        return ""

    cues: list[str] = ["WEBVTT", ""]
    for ev in events:
        if not isinstance(ev, dict):
            continue
        segs = ev.get("segs")
        if not isinstance(segs, list):
            continue
        text = "".join(str(s.get("utf8", "")) for s in segs if isinstance(s, dict))
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        start_ms = int(ev.get("tStartMs") or 0)
        dur_ms = int(ev.get("dDurationMs") or 2000)
        end_ms = start_ms + max(250, dur_ms)
        cues.append(f"{_fmt_ms(start_ms, sep='.')} --> {_fmt_ms(end_ms, sep='.')}")
        cues.append(text)
        cues.append("")
    return "\n".join(cues).strip() + "\n" if len(cues) > 2 else ""


def convert_vtt_to_srt(vtt_text: str) -> str:
    """Convert WebVTT subtitles into clean, player-compatible SubRip (.srt)."""
    lines = vtt_text.splitlines()
    out: list[str] = []
    cue_idx = 0
    i = 0
    n = len(lines)

    while i < n:
        stripped = lines[i].strip()
        if not stripped or stripped.startswith(_HEADER_PREFIXES):
            if stripped.startswith(("NOTE", "STYLE", "REGION")):
                while i < n and lines[i].strip():
                    i += 1
            i += 1
            continue

        m = _TIMECODE_RE.match(stripped)
        if not m:
            i += 1
            continue

        h1 = (m.group(1) or "00:").zfill(3)
        m1, s1, ms1 = m.group(2), m.group(3), m.group(4).replace(".", ",")
        h2 = (m.group(5) or "00:").zfill(3)
        m2, s2, ms2 = m.group(6), m.group(7), m.group(8).replace(".", ",")
        ts_line = f"{h1}{m1}:{s1}{ms1} --> {h2}{m2}:{s2}{ms2}"

        i += 1
        cue_lines: list[str] = []
        while i < n and lines[i].strip():
            cleaned = html.unescape(_VTT_INLINE_TAG_RE.sub("", lines[i])).strip()
            if cleaned:
                cue_lines.append(cleaned)
            i += 1

        if cue_lines:
            cue_idx += 1
            out.append(str(cue_idx))
            out.append(ts_line)
            out.extend(cue_lines)
            out.append("")

    return "\n".join(out).strip() + "\n" if out else ""


def normalize_subtitle_to_transcript(raw_subtitle: str) -> str:
    """Convert raw WebVTT, SRT, or JSON3 subtitles into clean, deduplicated plain text."""
    stripped_start = raw_subtitle.lstrip()
    if stripped_start.startswith("{"):
        converted = convert_json3_to_vtt(raw_subtitle)
        if converted:
            raw_subtitle = converted

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
