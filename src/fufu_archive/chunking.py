"""Dependency-free, boundary-aware text chunking.

Chunk sizes and overlaps are measured in Python Unicode characters (code
points), i.e. with :func:`len`, rather than bytes, words, or model tokens.
"""

from __future__ import annotations

import re


_PARAGRAPH_BOUNDARY = re.compile(r"\n[ \t]*\n+")
_SENTENCE_BOUNDARY = re.compile(r"[。！？!?；;.]|\n")


def _validate_sizes(target_size: int, overlap: int) -> None:
    if isinstance(target_size, bool) or not isinstance(target_size, int) or target_size < 1:
        raise ValueError("target_size must be a positive integer")
    if isinstance(overlap, bool) or not isinstance(overlap, int) or overlap < 0:
        raise ValueError("overlap must be a non-negative integer")
    if overlap >= target_size:
        raise ValueError("overlap must be smaller than target_size")


def _last_boundary(pattern: re.Pattern[str], text: str, start: int, stop: int) -> int | None:
    boundary: int | None = None
    for match in pattern.finditer(text, start, stop):
        boundary = match.end()
    return boundary


def _chunk_end(text: str, start: int, target_size: int, overlap: int) -> int:
    hard_end = min(len(text), start + target_size)
    if hard_end == len(text):
        return hard_end

    # Avoid tiny fragments caused by a boundary immediately after an overlap.
    # A boundary before the overlap window can make the next chunk repeat
    # the same early boundary when overlap is close to target_size.
    preferred_start = start + max(overlap + 1, target_size // 2)
    paragraph_end = _last_boundary(
        _PARAGRAPH_BOUNDARY, text, preferred_start, hard_end
    )
    if paragraph_end is not None:
        return paragraph_end

    sentence_end = _last_boundary(
        _SENTENCE_BOUNDARY, text, preferred_start, hard_end
    )
    return sentence_end if sentence_end is not None else hard_end


def chunk_text(text: str, *, target_size: int, overlap: int = 0) -> list[str]:
    """Split text into ordered character-sized chunks with source overlap.

    Paragraph boundaries are preferred, followed by sentence/punctuation
    boundaries. If neither exists, the text is split safely at ``target_size``.
    Source text is preserved exactly. Adjacent chunks use the requested
    overlap up to half the target size; larger overlaps are capped so every
    full chunk advances by at least half a target chunk instead of producing a
    long series of near-identical chunks.
    """
    _validate_sizes(target_size, overlap)
    if not text.strip():
        return []
    if len(text) <= target_size:
        return [text]

    chunks: list[str] = []
    effective_overlap = min(overlap, target_size // 2)
    start = 0
    while start < len(text):
        end = _chunk_end(text, start, target_size, effective_overlap)
        candidate = text[start:end]
        if candidate.strip():
            chunks.append(candidate)
        if end >= len(text):
            break
        next_start = end - min(effective_overlap, end - start)
        start = max(start + 1, next_start)
    return chunks
