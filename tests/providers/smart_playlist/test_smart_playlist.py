"""Tests for the Smart Playlist plugin provider."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import (
    AlbumType,
    ImageType,
    MediaType,
    ProviderFeature,
    ProviderType,
)
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import (
    Genre,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    Track,
)
from music_assistant_models.media_items.metadata import MediaItemMetadata
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import DYNAMIC_PLAYLIST_SAMPLE_SIZE
from music_assistant.helpers.track_filter import track_filter
from music_assistant.models.plugin import AIEngine, PluginProvider
from music_assistant.providers.radio_playlist import RadioPlaylistProvider
from music_assistant.providers.smart_playlist import (
    CONF_AI_DESCRIPTIONS,
    CONF_AI_ENGINE,
    MAX_AI_DESCRIPTION_BYTES,
    SmartPlaylistProvider,
)
from music_assistant.providers.smart_playlist.helpers import (
    LOGIC_AND,
    LOGIC_OR,
    RULES_FILENAME,
    SmartPlaylistRules,
    write_json,
)
from tests.common import use_real_create_task

# ---------------------------------------------------------------------------
# SmartPlaylistRules unit tests
# ---------------------------------------------------------------------------


class TestSmartPlaylistRules:
    """Tests for the SmartPlaylistRules dataclass."""

    def test_defaults(self) -> None:
        """Rules are created with sensible defaults."""
        rules = SmartPlaylistRules()
        assert rules.genre_ids == []
        assert rules.artist_ids == []
        assert rules.album_ids == []
        assert rules.favorites_only is False
        assert rules.seed_track_uris == []
        assert rules.seed_artist_uris == []
        assert rules.seed_album_uris == []
        assert rules.seed_playlist_uris == []
        assert rules.min_popularity is None
        assert rules.logic == LOGIC_AND
        assert rules.limit == 100

    def test_round_trip_serialization(self) -> None:
        """to_dict / from_dict round-trip preserves all fields."""
        original = SmartPlaylistRules(
            genre_ids=[1, 2, 3],
            artist_ids=[10],
            album_ids=[],
            favorites_only=True,
            seed_track_uris=["library://track/42", "library://track/43"],
            seed_artist_uris=["library://artist/7"],
            seed_album_uris=["library://album/3"],
            seed_playlist_uris=["library://playlist/9"],
            seed_names={"library://track/42": "Some Track"},
            min_popularity=50,
            logic=LOGIC_OR,
            limit=25,
        )
        recovered = SmartPlaylistRules.from_dict(original.to_dict())
        assert recovered == original

    def test_all_seed_uris_dedupes_across_lists(self) -> None:
        """all_seed_uris() returns each URI once even when duplicated across lists."""
        rules = SmartPlaylistRules(
            seed_track_uris=["a", "b"],
            seed_artist_uris=["b", "c"],
            seed_album_uris=["d"],
            seed_playlist_uris=[],
        )
        assert rules.all_seed_uris() == ["a", "b", "c", "d"]

    def test_from_dict_partial(self) -> None:
        """from_dict tolerates missing keys by using defaults."""
        rules = SmartPlaylistRules.from_dict({"favorites_only": True})
        assert rules.favorites_only is True
        assert rules.genre_ids == []
        assert rules.logic == LOGIC_AND

    def test_human_readable_no_rules(self) -> None:
        """human_readable for empty rules returns fallback message."""
        rules = SmartPlaylistRules()
        assert "No rules" in rules.human_readable()

    def test_human_readable_with_rules(self) -> None:
        """human_readable includes all active filter names."""
        rules = SmartPlaylistRules(
            genre_ids=[1],
            favorites_only=True,
            min_popularity=60,
            logic=LOGIC_AND,
        )
        summary = rules.human_readable()
        assert "Favorites only" in summary
        assert "Genre" in summary
        assert "popularity" in summary.lower()
        assert LOGIC_AND in summary

    def test_human_readable_or_logic(self) -> None:
        """human_readable uses OR as connector when logic=OR."""
        rules = SmartPlaylistRules(
            genre_ids=[1],
            artist_ids=[5],
            logic=LOGIC_OR,
        )
        assert LOGIC_OR in rules.human_readable()

    def test_from_dict_null_list_fields_treated_as_empty(self) -> None:
        """from_dict treats null for list fields as empty list."""
        rules = SmartPlaylistRules.from_dict(
            {"genre_ids": None, "artist_ids": None, "album_ids": None}
        )
        assert rules.genre_ids == []
        assert rules.artist_ids == []
        assert rules.album_ids == []

    def test_from_dict_null_dict_fields_treated_as_empty(self) -> None:
        """from_dict treats null for dict fields as empty dict."""
        rules = SmartPlaylistRules.from_dict(
            {"genre_names": None, "artist_names": None, "album_names": None}
        )
        assert rules.genre_names == {}
        assert rules.artist_names == {}
        assert rules.album_names == {}

    def test_from_dict_non_numeric_id_raises(self) -> None:
        """from_dict raises InvalidDataError for non-numeric ids."""
        with pytest.raises(InvalidDataError):
            SmartPlaylistRules.from_dict({"genre_ids": ["abc"]})

    def test_from_dict_wrong_type_for_names_dict_raises(self) -> None:
        """from_dict raises InvalidDataError when a names field is not a dict."""
        with pytest.raises(InvalidDataError):
            SmartPlaylistRules.from_dict({"genre_names": "invalid"})

    def test_from_dict_excluded_null_fields_treated_as_empty(self) -> None:
        """from_dict treats null for excluded_* fields as empty."""
        rules = SmartPlaylistRules.from_dict(
            {
                "excluded_artist_ids": None,
                "excluded_album_ids": None,
                "excluded_track_uris": None,
                "excluded_artist_names": None,
                "excluded_album_names": None,
            }
        )
        assert rules.excluded_artist_ids == []
        assert rules.excluded_album_ids == []
        assert rules.excluded_track_uris == []
        assert rules.excluded_artist_names == {}
        assert rules.excluded_album_names == {}

    def test_duration_fields_round_trip(self) -> None:
        """min_duration and max_duration survive serialization."""
        original = SmartPlaylistRules(min_duration=180, max_duration=600)
        recovered = SmartPlaylistRules.from_dict(original.to_dict())
        assert recovered.min_duration == 180
        assert recovered.max_duration == 600

    def test_last_played_field_round_trip(self) -> None:
        """last_played_before_value and last_played_before_unit survive serialization."""
        original = SmartPlaylistRules(last_played_before_value=30, last_played_before_unit="days")
        recovered = SmartPlaylistRules.from_dict(original.to_dict())
        assert recovered.last_played_before_value == 30
        assert recovered.last_played_before_unit == "days"

    def test_from_dict_null_duration_fields_treated_as_none(self) -> None:
        """from_dict treats null for duration and last_played fields as None."""
        rules = SmartPlaylistRules.from_dict(
            {
                "min_duration": None,
                "max_duration": None,
                "last_played_before_value": None,
                "last_played_before_unit": None,
            }
        )
        assert rules.min_duration is None
        assert rules.max_duration is None
        assert rules.last_played_before_value is None
        assert rules.last_played_before_unit is None


# ---------------------------------------------------------------------------
# Plugin validation tests
# ---------------------------------------------------------------------------


class TestRuleValidation:
    """Tests for _validate_rules inside the plugin."""

    def _make_plugin(self) -> SmartPlaylistProvider:
        """Create a SmartPlaylistProvider with mocked mass."""
        mass = MagicMock()
        manifest = MagicMock()
        manifest.domain = "smart_playlist"
        config = MagicMock()
        config.get_value.return_value = "GLOBAL"
        return SmartPlaylistProvider(mass, manifest, config, set())

    def test_valid_rules_pass(self) -> None:
        """Valid rules do not raise."""
        plugin = self._make_plugin()
        rules = SmartPlaylistRules(logic=LOGIC_AND, limit=50)
        plugin._validate_rules(rules)  # should not raise

    def test_invalid_logic_raises(self) -> None:
        """Unknown logic operator raises InvalidDataError."""
        plugin = self._make_plugin()
        rules = SmartPlaylistRules(logic="XOR")
        with pytest.raises(InvalidDataError, match="logic"):
            plugin._validate_rules(rules)

    def test_limit_out_of_range_raises(self) -> None:
        """Limit outside 1-2000 raises InvalidDataError."""
        plugin = self._make_plugin()
        rules = SmartPlaylistRules(limit=0)
        with pytest.raises(InvalidDataError, match="limit"):
            plugin._validate_rules(rules)

        rules_too_high = SmartPlaylistRules(limit=9999)
        with pytest.raises(InvalidDataError, match="limit"):
            plugin._validate_rules(rules_too_high)

    def test_popularity_out_of_range_raises(self) -> None:
        """min_popularity outside 0-100 raises InvalidDataError."""
        plugin = self._make_plugin()
        rules = SmartPlaylistRules(min_popularity=150)
        with pytest.raises(InvalidDataError, match="popularity"):
            plugin._validate_rules(rules)

    def test_too_many_seeds_raises(self) -> None:
        """More than MAX_SEEDS combined seeds raises InvalidDataError."""
        plugin = self._make_plugin()
        rules = SmartPlaylistRules(
            seed_track_uris=[f"library://track/{i}" for i in range(6)],
            seed_artist_uris=[f"library://artist/{i}" for i in range(6)],
        )
        with pytest.raises(InvalidDataError, match="Too many seeds"):
            plugin._validate_rules(rules)

    def test_negative_min_duration_raises(self) -> None:
        """Negative min_duration raises InvalidDataError."""
        plugin = self._make_plugin()
        rules = SmartPlaylistRules(min_duration=-10)
        with pytest.raises(InvalidDataError, match="min_duration"):
            plugin._validate_rules(rules)

    def test_negative_max_duration_raises(self) -> None:
        """Negative max_duration raises InvalidDataError."""
        plugin = self._make_plugin()
        rules = SmartPlaylistRules(max_duration=-5)
        with pytest.raises(InvalidDataError, match="max_duration"):
            plugin._validate_rules(rules)

    def test_min_duration_greater_than_max_raises(self) -> None:
        """min_duration > max_duration raises InvalidDataError."""
        plugin = self._make_plugin()
        rules = SmartPlaylistRules(min_duration=600, max_duration=300)
        with pytest.raises(InvalidDataError, match=r"min_duration.*max_duration"):
            plugin._validate_rules(rules)

    def test_last_played_before_value_zero_raises(self) -> None:
        """last_played_before_value < 1 raises InvalidDataError."""
        plugin = self._make_plugin()
        rules = SmartPlaylistRules(last_played_before_value=0, last_played_before_unit="days")
        with pytest.raises(InvalidDataError, match="last_played_before_value"):
            plugin._validate_rules(rules)

    def test_valid_duration_and_last_played_pass(self) -> None:
        """Valid duration and last_played values do not raise."""
        plugin = self._make_plugin()
        rules = SmartPlaylistRules(
            min_duration=180,
            max_duration=600,
            last_played_before_value=30,
            last_played_before_unit="days",
        )
        plugin._validate_rules(rules)  # should not raise

    def test_last_played_invalid_unit_raises(self) -> None:
        """Invalid last_played_before_unit raises InvalidDataError."""
        plugin = self._make_plugin()
        rules = SmartPlaylistRules(last_played_before_value=10, last_played_before_unit="years")
        with pytest.raises(InvalidDataError, match="last_played_before_unit"):
            plugin._validate_rules(rules)

    def test_last_played_only_value_set_raises(self) -> None:
        """Only last_played_before_value set (no unit) raises InvalidDataError."""
        plugin = self._make_plugin()
        rules = SmartPlaylistRules(last_played_before_value=10, last_played_before_unit=None)
        with pytest.raises(InvalidDataError, match="last_played"):
            plugin._validate_rules(rules)

    def test_last_played_only_unit_set_raises(self) -> None:
        """Only last_played_before_unit set (no value) raises InvalidDataError."""
        plugin = self._make_plugin()
        rules = SmartPlaylistRules(last_played_before_value=None, last_played_before_unit="days")
        with pytest.raises(InvalidDataError, match="last_played"):
            plugin._validate_rules(rules)


# ---------------------------------------------------------------------------
# Persistence tests  (using tmp_path, no real MA instance needed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rules_persist_to_disk(tmp_path: Any) -> None:
    """Rules saved to disk survive plugin reload."""
    rules_dir = tmp_path / "smart_playlists"
    rules_dir.mkdir()

    mass = MagicMock()
    mass.storage_path = str(tmp_path)
    mass.cache.clear = AsyncMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"

    plugin = SmartPlaylistProvider(mass, manifest, config, set())
    await plugin.handle_async_init()
    plugin._rules_dir = str(rules_dir)

    rules = SmartPlaylistRules(genre_ids=[1, 2], favorites_only=True)
    await plugin._save_rules("42", rules)

    # Simulate reload
    plugin2 = SmartPlaylistProvider(mass, manifest, config, set())
    await plugin2.handle_async_init()
    plugin2._rules_dir = str(rules_dir)
    plugin2._rules_store = {}
    plugin2._names_store = {}
    await plugin2._load_rules_from_disk()

    assert "42" in plugin2._rules_store
    assert plugin2._rules_store["42"] == rules


# ---------------------------------------------------------------------------
# Evaluate-rules unit tests with mocked mass
# ---------------------------------------------------------------------------


def _make_mock_track(
    item_id: str = "1",
    uri: str = "library://track/1",
    artist_ids: list[str] | None = None,
    album_id: str | None = None,
    favorite: bool = False,
    popularity: int | None = None,
    provider_instance: str = "library",
    duration: int | None = None,
    last_played: int = 0,
    explicit: bool | None = None,
) -> MagicMock:
    """Build a minimal mock Track object."""
    track = MagicMock()
    track.item_id = item_id
    track.uri = uri
    track.name = f"Track {item_id}"
    track.favorite = favorite
    track.duration = duration
    track.last_played = last_played

    mapping = MagicMock()
    mapping.provider_instance = provider_instance
    mapping.item_id = item_id
    track.provider_mappings = [mapping]

    artist = MagicMock()
    artist.item_id = (artist_ids or ["100"])[0]
    artist.name = "Artist"
    track.artists = [artist]

    album = MagicMock()
    album.item_id = album_id or "200"
    track.album = album

    track.metadata = MagicMock()
    track.metadata.popularity = popularity
    track.metadata.explicit = explicit

    return track


@pytest.mark.asyncio
async def test_evaluate_and_no_filters_returns_library() -> None:
    """With no filters, AND logic returns the entire library."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    tracks = [_make_mock_track(str(i), f"library://track/{i}") for i in range(10)]
    cast("Any", plugin)._get_library_tracks = AsyncMock(return_value=tracks)

    rules = SmartPlaylistRules(logic=LOGIC_AND, limit=10)
    result = await plugin._evaluate_and(rules)
    assert len(result) == 10


