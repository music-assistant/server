"""
Tests for importing a rating from file tags as the favorite flag.

The rating itself is not stored: it selects the value of the existing tri-state
favorite flag, and only when the provider setting enables the import.
"""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.enums import MediaType

from music_assistant.helpers.rating import POPM_SCALE_ITUNES, POPM_SCALE_WINDOWS
from music_assistant.helpers.tags import AudioTags
from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.constants import (
    CONF_ENTRY_RATING_DISLIKE_THRESHOLD,
    CONF_ENTRY_RATING_FAVORITE_THRESHOLD,
    CONF_ENTRY_RATING_IMPORT_ENABLED,
    CONF_ENTRY_RATING_POPM_SCALE,
    CONF_ENTRY_RATING_TAG_SCALE,
)

INSTANCE_ID = "filesystem_local--test"


def _create_provider(
    import_enabled: bool = False,
    favorite_threshold: float = 8.0,
    dislike_threshold: float = 2.0,
    popm_scale: str = POPM_SCALE_WINDOWS,
    tag_scale: str = "percent",
) -> LocalFileSystemProvider:
    """
    Create a LocalFileSystemProvider with the rating import settings under test.

    :param import_enabled: Whether the rating import setting is enabled.
    :param favorite_threshold: Minimum normalized rating to import as a favorite.
    :param dislike_threshold: Maximum normalized rating to import as a dislike.
    :param popm_scale: The scale ID3 POPM values are read with.
    :param tag_scale: The scale RATING values are read with.
    """
    config_values = {
        CONF_ENTRY_RATING_IMPORT_ENABLED.key: import_enabled,
        CONF_ENTRY_RATING_FAVORITE_THRESHOLD.key: favorite_threshold,
        CONF_ENTRY_RATING_DISLIKE_THRESHOLD.key: dislike_threshold,
        CONF_ENTRY_RATING_POPM_SCALE.key: popm_scale,
        CONF_ENTRY_RATING_TAG_SCALE.key: tag_scale,
    }

    mock_config = MagicMock()
    mock_config.get_value = MagicMock(side_effect=lambda key: config_values.get(key))
    mock_config.instance_id = INSTANCE_ID

    with patch.object(LocalFileSystemProvider, "__init__", lambda *_a, **_kw: None):
        provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)

    provider.config = mock_config
    provider.mass = MagicMock()
    provider.mass.music.favorites.record_from_provider = AsyncMock()
    return provider


def _record_mock(provider: LocalFileSystemProvider) -> AsyncMock:
    """Return the favorites recorder mock, typed so the mock assertions type check."""
    return cast("AsyncMock", provider.mass.music.favorites.record_from_provider)


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

    async def test_nothing_is_recorded_while_disabled(self) -> None:
        """No favorite state is reported to the favorites controller either."""
        provider = _create_provider(import_enabled=False)
        await provider._record_rating_favorite(_tags(popm=255), 42)
        _record_mock(provider).assert_not_called()


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

    def test_thresholds_follow_the_settings(self) -> None:
        """Lowering the favorite threshold promotes a three star rating to a favorite."""
        provider = _create_provider(import_enabled=True, favorite_threshold=6.0)
        assert provider._favorite_from_tags(_tags(popm=128)) is True

    def test_dislike_threshold_follows_the_settings(self) -> None:
        """Raising the dislike threshold turns a three star rating into a dislike."""
        provider = _create_provider(import_enabled=True, dislike_threshold=6.0)
        assert provider._favorite_from_tags(_tags(popm=128)) is False


class TestRatingScales:
    """The scale settings decide how a library's values are read."""

    def test_itunes_scale_promotes_the_same_byte(self) -> None:
        """A byte that is two stars on the Windows scale is four stars on iTunes."""
        windows = _create_provider(import_enabled=True, popm_scale=POPM_SCALE_WINDOWS)
        itunes = _create_provider(import_enabled=True, popm_scale=POPM_SCALE_ITUNES)
        assert windows._favorite_from_tags(_tags(popm=80)) is None
        assert itunes._favorite_from_tags(_tags(popm=80)) is True

    def test_star_scale_reads_a_vorbis_rating_as_stars(self) -> None:
        """A foobar2000 star rating must not be read as a percentage and called a dislike."""
        percent = _create_provider(import_enabled=True, tag_scale="percent")
        stars = _create_provider(import_enabled=True, tag_scale="stars")
        assert percent._favorite_from_tags(_tags(rating="5")) is False
        assert stars._favorite_from_tags(_tags(rating="5")) is True


class TestRecordingTheFavorite:
    """The state is reported to the favorites controller, not written onto the item."""

    async def test_favorite_is_recorded_for_the_library_item(self) -> None:
        """A five star file reports a like for the database id of the library track."""
        provider = _create_provider(import_enabled=True)
        await provider._record_rating_favorite(_tags(popm=255), 42)
        _record_mock(provider).assert_awaited_once_with(INSTANCE_ID, MediaType.TRACK, 42, True)

    async def test_dislike_is_recorded_for_the_library_item(self) -> None:
        """A one star file reports a dislike."""
        provider = _create_provider(import_enabled=True)
        await provider._record_rating_favorite(_tags(popm=1), 7)
        _record_mock(provider).assert_awaited_once_with(INSTANCE_ID, MediaType.TRACK, 7, False)

    async def test_nothing_is_recorded_for_an_unrated_file(self) -> None:
        """An unrated file reports nothing, so an existing state is left alone."""
        provider = _create_provider(import_enabled=True)
        await provider._record_rating_favorite(_tags(), 42)
        _record_mock(provider).assert_not_called()

    async def test_nothing_is_recorded_for_a_neutral_rating(self) -> None:
        """A rating inside the neutral band reports nothing."""
        provider = _create_provider(import_enabled=True)
        await provider._record_rating_favorite(_tags(popm=128), 42)
        _record_mock(provider).assert_not_called()
