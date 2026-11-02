"""Read-only comparison of a stored embedding with a supplied legacy embedder."""

from __future__ import annotations

import json
import math
import struct
from numbers import Real
from typing import Any, Callable


EMBED_DIM = 384
_FLOAT32_MAX = 3.4028234663852886e38
_MATCH_TOLERANCE = 1e-7


def _parse_vector(value: Any) -> list[float] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    if not isinstance(value, list) or len(value) != EMBED_DIM:
        return None

    vector: list[float] = []
    for coordinate in value:
        if isinstance(coordinate, bool) or not isinstance(coordinate, Real):
            return None
        try:
            converted = float(coordinate)
            if not math.isfinite(converted) or abs(converted) > _FLOAT32_MAX:
                return None
            struct.pack("<f", converted)
        except (OverflowError, TypeError, ValueError):
            return None
        vector.append(converted)

    if not any(float(f"{coordinate:.6f}") != 0.0 for coordinate in vector):
        return None
    return vector


def _matches_legacy(stored: list[float], legacy: Any) -> bool:
    expected = _parse_vector(legacy)
    if expected is None:
        raise ValueError("Legacy reference function returned an invalid vector")
    for stored_coordinate, legacy_coordinate in zip(stored, expected):
        writer_coordinate = float(f"{legacy_coordinate:.6f}")
        if abs(stored_coordinate - writer_coordinate) > _MATCH_TOLERANCE:
            return False
    return True


def classify(text: Any, vector: Any, legacy_embed: Callable[[str], Any]) -> str:
    """Classify a stored vector against the exact legacy embedding function.

    ``not_matching_known_hash`` means only that this vector did not match the
    supplied legacy output; it is not evidence of semantic embedding quality.
    """
    parsed_vector = _parse_vector(vector)
    if parsed_vector is None:
        return "invalid_vector"
    if not isinstance(text, str):
        return "missing_text"

    legacy_vector = legacy_embed(text.strip()[:8000])
    if _matches_legacy(parsed_vector, legacy_vector):
        return "known_hash_match"
    return "not_matching_known_hash"
