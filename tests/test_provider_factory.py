import pytest
from fufu_archive.config import Settings
from fufu_archive.provider_factory import build_semantic_provider

def settings(**kw):
    base=dict(db_path=__import__("pathlib").Path("db"),host="127.0.0.1",port=8000,api_token=None)
    base.update(kw); return Settings(**base)

def test_provider_is_disabled_by_default(): assert build_semantic_provider(settings()) is None

def test_deepseek_factory_is_explicit_and_fixed():
    p=build_semantic_provider(settings(semantic_provider="deepseek")); assert p.model=="deepseek-v4-flash"

def test_unknown_or_wrong_model_fails_closed():
    with pytest.raises(ValueError): build_semantic_provider(settings(semantic_provider="codex"))
    with pytest.raises(ValueError): build_semantic_provider(settings(semantic_provider="deepseek",semantic_model="deepseek-v4-pro"))
