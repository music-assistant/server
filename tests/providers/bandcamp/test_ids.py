"""Tests for the Bandcamp composite-artist-ID utilities."""

from __future__ import annotations

import pytest

from music_assistant.providers.bandcamp._ids import (
    make_artist_id,
    parse_artist_id,
    slugify_performer,
)


class TestSlugifyPerformer:
    """Slug generation rules for performer names."""

    def test_lowercases_alphanumerics(self) -> None:
        """Bare alphabetic names round-trip lowercased with no separators."""
        assert slugify_performer("Mortaja") == "mortaja"

    def test_collapses_spaces_to_single_hyphen(self) -> None:
        """Spaces between words collapse to one hyphen each."""
        assert slugify_performer("The Green Arrows") == "the-green-arrows"

    def test_collapses_repeated_punctuation(self) -> None:
        """Runs of any non-alnum chars collapse into a single separator."""
        assert slugify_performer("Apollo Brown / OC") == "apollo-brown-oc"

    def test_normalizes_ampersand_to_and(self) -> None:
        """Ampersands use the same identity spelling as the word ``and``."""
        assert slugify_performer("Apollo Brown & OC") == "apollo-brown-and-oc"
        assert slugify_performer("Apollo Brown and OC") == "apollo-brown-and-oc"

    def test_strips_leading_and_trailing_separators(self) -> None:
        """Leading/trailing separators are removed."""
        assert slugify_performer("  --foo--  ") == "foo"

    def test_unicode_letters_are_dropped(self) -> None:
        """Non-ASCII letters are intentionally dropped (lossy slug)."""
        # Slug is intentionally lossy: non-ASCII letters do not round-trip.
        # This is acceptable because slugs are scoped per band_id, so the
        # only failure mode is rare same-band performer-name collisions.
        assert slugify_performer("Adiós Mundo Cruel") == "adi-s-mundo-cruel"

    def test_empty_input_returns_empty(self) -> None:
        """Empty input yields empty output, not a crash."""
        assert slugify_performer("") == ""

    def test_only_punctuation_returns_empty(self) -> None:
        """Strings with no slug-eligible characters yield empty output."""
        assert slugify_performer("///") == ""


class TestMakeArtistId:
    """Composite ID construction from band_id + performer."""

    def test_no_performer_returns_plain_band_id(self) -> None:
        """Omitted performer means "the band itself" → plain numeric ID."""
        assert make_artist_id(123) == "123"

    def test_none_performer_returns_plain_band_id(self) -> None:
        """Explicit None performer is treated the same as omitted."""
        assert make_artist_id(123, None) == "123"

    def test_empty_performer_returns_plain_band_id(self) -> None:
        """Empty string performer is treated the same as omitted."""
        assert make_artist_id(123, "") == "123"

    def test_with_performer_synthesizes(self) -> None:
        """Provided performer name produces a synthetic `{band_id}:{slug}` form."""
        assert make_artist_id(441379041, "Mortaja") == "441379041:mortaja"

    def test_string_band_id_is_accepted(self) -> None:
        """band_id may be passed as a string for caller convenience."""
        assert make_artist_id("441379041", "Mortaja") == "441379041:mortaja"

    def test_performer_with_only_punctuation_falls_back_to_plain(self) -> None:
        """A performer whose slug is empty degrades to the plain band ID."""
        # If the slug ends up empty we cannot build a useful synthetic
        # ID, so we degrade to the band's plain ID rather than emit a
        # malformed `123:` value.
        assert make_artist_id(123, "///") == "123"


class TestParseArtistId:
    """Reverse of make_artist_id."""

    def test_plain_band_id(self) -> None:
        """A plain numeric ID parses with no performer slug."""
        assert parse_artist_id("123") == (123, None)

    def test_synthetic_id(self) -> None:
        """A synthetic ID splits into (band_id, slug)."""
        assert parse_artist_id("441379041:mortaja") == (441379041, "mortaja")

    def test_synthetic_with_hyphenated_slug(self) -> None:
        """Hyphens inside the slug are preserved (not a separator with band_id)."""
        assert parse_artist_id("123:the-green-arrows") == (123, "the-green-arrows")

    def test_non_numeric_band_id_raises(self) -> None:
        """Non-numeric band_id portions surface as ValueError."""
        with pytest.raises(ValueError):  # noqa: PT011
            parse_artist_id("abc:slug")
