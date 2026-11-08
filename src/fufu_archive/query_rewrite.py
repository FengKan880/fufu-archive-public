"""Dependency-free, deterministic query variants for conversational search."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable


# These lists intentionally contain only surface-level conversational wording.
# Query rewriting must never invent related terms or semantic synonyms.
ENGLISH_FILLERS = (
    "i would like to know",
    "i want to know",
    "could you please",
    "would you please",
    "can you please",
    "do you know",
    "could you",
    "would you",
    "can you",
    "please",
    "tell me",
    "show me",
    "where is",
    "what is",
)

CHINESE_FILLERS = (
    "可以不可以",
    "我想知道",
    "我想了解",
    "可不可以",
    "告诉我",
    "查一查",
    "查一下",
    "请问",
    "能不能",
    "能否",
    "是否可以",
    "帮忙",
    "帮我",
    "麻烦",
    "请",
    "一下",
    "呢",
    "吗",
)


def _normalize(query: str) -> str:
    """Collapse whitespace and replace punctuation/symbols with spaces."""
    normalized = unicodedata.normalize("NFKC", query)
    kept: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        if category[0] in {"L", "M", "N"}:
            kept.append(character)
        else:
            kept.append(" ")
    return " ".join("".join(kept).split())


def _remove_english_fillers(query: str, fillers: Iterable[str]) -> str:
    rewritten = query
    for filler in fillers:
        pattern = rf"(?<![A-Za-z0-9]){re.escape(filler)}(?![A-Za-z0-9])"
        rewritten = re.sub(pattern, " ", rewritten, flags=re.IGNORECASE)
    return rewritten


def _remove_chinese_fillers(query: str, fillers: Iterable[str]) -> str:
    rewritten = query
    for filler in fillers:
        rewritten = rewritten.replace(filler, " ")
    return rewritten


def rewrite_query(query: str) -> list[str]:
    """Return up to three ordered variants, always preserving ``query`` first."""
    variants = [query]
    normalized = _normalize(query)
    if normalized and normalized not in variants:
        variants.append(normalized)

    without_fillers = _remove_english_fillers(normalized, ENGLISH_FILLERS)
    without_fillers = _remove_chinese_fillers(without_fillers, CHINESE_FILLERS)
    without_fillers = " ".join(without_fillers.split())
    if without_fillers and without_fillers not in variants:
        variants.append(without_fillers)

    return variants[:3]