@pytest.mark.asyncio
async def test_evaluate_and_artist_filter() -> None:
    """AND logic filters tracks to only those from the specified artists."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    artist_a = _make_mock_track("1", artist_ids=["10"])
    artist_b = _make_mock_track("2", artist_ids=["20"])
    cast("Any", plugin)._get_library_tracks = AsyncMock(return_value=[artist_a, artist_b])

    rules = SmartPlaylistRules(artist_ids=[10], logic=LOGIC_AND, limit=10)
    result = await plugin._evaluate_and(rules)
    assert len(result) == 1
    assert result[0].item_id == "1"


@pytest.mark.asyncio
async def test_evaluate_or_genre_and_artist_union() -> None:
    """OR logic returns union of genre tracks and artist tracks."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    genre_track = _make_mock_track("1", uri="library://track/1", artist_ids=["99"])
    artist_track = _make_mock_track("2", uri="library://track/2", artist_ids=["10"])

    async def mock_get_library(**kwargs: Any) -> list[MagicMock]:
        if kwargs.get("genre_ids"):
            return [genre_track]
        return [genre_track, artist_track]

    cast("Any", plugin)._get_library_tracks = mock_get_library

    rules = SmartPlaylistRules(
        genre_ids=[5],
        artist_ids=[10],
        logic=LOGIC_OR,
        limit=10,
    )
    result = await plugin._evaluate_or(rules)
    uris = {t.uri for t in result}
    assert "library://track/1" in uris
    assert "library://track/2" in uris


@pytest.mark.asyncio
async def test_popularity_filter_applied() -> None:
    """Tracks below min_popularity are filtered out."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    low_pop = _make_mock_track("1", uri="library://track/1", popularity=30)
    high_pop = _make_mock_track("2", uri="library://track/2", popularity=80)
    cast("Any", plugin)._get_library_tracks = AsyncMock(return_value=[low_pop, high_pop])

    rules = SmartPlaylistRules(min_popularity=50, logic=LOGIC_AND, limit=10)
    result = await plugin._evaluate_rules(rules)
    assert all(t.metadata.popularity is None or t.metadata.popularity >= 50 for t in result)
    uris = [t.uri for t in result]
    assert "library://track/1" not in uris
    assert "library://track/2" in uris


@pytest.mark.asyncio
async def test_favorites_only_filter() -> None:
    """favorites_only=True passes favorite=True to the DB query layer."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    fav = _make_mock_track("1", uri="library://track/1", favorite=True)
    not_fav = _make_mock_track("2", uri="library://track/2", favorite=False)
    all_tracks = [fav, not_fav]

    async def mock_get_library(**kwargs: Any) -> list[MagicMock]:
        if kwargs.get("favorite") is True:
            return [t for t in all_tracks if t.favorite]
        return all_tracks

    cast("Any", plugin)._get_library_tracks = mock_get_library

    rules = SmartPlaylistRules(favorites_only=True, logic=LOGIC_AND, limit=10)
    result = await plugin._evaluate_rules(rules)
    uris = [t.uri for t in result]
    assert "library://track/1" in uris
    assert "library://track/2" not in uris


@pytest.mark.asyncio
async def test_explicit_only_filter() -> None:
    """explicit=True passes explicit=True to _get_library_tracks for SQL filtering."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    explicit_track = _make_mock_track("1", uri="library://track/1", explicit=True)
    clean_track = _make_mock_track("2", uri="library://track/2", explicit=False)
    unknown_track = _make_mock_track("3", uri="library://track/3", explicit=None)

    async def mock_get_library(**kwargs: Any) -> list[MagicMock]:
        explicit_filter = kwargs.get("explicit")
        all_tracks = [explicit_track, clean_track, unknown_track]
        if explicit_filter is True:
            # Simulate SQL filter: json_extract(...) = 1
            return [t for t in all_tracks if t.metadata.explicit is True]
        if explicit_filter is False:
            # Simulate SQL filter: IS NULL OR = 0
            return [t for t in all_tracks if t.metadata.explicit is not True]
        return all_tracks

    cast("Any", plugin)._get_library_tracks = mock_get_library

    # Test explicit only
    rules = SmartPlaylistRules(explicit=True, logic=LOGIC_AND, limit=10)
    result = await plugin._evaluate_rules(rules)
    uris = [t.uri for t in result]
    assert "library://track/1" in uris
    assert "library://track/2" not in uris
    assert "library://track/3" not in uris


@pytest.mark.asyncio
async def test_no_explicit_filter() -> None:
    """explicit=False excludes explicit tracks via SQL filtering."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    explicit_track = _make_mock_track("1", uri="library://track/1", explicit=True)
    clean_track = _make_mock_track("2", uri="library://track/2", explicit=False)
    unknown_track = _make_mock_track("3", uri="library://track/3", explicit=None)

    async def mock_get_library(**kwargs: Any) -> list[MagicMock]:
        explicit_filter = kwargs.get("explicit")
        all_tracks = [explicit_track, clean_track, unknown_track]
        if explicit_filter is True:
            return [t for t in all_tracks if t.metadata.explicit is True]
        if explicit_filter is False:
            # Simulate SQL filter: IS NULL OR = 0
            return [t for t in all_tracks if t.metadata.explicit is not True]
        return all_tracks

    cast("Any", plugin)._get_library_tracks = mock_get_library

    # Test no explicit
    rules = SmartPlaylistRules(explicit=False, logic=LOGIC_AND, limit=10)
    result = await plugin._evaluate_rules(rules)
    uris = [t.uri for t in result]
    assert "library://track/1" not in uris
    assert "library://track/2" in uris
    assert "library://track/3" in uris


@pytest.mark.asyncio
async def test_allow_explicit_filter() -> None:
    """explicit=None returns all tracks regardless of explicit flag."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    explicit_track = _make_mock_track("1", uri="library://track/1", explicit=True)
    clean_track = _make_mock_track("2", uri="library://track/2", explicit=False)
    unknown_track = _make_mock_track("3", uri="library://track/3", explicit=None)

    all_tracks = [explicit_track, clean_track, unknown_track]
    cast("Any", plugin)._get_library_tracks = AsyncMock(return_value=all_tracks)

    # Test allow explicit (no filter)
    rules = SmartPlaylistRules(explicit=None, logic=LOGIC_AND, limit=10)
    result = await plugin._evaluate_rules(rules)
    uris = [t.uri for t in result]
    assert "library://track/1" in uris
    assert "library://track/2" in uris
    assert "library://track/3" in uris


@pytest.mark.asyncio
async def test_explicit_filter_generates_sql_query_parts() -> None:
    """_get_library_tracks passes explicit parameter correctly to library_items."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    # Mock tracks.library_items to capture kwargs
    library_items_mock = AsyncMock(return_value=[])
    mass.music.tracks.library_items = library_items_mock

    # Test explicit=True passes correctly
    await plugin._get_library_tracks(explicit=True, limit=10)
    call_kwargs = library_items_mock.call_args.kwargs
    assert call_kwargs["explicit"] is True

    # Test explicit=False passes correctly
    library_items_mock.reset_mock()
    await plugin._get_library_tracks(explicit=False, limit=10)
    call_kwargs = library_items_mock.call_args.kwargs
    assert call_kwargs["explicit"] is False

    # Test explicit=None passes correctly
    library_items_mock.reset_mock()
    await plugin._get_library_tracks(explicit=None, limit=10)
    call_kwargs = library_items_mock.call_args.kwargs
    assert call_kwargs["explicit"] is None


@pytest.mark.asyncio
async def test_limit_is_respected() -> None:
    """Result is capped at rules.limit."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    tracks = [_make_mock_track(str(i), f"library://track/{i}") for i in range(50)]
    cast("Any", plugin)._get_library_tracks = AsyncMock(return_value=tracks)

    rules = SmartPlaylistRules(limit=5)
    result = await plugin._evaluate_rules(rules)
    assert len(result) <= 5


@pytest.mark.asyncio
async def test_duration_filter_min_only() -> None:
    """Tracks shorter than min_duration are filtered out."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    short_track = _make_mock_track("1", uri="library://track/1", duration=120)  # 2 minutes
    long_track = _make_mock_track("2", uri="library://track/2", duration=300)  # 5 minutes
    cast("Any", plugin)._get_library_tracks = AsyncMock(return_value=[short_track, long_track])

    rules = SmartPlaylistRules(min_duration=180, logic=LOGIC_AND, limit=10)  # 3 minutes min
    result = await plugin._evaluate_rules(rules)
    uris = [t.uri for t in result]
    assert "library://track/1" not in uris  # too short
    assert "library://track/2" in uris


@pytest.mark.asyncio
async def test_duration_filter_max_only() -> None:
    """Tracks longer than max_duration are filtered out."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    short_track = _make_mock_track("1", uri="library://track/1", duration=120)  # 2 minutes
    long_track = _make_mock_track("2", uri="library://track/2", duration=600)  # 10 minutes
    cast("Any", plugin)._get_library_tracks = AsyncMock(return_value=[short_track, long_track])

    rules = SmartPlaylistRules(max_duration=300, logic=LOGIC_AND, limit=10)  # 5 minutes max
    result = await plugin._evaluate_rules(rules)
    uris = [t.uri for t in result]
    assert "library://track/1" in uris
    assert "library://track/2" not in uris  # too long


@pytest.mark.asyncio
async def test_duration_filter_between() -> None:
    """Only tracks within min_duration and max_duration pass."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    too_short = _make_mock_track("1", uri="library://track/1", duration=120)  # 2 min
    just_right = _make_mock_track("2", uri="library://track/2", duration=240)  # 4 min
    too_long = _make_mock_track("3", uri="library://track/3", duration=600)  # 10 min
    cast("Any", plugin)._get_library_tracks = AsyncMock(
        return_value=[too_short, just_right, too_long]
    )

    rules = SmartPlaylistRules(
        min_duration=180, max_duration=300, logic=LOGIC_AND, limit=10
    )  # 3-5 minutes
    result = await plugin._evaluate_rules(rules)
    uris = [t.uri for t in result]
    assert "library://track/1" not in uris  # too short
    assert "library://track/2" in uris  # perfect
    assert "library://track/3" not in uris  # too long


