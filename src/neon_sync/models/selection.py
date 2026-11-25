"""Bounded, positive playlist selections shared by validation and execution."""

from __future__ import annotations

import re

_ITEM = re.compile(r"([1-9]\d*)(?:[-:]([1-9]\d*)(?::([1-9]\d*))?)?")


def playlist_indices(selection: str, *, limit: int = 500) -> list[int]:
    """Parse inclusive N, N-M, and N:M:STEP ranges, preserving unique order."""
    indices: dict[int, None] = {}
    for segment in selection.split(","):
        match = _ITEM.fullmatch(segment.strip())
        if match is None:
            raise ValueError("Use positive playlist positions, for example 1-5, 1,3,7, or 1:9:2.")
        start = int(match[1])
        end = int(match[2] or start)
        step = int(match[3] or 1)
        if end < start:
            raise ValueError("Playlist range end must be greater than or equal to its start.")
        if (end - start) // step + 1 > limit:
            raise ValueError(f"Select at most {limit} playlist items per job.")
        for index in range(start, end + 1, step):
            indices[index] = None
            if len(indices) > limit:
                raise ValueError(f"Select at most {limit} playlist items per job.")
    return list(indices)
