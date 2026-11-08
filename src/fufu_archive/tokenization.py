"""Safe, dependency-optional tokenisation for SQLite FTS5."""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_WORD = re.compile(r"[A-Za-z0-9_]+")
_DICTIONARY_DIR = Path(__file__).resolve().parents[2] / "resources" / "dict"
DEFAULT_JIEBA_DICTIONARY_PATHS = tuple(
    _DICTIONARY_DIR / filename
    for filename in (
        "hermes_terms.txt",
        "minecraft_terms.txt",
        "plant_terms.txt",
        "vrc_terms.txt",
    )
)

# Query-side stopwords: interrogatives, particles, and delexicalised words
# that almost never appear inside the indexed document text. Keeping them in
# an AND query makes real conversational queries (which carry these words)
# fail to match. Indexing keeps the full text; only the query drops them.
_QUERY_STOPWORDS = frozenset(
    {
        "的", "了", "是", "在", "和", "与", "及", "或", "都", "也", "很", "要", "会",
        "能", "可以", "怎么", "为什么", "什么", "怎么办", "怎样", "如何", "咋", "吗",
        "呢", "吧", "呀", "啊", "哦", "嘛", "啦", "这", "那", "有", "没", "不", "别",
        "请", "帮", "个", "只", "才", "又", "再", "还", "而", "但", "且", "被", "把",
        "让", "给", "对", "于", "从", "到", "往", "向", "出来", "下去", "起来", "一下",
        "一个", "自己", "这种", "那些", "这些", "里", "上", "下", "中", "内", "外", "就",
    }
)


def _fallback_cjk_terms(run: str) -> list[str]:
    """Return single characters and bigrams so CJK text remains searchable."""
    terms = list(run)
    terms.extend(run[index : index + 2] for index in range(len(run) - 1))
    return terms


def _normalise_dictionary_paths(
    dictionary_paths: Iterable[str | Path] | None,
) -> tuple[Path, ...]:
    selected_paths = (
        DEFAULT_JIEBA_DICTIONARY_PATHS
        if dictionary_paths is None
        else dictionary_paths
    )
    return tuple(
        dict.fromkeys(Path(path).expanduser().resolve() for path in selected_paths)
    )


@lru_cache(maxsize=None)
def _jieba_tokenizer(dictionary_paths: tuple[Path, ...]):
    try:
        import jieba  # type: ignore[import-not-found]
    except ImportError:
        return None

    tokenizer = jieba.Tokenizer()
    for path in dictionary_paths:
        if path.is_file():
            tokenizer.load_userdict(str(path))
    return tokenizer


def clear_jieba_tokenizer_cache() -> None:
    """Discard cached jieba tokenizers, primarily for tests or dictionary edits."""
    _jieba_tokenizer.cache_clear()


def _jieba_terms(
    run: str, dictionary_paths: Iterable[str | Path] | None = None
) -> list[str] | None:
    tokenizer = _jieba_tokenizer(_normalise_dictionary_paths(dictionary_paths))
    if tokenizer is None:
        return None
    return [term.strip() for term in tokenizer.lcut(run, HMM=False) if term.strip()]


def tokenize(
    text: str, *, dictionary_paths: Iterable[str | Path] | None = None
) -> list[str]:
    """Produce indexable terms without passing user syntax through to FTS5.

    The fallback generates CJK unigrams and bigrams. This has modest recall-oriented
    P0 behaviour and avoids requiring a language model or external native extension.
    """
    terms: list[str] = []
    cursor = 0
    for match in _CJK_RUN.finditer(text):
        for word in _WORD.findall(text[cursor : match.start()]):
            terms.append(word.lower())
        run = match.group(0)
        segmented = _jieba_terms(run, dictionary_paths)
        terms.extend(segmented if segmented is not None else _fallback_cjk_terms(run))
        cursor = match.end()
    terms.extend(word.lower() for word in _WORD.findall(text[cursor:]))
    return list(dict.fromkeys(term for term in terms if term))


def fts_document_text(text: str) -> str:
    """Return the only text stored in the FTS index."""
    return " ".join(tokenize(text))


def _query_terms(text: str) -> list[str]:
    """Produce conservative query terms for a possibly partial CJK phrase.

    Indexing fallback emits bigrams for recall, but requiring every query bigram
    would make a phrase such as ``中文检索`` fail against ``中文知识检索``. In
    fallback mode query only the individual CJK characters; jieba terms remain
    useful when the optional dependency is present.
    """
    terms: list[str] = []
    cursor = 0
    for match in _CJK_RUN.finditer(text):
        terms.extend(word.lower() for word in _WORD.findall(text[cursor : match.start()]))
        run = match.group(0)
        segmented = _jieba_terms(run)
        terms.extend(segmented if segmented is not None else list(run))
        cursor = match.end()
    terms.extend(word.lower() for word in _WORD.findall(text[cursor:]))
    return list(
        dict.fromkeys(term for term in terms if term and term not in _QUERY_STOPWORDS)
    )


def safe_match_query(query: str) -> str | None:
    """Build a literal-only FTS query from trusted token values.

    Stopwords are dropped, then remaining tokens are OR-ed so a conversational
    query still recalls documents that share only some of its content words.
    BM25 ranking (not AND filtering) decides order. Each token is
    double-quoted and quotes are escaped; input never becomes FTS operators
    such as OR, NEAR, column filters, or prefix-search syntax.
    """
    terms = _query_terms(query)
    if not terms:
        return None
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