@pytest.mark.asyncio
async def test_duration_filter_skips_tracks_without_duration() -> None:
    """Tracks with duration=None are excluded when duration filter is active."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    no_duration = _make_mock_track("1", uri="library://track/1", duration=None)
    has_duration = _make_mock_track("2", uri="library://track/2", duration=240)
    cast("Any", plugin)._get_library_tracks = AsyncMock(return_value=[no_duration, has_duration])

    rules = SmartPlaylistRules(min_duration=180, logic=LOGIC_AND, limit=10)
    result = await plugin._evaluate_rules(rules)
    uris = [t.uri for t in result]
    assert "library://track/1" not in uris  # no duration
    assert "library://track/2" in uris


@pytest.mark.asyncio
async def test_last_played_filter() -> None:
    """Tracks played recently are filtered out."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    now = int(time.time())
    never_played = _make_mock_track("1", uri="library://track/1", last_played=0)
    played_recently = _make_mock_track(
        "2", uri="library://track/2", last_played=now - 86400
    )  # 1 day ago
    played_long_ago = _make_mock_track(
        "3", uri="library://track/3", last_played=now - (60 * 86400)
    )  # 60 days ago
    cast("Any", plugin)._get_library_tracks = AsyncMock(
        return_value=[never_played, played_recently, played_long_ago]
    )

    # Test with days unit
    rules = SmartPlaylistRules(
        last_played_before_value=30, last_played_before_unit="days", logic=LOGIC_AND, limit=10
    )  # Not played in last 30 days
    result = await plugin._evaluate_rules(rules)
    uris = [t.uri for t in result]
    assert "library://track/1" in uris  # never played = included
    assert "library://track/2" not in uris  # played 1 day ago = excluded
    assert "library://track/3" in uris  # played 60 days ago = included

    # Test with hours unit
    rules = SmartPlaylistRules(
        last_played_before_value=12, last_played_before_unit="hours", logic=LOGIC_AND, limit=10
    )  # Not played in last 12 hours
    result = await plugin._evaluate_rules(rules)
    uris = [t.uri for t in result]
    assert "library://track/1" in uris  # never played = included
    assert "library://track/2" in uris  # played 1 day ago = included
    assert "library://track/3" in uris  # played 60 days ago = included

    # Test with weeks unit
    rules = SmartPlaylistRules(
        last_played_before_value=2, last_played_before_unit="weeks", logic=LOGIC_AND, limit=10
    )  # Not played in last 2 weeks
    result = await plugin._evaluate_rules(rules)
    uris = [t.uri for t in result]
    assert "library://track/1" in uris  # never played = included
    assert "library://track/2" not in uris  # played 1 day ago = excluded
    assert "library://track/3" in uris  # played 60 days ago = included


# ---------------------------------------------------------------------------
# New feature tests: seed_artist, exclusions, dedup, validation, count_tracks
# ---------------------------------------------------------------------------


class TestNewValidation:
    """Validate seed and range checks."""

    def _make_plugin(self) -> SmartPlaylistProvider:
        mass = MagicMock()
        manifest = MagicMock()
        manifest.domain = "smart_playlist"
        config = MagicMock()
        config.get_value.return_value = "GLOBAL"
        return SmartPlaylistProvider(mass, manifest, config, set())

    def test_mixed_seeds_within_cap_passes(self) -> None:
        """A mix of seed types under the cap validates."""
        plugin = self._make_plugin()
        rules = SmartPlaylistRules(
            seed_track_uris=["library://track/1", "library://track/2"],
            seed_artist_uris=["library://artist/3"],
            seed_album_uris=["library://album/4"],
            seed_playlist_uris=["library://playlist/5"],
        )
        plugin._validate_rules(rules)  # should not raise


@pytest.mark.asyncio
async def test_seed_mode_uses_tracks_from_seeds() -> None:
    """When any seed URI is set, evaluator collects tracks via _tracks_from_seeds."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    similar_tracks = [_make_mock_track("10", "library://track/10")]
    cast("Any", plugin)._tracks_from_seeds = AsyncMock(return_value=similar_tracks)
    cast("Any", plugin)._get_library_tracks = AsyncMock(return_value=[])

    rules = SmartPlaylistRules(
        seed_artist_uris=["library://artist/5"],
        seed_album_uris=["library://album/9"],
        limit=10,
    )
    result = await plugin._evaluate_rules(rules)

    cast("Any", plugin)._tracks_from_seeds.assert_awaited_once()
    awaited_args = cast("Any", plugin)._tracks_from_seeds.await_args
    assert awaited_args.args[0] == ["library://artist/5", "library://album/9"]
    assert awaited_args.kwargs["target_size"] == 10
    assert awaited_args.kwargs["is_dynamic"] is True
    cast("Any", plugin)._get_library_tracks.assert_not_awaited()
    assert len(result) == 1


def _radio_track(item_id: str, duration: int = 200) -> MagicMock:
    """Build a minimal mock track usable by the real RadioPlaylistProvider generator."""
    track = MagicMock()
    track.item_id = item_id
    track.provider = "test"
    track.uri = f"test://track/{item_id}"
    track.name = f"Track {item_id}"
    track.duration = duration
    return track


def _real_radio_provider(mass: MagicMock) -> RadioPlaylistProvider:
    """Build a real RadioPlaylistProvider bound to a mocked mass, without full provider setup."""
    prov = RadioPlaylistProvider.__new__(RadioPlaylistProvider)
    prov.mass = mass
    return prov


@pytest.mark.asyncio
async def test_tracks_from_seeds_pools_base_and_similar() -> None:
    """_tracks_from_seeds accumulates a seed's endless-mix batches, deduped, in call order."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    seed = _make_mock_track("seed", "library://track/seed")
    base = _make_mock_track("base", "library://track/base")
    sim1 = _make_mock_track("sim1", "library://track/sim1")
    sim2 = _make_mock_track("sim2", "library://track/sim2")

    ctrl = MagicMock()
    ctrl.get = AsyncMock(return_value=seed)
    mass.music.get_controller = MagicMock(return_value=ctrl)

    radio_prov = MagicMock()
    radio_prov.get_dynamic_tracks = AsyncMock(return_value=[base, sim1, sim2])
    mass.get_provider = MagicMock(return_value=radio_prov)

    result = await plugin._tracks_from_seeds(
        ["library://track/10"], target_size=10, is_dynamic=True
    )

    first_call = radio_prov.get_dynamic_tracks.await_args_list[0]
    assert first_call.args[0] == [seed]
    assert first_call.kwargs["include_base_tracks"] is True
    assert first_call.kwargs["target_size"] == 10

    ids = [track.item_id for track in result]
    assert ids == ["base", "sim1", "sim2"]
    # the mock returns the same batch every call, so the second call adds nothing new and stops
    assert radio_prov.get_dynamic_tracks.await_count == 2


@pytest.mark.asyncio
async def test_tracks_from_seeds_samples_evenly_across_seeds() -> None:
    """A large seed must not crowd the pool: each seed contributes evenly (round-robin)."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    seed_a = _make_mock_track("seed_a", "library://track/seed_a")
    seed_b = _make_mock_track("seed_b", "library://track/seed_b")
    base_a = _make_mock_track("base_a", "library://track/base_a")
    base_b = _make_mock_track("base_b", "library://track/base_b")
    # Seed A's endless mix yields far more tracks than seed B's; the round-robin interleave
    # must still let B contribute before A's pool is exhausted.
    a_batch = [
        base_a,
        *(_make_mock_track(f"a_sim_{i}", f"library://track/a_sim_{i}") for i in range(30)),
    ]
    b_batch = [base_b, _make_mock_track("b_sim_0", "library://track/b_sim_0")]
    batches = {seed_a: a_batch, seed_b: b_batch}

    ctrl = MagicMock()
    ctrl.get = AsyncMock(side_effect=[seed_a, seed_b])
    mass.music.get_controller = MagicMock(return_value=ctrl)

    radio_prov = MagicMock()
    radio_prov.get_dynamic_tracks = AsyncMock(
        side_effect=lambda seeds, **_kwargs: batches[seeds[0]]
    )
    mass.get_provider = MagicMock(return_value=radio_prov)

    result = await plugin._tracks_from_seeds(
        ["library://track/seed_a", "library://track/seed_b"], target_size=4, is_dynamic=True
    )

    ids = {track.item_id for track in result}
    # seed B's tracks must survive even though seed A dwarfs it
    assert "base_b" in ids
    assert "b_sim_0" in ids
    assert "base_a" in ids
    # round-robin, not concatenation: A and B alternate at the head of the pool
    head = [track.item_id for track in result[:2]]
    assert head == ["base_a", "base_b"]


@pytest.mark.asyncio
async def test_tracks_from_seeds_single_batch_meets_dynamic_target() -> None:
    """A single endless-mix batch already meeting the target stops after one round."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    # a playlist seed is the realistic multi-track case; a track seed resolves to just itself
    seed = _make_mock_track("seed", "library://playlist/seed")
    seed.media_type = MediaType.PLAYLIST
    ctrl = MagicMock()
    ctrl.get = AsyncMock(return_value=seed)
    mass.music.get_controller = MagicMock(return_value=ctrl)

    base_tracks = [_radio_track(f"base_{i}") for i in range(40)]
    mass.player_queues.get_tracks_for_playback = AsyncMock(return_value=base_tracks)
    mass.music.tracks.similar_tracks = AsyncMock(
        side_effect=lambda item_id, _provider, **_kwargs: [
            _radio_track(f"{item_id}_sim_{i}") for i in range(25)
        ]
    )
    mass.get_provider = MagicMock(return_value=_real_radio_provider(mass))

    result = await plugin._tracks_from_seeds(
        ["library://playlist/seed"], target_size=25, is_dynamic=True
    )

    mass.player_queues.get_tracks_for_playback.assert_awaited_once()
    base_ids = {track.item_id for track in base_tracks}
    assert sum(1 for track in result if track.item_id in base_ids) >= 5
    assert len(result) <= 75


@pytest.mark.asyncio
async def test_tracks_from_seeds_accumulates_batches_for_large_target() -> None:
    """Static: a single ~55-track batch can't fill a target of 100, so batches accumulate."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    # a playlist seed is the realistic multi-track case; a track seed resolves to just itself
    seed = _make_mock_track("seed", "library://playlist/seed")
    seed.media_type = MediaType.PLAYLIST
    ctrl = MagicMock()
    ctrl.get = AsyncMock(return_value=seed)
    mass.music.get_controller = MagicMock(return_value=ctrl)

    base_tracks = [_radio_track(f"base_{i}") for i in range(40)]
    mass.player_queues.get_tracks_for_playback = AsyncMock(return_value=base_tracks)
    mass.music.tracks.similar_tracks = AsyncMock(
        side_effect=lambda item_id, _provider, **_kwargs: [
            _radio_track(f"{item_id}_sim_{i}") for i in range(25)
        ]
    )
    mass.get_provider = MagicMock(return_value=_real_radio_provider(mass))

    result = await plugin._tracks_from_seeds(
        ["library://playlist/seed"], target_size=100, is_dynamic=False
    )

    assert mass.player_queues.get_tracks_for_playback.await_count > 1
    assert len(result) >= 100


@pytest.mark.asyncio
async def test_tracks_from_seeds_static_gets_headroom_above_target() -> None:
    """Static generation accumulates well past target_size, giving post-filters headroom."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    seed = _make_mock_track("seed", "library://playlist/seed")
    seed.media_type = MediaType.PLAYLIST
    ctrl = MagicMock()
    ctrl.get = AsyncMock(return_value=seed)
    mass.music.get_controller = MagicMock(return_value=ctrl)

    base_tracks = [_radio_track(f"base_{i}") for i in range(40)]
    mass.player_queues.get_tracks_for_playback = AsyncMock(return_value=base_tracks)
    mass.music.tracks.similar_tracks = AsyncMock(
        side_effect=lambda item_id, _provider, **_kwargs: [
            _radio_track(f"{item_id}_sim_{i}") for i in range(25)
        ]
    )
    mass.get_provider = MagicMock(return_value=_real_radio_provider(mass))

    result = await plugin._tracks_from_seeds(
        ["library://playlist/seed"], target_size=100, is_dynamic=False
    )

    assert len({track.item_id for track in result}) > 200


