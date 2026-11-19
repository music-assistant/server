"""Tests for the Music Quiz title-reveal letter hints."""

from __future__ import annotations

import random

import pytest

from music_assistant.providers.music_quiz.hints import (
    MAX_HINT_TITLE_LENGTH,
    build_hint_fractions,
    build_mask,
    build_reveal_order,
    clean_hint_title,
    hidden_positions,
    hint_count,
    is_hint_title_usable,
    strip_emoji,
)
from music_assistant.providers.music_quiz.models import TitleRevealHintSpeed


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Bohemian Rhapsody - Remastered 2011", "Bohemian Rhapsody"),
        ("Hey Jude (Remastered 2015)", "Hey Jude"),
        ("Uptown Funk (feat. Bruno Mars)", "Uptown Funk"),
        ("Smells Like Teen Spirit [Live at Reading]", "Smells Like Teen Spirit"),
        ("(Don't Fear) The Reaper", "(Don't Fear) The Reaper"),
        ("Song 2 (2012 Remaster)", "Song 2"),
        ("Fire 🔥 (Remastered 2011)", "Fire"),
        ('Danger Zone - From "Top Gun" Original Soundtrack', "Danger Zone"),
        ("Shallow - From A Star Is Born", "Shallow"),
        ('My Heart Will Go On - Love Theme from "Titanic"', "My Heart Will Go On"),
        ("Song [From The Motion Picture]", "Song"),
        ("Song - Mono [From The Motion Picture]", "Song"),
        ("Song - Recorded at Spotify Studios NYC", "Song"),
        ("Song - Demo", "Song"),
        ("Rock - N - Roll", "Rock - N - Roll"),
        ("From Me to You", "From Me to You"),
        ("Theme from New York, New York", "Theme from New York, New York"),
        ("Live and Let Die", "Live and Let Die"),
        ("Stereo Hearts", "Stereo Hearts"),
    ],
)
def test_clean_hint_title_strips_version_suffixes(title: str, expected: str) -> None:
    """Drop the version details a player cannot be expected to guess."""
    assert clean_hint_title(title) == expected


def test_mask_always_shows_special_characters() -> None:
    """Mask letters and digits while spaces and punctuation stay visible."""
    assert build_mask("Don't Stop - 99%", set()) == [
        None,
        None,
        None,
        "'",
        None,
        " ",
        None,
        None,
        None,
        None,
        " ",
        "-",
        " ",
        None,
        None,
        "%",
    ]


def test_mask_masks_umlauts_and_non_latin_letters() -> None:
    """Treat accented and non-Latin letters as letters to guess."""
    assert build_mask("Über 夜", set()) == [None, None, None, None, " ", None]
    assert build_mask("Über 夜", {0, 5}) == ["Ü", None, None, None, " ", "夜"]


def test_reveal_order_covers_every_letter_and_spreads_across_words() -> None:
    """Reveal each letter once, giving every word a letter before any word gets two."""
    title = "Smells Like Teen Spirit"
    order = build_reveal_order(title, random.Random(4))  # noqa: S311 - seeded for determinism

    assert sorted(order) == hidden_positions(title)
    first_word_of = {
        index: word_index
        for word_index, word in enumerate(title.split())
        for index in range(title.index(word), title.index(word) + len(word))
    }
    assert {first_word_of[index] for index in order[:4]} == {0, 1, 2, 3}


@pytest.mark.parametrize(
    ("letter_count", "speed", "expected"),
    [
        (2, TitleRevealHintSpeed.FAST, 1),
        (10, TitleRevealHintSpeed.SLOW, 4),
        (10, TitleRevealHintSpeed.NORMAL, 6),
        (10, TitleRevealHintSpeed.FAST, 7),
        (4, TitleRevealHintSpeed.FAST, 3),
    ],
)
def test_hint_count_never_reveals_the_whole_title(
    letter_count: int, speed: TitleRevealHintSpeed, expected: int
) -> None:
    """Scale the number of hints with the speed but always leave a letter hidden."""
    assert hint_count(letter_count, speed) == expected


def test_hint_fractions_spread_evenly_after_a_grace_period() -> None:
    """Hold back the first hint and reveal the last one before the deadline."""
    assert build_hint_fractions(0) == []
    assert build_hint_fractions(1) == [0.2]
    assert build_hint_fractions(3) == [0.2, 0.525, 0.85]


@pytest.mark.parametrize(
    ("title", "usable"),
    [
        ("Hey Jude", True),
        ("Hi", True),
        ("X", False),
        ("!!!", False),
        ("a" * (MAX_HINT_TITLE_LENGTH + 1), False),
    ],
)
def test_is_hint_title_usable(title: str, usable: bool) -> None:
    """Skip titles that are too short or too long for a fair round."""
    assert is_hint_title_usable(title) is usable


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Fire 🔥", "Fire"),
        ("❤️ Love Song", "Love Song"),
        ("Party 🎉🎉 Time", "Party Time"),
        ("Hey 👋🏽 You", "Hey You"),
        ("👨\u200d👩\u200d👧 Family", "Family"),
        ("🇩🇪 Deutschland", "Deutschland"),
        ("1️⃣ Number", "Number"),
        ("Song - 🔥", "Song"),
        ("Star ★ Light", "Star Light"),
        ("99 Luftballons", "99 Luftballons"),
        ("Don't Stop - Live", "Don't Stop - Live"),
    ],
)
def test_strip_emoji_removes_pictographs_and_their_glue(text: str, expected: str) -> None:
    """Remove emoji, including joined and modified sequences, without touching real text."""
    assert strip_emoji(text) == expected


def test_emoji_only_title_is_not_usable() -> None:
    """Skip a track whose title is nothing but emoji."""
    assert is_hint_title_usable(clean_hint_title("🔥🔥🔥")) is False
