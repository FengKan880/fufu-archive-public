from __future__ import annotations

from fufu_archive.query_rewrite import rewrite_query


def test_chinese_conversational_fillers_are_removed() -> None:
    query = "请问，能不能帮我查一下 Fufu Archive 的索引状态呢？"

    variants = rewrite_query(query)

    assert variants == [
        query,
        "请问 能不能帮我查一下 Fufu Archive 的索引状态呢",
        "Fufu Archive 的索引状态",
    ]


def test_english_whitespace_and_punctuation_are_normalized() -> None:
    query = "  Could you please, find   Fufu-Archive docs?!  "

    variants = rewrite_query(query)

    assert variants == [
        query,
        "Could you please find Fufu Archive docs",
        "find Fufu Archive docs",
    ]


def test_original_query_is_always_first_and_variant_count_is_bounded() -> None:
    query = "Please tell me: where is the Fufu archive documentation?"

    variants = rewrite_query(query)

    assert variants[0] == query
    assert len(variants) <= 3


def test_empty_and_duplicate_derived_variants_are_omitted() -> None:
    assert rewrite_query("plain terms") == ["plain terms"]
    assert rewrite_query("Please??") == ["Please??", "Please"]

