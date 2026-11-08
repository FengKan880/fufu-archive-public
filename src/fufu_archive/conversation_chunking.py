"""Validated, turn-aware inputs for semantic session summarization."""
from __future__ import annotations
from collections.abc import Mapping, Sequence
from typing import Any

TEXT_PART_TYPES={"input_text","output_text","text"}

def content_text(value: Any) -> str:
    if isinstance(value,str): return value
    if isinstance(value,list):
        parts=[]
        for part in value:
            if isinstance(part,str): parts.append(part)
            elif isinstance(part,Mapping) and part.get("type") in TEXT_PART_TYPES and isinstance(part.get("text"),str): parts.append(part["text"])
        return "\n".join(parts)
    return ""

def request_snapshot_messages(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Extract text from a request dump without inventing archive message IDs.

    Returned entries carry ``snapshot_position`` only. They are diagnostic input,
    not candidate-ready archive records; callers must resolve actual source IDs.
    """
    raw=body.get("input", body.get("messages", []))
    if not isinstance(raw,list): return []
    result=[]
    for position,message in enumerate(raw):
        if not isinstance(message,Mapping) or message.get("role") not in {"user","assistant"}: continue
        text=content_text(message.get("content"))
        if text.strip(): result.append({"snapshot_position":position,"role":message["role"],"content":text})
    return result

def normalize_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep only representable user/assistant messages with stable unique source IDs."""
    normalized=[]; seen=set()
    for message in messages:
        if not isinstance(message,Mapping) or message.get("role") not in {"user","assistant"}: continue
        source_id=message.get("source_message_id")
        if type(source_id) is not int or source_id < 1: raise ValueError("message source_message_id must be a positive integer")
        if source_id in seen: raise ValueError("duplicate source_message_id")
        text=content_text(message.get("content"))
        if not text.strip(): continue
        seen.add(source_id); normalized.append({"source_message_id":source_id,"role":message["role"],"content":text})
    return normalized

def _turns(messages: Sequence[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    turns=[]; current=[]; has_assistant=False
    for message in messages:
        # A new user starts a new turn only after an assistant has answered.
        if message["role"]=="user" and current and has_assistant:
            turns.append(current); current=[]; has_assistant=False
        current.append(message)
        has_assistant = has_assistant or message["role"]=="assistant"
    if current: turns.append(current)
    return turns

def chunk_conversation(messages: Sequence[Mapping[str, Any]], *, max_chars: int=30000) -> list[dict[str, Any]]:
    """Soft-limit chunks at complete turn boundaries; chunks never overlap."""
    if type(max_chars) is not int or max_chars < 1: raise ValueError("max_chars must be positive")
    turns=_turns(normalize_messages(messages))
    chunks=[]; start=0
    while start<len(turns):
        end=start; size=0
        while end<len(turns):
            turn_size=sum(len(m["content"])+32 for m in turns[end])
            if end>start and size+turn_size>max_chars: break
            size+=turn_size; end+=1
        flat=[m for turn in turns[start:end] for m in turn]
        chunks.append({"chunk_index":len(chunks),"messages":flat,"source_message_ids":[m["source_message_id"] for m in flat],"transcript":"\n\n".join(f"[{m['source_message_id']}] {m['role']}: {m['content']}" for m in flat),"start_turn":start,"end_turn":end})
        start=end
    return chunks

__all__=["content_text","request_snapshot_messages","normalize_messages","chunk_conversation"]