@pytest.mark.asyncio
async def test_tracks_from_seeds_low_yield_batches_keep_accumulating() -> None:
    """Batches yielding few (but new) tracks keep accumulating until the budget is met."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    seed = _make_mock_track("seed", "library://playlist/seed")
    seed.media_type = MediaType.PLAYLIST
    ctrl = MagicMock()
    ctrl.get = AsyncMock(return_value=seed)
    mass.music.get_controller = MagicMock(return_value=ctrl)

    # every batch yields exactly 5 fresh tracks; a round cap sized for ~25-track batches
    # would stop far short of the 150-track static budget (50 * 3)
    counter = iter(range(10_000))
    radio_prov = MagicMock()
    radio_prov.get_dynamic_tracks = AsyncMock(
        side_effect=lambda *_args, **_kwargs: [
            _radio_track(f"track_{next(counter)}") for _ in range(5)
        ]
    )
    mass.get_provider = MagicMock(return_value=radio_prov)

    result = await plugin._tracks_from_seeds(
        ["library://playlist/seed"], target_size=50, is_dynamic=False
    )

    assert len(result) == 150
    assert radio_prov.get_dynamic_tracks.await_count == 30


@pytest.mark.asyncio
async def test_exclusion_filters_out_excluded_artist() -> None:
    """Tracks from excluded artists are removed from the result."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    included = _make_mock_track("1", "library://track/1", artist_ids=["10"])
    excluded = _make_mock_track("2", "library://track/2", artist_ids=["99"])
    cast("Any", plugin)._get_library_tracks = AsyncMock(return_value=[included, excluded])

    rules = SmartPlaylistRules(excluded_artist_ids=[99], limit=10)
    result = await plugin._evaluate_rules(rules)
    uris = [t.uri for t in result]
    assert "library://track/1" in uris
    assert "library://track/2" not in uris


@pytest.mark.asyncio
async def test_exclusion_filters_out_excluded_uri() -> None:
    """Tracks whose URI is in excluded_track_uris are removed."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    t1 = _make_mock_track("1", "library://track/1")
    t2 = _make_mock_track("2", "library://track/2")
    cast("Any", plugin)._get_library_tracks = AsyncMock(return_value=[t1, t2])

    rules = SmartPlaylistRules(excluded_track_uris=["library://track/2"], limit=10)
    result = await plugin._evaluate_rules(rules)
    assert all(t.uri != "library://track/2" for t in result)


@pytest.mark.asyncio
async def test_evaluate_rules_removes_duplicate_track_uris() -> None:
    """Smart playlist evaluation should not return the same track URI multiple times."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    provider_mapping = ProviderMapping(
        item_id="1",
        provider_domain="library",
        provider_instance="library",
        available=True,
    )
    dup_a_1 = Track(
        item_id="1",
        provider="library",
        name="Track 1",
        uri="library://track/dup",
        provider_mappings={provider_mapping},
    )
    dup_a_2 = Track(
        item_id="2",
        provider="library",
        name="Track 2",
        uri="library://track/dup",
        provider_mappings={provider_mapping},
    )
    dup_a_3 = Track(
        item_id="3",
        provider="library",
        name="Track 3",
        uri="library://track/dup",
        provider_mappings={provider_mapping},
    )
    uniq_b = Track(
        item_id="4",
        provider="library",
        name="Track 4",
        uri="library://track/unique",
        provider_mappings={provider_mapping},
    )
    cast("Any", plugin)._get_library_tracks = AsyncMock(
        return_value=[dup_a_1, dup_a_2, dup_a_3, uniq_b]
    )
    cast("Any", plugin)._enrich_tracks_with_db_genres = AsyncMock(return_value=None)

    rules = SmartPlaylistRules(limit=10, logic=LOGIC_AND)
    result = await plugin._evaluate_rules(rules)

    uris = [track.uri for track in result]
    assert uris.count("library://track/dup") == 1
    assert "library://track/unique" in uris


@pytest.mark.asyncio
async def test_evaluate_rules_dedup_skips_unavailable_tracks() -> None:
    """Dedup should skip unavailable tracks before adding to the result set."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    available_track = _make_mock_track("1", "library://track/available")
    available_track.available = True
    unavailable_track = _make_mock_track("2", "library://track/unavailable")
    unavailable_track.available = False
    cast("Any", plugin)._get_library_tracks = AsyncMock(
        return_value=[available_track, unavailable_track]
    )

    rules = SmartPlaylistRules(limit=10, logic=LOGIC_AND)
    result = await plugin._evaluate_rules(rules)

    assert len(result) == 1
    assert result[0].uri == "library://track/available"


def _swallow_task(coro: Any, **_: Any) -> None:
    """Close coroutines passed to a mocked mass.create_task so pytest stays quiet."""
    coro.close()


@pytest.mark.asyncio
async def test_get_playlist_tracks_dynamic_cold_evaluates_and_caches(tmp_path: Any) -> None:
    """On a fully-cold cache the sample is evaluated and a store task is scheduled."""
    mass = MagicMock()
    mass.storage_path = str(tmp_path)
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.cache.set = AsyncMock()
    use_real_create_task(mass)
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())
    await plugin.handle_async_init()

    tracks = [_make_mock_track(str(i), f"library://track/{i}") for i in range(50)]
    library_mock = AsyncMock(return_value=tracks)
    cast("Any", plugin)._get_library_tracks = library_mock

    rules = SmartPlaylistRules(limit=100, is_dynamic=True)
    plugin._rules_store["abc"] = rules

    result = await plugin.get_playlist_tracks("abc")
    assert len(result) <= DYNAMIC_PLAYLIST_SAMPLE_SIZE
    assert len(result) > 5
    # Observable behaviour: the wrapped evaluator ran and its result was stored.
    library_mock.assert_awaited()
    mass.cache.set.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_playlist_tracks_dynamic_returns_fresh_cache(tmp_path: Any) -> None:
    """A fresh cache hit short-circuits evaluation and does not touch the stale path."""
    mass = MagicMock()
    mass.storage_path = str(tmp_path)
    cached = [_make_mock_track(str(i), f"library://track/cached-{i}") for i in range(3)]
    mass.cache.get_with_freshness = AsyncMock(return_value=(cached, True, True))
    mass.cache.set = AsyncMock()
    mass.create_task = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())
    await plugin.handle_async_init()

    evaluate_mock = AsyncMock(return_value=[])
    cast("Any", plugin)._evaluate_rules = evaluate_mock

    plugin._rules_store["abc"] = SmartPlaylistRules(limit=100, is_dynamic=True)
    result = await plugin.get_playlist_tracks("abc")
    assert result == cached
    evaluate_mock.assert_not_awaited()
    # A single freshness lookup runs; no scheduled refresh.
    mass.cache.get_with_freshness.assert_awaited_once()
    mass.create_task.assert_not_called()


@pytest.mark.asyncio
async def test_get_playlist_tracks_dynamic_serves_stale_and_refreshes(tmp_path: Any) -> None:
    """A stale-only cache hit is returned immediately and refresh is scheduled."""
    mass = MagicMock()
    mass.storage_path = str(tmp_path)
    stale = [_make_mock_track(str(i), f"library://track/stale-{i}") for i in range(3)]
    # The single freshness lookup returns the expired entry (a stale hit).
    mass.cache.get_with_freshness = AsyncMock(return_value=(stale, False, True))
    mass.cache.set = AsyncMock()
    mass.create_task = MagicMock(side_effect=_swallow_task)
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())
    await plugin.handle_async_init()

    evaluate_mock = AsyncMock(return_value=[])
    cast("Any", plugin)._evaluate_rules = evaluate_mock

    plugin._rules_store["abc"] = SmartPlaylistRules(limit=100, is_dynamic=True)
    result = await plugin.get_playlist_tracks("abc")
    assert result == stale
    # Synchronous evaluation is skipped — the caller gets the stale sample immediately.
    evaluate_mock.assert_not_awaited()
    # A background refresh is scheduled (task_id keeps it deduped across concurrent calls).
    mass.create_task.assert_called_once()
    task_id = mass.create_task.call_args.kwargs.get("task_id")
    assert task_id
    assert "abc" in task_id


@pytest.mark.asyncio
async def test_get_playlist_tracks_static_uses_full_limit(tmp_path: Any) -> None:
    """get_playlist_tracks uses full rules.limit for static (non-dynamic) playlists."""
    mass = MagicMock()
    mass.storage_path = str(tmp_path)
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())
    await plugin.handle_async_init()

    tracks = [_make_mock_track(str(i), f"library://track/{i}") for i in range(50)]
    cast("Any", plugin)._get_library_tracks = AsyncMock(return_value=tracks)

    rules = SmartPlaylistRules(limit=20, is_dynamic=False)
    plugin._rules_store["xyz"] = rules

    result = await plugin.get_playlist_tracks("xyz")
    assert len(result) <= 20
    assert len(result) > 5  # proves limit was not capped at 5


@pytest.mark.asyncio
async def test_get_playlist_resolves_library_id_to_provider_uuid(tmp_path: Any) -> None:
    """get_playlist resolves a library id input to the stored provider UUID."""
    mass = MagicMock()
    mass.storage_path = str(tmp_path)
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())
    await plugin.handle_async_init()

    plugin._rules_store["abc"] = SmartPlaylistRules(limit=10, is_dynamic=True)

    mapping = MagicMock()
    mapping.provider_instance = plugin.instance_id
    mapping.item_id = "abc"
    library_item = MagicMock()
    library_item.provider_mappings = [mapping]
    # Mock get_library_item for resolving "123" -> "abc"
    mass.music.playlists.get_library_item = AsyncMock(return_value=library_item)
    # Mock get_library_item_by_prov_id to return None (no artwork)
    mass.music.playlists.get_library_item_by_prov_id = AsyncMock(return_value=None)

    playlist = await plugin.get_playlist("123")
    assert playlist.item_id == "abc"


@pytest.mark.asyncio
async def test_get_playlist_loads_library_artwork(
    tmp_path: Any,
) -> None:
    """get_playlist loads artwork from the library when available."""
    mass = MagicMock()
    mass.storage_path = str(tmp_path)
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())
    await plugin.handle_async_init()

    plugin._rules_store["abc"] = SmartPlaylistRules(limit=10, is_dynamic=False)

    # Mock library item with artwork
    library_artwork = MediaItemImage(
        type=ImageType.THUMB,
        path="generated_artwork.jpg",
        provider="playlist_art",
        remotely_accessible=False,
    )
    library_playlist = MagicMock()
    library_playlist.metadata.images = UniqueList([library_artwork])

    mass.music.playlists.get_library_item_by_prov_id = AsyncMock(return_value=library_playlist)

    playlist = await plugin.get_playlist("abc")
    assert playlist.metadata.images is not None
    assert len(playlist.metadata.images) == 1
    assert playlist.metadata.images[0].path == "generated_artwork.jpg"
    assert playlist.metadata.images[0].provider == "playlist_art"

    # Verify library lookup was called
    mass.music.playlists.get_library_item_by_prov_id.assert_awaited_once_with(
        "abc", plugin.instance_id
    )


@pytest.mark.asyncio
async def test_get_playlist_tracks_dynamic_uses_resolved_provider_id(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dynamic track fetch uses resolved provider UUID for the cached sample lookup."""
    mass = MagicMock()
    mass.storage_path = str(tmp_path)
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())
    await plugin.handle_async_init()

    plugin._rules_store["abc"] = SmartPlaylistRules(limit=100, is_dynamic=True)

    mapping = MagicMock()
    mapping.provider_instance = plugin.instance_id
    mapping.item_id = "abc"
    library_item = MagicMock()
    library_item.provider_mappings = [mapping]
    # Mock get_library_item for resolving "123" -> "abc"
    mass.music.playlists.get_library_item = AsyncMock(return_value=library_item)
    # Mock get_library_item_by_prov_id to return None (no artwork)
    mass.music.playlists.get_library_item_by_prov_id = AsyncMock(return_value=None)

    expected = [_make_mock_track("1", "library://track/1")]
    cached_dynamic_sample_mock = AsyncMock(return_value=expected)
    monkeypatch.setattr(plugin, "_cached_dynamic_sample", cached_dynamic_sample_mock)

    result = await plugin.get_playlist_tracks("123")

    assert result == expected
    cached_dynamic_sample_mock.assert_awaited_once_with("abc", ())


