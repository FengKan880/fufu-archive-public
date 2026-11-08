"""Deterministic heuristic scanning for instruction-like ingested text.

The scanner only reports matches. It does not interpret, execute, reject, or
modify the supplied text.
"""

from __future__ import annotations

import re
from typing import TypedDict


SCANNER_VERSION = "1.0.0"
INJECTION_SCAN_METADATA_KEY = "security.injection_scan"


class PatternMatch(TypedDict):
    pattern_id: str
    category: str


class InjectionScanResult(TypedDict):
    injection_suspected: bool
    scanner_version: str
    matched_patterns: list[PatternMatch]


_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "ignore_previous_instructions_en",
        "instruction_override",
        re.compile(
            r"\b(?:ignore|disregard|forget|override)\s+"
            r"(?:(?:all|any|the)\s+)?"
            r"(?:(?:previous|prior|above|system|developer)\s+)+"
            r"(?:instructions?|prompts?|messages?|rules?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "ignore_previous_instructions_zh",
        "instruction_override",
        re.compile(
            r"(?:忽略|无视|绕过|不要理会|不必理会|抛开)"
            r"(?:所有|任何|先前|之前|以上|上述|系统|开发者|原有|现有|的|\s){0,8}"
            r"(?:指令|提示词|提示|消息|规则)",
            re.IGNORECASE,
        ),
    ),
    (
        "sensitive_information_request_en",
        "sensitive_information_request",
        re.compile(
            r"\b(?:reveal|show|print|display|expose|leak|dump|return|provide|repeat)\b"
            r"[^\r\n.!?]{0,48}"
            r"\b(?:system\s+prompts?|developer\s+(?:messages?|instructions?)|"
            r"api[_ -]?keys?|access[_ -]?tokens?|tokens?|passwords?|secrets?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "sensitive_information_request_zh",
        "sensitive_information_request",
        re.compile(
            r"(?:显示|展示|泄露|透露|输出|打印|给出|提供|复述|告诉我)"
            r"[^\r\n，。！？]{0,24}"
            r"(?:系统提示词|系统提示|开发者(?:消息|指令)|API\s*密钥|"
            r"访问令牌|令牌|密码|秘密)",
            re.IGNORECASE,
        ),
    ),
    (
        "fake_tool_call_markup",
        "fake_tool_call",
        re.compile(r"<\s*/?\s*(?:tool_call|function_call)\s*>", re.IGNORECASE),
    ),
    (
        "fake_tool_call_json",
        "fake_tool_call",
        re.compile(r'''["']tool_call["']\s*:''', re.IGNORECASE),
    ),
    (
        "fake_function_call_json",
        "fake_tool_call",
        re.compile(r'''["']function_call["']\s*:''', re.IGNORECASE),
    ),
    (
        "role_hijack_en",
        "instruction_hijacking",
        re.compile(
            r"\b(?:you\s+are\s+now\s+(?:the\s+)?"
            r"(?:system|developer|assistant|administrator)|"
            r"from\s+now\s+on\s*,?\s+(?:only\s+)?"
            r"(?:follow|obey|execute)\s+(?:my|these|the\s+new)\s+instructions?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "role_hijack_zh",
        "instruction_hijacking",
        re.compile(
            r"(?:你现在是(?:系统|开发者|管理员|助手)|"
            r"从现在开始(?:只)?(?:听从|遵循|执行)(?:我的|这些|新的)?指令)",
            re.IGNORECASE,
        ),
    ),
)


def scan_injection(text: str) -> InjectionScanResult:
    """Return stable metadata for suspicious instruction-like patterns."""
    matches: list[PatternMatch] = []
    seen: set[str] = set()
    for pattern_id, category, pattern in _PATTERNS:
        if pattern_id not in seen and pattern.search(text) is not None:
            seen.add(pattern_id)
            matches.append({"pattern_id": pattern_id, "category": category})
    return {
        "injection_suspected": bool(matches),
        "scanner_version": SCANNER_VERSION,
        "matched_patterns": matches,
    }
