from __future__ import annotations

import pytest

from fufu_archive.chunking import chunk_text


def test_short_text_is_one_unchanged_chunk() -> None:
    text = "短文本 keeps its original form."

    assert chunk_text(text, target_size=100, overlap=10) == [text]


def test_paragraph_boundary_is_preferred_over_sentence_boundary() -> None:
    text = "第一句结束。第一段继续。\n\n第二段开始，也有足够内容。第二段结束。"

    chunks = chunk_text(text, target_size=24, overlap=4)

    assert len(chunks) > 1
    assert chunks[0].endswith("\n\n")
    assert "第二段开始" in "".join(chunks)


def test_sentence_boundary_is_used_for_mixed_chinese_english_prose() -> None:
    text = "这是第一句。This is sentence two! 最后一句包含更多内容用于切分。"

    chunks = chunk_text(text, target_size=28, overlap=3)

    assert len(chunks) > 1
    assert chunks[0][-1] in "。！？!?；;."


def test_adjacent_chunks_preserve_requested_character_overlap() -> None:
    text = "甲乙丙丁戊己庚辛壬癸。abcdefghij.甲乙丙丁戊己庚辛壬癸。"
    overlap = 5

    chunks = chunk_text(text, target_size=18, overlap=overlap)

    assert len(chunks) > 1
    for previous, current in zip(chunks, chunks[1:]):
        assert previous[-overlap:] == current[:overlap]


def test_very_long_unpunctuated_text_makes_safe_ordered_chunks() -> None:
    text = "abcdefghijklmnopqrstuvwxyz" * 20

    chunks = chunk_text(text, target_size=50, overlap=7)

    assert len(chunks) > 1
    assert all(chunk.strip() for chunk in chunks)
    assert all(len(chunk) <= 50 for chunk in chunks)
    assert chunks[0] == text[:50]
    assert chunks[1] == text[43:93]
    assert chunks[-1].endswith(text[-7:])


def test_boundary_just_past_target_does_not_make_an_oversized_chunk() -> None:
    target_size = 16
    text = "a" * target_size + ".tail"

    chunks = chunk_text(text, target_size=target_size)

    assert chunks[0] == "a" * target_size
    assert all(len(chunk) <= target_size for chunk in chunks)


def test_overlap_near_target_is_capped_and_does_not_repeat_early_boundary() -> None:
    text = "abcde。fghijklmnopqrstuvwxyz"

    chunks = chunk_text(text, target_size=10, overlap=9)

    assert chunks == [
        "abcde。fghi",
        "。fghijklmn",
        "jklmnopqrs",
        "opqrstuvwx",
        "tuvwxyz",
    ]
    assert all(len(chunk) <= 10 for chunk in chunks)
    assert all(previous[-5:] == current[:5] for previous, current in zip(chunks, chunks[1:]))


def test_whitespace_only_input_returns_no_chunks() -> None:
    assert chunk_text(" \n\t ", target_size=10, overlap=2) == []


@pytest.mark.parametrize(
    ("target_size", "overlap", "message"),
    [
        (0, 0, "target_size must be a positive integer"),
        (True, 0, "target_size must be a positive integer"),
        (10, -1, "overlap must be a non-negative integer"),
        (10, True, "overlap must be a non-negative integer"),
        (10, 10, "overlap must be smaller than target_size"),
        (10, 11, "overlap must be smaller than target_size"),
    ],
)
def test_invalid_sizes_raise_clear_value_error(
    target_size: int, overlap: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        chunk_text("valid text", target_size=target_size, overlap=overlap)