@pytest.mark.asyncio
async def test_get_playlist_tracks_dynamic_cache_key_differs_by_provider_filter(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different provider filters produce different cache keys for dynamic playlists."""
    mass = MagicMock()
    mass.storage_path = str(tmp_path)
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())
    await plugin.handle_async_init()

    plugin._rules_store["abc"] = SmartPlaylistRules(limit=100, is_dynamic=True)

    cached_dynamic_sample_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(plugin, "_cached_dynamic_sample", cached_dynamic_sample_mock)

    # Call once with no user (no provider filter)
    monkeypatch.setattr("music_assistant.providers.smart_playlist.get_current_user", lambda: None)
    await plugin.get_playlist_tracks("abc")

    # Call again with a user that has a provider filter
    user_with_filter = MagicMock()
    user_with_filter.provider_filter = ["spotify_instance_id", "tidal_instance_id"]
    monkeypatch.setattr(
        "music_assistant.providers.smart_playlist.get_current_user",
        lambda: user_with_filter,
    )
    await plugin.get_playlist_tracks("abc")

    calls = cached_dynamic_sample_mock.await_args_list
    assert len(calls) == 2
    # The second argument (user_provider_filter) must differ between the two calls.
    assert calls[0].args[1] != calls[1].args[1]
    assert calls[0].args[1] == ()
    assert calls[1].args[1] == ("spotify_instance_id", "tidal_instance_id")


@pytest.mark.asyncio
async def test_count_tracks_returns_count_and_duration(tmp_path: Any) -> None:
    """count_tracks returns a dict with count and duration_seconds."""
    mass = MagicMock()
    mass.storage_path = str(tmp_path)
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())
    await plugin.handle_async_init()

    tracks = []
    for i in range(3):
        t = _make_mock_track(str(i), f"library://track/{i}")
        t.duration = 200
        t.last_played = 0  # never played
        tracks.append(t)

    cast("Any", plugin)._get_library_tracks = AsyncMock(return_value=tracks)

    result = await plugin.count_tracks(SmartPlaylistRules(limit=10).to_dict())
    assert result["count"] == 3
    assert result["duration_seconds"] == 600


# ---------------------------------------------------------------------------
# album_type filter tests
# ---------------------------------------------------------------------------


def _make_mock_track_with_album_type(
    item_id: str,
    uri: str,
    album_type: str = "unknown",
) -> MagicMock:
    """Build a minimal mock Track with a unique album.item_id per item_id."""
    track = _make_mock_track(item_id, uri)
    track.album = MagicMock()
    # Unique album ID per track (item_id "1" → album "1000") so library_items mocks are precise.
    track.album.item_id = str(int(item_id) * 1000)
    track.album.year = None
    track.album.album_type = AlbumType(album_type)
    return track


class TestSmartPlaylistRulesAlbumType:
    """Tests for album_types / excluded_album_types fields on SmartPlaylistRules."""

    def test_album_types_defaults_to_empty(self) -> None:
        """album_types and excluded_album_types default to empty lists."""
        rules = SmartPlaylistRules()
        assert rules.album_types == []
        assert rules.excluded_album_types == []

    def test_album_types_round_trip(self) -> None:
        """album_types / excluded_album_types survive a to_dict / from_dict round-trip."""
        rules = SmartPlaylistRules(
            album_types=["album", "ep"],
            excluded_album_types=["single", "compilation"],
        )
        recovered = SmartPlaylistRules.from_dict(rules.to_dict())
        assert recovered.album_types == ["album", "ep"]
        assert recovered.excluded_album_types == ["single", "compilation"]

    def test_old_json_without_album_types_loads_cleanly(self) -> None:
        """Rules JSON without album_types fields deserializes with empty defaults."""
        rules = SmartPlaylistRules.from_dict({"limit": 50, "favorites_only": True})
        assert rules.album_types == []
        assert rules.excluded_album_types == []

    def test_human_readable_includes_album_types(self) -> None:
        """human_readable mentions album_types when set."""
        rules = SmartPlaylistRules(album_types=["album", "ep"])
        summary = rules.human_readable()
        assert "album" in summary
        assert "ep" in summary

    def test_human_readable_includes_excluded_album_types(self) -> None:
        """human_readable mentions excluded_album_types when set."""
        rules = SmartPlaylistRules(excluded_album_types=["single"])
        assert "single" in rules.human_readable()


class TestAlbumTypeValidation:
    """Tests for album_type validation in validate_rules."""

    def _make_plugin(self) -> SmartPlaylistProvider:
        mass = MagicMock()
        manifest = MagicMock()
        manifest.domain = "smart_playlist"
        config = MagicMock()
        config.get_value.return_value = "GLOBAL"
        return SmartPlaylistProvider(mass, manifest, config, set())

    def test_valid_album_types_pass(self) -> None:
        """All AlbumType values are valid."""
        plugin = self._make_plugin()
        rules = SmartPlaylistRules(
            album_types=["album", "single", "ep", "live", "soundtrack", "compilation"]
        )
        plugin._validate_rules(rules)  # must not raise

    def test_invalid_album_type_raises(self) -> None:
        """Unknown album_type value raises InvalidDataError."""
        plugin = self._make_plugin()
        rules = SmartPlaylistRules(album_types=["not_a_real_type"])
        with pytest.raises(InvalidDataError, match="album_types"):
            plugin._validate_rules(rules)

    def test_invalid_excluded_album_type_raises(self) -> None:
        """Unknown excluded_album_type value raises InvalidDataError."""
        plugin = self._make_plugin()
        rules = SmartPlaylistRules(excluded_album_types=["bogus"])
        with pytest.raises(InvalidDataError, match="excluded_album_types"):
            plugin._validate_rules(rules)


@pytest.mark.asyncio
async def test_evaluate_rules_album_types_filter() -> None:
    """album_types filter keeps only tracks whose album ID is in the allowed set."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    album_track = _make_mock_track_with_album_type("1", "library://track/1", "album")
    single_track = _make_mock_track_with_album_type("2", "library://track/2", "single")
    unknown_track = _make_mock_track_with_album_type("3", "library://track/3", "unknown")

    for t in (album_track, single_track, unknown_track):
        t.available = True
        t.last_played = 0
        t.metadata = MagicMock()
        t.metadata.genres = None

    cast("Any", plugin)._get_library_tracks = AsyncMock(
        return_value=[album_track, single_track, unknown_track]
    )
    cast("Any", plugin)._enrich_tracks_with_db_genres = AsyncMock(return_value=None)
    # albums.library_items returns only the "album" type album (album_track.album.item_id = "1000")
    mock_album = MagicMock()
    mock_album.item_id = "1000"
    mass.music.albums.library_items = AsyncMock(return_value=[mock_album])

    rules = SmartPlaylistRules(album_types=["album"], limit=10)
    result = await plugin._evaluate_rules(rules)
    uris = [t.uri for t in result]
    assert "library://track/1" in uris  # album → included
    assert "library://track/2" not in uris  # single → excluded
    assert "library://track/3" not in uris  # unknown album type → excluded


@pytest.mark.asyncio
async def test_evaluate_rules_excluded_album_types_filter() -> None:
    """excluded_album_types removes tracks whose album.album_type is in the exclusion list."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    album_track = _make_mock_track_with_album_type("1", "library://track/1", "album")
    single_track = _make_mock_track_with_album_type("2", "library://track/2", "single")

    for t in (album_track, single_track):
        t.available = True
        t.last_played = 0
        t.metadata = MagicMock()
        t.metadata.genres = None

    cast("Any", plugin)._get_library_tracks = AsyncMock(return_value=[album_track, single_track])
    cast("Any", plugin)._enrich_tracks_with_db_genres = AsyncMock(return_value=None)
    # albums.library_items returns only the "single" type album (single_track.album.item_id = "2000")
    mock_album = MagicMock()
    mock_album.item_id = "2000"
    mass.music.albums.library_items = AsyncMock(return_value=[mock_album])

    rules = SmartPlaylistRules(excluded_album_types=["single"], limit=10)
    result = await plugin._evaluate_rules(rules)
    uris = [t.uri for t in result]
    assert "library://track/1" in uris  # album → kept
    assert "library://track/2" not in uris  # single → excluded


@pytest.mark.asyncio
async def test_seed_mode_album_types_filter_is_applied() -> None:
    """album_types filter is enforced in seed mode via _apply_seed_post_filters."""
    mass = MagicMock()
    mass.music.genres.get_library_item = AsyncMock(side_effect=Exception("not called"))
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    album_track = _make_mock_track_with_album_type("1", "library://track/1", "album")
    single_track = _make_mock_track_with_album_type("2", "library://track/2", "single")

    for t in (album_track, single_track):
        t.available = True
        t.last_played = 0
        t.metadata = MagicMock()
        t.metadata.popularity = None
        t.metadata.genres = None
        t.favorite = False

    # Seed mode is triggered when seed_track_uris is non-empty.
    # Mock _tracks_from_seeds to return mixed album types.
    cast("Any", plugin)._tracks_from_seeds = AsyncMock(return_value=[album_track, single_track])
    cast("Any", plugin)._enrich_tracks_with_db_genres = AsyncMock(return_value=None)
    # albums.library_items returns only the "album" type album (album_track.album.item_id = "1000")
    mock_album = MagicMock()
    mock_album.item_id = "1000"
    mass.music.albums.library_items = AsyncMock(return_value=[mock_album])

    rules = SmartPlaylistRules(
        seed_track_uris=["library://track/99"],
        album_types=["album"],
        limit=10,
    )
    result = await plugin._evaluate_rules(rules)
    uris = [t.uri for t in result]
    assert "library://track/1" in uris  # album → kept
    assert "library://track/2" not in uris  # single → filtered out by _apply_seed_post_filters


# ---------------------------------------------------------------------------
# Database genre enrichment tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_tracks_with_db_genres_adds_missing_genres() -> None:
    """_enrich_tracks_with_db_genres should query DB and add genres to tracks without them."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    # Track with no metadata.genres
    track_no_genres = Track(
        item_id="123",
        provider="library",
        name="Track Without Genres",
        uri="library://track/123",
        provider_mappings={
            ProviderMapping(
                item_id="123",
                provider_domain="library",
                provider_instance="library",
                available=True,
            )
        },
    )
    track_no_genres.metadata = MediaItemMetadata()
    track_no_genres.metadata.genres = None

    # Mock genre controller response: track 123 has genres "Rock" and "Alternative"
    rock_genre = Genre(item_id="1", provider="library", name="Rock", provider_mappings=set())
    alternative_genre = Genre(
        item_id="2", provider="library", name="Alternative", provider_mappings=set()
    )
    mass.music.genres.get_genres_for_media_item = AsyncMock(
        return_value=[rock_genre, alternative_genre]
    )

    await plugin._enrich_tracks_with_db_genres([track_no_genres])

    genres = track_no_genres.metadata.genres
    assert genres == {"Rock", "Alternative"}
    mass.music.genres.get_genres_for_media_item.assert_called_once()  # type: ignore[unreachable]


@pytest.mark.asyncio
async def test_enrich_tracks_with_db_genres_skips_tracks_with_existing_genres() -> None:
    """Tracks that already have genres should not be queried."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    # Track with existing genres
    track_with_genres = Track(
        item_id="456",
        provider="library",
        name="Track With Genres",
        uri="library://track/456",
        provider_mappings={
            ProviderMapping(
                item_id="456",
                provider_domain="library",
                provider_instance="library",
                available=True,
            )
        },
    )
    track_with_genres.metadata = MediaItemMetadata()
    track_with_genres.metadata.genres = {"Pop", "Dance"}

    mass.music.genres.get_genres_for_media_item = AsyncMock()

    await plugin._enrich_tracks_with_db_genres([track_with_genres])

    # Should not query genres controller since track already has genres
    mass.music.genres.get_genres_for_media_item.assert_not_called()
    assert track_with_genres.metadata.genres == {"Pop", "Dance"}


@pytest.mark.asyncio
async def test_enrich_tracks_with_db_genres_only_queries_library_tracks() -> None:
    """Non-library tracks (streaming) should not be queried."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    # Streaming track (item_id is not a digit string)
    streaming_track = Track(
        item_id="spotify:track:abc123",
        provider="spotify",
        name="Streaming Track",
        uri="spotify://track/abc123",
        provider_mappings={
            ProviderMapping(
                item_id="spotify:track:abc123",
                provider_domain="spotify",
                provider_instance="spotify_instance",
                available=True,
            )
        },
    )

    mass.music.genres.get_genres_for_media_item = AsyncMock()
    mass.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=None)

    await plugin._enrich_tracks_with_db_genres([streaming_track])

    # Should not query genres controller for non-library tracks
    mass.music.genres.get_genres_for_media_item.assert_not_called()


@pytest.mark.asyncio
async def test_enrich_tracks_with_db_genres_handles_empty_list() -> None:
    """Empty track list should return immediately without querying."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    mass.music.genres.get_genres_for_media_item = AsyncMock()

    await plugin._enrich_tracks_with_db_genres([])

    mass.music.genres.get_genres_for_media_item.assert_not_called()


@pytest.mark.asyncio
async def test_seed_mode_enriches_genres_when_excluded_genres_present() -> None:
    """Seed mode should enrich genres when excluded_genre_ids or excluded_genre_names are set."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    # Create a library track without genres
    track = MagicMock()
    track.item_id = "123"
    track.uri = "library://track/123"
    track.provider = "library"
    track.available = True
    track.metadata = MagicMock()
    track.metadata.genres = None
    track.metadata.popularity = 50

    # Mock _tracks_from_seeds to return our test track
    cast("Any", plugin)._tracks_from_seeds = AsyncMock(return_value=[track])

    # Mock _enrich_tracks_with_db_genres to verify it gets called
    enrich_mock = AsyncMock()
    cast("Any", plugin)._enrich_tracks_with_db_genres = enrich_mock

    # Mock required methods
    cast("Any", plugin)._resolve_excluded_genre_names = AsyncMock(return_value={"rock"})
    cast("Any", plugin)._get_album_ids_for_types = AsyncMock(return_value=[])
    cast("Any", plugin)._apply_exclusions = MagicMock(return_value=[track])
    cast("Any", plugin)._deduplicate_tracks = MagicMock(return_value=[track])

    # Test with excluded_genre_ids only (no included genres)
    rules = SmartPlaylistRules(
        seed_track_uris=["library://track/99"],
        excluded_genre_ids=[1],
        limit=10,
    )
    await plugin._evaluate_rules(rules)

    # Verify enrichment was called in seed mode
    enrich_mock.assert_called_once()


