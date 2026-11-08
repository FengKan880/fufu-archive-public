"""Fail-closed chunk refinement: all chunks must succeed before synthesis."""
from __future__ import annotations
from collections.abc import Mapping, Sequence
from typing import Any
from .conversation_chunking import chunk_conversation
from .semantic_provider import SemanticExtractionRequest

def _reason(error: Exception) -> str:
    return getattr(error,"reason_code",error.__class__.__name__)
def _items(value: Any) -> list[dict[str,Any]]:
    if not isinstance(value,Mapping) or not isinstance(value.get("items"),list): raise ValueError("provider result has no items list")
    return [dict(item) for item in value["items"] if isinstance(item,Mapping)]
def summarize_session_in_chunks(messages: Sequence[dict[str, Any]], provider: Any, *, profile: str|None=None, session_id: str="session", content_sha256: str="", max_chars: int=30000, max_calls: int=64, max_chunk_attempts: int=2) -> dict[str, Any]:
    chunks=chunk_conversation(messages,max_chars=max_chars)
    if not chunks: return {"status":"pending","items":[],"chunks":[],"synthesis":"not-needed"}
    if type(max_chunk_attempts) is not int or max_chunk_attempts < 1: raise ValueError("max_chunk_attempts must be positive")
    if len(chunks)*max_chunk_attempts+1>max_calls: return {"status":"blocked","items":[],"chunks":[],"synthesis":"blocked","reason":"call_budget_exceeded"}
    summaries=[]; diagnostics=[]
    for chunk in chunks:
        req=SemanticExtractionRequest(profile,session_id,content_sha256,tuple(chunk["source_message_ids"]),chunk["transcript"])
        error=None
        for attempt in range(1,max_chunk_attempts+1):
            try:
                summaries.append({"chunk_index":chunk["chunk_index"],"source_message_ids":chunk["source_message_ids"],"items":_items(provider.extract(req))})
                diagnostics.append({"chunk_index":chunk["chunk_index"],"status":"ok","attempts":attempt,"source_message_ids":chunk["source_message_ids"]}); error=None; break
            except Exception as caught: error=caught
        if error is not None: diagnostics.append({"chunk_index":chunk["chunk_index"],"status":"failed","attempts":max_chunk_attempts,"reason":_reason(error)})
    # Never synthesize or expose candidate items from an incomplete source session.
    if any(d["status"]=="failed" for d in diagnostics): return {"status":"blocked","items":[],"chunks":diagnostics,"synthesis":"blocked","reason":"chunk_refinement_blocked"}
    if len(summaries)==1:
        # Normalise evidence semantics even for injected providers that do not
        # implement the concrete DeepSeek provider's local provenance fields.
        single_items = summaries[0]["items"]
        source_ids = sorted(set(summaries[0]["source_message_ids"]))
        for item in single_items:
            item["evidence"] = source_ids
            item["evidence_scope"] = "input_range"
        return {"status":"pending","items":single_items,"chunks":diagnostics,"synthesis":"not-needed"}
    lines=[]; all_ids=[]
    for summary in summaries:
        all_ids.extend(summary["source_message_ids"])
        for item in summary["items"]: lines.append(f"chunk {summary['chunk_index']}: {item.get('title','')}: {item.get('content','')}")
    if not lines: return {"status":"pending","items":[],"chunks":diagnostics,"synthesis":"empty"}
    try:
        request=SemanticExtractionRequest(profile,session_id,content_sha256,tuple(all_ids),"\n".join(lines))
        call=getattr(provider,"synthesize",provider.extract)
        final=_items(call(request))
    except Exception as error:
        return {"status":"blocked","items":[],"chunks":diagnostics,"synthesis":"blocked","reason":"synthesis_"+_reason(error)}
    # Provider evidence represents the complete input range, never per-claim proof.
    for item in final:
        item["evidence"]=sorted(set(all_ids)); item["evidence_scope"]="session_input_range"
    return {"status":"pending","items":final,"chunks":diagnostics,"synthesis":"completed"}
__all__=["summarize_session_in_chunks"]
