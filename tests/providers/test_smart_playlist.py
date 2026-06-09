"""Tests for the Smart Playlist plugin provider."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import AlbumType, ProviderFeature
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import Playlist, ProviderMapping, Track
from music_assistant_models.media_items.metadata import MediaItemMetadata

from music_assistant.constants import DYNAMIC_PLAYLIST_SAMPLE_SIZE
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.smart_playlist import (
    CONF_AI_DESCRIPTIONS,
    SmartPlaylistProvider,
)
from music_assistant.providers.smart_playlist.helpers import (
    LOGIC_AND,
    LOGIC_OR,
    RULES_FILENAME,
    SmartPlaylistRules,
    write_json,
)

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
) -> MagicMock:
    """Build a minimal mock Track object."""
    track = MagicMock()
    track.item_id = item_id
    track.uri = uri
    track.name = f"Track {item_id}"
    track.favorite = favorite

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

    def test_dedup_hours_out_of_range_raises(self) -> None:
        """dedup_hours outside 1-2160 raises InvalidDataError."""
        plugin = self._make_plugin()
        with pytest.raises(InvalidDataError, match="dedup_hours"):
            plugin._validate_rules(SmartPlaylistRules(dedup_hours=0))
        with pytest.raises(InvalidDataError, match="dedup_hours"):
            plugin._validate_rules(SmartPlaylistRules(dedup_hours=9000))

    def test_dedup_hours_valid_passes(self) -> None:
        """dedup_hours within 1-2160 does not raise."""
        plugin = self._make_plugin()
        plugin._validate_rules(SmartPlaylistRules(dedup_hours=24))  # should not raise

    def test_dedup_hours_above_retention_raises(self) -> None:
        """dedup_hours beyond the 90-day (2160h) playlog retention raises."""
        plugin = self._make_plugin()
        with pytest.raises(InvalidDataError, match="dedup_hours"):
            plugin._validate_rules(SmartPlaylistRules(dedup_hours=2161))
        plugin._validate_rules(SmartPlaylistRules(dedup_hours=2160))  # boundary, should not raise


@pytest.mark.asyncio
async def test_seed_mode_delegates_to_dynamic_radio_helper() -> None:
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
    cast("Any", plugin)._get_library_tracks.assert_not_awaited()
    assert len(result) == 1


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


def _make_played_mapping(provider: str, item_id: str) -> MagicMock:
    """Build a minimal mock ItemMapping as returned by recently_played."""
    mapping = MagicMock()
    mapping.provider = provider
    mapping.item_id = item_id
    return mapping


@pytest.mark.asyncio
async def test_dedup_removes_recently_played() -> None:
    """Tracks present in the playlog within dedup_hours are excluded; others are kept."""
    mass = MagicMock()
    mass.music.recently_played = AsyncMock(return_value=[_make_played_mapping("library", "1")])
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    recent = _make_mock_track("1", "library://track/1")
    old = _make_mock_track("2", "library://track/2")
    never = _make_mock_track("3", "library://track/3")

    cast("Any", plugin)._get_library_tracks = AsyncMock(return_value=[recent, old, never])

    rules = SmartPlaylistRules(dedup_hours=1, limit=2)  # limit <= non-recent count
    result = await plugin._evaluate_rules(rules)
    uris = {t.uri for t in result}
    assert "library://track/1" not in uris
    assert "library://track/2" in uris
    assert "library://track/3" in uris


@pytest.mark.asyncio
async def test_dedup_removes_recently_played_streaming_track() -> None:
    """A non-library (streaming) track in the playlog is excluded via its provider mapping."""
    mass = MagicMock()
    mass.music.recently_played = AsyncMock(
        return_value=[_make_played_mapping("spotify--abc", "s1")]
    )
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    played = _make_mock_track("s1", "spotify://track/s1", provider_instance="spotify--abc")
    fresh = _make_mock_track("s2", "spotify://track/s2", provider_instance="spotify--abc")
    cast("Any", plugin)._get_library_tracks = AsyncMock(return_value=[played, fresh])

    rules = SmartPlaylistRules(dedup_hours=1, limit=1)
    result = await plugin._evaluate_rules(rules)
    uris = {t.uri for t in result}
    assert "spotify://track/s1" not in uris
    assert "spotify://track/s2" in uris


@pytest.mark.asyncio
async def test_dedup_fallback_when_pool_exhausted() -> None:
    """When all tracks were recently played, dedup is ignored and the full pool is returned."""
    mass = MagicMock()
    tracks = [_make_mock_track(str(i), f"library://track/{i}") for i in range(5)]
    mass.music.recently_played = AsyncMock(
        return_value=[_make_played_mapping("library", str(i)) for i in range(5)]
    )
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    cast("Any", plugin)._get_library_tracks = AsyncMock(return_value=tracks)

    rules = SmartPlaylistRules(dedup_hours=1, limit=5)
    result = await plugin._evaluate_rules(rules)
    # Pool exhausted → fallback to full pool
    assert len(result) == 5


@pytest.mark.asyncio
async def test_dedup_partial_fill_prefers_old_library_over_streaming() -> None:
    """Partial-exhaustion fill must not rank streaming tracks (last_played=0) as oldest."""
    mass = MagicMock()
    mass.music.recently_played = AsyncMock(
        return_value=[
            _make_played_mapping("library", "lo"),
            _make_played_mapping("spotify--abc", "sn"),
        ]
    )
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    never = _make_mock_track("n", "library://track/n")  # not in playlog -> survives dedup
    lib_old = _make_mock_track("lo", "library://track/lo")
    lib_old.last_played = 100  # genuinely old play
    stream_new = _make_mock_track("sn", "spotify://track/sn", provider_instance="spotify--abc")
    stream_new.last_played = 0  # streaming track: no library timestamp

    cast("Any", plugin)._get_library_tracks = AsyncMock(return_value=[never, lib_old, stream_new])

    # limit=2: `never` fills one slot, the remaining slot is filled from the
    # recently-played remainder; the old library track should win over the
    # just-played streaming track.
    rules = SmartPlaylistRules(dedup_hours=1, limit=2)
    result = await plugin._evaluate_rules(rules)
    uris = {t.uri for t in result}
    assert "library://track/n" in uris
    assert "library://track/lo" in uris
    assert "spotify://track/sn" not in uris


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
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock()
    mass.create_task = MagicMock(side_effect=_swallow_task)
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
    # Observable behaviour: the wrapped evaluator ran and a store task was scheduled.
    library_mock.assert_awaited()
    mass.create_task.assert_called_once()


@pytest.mark.asyncio
async def test_get_playlist_tracks_dynamic_returns_fresh_cache(tmp_path: Any) -> None:
    """A fresh cache hit short-circuits evaluation and does not touch the stale path."""
    mass = MagicMock()
    mass.storage_path = str(tmp_path)
    cached = [_make_mock_track(str(i), f"library://track/cached-{i}") for i in range(3)]
    mass.cache.get = AsyncMock(return_value=cached)
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
    # Only the fresh lookup runs; no stale lookup, no scheduled refresh.
    mass.cache.get.assert_awaited_once()
    mass.create_task.assert_not_called()


@pytest.mark.asyncio
async def test_get_playlist_tracks_dynamic_serves_stale_and_refreshes(tmp_path: Any) -> None:
    """A stale-only cache hit is returned immediately and refresh is scheduled."""
    mass = MagicMock()
    mass.storage_path = str(tmp_path)
    stale = [_make_mock_track(str(i), f"library://track/stale-{i}") for i in range(3)]
    # First (fresh) lookup misses, second (stale-allowed) lookup returns the expired entry.
    mass.cache.get = AsyncMock(side_effect=[None, stale])
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
    mass.music.playlists.get_library_item = AsyncMock(return_value=library_item)

    playlist = await plugin.get_playlist("123")
    assert playlist.item_id == "abc"


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
    mass.music.playlists.get_library_item = AsyncMock(return_value=library_item)

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
# AI-generated description tests
# ---------------------------------------------------------------------------


def _make_ai_provider(response: str = "A mellow mix for the evening.") -> MagicMock:
    """Build a mock plugin provider that supports ai_query and returns the given response."""
    provider = MagicMock(spec=PluginProvider)
    provider.ai_query = AsyncMock(return_value=response)
    return provider


def _make_ai_plugin(
    tmp_path: Any,
    *,
    ai_enabled: bool = True,
    ai_provider: Any = None,
) -> SmartPlaylistProvider:
    """Build a SmartPlaylistProvider wired for AI-description tests."""
    mass = MagicMock()
    mass.storage_path = str(tmp_path)
    mass.cache.clear = AsyncMock()
    mass.metadata.locale = "en_US"
    providers = [ai_provider] if ai_provider is not None else []
    mass.get_providers_supporting_feature = MagicMock(return_value=providers)
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.side_effect = lambda key, *_args: (
        ai_enabled if key == CONF_AI_DESCRIPTIONS else "GLOBAL"
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
        ProviderFeature.AI_QUERY
    )


@pytest.mark.asyncio
async def test_generate_ai_description_provider_error_returns_none(tmp_path: Any) -> None:
    """A failing AI provider falls back to None instead of raising."""
    ai_provider = MagicMock(spec=PluginProvider)
    ai_provider.ai_query = AsyncMock(side_effect=Exception("boom"))
    plugin = _make_ai_plugin(tmp_path, ai_enabled=True, ai_provider=ai_provider)

    result = await plugin._generate_ai_description("X", SmartPlaylistRules(favorites_only=True))

    assert result is None


@pytest.mark.asyncio
async def test_generate_ai_description_falls_back_to_next_provider(tmp_path: Any) -> None:
    """If the first provider errors, the next available provider is tried."""
    bad = MagicMock(spec=PluginProvider)
    bad.ai_query = AsyncMock(side_effect=Exception("boom"))
    good = _make_ai_provider("Second provider result.")
    plugin = _make_ai_plugin(tmp_path, ai_enabled=True)
    cast("Any", plugin.mass).get_providers_supporting_feature = MagicMock(return_value=[bad, good])

    result = await plugin._generate_ai_description("X", SmartPlaylistRules(favorites_only=True))

    assert result == "Second provider result."
    bad.ai_query.assert_awaited_once()
    good.ai_query.assert_awaited_once()


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
async def test_build_playlist_uses_stored_ai_description(tmp_path: Any) -> None:
    """_build_playlist uses the stored AI description verbatim (no prefix)."""
    plugin = _make_ai_plugin(tmp_path)
    await plugin.handle_async_init()
    plugin._names_store["abc"] = "My List"
    plugin._descriptions_store["abc"] = "Hand-crafted AI summary."

    playlist = plugin._build_playlist("abc", SmartPlaylistRules(favorites_only=True))

    assert playlist.metadata.description == "Hand-crafted AI summary."


@pytest.mark.asyncio
async def test_build_playlist_ignores_stored_description_when_disabled(tmp_path: Any) -> None:
    """With the toggle off, a stored AI description is ignored in favour of the summary."""
    plugin = _make_ai_plugin(tmp_path, ai_enabled=False)
    await plugin.handle_async_init()
    plugin._names_store["abc"] = "My List"
    plugin._descriptions_store["abc"] = "Old AI text."
    rules = SmartPlaylistRules(favorites_only=True)

    playlist = plugin._build_playlist("abc", rules)

    assert playlist.metadata.description == f"[Smart Playlist] {rules.human_readable()}"


@pytest.mark.asyncio
async def test_build_playlist_falls_back_to_human_readable(tmp_path: Any) -> None:
    """Without a stored AI description, _build_playlist uses the mechanical summary."""
    plugin = _make_ai_plugin(tmp_path)
    await plugin.handle_async_init()
    plugin._names_store["abc"] = "My List"
    rules = SmartPlaylistRules(favorites_only=True)

    playlist = plugin._build_playlist("abc", rules)

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
