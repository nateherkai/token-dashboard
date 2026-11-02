"""MCP-backed semantic embeddings for the token dashboard."""
from __future__ import annotations

import json
import math
import threading
from numbers import Real
from typing import Any, Optional

from . import mcp as mcp_client


EMBED_DIM = 384


_lock = threading.Lock()
_cache: dict[str, list[float]] = {}
_cache_max = 5000


def _cache_get(text: str) -> Optional[list[float]]:
    with _lock:
        vector = _cache.get(text)
        return list(vector) if vector is not None else None


def _cache_put(text: str, vector: list[float]) -> None:
    with _lock:
        if len(_cache) >= _cache_max:
            to_drop = max(1, len(_cache) // 10)
            for key in list(_cache)[:to_drop]:
                _cache.pop(key, None)
        _cache[text] = list(vector)


def _validate_vector(value: Any) -> Optional[list[float]]:
    if not isinstance(value, list) or len(value) != EMBED_DIM:
        return None

    vector: list[float] = []
    try:
        for coordinate in value:
            if isinstance(coordinate, bool) or not isinstance(coordinate, Real):
                return None
            converted = float(coordinate)
            if not math.isfinite(converted) or abs(converted) > 3.4028234663852886e38:
                return None
            vector.append(converted)
    except (TypeError, ValueError, OverflowError):
        return None

    if not any(round(coordinate, 6) != 0.0 for coordinate in vector):
        return None
    return vector


def _candidate_from_mapping(mapping: Any) -> Optional[list[float]]:
    if not isinstance(mapping, dict):
        return None
    for key in ("embedding", "vector", "body_vec"):
        if key in mapping:
            vector = _validate_vector(mapping[key])
            if vector is not None:
                return vector
    return None


def _vectors_from_content(result: Any) -> list[Any]:
    if not isinstance(result, dict) or result.get("isError"):
        return []

    candidates: list[Any] = []
    structured = _candidate_from_mapping(result)
    if structured is not None:
        candidates.append(structured)

    structured = _candidate_from_mapping(result.get("structuredContent"))
    if structured is not None:
        candidates.append(structured)

    content = result.get("content")
    if not isinstance(content, list):
        return candidates
    for block in content:
        if not isinstance(block, dict):
            continue
        structured = _candidate_from_mapping(block)
        if structured is not None:
            candidates.append(structured)

        text = block.get("text")
        if not isinstance(text, str):
            continue
        try:
            payload = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        structured = _candidate_from_mapping(payload)
        if structured is not None:
            candidates.append(structured)
    return candidates


def _via_mcp(text: str) -> Optional[list[float]]:
    """Call MCP and return only a structurally validated provider vector."""
    if not mcp_client.is_configured():
        return None
    try:
        result = mcp_client.call_tool(
            mcp_client.http_url(),
            "code_embed",
            {"text": text},
            timeout=15.0,
        )
    except Exception:
        return None

    for candidate in _vectors_from_content(result):
        vector = _validate_vector(candidate)
        if vector is not None:
            return vector
    return None


def embed(text: str) -> Optional[list[float]]:
    """Return a validated provider embedding, or ``None`` on failure."""
    if text is None:
        text = ""
    elif not isinstance(text, str):
        text = str(text)
    text = text.strip()[:8000]

    cached = _cache_get(text)
    if cached is not None:
        return cached

    vector = _via_mcp(text)
    if vector is None:
        return None
    _cache_put(text, vector)
    return list(vector)


def embed_batch(texts: list) -> list[Optional[list[float]]]:
    """Embed each text independently, preserving failures as ``None``."""
    return [embed(text) for text in texts]


def is_real_embedding_available() -> bool:
    """Return whether a probe produces a structurally valid provider vector."""
    return _via_mcp("token-dashboard probe") is not None
