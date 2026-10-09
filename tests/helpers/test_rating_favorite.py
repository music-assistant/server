"""
Tests for importing a rating embedded in file tags as a favorite.

The rating itself is not stored: it only selects the value of the existing
tri-state ``favorite`` flag, so these tests cover the normalization of each
format's scale and the threshold mapping that turns it into the flag.
"""

from __future__ import annotations

import pytest

from music_assistant.helpers.rating import (
    favorite_from_rating,
    percentage_to_rating,
    popm_to_rating,
)
from music_assistant.helpers.tags import AudioTags

LIKE_THRESHOLD = 8.0
DISLIKE_THRESHOLD = 2.0


class TestPopmToRating:
    """POPM has no scale everyone agrees on, so each tagger's values must map back."""

    @pytest.mark.parametrize(
        ("popm_rating", "expected"),
        [
            (0, None),  # 0 is "unrated/unknown" per the ID3 spec
            # Windows Media Player / Explorer
            (1, 2.0),
            (64, 4.0),
            (128, 6.0),
            (196, 8.0),
            (255, 10.0),
            # iTunes
            (20, 2.0),
            (40, 4.0),
            (60, 6.0),
            (80, 8.0),
            (100, 10.0),
            # MediaMonkey
            (23, 2.0),
            (118, 6.0),
            (186, 8.0),
            (252, 10.0),
        ],
    )
    def test_star_ratings_round_trip(self, popm_rating: int, expected: float | None) -> None:
        """A rating reads back as the stars that were set, whichever tagger wrote it."""
        assert popm_to_rating(popm_rating) == expected

    @pytest.mark.parametrize(("popm_rating", "expected"), [(70, 4.0), (90, 8.0), (250, 10.0)])
    def test_values_between_anchors_snap_to_the_nearest(
        self, popm_rating: int, expected: float
    ) -> None:
        """A byte no tagger writes deliberately still lands on a sane star rating."""
        assert popm_to_rating(popm_rating) == expected

    def test_the_explorer_read_bands_do_not_mangle_itunes_values(self) -> None:
        """ITunes writes 80 for four stars, which Microsoft's read bands call two stars."""
        assert popm_to_rating(80) == 8.0


class TestPercentageToRating:
    """Vorbis RATING and the MP4 rate atom are written on a 0-100 scale."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(0, None), (20, 2.0), (40, 4.0), (60, 6.0), (80, 8.0), (100, 10.0)],
    )
    def test_percentage_is_scaled_to_the_normalized_rating(
        self, value: float, expected: float | None
    ) -> None:
        """A 0-100 value lands on the matching normalized rating."""
        assert percentage_to_rating(value) == expected

    def test_out_of_range_values_are_clamped(self) -> None:
        """A value above the documented range is clamped rather than dropped."""
        assert percentage_to_rating(255) == 10.0


class TestFavoriteFromRating:
    """favorite is tri-state, so the mapping keeps a neutral band."""

    def test_rating_at_or_above_the_threshold_is_a_like(self) -> None:
        """A rating at the favorite threshold counts as a like."""
        assert (
            favorite_from_rating(
                8.0, favorite_threshold=LIKE_THRESHOLD, dislike_threshold=DISLIKE_THRESHOLD
            )
            is True
        )

    def test_rating_at_or_below_the_threshold_is_a_dislike(self) -> None:
        """A rating at the dislike threshold counts as a dislike."""
        assert (
            favorite_from_rating(
                2.0, favorite_threshold=LIKE_THRESHOLD, dislike_threshold=DISLIKE_THRESHOLD
            )
            is False
        )

    def test_rating_between_the_thresholds_stays_neutral(self) -> None:
        """The band between the two thresholds leaves the flag unset."""
        assert (
            favorite_from_rating(
                6.0, favorite_threshold=LIKE_THRESHOLD, dislike_threshold=DISLIKE_THRESHOLD
            )
            is None
        )

    def test_unrated_file_stays_neutral(self) -> None:
        """A file with no rating is never turned into a dislike."""
        assert (
            favorite_from_rating(
                None, favorite_threshold=LIKE_THRESHOLD, dislike_threshold=DISLIKE_THRESHOLD
            )
            is None
        )


def _audio_tags(**extra_tags: object) -> AudioTags:
    """Build AudioTags carrying the given raw tag values."""
    return AudioTags(
        raw={},
        sample_rate=44100,
        channels=2,
        bits_per_sample=16,
        format="mp3",
        bit_rate=320000,
        duration=1.0,
        tags={"title": "A title", **extra_tags},
        has_cover_image=False,
        filename="a-title.mp3",
    )


class TestAudioTagsRating:
    """The tag dict is what the provider sees, whichever format the file is in."""

    @pytest.mark.parametrize(
        ("tag_key", "tag_value", "expected"),
        [
            ("popm", 255, 10.0),  # ID3
            ("popm", "196", 8.0),  # a string from any tagger that stores text
            ("rating", "80", 8.0),  # Vorbis / MP4 freeform
            ("rating", 80, 8.0),
            ("rating", "0", None),  # explicitly unrated
            ("popm", 0, None),
        ],
    )
    def test_rating_is_normalized(
        self, tag_key: str, tag_value: object, expected: float | None
    ) -> None:
        """Each format's raw value is normalized to the shared scale."""
        assert _audio_tags(**{tag_key: tag_value}).rating == expected

    def test_unparseable_values_are_ignored(self) -> None:
        """A malformed rating must not break the scan or invent a favorite."""
        assert _audio_tags(popm="not-a-number").rating is None
        assert _audio_tags(rating="not-a-number").rating is None

    def test_file_without_a_rating_tag_returns_none(self) -> None:
        """A file with no rating tag at all is simply unrated."""
        assert _audio_tags().rating is None