@pytest.mark.asyncio
async def test_enrich_tracks_with_db_genres_handles_duplicate_item_ids() -> None:
    """Multiple Track objects with the same item_id should all be enriched."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    # Create two different Track objects with the same library item_id
    mapping1 = ProviderMapping(
        item_id="123",
        provider_domain="library",
        provider_instance="library",
        available=True,
    )
    track1 = Track(
        item_id="spotify:track:abc",
        provider="spotify",
        name="Track 1",
        uri="spotify://track/abc",
        provider_mappings={mapping1},
    )
    track1.metadata = MediaItemMetadata()
    track1.metadata.genres = None

    mapping2 = ProviderMapping(
        item_id="123",
        provider_domain="library",
        provider_instance="library",
        available=True,
    )
    track2 = Track(
        item_id="spotify:track:def",
        provider="spotify",
        name="Track 2",
        uri="spotify://track/def",
        provider_mappings={mapping2},
    )
    track2.metadata = MediaItemMetadata()
    track2.metadata.genres = None

    # Mock genre controller response
    rock_genre = Genre(item_id="1", provider="library", name="Rock", provider_mappings=set())
    alternative_genre = Genre(
        item_id="2", provider="library", name="Alternative", provider_mappings=set()
    )
    mass.music.genres.get_genres_for_media_item = AsyncMock(
        return_value=[rock_genre, alternative_genre]
    )
    library_track = MagicMock()
    library_track.item_id = "123"
    mass.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=library_track)

    await plugin._enrich_tracks_with_db_genres([track1, track2])

    # Both tracks should have been enriched
    assert track1.metadata.genres == {"Rock", "Alternative"}
    genres2 = track2.metadata.genres  # type: ignore[unreachable]
    assert genres2 == {"Rock", "Alternative"}


# ---------------------------------------------------------------------------
# AI-generated description tests
# ---------------------------------------------------------------------------


def _make_ai_provider(
    response: str = "A mellow mix for the evening.", instance_id: str = "ai--1"
) -> MagicMock:
    """Build a mock plugin provider exposing one AI engine returning the given response."""
    provider = MagicMock(spec=PluginProvider)
    provider.instance_id = instance_id
    provider.ai_query = AsyncMock(return_value=response)
    provider.get_ai_engines = AsyncMock(
        return_value=[AIEngine(id="engine", name=instance_id, provider=provider)]
    )
    return provider


def _make_ai_plugin(
    tmp_path: Any,
    *,
    ai_enabled: bool = True,
    ai_provider: Any = None,
    ai_engine: str | None = None,
) -> SmartPlaylistProvider:
    """
    Build a SmartPlaylistProvider wired for AI-description tests.

    :param ai_engine: The stored engine selection; defaults to the given provider's engine,
        as the load-time seeding would have stored it. Pass "" to start out unseeded.
    """
    mass = MagicMock()
    mass.storage_path = str(tmp_path)
    mass.cache.clear = AsyncMock()
    mass.metadata.locale = "en_US"
    mass.music.playlists.get_library_item_by_prov_id = AsyncMock(return_value=None)
    providers = [ai_provider] if ai_provider is not None else []
    mass.get_providers_supporting_feature = MagicMock(return_value=providers)
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    if ai_engine is None and ai_provider is not None:
        ai_engine = f"{ai_provider.instance_id}/engine"
    config_values: dict[str, Any] = {
        CONF_AI_DESCRIPTIONS: ai_enabled,
        CONF_AI_ENGINE: ai_engine or None,
    }
    config.get_value.side_effect = lambda key, *_args: config_values.get(key, "GLOBAL")
    mass.config.get_raw_provider_config_value.side_effect = lambda _instance_id, key, default=None: (
        config_values.get(key, default)
    )
    mass.config.set_raw_provider_config_value.side_effect = (
        lambda _instance_id, key, value, **_kwargs: config_values.__setitem__(key, value)
    )
    return SmartPlaylistProvider(mass, manifest, config, set())


def _make_library_item(plugin: SmartPlaylistProvider, prov_id: str, db_id: int = 7) -> MagicMock:
    """Build a mock library playlist item mapped back to the given provider id."""
    mapping = MagicMock()
    mapping.provider_instance = plugin.instance_id
    mapping.item_id = prov_id
    library_item = MagicMock()
    library_item.item_id = db_id
    library_item.provider_mappings = [mapping]
    return library_item


def _capture_scheduled(names: list[str]) -> Any:
    """Return a create_task side-effect that records scheduled coroutine names and closes them."""

    def _side_effect(coro: Any, **_: Any) -> None:
        names.append(coro.cr_code.co_name)
        coro.close()

    return _side_effect


@pytest.mark.asyncio
async def test_generate_ai_description_uses_provider(tmp_path: Any) -> None:
    """When enabled and a provider is available, the AI response is returned."""
    ai_provider = _make_ai_provider("Chill evening vibes.")
    plugin = _make_ai_plugin(tmp_path, ai_enabled=True, ai_provider=ai_provider)

    rules = SmartPlaylistRules(favorites_only=True)
    result = await plugin._generate_ai_description("Evening Chill", rules)

    assert result == "Chill evening vibes."
    ai_provider.ai_query.assert_awaited_once()
    prompt = ai_provider.ai_query.await_args.args[0]
    assert "Evening Chill" in prompt
    assert "Favorites only" in prompt


@pytest.mark.asyncio
async def test_generate_ai_description_includes_locale(tmp_path: Any) -> None:
    """The configured locale is passed to the provider so it answers in that language."""
    ai_provider = _make_ai_provider("Een rustige mix voor de avond.")
    plugin = _make_ai_plugin(tmp_path, ai_enabled=True, ai_provider=ai_provider)
    cast("Any", plugin.mass).metadata.locale = "nl_NL"

    await plugin._generate_ai_description("Avond Chill", SmartPlaylistRules(favorites_only=True))

    prompt = ai_provider.ai_query.await_args.args[0]
    assert "nl_NL" in prompt


@pytest.mark.asyncio
async def test_generate_ai_description_disabled_returns_none(tmp_path: Any) -> None:
    """With the toggle off, the AI provider is never called."""
    ai_provider = _make_ai_provider()
    plugin = _make_ai_plugin(tmp_path, ai_enabled=False, ai_provider=ai_provider)

    result = await plugin._generate_ai_description("X", SmartPlaylistRules())

    assert result is None
    ai_provider.ai_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_generate_ai_description_no_provider_returns_none(tmp_path: Any) -> None:
    """With no AI_QUERY provider available, None is returned."""
    plugin = _make_ai_plugin(tmp_path, ai_enabled=True, ai_provider=None)

    result = await plugin._generate_ai_description("X", SmartPlaylistRules())

    assert result is None
    cast("Any", plugin.mass).get_providers_supporting_feature.assert_called_once_with(
        ProviderFeature.AI_QUERY, priority=(ProviderType.PLUGIN,)
    )


@pytest.mark.asyncio
async def test_generate_ai_description_provider_error_returns_none(tmp_path: Any) -> None:
    """A failing AI provider falls back to None instead of raising."""
    ai_provider = _make_ai_provider()
    ai_provider.ai_query = AsyncMock(side_effect=Exception("boom"))
    plugin = _make_ai_plugin(tmp_path, ai_enabled=True, ai_provider=ai_provider)

    result = await plugin._generate_ai_description("X", SmartPlaylistRules(favorites_only=True))

    assert result is None


@pytest.mark.asyncio
async def test_generate_ai_description_stalled_provider_returns_none(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A stalled AI provider gives up instead of leaving the task pending forever."""
    monkeypatch.setattr("music_assistant.providers.smart_playlist.AI_QUERY_TIMEOUT_SECONDS", 0.01)

    async def _answers_too_late(*_args: Any, **_kwargs: Any) -> str:
        await asyncio.sleep(5)
        return "A mellow mix for the evening."

    ai_provider = _make_ai_provider()
    ai_provider.ai_query = AsyncMock(side_effect=_answers_too_late)
    plugin = _make_ai_plugin(tmp_path, ai_enabled=True, ai_provider=ai_provider)
    caplog.set_level(logging.DEBUG, logger=plugin.logger.name)

    result = await plugin._generate_ai_description("X", SmartPlaylistRules(favorites_only=True))

    assert result is None
    assert "no response within" in caplog.text


