"""Explicit semantic-provider construction; disabled unless configured."""
from __future__ import annotations
from .config import Settings

def build_semantic_provider(settings: Settings):
    name=(settings.semantic_provider or "").strip().lower()
    if not name: return None
    if name != "deepseek": raise ValueError(f"unsupported semantic provider: {name}")
    if settings.semantic_model != "deepseek-v4-flash": raise ValueError("DeepSeek semantic model must be deepseek-v4-flash")
    from .deepseek_provider import DeepSeekV4FlashProvider
    return DeepSeekV4FlashProvider(model=settings.semantic_model)

__all__=["build_semantic_provider"]
