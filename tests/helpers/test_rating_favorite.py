"""
Tests for importing a rating embedded in file tags as a favorite.

The rating itself is not stored: it only selects the value of the existing
tri-state ``favorite`` flag, so these tests cover the normalization of each
format's scale and the threshold mapping that turns it into the flag.
"""

from __future__ import annotations

import pytest

from music_assistant.helpers.rating import (
    POPM_SCALE_ITUNES,
    POPM_SCALE_WINDOWS,
    TAG_SCALE_PERCENT,
    TAG_SCALE_STARS,
    favorite_from_rating,
    popm_to_rating,
    tag_value_to_rating,
)
from music_assistant.helpers.tags import AudioTags

LIKE_THRESHOLD = 8.0
DISLIKE_THRESHOLD = 2.0


class TestPopmToRating:
    """POPM has no scale everyone agrees on, so the scale is chosen per library."""

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
            # MediaMonkey writes the same whole star values into the same bands
            (23, 2.0),
            (118, 6.0),
            (186, 8.0),
            (252, 10.0),
        ],
    )
    def test_windows_scale_reads_its_own_values(
        self, popm_rating: int, expected: float | None
    ) -> None:
        """The default scale reads back the stars Windows and MediaMonkey wrote."""
        assert popm_to_rating(popm_rating, POPM_SCALE_WINDOWS) == expected

    @pytest.mark.parametrize(
        ("popm_rating", "expected"),
        [(0, None), (20, 2.0), (40, 4.0), (60, 6.0), (80, 8.0), (100, 10.0)],
    )
    def test_itunes_scale_reads_its_own_values(
        self, popm_rating: int, expected: float | None
    ) -> None:
        """ITunes squeezes five stars into 20-100, so it needs its own bands."""
        assert popm_to_rating(popm_rating, POPM_SCALE_ITUNES) == expected

    def test_the_windows_scale_would_misread_itunes_values(self) -> None:
        """This is why the scale is a setting rather than a guess per file."""
        assert popm_to_rating(80, POPM_SCALE_WINDOWS) == 4.0
        assert popm_to_rating(80, POPM_SCALE_ITUNES) == 8.0

    @pytest.mark.parametrize("scale", [POPM_SCALE_WINDOWS, POPM_SCALE_ITUNES])
    def test_every_scale_is_monotonic(self, scale: str) -> None:
        """A higher byte must never produce a lower rating, on any scale."""
        previous = 0.0
        for byte in range(1, 256):
            rating = popm_to_rating(byte, scale)
            assert rating is not None
            assert rating >= previous, f"byte {byte} went backwards on {scale}"
            previous = rating

    def test_an_unknown_scale_falls_back_to_the_default(self) -> None:
        """A stale config value must not raise during a scan."""
        assert popm_to_rating(64, "nonsense") == 4.0


class TestTagValueToRating:
    """Vorbis RATING and the MP4 RATING tag carry their own scale."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(0, None), (20, 2.0), (40, 4.0), (60, 6.0), (80, 8.0), (100, 10.0)],
    )
    def test_percentage_scale(self, value: float, expected: float | None) -> None:
        """A 0-100 value lands on the matching normalized rating."""
        assert tag_value_to_rating(value, TAG_SCALE_PERCENT) == expected

    @pytest.mark.parametrize(
        ("value", "expected"), [(0, None), (1, 2.0), (2, 4.0), (3, 6.0), (4, 8.0), (5, 10.0)]
    )
    def test_star_scale(self, value: float, expected: float | None) -> None:
        """A 1-5 value must not be read as a percentage, which would call it a dislike."""
        assert tag_value_to_rating(value, TAG_SCALE_STARS) == expected

    @pytest.mark.parametrize("scale", [TAG_SCALE_PERCENT, TAG_SCALE_STARS])
    def test_every_scale_is_monotonic(self, scale: str) -> None:
        """A higher value must never produce a lower rating, on any scale."""
        previous = 0.0
        for value in range(1, 101):
            rating = tag_value_to_rating(value, scale)
            assert rating is not None
            assert rating >= previous, f"value {value} went backwards on {scale}"
            previous = rating

    def test_out_of_range_values_are_clamped(self) -> None:
        """A value above the documented range is clamped rather than dropped."""
        assert tag_value_to_rating(255, TAG_SCALE_PERCENT) == 10.0
        assert tag_value_to_rating(9, TAG_SCALE_STARS) == 10.0


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


class TestAudioTagsRawRating:
    """The tag dict is what the provider sees, whichever format the file is in."""

    @pytest.mark.parametrize(
        ("tag_key", "tag_value", "expected"),
        [
            ("popm", 255, 255),  # ID3, as an int
            ("popm", "196", 196),  # and as text, which some taggers produce
            ("rating", "80", 80.0),  # Vorbis / MP4 freeform
            ("rating", 80, 80.0),
            ("rating", "80.0", 80.0),
        ],
    )
    def test_raw_values_are_returned_unnormalized(
        self, tag_key: str, tag_value: object, expected: float
    ) -> None:
        """The provider picks the scale, so the raw value comes through as-is."""
        tags = _audio_tags(**{tag_key: tag_value})
        if tag_key == "popm":
            assert tags.popm_rating == expected
        else:
            assert tags.rating_tag == expected

    def test_unparseable_values_are_ignored(self) -> None:
        """A malformed rating must not break the scan or invent a favorite."""
        assert _audio_tags(popm="not-a-number").popm_rating is None
        assert _audio_tags(rating="not-a-number").rating_tag is None

    def test_file_without_a_rating_tag_returns_none(self) -> None:
        """A file with no rating tag at all is simply unrated."""
        tags = _audio_tags()
        assert tags.popm_rating is None
        assert tags.rating_tag is None
