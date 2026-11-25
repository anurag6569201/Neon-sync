"""Preserve provider byte counts and label bitrate-derived estimates explicitly."""

from __future__ import annotations

import math


def source_size(raw: dict, *, duration: float | None = None, live: bool = False) -> tuple[int | None, bool]:
    if live:
        return None, False
    for key, approximate in (("filesize", False), ("size_bytes", False), ("filesize_approx", True)):
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            return int(value), approximate
    seconds = raw.get("duration") if raw.get("duration") is not None else duration
    bitrate = raw.get("tbr")
    if bitrate is None:
        parts = [raw.get("vbr"), raw.get("abr")]
        bitrate = sum(value for value in parts if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0)
    if all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0 for value in (seconds, bitrate)):
        size = seconds * bitrate * 125
        if math.isfinite(size):
            return round(size), True
    return None, False
