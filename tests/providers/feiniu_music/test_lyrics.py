"""Synthetic lyrics only: selection, time alignment and malformed responses."""

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.feiniu_music.lyrics import parse_lyrics


def test_preferred_lyric_and_plain_text_fallback() -> None:
    """Honor native selection instead of accidentally merging different lyric versions."""
    result = parse_lyrics(
        {
            "list": [
                {"guid": "timed", "content": "[00:01.00]Synthetic line"},
                {"guid": "plain", "content": "Selected synthetic text"},
            ],
            "preferred": "plain",
        }
    )
    assert result == ("Selected synthetic text", None)
    assert parse_lyrics({"list": [{"content": " "}, {"content": "Fallback"}]}) == ("Fallback", None)
    assert parse_lyrics({"list": []}) == (None, None)


@pytest.mark.parametrize(("offset", "expected"), [(1500, "[00:00.500]"), (-250, "[00:02.250]")])
def test_native_offset_uses_milliseconds_and_correct_direction(offset: int, expected: str) -> None:
    """Positive native alignment advances text; negative alignment delays it."""
    plain, synced = parse_lyrics(
        {"list": [{"content": "[00:02.000]Synthetic line", "offset": offset}]}
    )
    assert plain == "Synthetic line"
    assert synced == expected + "Synthetic line"


@pytest.mark.parametrize(
    ("offset", "expected"), [(0, "[00:02.500]"), (1000, "[00:01.500]"), (None, "[00:02.000]")]
)
def test_embedded_offset_and_native_alignment_do_not_double_apply(
    offset: int | None, expected: str
) -> None:
    """LRC tag timing and the player's chosen alignment are separate adjustments."""
    plain, synced = parse_lyrics(
        {"list": [{"content": "[offset:500]\n[00:02.00]Synthetic", "offset": offset}]}
    )
    assert plain == "Synthetic"
    assert synced == expected + "Synthetic"


def test_repeated_timestamps_and_negative_times() -> None:
    """Expand repeated lines, strip metadata/word tags and drop negative timestamps."""
    plain, synced = parse_lyrics(
        {"list": [{"content": "[ar:Example]\n[00:00.5][00:03.00]<00:03.00>Test", "offset": 1000}]}
    )
    assert plain == "Test\nTest"
    assert synced == "[00:02.000]Test"


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"list": None},
        {"list": ["wrong"]},
        {"list": [{"content": "[00:01.00]Test", "offset": "wrong"}]},
    ],
)
def test_malformed_lyric_contract_raises(data: dict[str, object]) -> None:
    """Schema errors are distinct from an ordinary track with no lyrics."""
    with pytest.raises(InvalidDataError):
        parse_lyrics(data)