@pytest.mark.asyncio
async def test_generate_ai_description_reports_a_provider_side_timeout_as_a_failure(
    tmp_path: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A timeout raised by the provider itself is not reported as our own cap."""
    ai_provider = _make_ai_provider()
    ai_provider.ai_query = AsyncMock(side_effect=TimeoutError)
    plugin = _make_ai_plugin(tmp_path, ai_enabled=True, ai_provider=ai_provider)
    caplog.set_level(logging.DEBUG, logger=plugin.logger.name)

    result = await plugin._generate_ai_description("X", SmartPlaylistRules(favorites_only=True))

    assert result is None
    assert "no response within" not in caplog.text


@pytest.mark.asyncio
async def test_generate_ai_description_stays_on_the_configured_engine(tmp_path: Any) -> None:
    """A failing selection yields no description instead of asking another engine."""
    failing = _make_ai_provider(instance_id="ai--bad")
    failing.ai_query = AsyncMock(side_effect=Exception("boom"))
    other = _make_ai_provider("Second provider result.", instance_id="ai--good")
    plugin = _make_ai_plugin(tmp_path, ai_enabled=True, ai_engine="ai--bad/engine")
    cast("Any", plugin.mass).get_providers_supporting_feature = MagicMock(
        return_value=[failing, other]
    )

    result = await plugin._generate_ai_description("X", SmartPlaylistRules(favorites_only=True))

    assert result is None
    failing.ai_query.assert_awaited_once()
    other.ai_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_use_adopts_a_concrete_engine_selection(tmp_path: Any) -> None:
    """An instance without a stored selection adopts one on first use, not at load."""
    ai_provider = _make_ai_provider("Chill evening vibes.")
    plugin = _make_ai_plugin(tmp_path, ai_provider=ai_provider, ai_engine="")
    await plugin.handle_async_init()
    mass = cast("Any", plugin.mass)
    mass.config.set_raw_provider_config_value.assert_not_called()

    assert (
        await plugin._generate_ai_description("Evening Chill", SmartPlaylistRules())
        == "Chill evening vibes."
    )

    assert mass.config.set_raw_provider_config_value.call_args.args == (
        plugin.instance_id,
        CONF_AI_ENGINE,
        "ai--1/engine",
    )


@pytest.mark.asyncio
async def test_disabled_toggle_does_not_schedule_refresh(tmp_path: Any) -> None:
    """With the toggle off, creating a playlist schedules no background AI refresh."""
    plugin = _make_ai_plugin(tmp_path, ai_enabled=False)
    await plugin.handle_async_init()
    mass = cast("Any", plugin.mass)
    mass.music.playlists.add_item_to_library = AsyncMock(return_value=MagicMock())
    scheduled: list[str] = []
    mass.create_task = MagicMock(side_effect=_capture_scheduled(scheduled))

    await plugin.create_smart_playlist("Evening Chill", {"favorites_only": True})

    assert scheduled == []


@pytest.mark.asyncio
async def test_generate_ai_description_blank_response_returns_none(tmp_path: Any) -> None:
    """A blank/whitespace AI response is treated as no description."""
    plugin = _make_ai_plugin(tmp_path, ai_enabled=True, ai_provider=_make_ai_provider("   "))

    result = await plugin._generate_ai_description("X", SmartPlaylistRules(favorites_only=True))

    assert result is None


@pytest.mark.asyncio
async def test_generate_ai_description_oversized_response_returns_none(tmp_path: Any) -> None:
    """A reply beyond the size cap is discarded."""
    oversized = "x" * (MAX_AI_DESCRIPTION_BYTES + 1)
    plugin = _make_ai_plugin(tmp_path, ai_enabled=True, ai_provider=_make_ai_provider(oversized))

    result = await plugin._generate_ai_description("X", SmartPlaylistRules(favorites_only=True))

    assert result is None


@pytest.mark.asyncio
async def test_generate_ai_description_accepts_a_response_at_the_size_cap(tmp_path: Any) -> None:
    """A reply exactly at the size cap is still accepted."""
    at_cap = "x" * MAX_AI_DESCRIPTION_BYTES
    plugin = _make_ai_plugin(tmp_path, ai_enabled=True, ai_provider=_make_ai_provider(at_cap))

    result = await plugin._generate_ai_description("X", SmartPlaylistRules(favorites_only=True))

    assert result == at_cap


@pytest.mark.asyncio
async def test_generate_ai_description_measures_the_cap_in_bytes(tmp_path: Any) -> None:
    """The cap counts utf-8 bytes, so multibyte replies are not measured as characters."""
    # under the cap as characters, over it as utf-8 bytes
    multibyte = "あ" * (MAX_AI_DESCRIPTION_BYTES // 2)
    plugin = _make_ai_plugin(tmp_path, ai_enabled=True, ai_provider=_make_ai_provider(multibyte))

    result = await plugin._generate_ai_description("X", SmartPlaylistRules(favorites_only=True))

    assert result is None


@pytest.mark.asyncio
async def test_build_playlist_uses_stored_ai_description(tmp_path: Any) -> None:
    """_build_playlist uses the stored AI description verbatim (no prefix)."""
    plugin = _make_ai_plugin(tmp_path)
    await plugin.handle_async_init()
    plugin._names_store["abc"] = "My List"
    plugin._descriptions_store["abc"] = "Hand-crafted AI summary."

    playlist = await plugin._build_playlist("abc", SmartPlaylistRules(favorites_only=True))

    assert playlist.metadata.description == "Hand-crafted AI summary."


@pytest.mark.asyncio
async def test_build_playlist_ignores_stored_description_when_disabled(tmp_path: Any) -> None:
    """With the toggle off, a stored AI description is ignored in favour of the summary."""
    plugin = _make_ai_plugin(tmp_path, ai_enabled=False)
    await plugin.handle_async_init()
    plugin._names_store["abc"] = "My List"
    plugin._descriptions_store["abc"] = "Old AI text."
    rules = SmartPlaylistRules(favorites_only=True)

    playlist = await plugin._build_playlist("abc", rules)

    assert playlist.metadata.description == f"[Smart Playlist] {rules.human_readable()}"


@pytest.mark.asyncio
async def test_build_playlist_falls_back_to_human_readable(tmp_path: Any) -> None:
    """Without a stored AI description, _build_playlist uses the mechanical summary."""
    plugin = _make_ai_plugin(tmp_path)
    await plugin.handle_async_init()
    plugin._names_store["abc"] = "My List"
    rules = SmartPlaylistRules(favorites_only=True)

    playlist = await plugin._build_playlist("abc", rules)

    assert playlist.metadata.description == f"[Smart Playlist] {rules.human_readable()}"


@pytest.mark.asyncio
async def test_ai_description_persists_to_disk(tmp_path: Any) -> None:
    """A stored AI description survives a plugin reload."""
    rules_dir = tmp_path / "smart_playlists"
    rules_dir.mkdir()

    plugin = _make_ai_plugin(tmp_path)
    await plugin.handle_async_init()
    plugin._rules_dir = str(rules_dir)
    plugin._names_store["42"] = "Name"
    plugin._descriptions_store["42"] = "Persisted AI text."
    await plugin._save_rules("42", SmartPlaylistRules(genre_ids=[1]))

    plugin2 = _make_ai_plugin(tmp_path)
    await plugin2.handle_async_init()
    plugin2._rules_dir = str(rules_dir)
    plugin2._rules_store = {}
    plugin2._names_store = {}
    plugin2._descriptions_store = {}
    await plugin2._load_rules_from_disk()

    assert plugin2._descriptions_store.get("42") == "Persisted AI text."


@pytest.mark.asyncio
async def test_write_json_preserves_original_on_failure(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed atomic replace must leave the existing file intact, never truncated."""
    target = tmp_path / "rules.json"
    await write_json(str(target), {"value": "original"})

    def _boom(*_: Any, **__: Any) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("music_assistant.providers.smart_playlist.helpers.Path.replace", _boom)
    with pytest.raises(OSError, match="replace failed"):
        await write_json(str(target), {"value": "new"})

    assert json.loads(target.read_text()) == {"value": "original"}
    # The temp file must be cleaned up so it can't accumulate on repeated failures.
    assert not (tmp_path / "rules.json.tmp").exists()


@pytest.mark.asyncio
async def test_write_json_cleans_temp_on_cancellation(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation during the write must not leave a temp file behind or corrupt the original."""
    target = tmp_path / "rules.json"
    await write_json(str(target), {"value": "original"})

    def _cancel(*_: Any, **__: Any) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr("music_assistant.providers.smart_playlist.helpers.Path.replace", _cancel)
    with pytest.raises(asyncio.CancelledError):
        await write_json(str(target), {"value": "new"})

    assert json.loads(target.read_text()) == {"value": "original"}
    assert not (tmp_path / "rules.json.tmp").exists()


@pytest.mark.asyncio
async def test_update_playlist_description_skips_when_unchanged(tmp_path: Any) -> None:
    """No library write/event when the description already matches."""
    plugin = _make_ai_plugin(tmp_path)
    await plugin.handle_async_init()
    existing = MagicMock()
    existing.metadata.description = "Same text."
    mass = cast("Any", plugin.mass)
    mass.music.playlists.get_library_item = AsyncMock(return_value=existing)
    mass.music.playlists.update_item_in_library = AsyncMock()

    await plugin._update_playlist_description(7, "Same text.")

    mass.music.playlists.update_item_in_library.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_playlist_description_writes_when_changed(tmp_path: Any) -> None:
    """The library item is rewritten only when the description actually differs."""
    plugin = _make_ai_plugin(tmp_path)
    await plugin.handle_async_init()
    existing = Playlist(
        item_id="1",
        provider="library",
        name="P",
        provider_mappings={
            ProviderMapping(item_id="1", provider_domain="library", provider_instance="library")
        },
    )
    existing.metadata = MediaItemMetadata(description="Old text.")
    mass = cast("Any", plugin.mass)
    mass.music.playlists.get_library_item = AsyncMock(return_value=existing)
    mass.music.playlists.update_item_in_library = AsyncMock()

    await plugin._update_playlist_description(7, "New text.")

    mass.music.playlists.update_item_in_library.assert_awaited_once()
    written = mass.music.playlists.update_item_in_library.await_args.args[1]
    assert written.metadata.description == "New text."


@pytest.mark.asyncio
async def test_create_smart_playlist_schedules_ai_generation(tmp_path: Any) -> None:
    """Creating a smart playlist schedules background AI description generation."""
    plugin = _make_ai_plugin(tmp_path)
    await plugin.handle_async_init()
    mass = cast("Any", plugin.mass)
    mass.music.playlists.add_item_to_library = AsyncMock(return_value=MagicMock())
    scheduled: list[str] = []
    mass.create_task = MagicMock(side_effect=_capture_scheduled(scheduled))

    await plugin.create_smart_playlist("Evening Chill", {"favorites_only": True})

    assert scheduled == ["_refresh_ai_description"]
    # Deduped per playlist so rapid calls don't run concurrent refreshes.
    kwargs = mass.create_task.call_args.kwargs
    assert kwargs["task_id"].startswith("smart_playlist_ai_desc_")
    assert kwargs["abort_existing"] is True


@pytest.mark.asyncio
async def test_update_rules_drops_stale_and_schedules_regeneration(tmp_path: Any) -> None:
    """Updating rules clears the stale AI description, sets the fallback, and regenerates."""
    plugin = _make_ai_plugin(tmp_path)
    await plugin.handle_async_init()
    plugin._rules_store["abc"] = SmartPlaylistRules(favorites_only=True)
    plugin._names_store["abc"] = "Name"
    plugin._descriptions_store["abc"] = "Stale AI text."
    mass = cast("Any", plugin.mass)
    scheduled: list[str] = []
    mass.create_task = MagicMock(side_effect=_capture_scheduled(scheduled))
    mass.music.playlists.get_library_item_by_prov_id = AsyncMock(
        return_value=_make_library_item(plugin, "abc")
    )
    cast("Any", plugin)._update_playlist_description = AsyncMock()

    await plugin.update_smart_playlist_rules("abc", {"genre_ids": [1]})

    assert "abc" not in plugin._descriptions_store
    # The stale description must also be invalidated on disk, not just in memory, so it
    # cannot be reloaded after a restart before the background refresh runs.
    persisted = json.loads((tmp_path / "smart_playlists" / RULES_FILENAME).read_text())
    assert persisted["abc"]["ai_description"] is None
    scheduled_desc = cast("Any", plugin)._update_playlist_description.await_args.args[1]
    assert scheduled_desc.startswith("[Smart Playlist]")
    assert scheduled == ["_refresh_ai_description"]
    assert mass.create_task.call_args.kwargs == {
        "task_id": "smart_playlist_ai_desc_abc",
        "abort_existing": True,
    }


@pytest.mark.asyncio
async def test_update_rules_skips_metadata_refresh_if_unchanged(tmp_path: Any) -> None:
    """Updating rules with identical values does not trigger metadata refresh."""
    plugin = _make_ai_plugin(tmp_path)
    await plugin.handle_async_init()
    initial_rules = SmartPlaylistRules(favorites_only=True, genre_ids=[1])
    plugin._rules_store["abc"] = initial_rules
    plugin._names_store["abc"] = "Name"
    mass = cast("Any", plugin.mass)
    mass.music.playlists.get_library_item_by_prov_id = AsyncMock(
        return_value=_make_library_item(plugin, "abc")
    )
    cast("Any", plugin)._update_playlist_description = AsyncMock()
    mass.create_task = MagicMock()
    mass.call_later = MagicMock()

    # Update with identical rules
    await plugin.update_smart_playlist_rules("abc", {"favorites_only": True, "genre_ids": [1]})

    # Should not trigger metadata refresh since rules didn't change
    mass.call_later.assert_not_called()


@pytest.mark.asyncio
async def test_update_rules_triggers_metadata_refresh_if_changed(tmp_path: Any) -> None:
    """Updating rules with different values triggers metadata refresh."""
    plugin = _make_ai_plugin(tmp_path)
    await plugin.handle_async_init()
    initial_rules = SmartPlaylistRules(favorites_only=True)
    plugin._rules_store["abc"] = initial_rules
    plugin._names_store["abc"] = "Name"
    mass = cast("Any", plugin.mass)
    library_item = _make_library_item(plugin, "abc")
    mass.music.playlists.get_library_item_by_prov_id = AsyncMock(return_value=library_item)
    cast("Any", plugin)._update_playlist_description = AsyncMock()
    mass.create_task = MagicMock()
    mass.call_later = MagicMock()

    # Update with different rules
    await plugin.update_smart_playlist_rules("abc", {"genre_ids": [1]})

    # Should trigger metadata refresh since rules changed
    mass.call_later.assert_called_once()
    args = mass.call_later.call_args
    assert args[0][0] == 5  # delay
    assert args[0][1] == mass.metadata.update_metadata  # function
    assert args[0][2] == library_item  # library_item
    assert args[1]["task_id"] == "smart_playlist_metadata_refresh_abc"
    assert args[1]["force_refresh"] is True


@pytest.mark.asyncio
async def test_refresh_ai_description_stores_and_updates(tmp_path: Any) -> None:
    """The background refresh stores the AI text and pushes it to the library item."""
    plugin = _make_ai_plugin(tmp_path, ai_provider=_make_ai_provider("Fresh AI summary."))
    await plugin.handle_async_init()
    plugin._rules_store["abc"] = SmartPlaylistRules(favorites_only=True)
    plugin._names_store["abc"] = "Name"
    cast("Any", plugin.mass).music.playlists.get_library_item_by_prov_id = AsyncMock(
        return_value=_make_library_item(plugin, "abc")
    )
    cast("Any", plugin)._update_playlist_description = AsyncMock()

    await plugin._refresh_ai_description("abc")

    assert plugin._descriptions_store["abc"] == "Fresh AI summary."
    cast("Any", plugin)._update_playlist_description.assert_awaited_once()
    assert (
        cast("Any", plugin)._update_playlist_description.await_args.args[1] == "Fresh AI summary."
    )


@pytest.mark.asyncio
async def test_refresh_ai_description_no_provider_uses_fallback(tmp_path: Any) -> None:
    """With no AI available, the refresh drops any stale text and writes the fallback."""
    plugin = _make_ai_plugin(tmp_path, ai_provider=None)
    await plugin.handle_async_init()
    rules = SmartPlaylistRules(favorites_only=True)
    plugin._rules_store["abc"] = rules
    plugin._names_store["abc"] = "Name"
    plugin._descriptions_store["abc"] = "Stale."
    cast("Any", plugin.mass).music.playlists.get_library_item_by_prov_id = AsyncMock(
        return_value=_make_library_item(plugin, "abc")
    )
    cast("Any", plugin)._update_playlist_description = AsyncMock()

    await plugin._refresh_ai_description("abc")

    assert "abc" not in plugin._descriptions_store
    written = cast("Any", plugin)._update_playlist_description.await_args.args[1]
    assert written == f"[Smart Playlist] {rules.human_readable()}"


@pytest.mark.asyncio
async def test_refresh_ai_description_oversized_reply_uses_fallback(tmp_path: Any) -> None:
    """An oversized reply drops the stored text and writes the fallback."""
    oversized = "x" * (MAX_AI_DESCRIPTION_BYTES + 1)
    plugin = _make_ai_plugin(tmp_path, ai_provider=_make_ai_provider(oversized))
    await plugin.handle_async_init()
    rules = SmartPlaylistRules(favorites_only=True)
    plugin._rules_store["abc"] = rules
    plugin._names_store["abc"] = "Name"
    plugin._descriptions_store["abc"] = "Good text."
    cast("Any", plugin.mass).music.playlists.get_library_item_by_prov_id = AsyncMock(
        return_value=_make_library_item(plugin, "abc")
    )
    cast("Any", plugin)._update_playlist_description = AsyncMock()

    await plugin._refresh_ai_description("abc")

    assert "abc" not in plugin._descriptions_store
    written = cast("Any", plugin)._update_playlist_description.await_args.args[1]
    assert written == f"[Smart Playlist] {rules.human_readable()}"


@pytest.mark.asyncio
async def test_load_rules_from_disk_drops_an_unusable_description(tmp_path: Any) -> None:
    """Only a short, textual persisted description is adopted on load."""
    plugin = _make_ai_plugin(tmp_path)
    await plugin.handle_async_init()
    await write_json(
        str(tmp_path / "smart_playlists" / RULES_FILENAME),
        {
            # both unusable entries come first, so an entry that raises instead of being
            # skipped would abort the load and cost the good entry below it
            "abc": {
                "name": "Name",
                "rules": SmartPlaylistRules(favorites_only=True).to_dict(),
                "ai_description": "x" * (MAX_AI_DESCRIPTION_BYTES + 1),
            },
            "def": {
                "name": "Corrupt",
                "rules": SmartPlaylistRules(favorites_only=True).to_dict(),
                "ai_description": {"not": "a string"},
            },
            "ghi": {
                "name": "Other",
                "rules": SmartPlaylistRules(favorites_only=True).to_dict(),
                "ai_description": "Short and fine.",
            },
        },
    )
    plugin._rules_store.clear()
    plugin._descriptions_store.clear()

    await plugin._load_rules_from_disk()

    assert set(plugin._rules_store) == {"abc", "def", "ghi"}
    assert "abc" not in plugin._descriptions_store
    assert "def" not in plugin._descriptions_store
    assert plugin._descriptions_store["ghi"] == "Short and fine."


@pytest.mark.asyncio
async def test_refresh_ai_description_skips_flush_when_unchanged(tmp_path: Any) -> None:
    """No rules-file flush when the stored description doesn't change (e.g. no AI provider)."""
    plugin = _make_ai_plugin(tmp_path, ai_provider=None)
    await plugin.handle_async_init()
    plugin._rules_store["abc"] = SmartPlaylistRules(favorites_only=True)
    plugin._names_store["abc"] = "Name"  # no stored description to begin with
    cast("Any", plugin)._flush_rules_to_disk = AsyncMock()
    cast("Any", plugin)._update_playlist_description = AsyncMock()
    cast("Any", plugin.mass).music.playlists.get_library_item_by_prov_id = AsyncMock(
        return_value=_make_library_item(plugin, "abc")
    )

    await plugin._refresh_ai_description("abc")

    cast("Any", plugin)._flush_rules_to_disk.assert_not_awaited()


# ---------------------------------------------------------------------------
# Genre AND logic filtering tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_tracks_with_all_genres_filters_correctly() -> None:  # noqa: PLR0915
    """_filter_tracks_with_all_genres returns only tracks that have ALL required genres."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    # Create mock tracks:
    # Track 1: Library track (direct)
    track_1 = _make_mock_track("1", uri="library://track/101")
    track_1.provider = "library"
    track_1.item_id = "101"

    # Track 2: Spotify track with library mapping
    track_2 = _make_mock_track("2", uri="spotify://track/abc")
    track_2.provider = "spotify"
    track_2.item_id = "abc"
    track_2_mapping = MagicMock()
    track_2_mapping.provider_domain = "spotify"
    track_2_mapping.provider_instance = None
    track_2_mapping.item_id = "abc"  # Provider's item ID
    track_2.provider_mappings = [track_2_mapping]

    # Track 3: Apple Music track with library mapping
    track_3 = _make_mock_track("3", uri="apple_music://track/xyz")
    track_3.provider = "apple_music"
    track_3.item_id = "xyz"
    track_3_mapping = MagicMock()
    track_3_mapping.provider_domain = "apple_music"
    track_3_mapping.provider_instance = None
    track_3_mapping.item_id = "xyz"  # Provider's item ID
    track_3.provider_mappings = [track_3_mapping]

    tracks = [track_1, track_2, track_3]

    # Mock get_library_item_by_prov_id to resolve provider items to library items
    async def mock_get_library_item(
        item_id: str, provider_instance_id_or_domain: str
    ) -> MagicMock | None:
        if provider_instance_id_or_domain == "spotify" and item_id == "abc":
            lib_track = MagicMock()
            lib_track.item_id = "102"  # Library DB ID
            return lib_track
        if provider_instance_id_or_domain == "apple_music" and item_id == "xyz":
            lib_track = MagicMock()
            lib_track.item_id = "103"  # Library DB ID
            return lib_track
        return None

    cast("Any", plugin.mass.music.tracks).get_library_item_by_prov_id = mock_get_library_item

    # Mock genres for each track:
    # Track 101: has genres 10 and 20 (matches requirement)
    # Track 102: has only genre 10 (missing 20, should be filtered out)
    # Track 103: has genres 10, 20, and 30 (matches requirement with extra genre)
    async def mock_get_genres(_media_type: Any, media_id: int) -> list[MagicMock]:
        genre_10 = MagicMock()
        genre_10.item_id = "10"
        genre_20 = MagicMock()
        genre_20.item_id = "20"
        genre_30 = MagicMock()
        genre_30.item_id = "30"

        if media_id == 101:
            return [genre_10, genre_20]
        if media_id == 102:
            return [genre_10]
        if media_id == 103:
            return [genre_10, genre_20, genre_30]
        return []

    cast("Any", plugin.mass.music.genres).get_genres_for_media_item = mock_get_genres

    # Require genres 10 AND 20
    result = await plugin._filter_tracks_with_all_genres(cast("Any", tracks), [10, 20])

    # Only tracks 1 and 3 should be returned (have both genres 10 and 20)
    assert len(result) == 2
    result_uris = {t.uri for t in result}
    assert "library://track/101" in result_uris
    assert "apple_music://track/xyz" in result_uris
    assert "spotify://track/abc" not in result_uris


@pytest.mark.asyncio
async def test_filter_tracks_with_all_genres_preserves_order() -> None:
    """_filter_tracks_with_all_genres preserves the original track order."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    # Create tracks in specific order: 103, 101, 102
    track_3 = _make_mock_track("3", uri="library://track/103")
    track_3.provider = "library"
    track_3.item_id = "103"

    track_1 = _make_mock_track("1", uri="spotify://track/abc")
    track_1.provider = "spotify"
    track_1.item_id = "abc"
    track_1_mapping = MagicMock()
    track_1_mapping.provider_domain = "spotify"
    track_1_mapping.provider_instance = None
    track_1_mapping.item_id = "abc"  # Provider's item ID
    track_1.provider_mappings = [track_1_mapping]

    track_2 = _make_mock_track("2", uri="apple_music://track/xyz")
    track_2.provider = "apple_music"
    track_2.item_id = "xyz"
    track_2_mapping = MagicMock()
    track_2_mapping.provider_domain = "apple_music"
    track_2_mapping.provider_instance = None
    track_2_mapping.item_id = "xyz"  # Provider's item ID
    track_2.provider_mappings = [track_2_mapping]

    tracks = [track_3, track_1, track_2]

    # Mock get_library_item_by_prov_id to resolve provider items to library items
    async def mock_get_library_item(
        item_id: str, provider_instance_id_or_domain: str
    ) -> MagicMock | None:
        if provider_instance_id_or_domain == "spotify" and item_id == "abc":
            lib_track = MagicMock()
            lib_track.item_id = "101"  # Library DB ID
            return lib_track
        if provider_instance_id_or_domain == "apple_music" and item_id == "xyz":
            lib_track = MagicMock()
            lib_track.item_id = "102"  # Library DB ID
            return lib_track
        return None

    cast("Any", plugin.mass.music.tracks).get_library_item_by_prov_id = mock_get_library_item

    # All three tracks have both required genres
    async def mock_get_genres(_media_type: Any, _media_id: int) -> list[MagicMock]:
        genre_10 = MagicMock()
        genre_10.item_id = "10"
        genre_20 = MagicMock()
        genre_20.item_id = "20"
        return [genre_10, genre_20]

    cast("Any", plugin.mass.music.genres).get_genres_for_media_item = mock_get_genres

    result = await plugin._filter_tracks_with_all_genres(cast("Any", tracks), [10, 20])

    # Order should be preserved: track 3, track 1, track 2
    assert len(result) == 3
    assert result[0].uri == "library://track/103"
    assert result[1].uri == "spotify://track/abc"
    assert result[2].uri == "apple_music://track/xyz"


@pytest.mark.asyncio
async def test_filter_tracks_with_all_genres_handles_non_numeric_genre_ids() -> None:
    """_filter_tracks_with_all_genres ignores genres with non-numeric IDs."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    track_1 = _make_mock_track("1", uri="library://track/101")
    track_1.provider = "library"
    track_1.item_id = "101"

    tracks = [track_1]

    # Return a mix of numeric and non-numeric genre IDs
    async def mock_get_genres(_media_type: Any, _media_id: int) -> list[MagicMock]:
        genre_10 = MagicMock()
        genre_10.item_id = "10"
        genre_20 = MagicMock()
        genre_20.item_id = "20"
        genre_invalid = MagicMock()
        genre_invalid.item_id = "bandcamp:123-456"  # non-numeric
        return [genre_10, genre_20, genre_invalid]

    cast("Any", plugin.mass.music.genres).get_genres_for_media_item = mock_get_genres

    # Should still match because track has genres 10 and 20 (ignoring the invalid one)
    result = await plugin._filter_tracks_with_all_genres(cast("Any", tracks), [10, 20])

    assert len(result) == 1
    assert result[0].uri == "library://track/101"


# ---------------------------------------------------------------------------
# get_playlist_tracks — recency track filter (dynamic playlists only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dynamic_playlist_applies_recency_filter() -> None:
    """A dynamic smart playlist drops filter-rejected tracks at the boundary."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    rules = SmartPlaylistRules(is_dynamic=True)
    cast("Any", plugin)._resolve_rules_for_playlist_id = AsyncMock(return_value=("42", rules))
    sample = [_make_mock_track("keep"), _make_mock_track("drop")]
    cast("Any", plugin)._cached_dynamic_sample = AsyncMock(return_value=sample)

    with track_filter(lambda track: track.item_id != "drop"):
        result = await plugin.get_playlist_tracks("42")

    assert [track.item_id for track in result] == ["keep"]


@pytest.mark.asyncio
async def test_static_playlist_ignores_recency_filter() -> None:
    """A non-dynamic smart playlist is evaluated as-is and never recency-filtered."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    rules = SmartPlaylistRules(is_dynamic=False)
    cast("Any", plugin)._resolve_rules_for_playlist_id = AsyncMock(return_value=("42", rules))
    evaluated = [_make_mock_track("keep"), _make_mock_track("drop")]
    cast("Any", plugin)._evaluate_rules = AsyncMock(return_value=evaluated)

    with track_filter(lambda track: track.item_id != "drop"):
        result = await plugin.get_playlist_tracks("42")

    assert [track.item_id for track in result] == ["keep", "drop"]
