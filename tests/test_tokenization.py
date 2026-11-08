from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from fufu_archive import tokenization


DICTIONARY_DIR = Path(__file__).parents[1] / "resources" / "dict"
EXPECTED_DICTIONARIES = {
    "hermes_terms.txt": "工具调用",
    "minecraft_terms.txt": "红石电路",
    "plant_terms.txt": "植物养护",
    "vrc_terms.txt": "全身追踪",
}


@pytest.fixture(autouse=True)
def isolated_jieba_cache():
    tokenization.clear_jieba_tokenizer_cache()
    yield
    tokenization.clear_jieba_tokenizer_cache()


def test_repository_dictionaries_use_jieba_userdict_format() -> None:
    assert {path.name for path in DICTIONARY_DIR.glob("*.txt")} == set(
        EXPECTED_DICTIONARIES
    )

    for filename, representative_term in EXPECTED_DICTIONARIES.items():
        lines = (DICTIONARY_DIR / filename).read_text(encoding="utf-8").splitlines()
        assert lines
        assert representative_term in {line.split()[0] for line in lines}
        for line in lines:
            columns = line.split()
            assert len(columns) == 3
            assert columns[0].strip() == columns[0]
            assert columns[1].isdigit() and int(columns[1]) > 0
            assert columns[2].isalpha() and columns[2].isascii()


def test_custom_dictionary_changes_jieba_segmentation(tmp_path: Path) -> None:
    pytest.importorskip("jieba")
    dictionary = tmp_path / "custom_terms.txt"
    dictionary.write_text("星露方块链 1000000 nz\n", encoding="utf-8")

    terms = tokenization.tokenize(
        "记录星露方块链状态", dictionary_paths=(dictionary,)
    )

    assert "星露方块链" in terms


def test_missing_dictionary_and_jieba_use_cjk_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "jieba", None)

    terms = tokenization.tokenize(
        "甲乙", dictionary_paths=(tmp_path / "missing.txt",)
    )

    assert terms == ["甲", "乙", "甲乙"]


def test_dictionary_is_loaded_only_once_for_repeated_tokenization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dictionary = tmp_path / "terms.txt"
    dictionary.write_text("星露方块链 1000000 nz\n", encoding="utf-8")
    loaded_paths: list[Path] = []

    class FakeTokenizer:
        def load_userdict(self, path: str) -> None:
            loaded_paths.append(Path(path))

        def lcut(self, run: str, HMM: bool = False) -> list[str]:
            assert HMM is False
            return [run]

    monkeypatch.setitem(sys.modules, "jieba", SimpleNamespace(Tokenizer=FakeTokenizer))

    tokenization.tokenize("第一次调用", dictionary_paths=(dictionary,))
    tokenization.tokenize("第二次调用", dictionary_paths=(dictionary,))

    assert loaded_paths == [dictionary.resolve()]


def test_query_stopwords_and_literal_or_behavior_remain_unchanged() -> None:
    assert tokenization.safe_match_query("请 alpha beta") == '"alpha" OR "beta"'
