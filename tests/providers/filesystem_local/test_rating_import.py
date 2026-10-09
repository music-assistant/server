"""
Tests for importing a rating from file tags as the favorite flag.

The rating itself is not stored: it selects the value of the existing tri-state
favorite flag, and only when the provider setting enables the import.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from music_assistant.helpers.tags import AudioTags
from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.constants import (
    CONF_ENTRY_RATING_DISLIKE_THRESHOLD,
    CONF_ENTRY_RATING_FAVORITE_THRESHOLD,
    CONF_ENTRY_RATING_IMPORT_ENABLED,
)


def _create_provider(
    import_enabled: bool = False,
    favorite_threshold: float = 8.0,
    dislike_threshold: float = 2.0,
) -> LocalFileSystemProvider:
    """
    Create a LocalFileSystemProvider with the rating import settings under test.

    :param import_enabled: Whether the rating import setting is enabled.
    :param favorite_threshold: Minimum normalized rating to import as a favorite.
    :param dislike_threshold: Maximum normalized rating to import as a dislike.
    """
    config_values = {
        CONF_ENTRY_RATING_IMPORT_ENABLED.key: import_enabled,
        CONF_ENTRY_RATING_FAVORITE_THRESHOLD.key: favorite_threshold,
        CONF_ENTRY_RATING_DISLIKE_THRESHOLD.key: dislike_threshold,
    }

    mock_config = MagicMock()
    mock_config.get_value = MagicMock(side_effect=lambda key: config_values.get(key))

    with patch.object(LocalFileSystemProvider, "__init__", lambda *_a, **_kw: None):
        provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)

    provider.config = mock_config
    return provider


def _tags(**extra_tags: object) -> AudioTags:
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


class TestRatingImportDisabled:
    """Import is off by default so an update cannot refavourite an existing library."""

    def test_rated_file_does_not_set_the_flag(self) -> None:
        """A five star file changes nothing while the setting is off."""
        provider = _create_provider(import_enabled=False)
        assert provider._favorite_from_tags(_tags(popm=255)) is None

    def test_low_rated_file_does_not_set_the_flag(self) -> None:
        """A one star file is not turned into a dislike while the setting is off."""
        provider = _create_provider(import_enabled=False)
        assert provider._favorite_from_tags(_tags(popm=1)) is None


class TestRatingImportEnabled:
    """With the setting on, the rating selects the favorite flag."""

    def test_five_stars_is_a_favorite(self) -> None:
        """A five star rating at or above the threshold imports as a favorite."""
        provider = _create_provider(import_enabled=True)
        assert provider._favorite_from_tags(_tags(popm=255)) is True

    def test_one_star_is_a_dislike(self) -> None:
        """A one star rating at or below the threshold imports as a dislike."""
        provider = _create_provider(import_enabled=True)
        assert provider._favorite_from_tags(_tags(popm=1)) is False

    def test_three_stars_stays_neutral(self) -> None:
        """A rating in the neutral band leaves the flag unset."""
        provider = _create_provider(import_enabled=True)
        assert provider._favorite_from_tags(_tags(popm=128)) is None

    def test_unrated_file_stays_neutral(self) -> None:
        """A file with no rating tag is never turned into a dislike."""
        provider = _create_provider(import_enabled=True)
        assert provider._favorite_from_tags(_tags()) is None

    def test_vorbis_rating_is_imported(self) -> None:
        """A Vorbis RATING value is normalized the same way as POPM."""
        provider = _create_provider(import_enabled=True)
        assert provider._favorite_from_tags(_tags(rating="100")) is True

    def test_thresholds_follow_the_settings(self) -> None:
        """Lowering the favorite threshold promotes a three star rating to a favorite."""
        provider = _create_provider(import_enabled=True, favorite_threshold=6.0)
        assert provider._favorite_from_tags(_tags(popm=128)) is True

    def test_dislike_threshold_follows_the_settings(self) -> None:
        """Raising the dislike threshold turns a three star rating into a dislike."""
        provider = _create_provider(import_enabled=True, dislike_threshold=6.0)
        assert provider._favorite_from_tags(_tags(popm=128)) is False
