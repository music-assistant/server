"""Tests for the Smart Playlist plugin provider."""

from __future__ import annotations

import time as _time
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.smart_playlist import (
    SmartPlaylistProvider,
)
from music_assistant.providers.smart_playlist.helpers import (
    LOGIC_AND,
    LOGIC_OR,
    SmartPlaylistRules,
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
        assert rules.seed_track_uri is None
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
            seed_track_uri="library://track/42",
            min_popularity=50,
            logic=LOGIC_OR,
            limit=25,
        )
        recovered = SmartPlaylistRules.from_dict(original.to_dict())
        assert recovered == original

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
) -> MagicMock:
    """Build a minimal mock Track object."""
    track = MagicMock()
    track.item_id = item_id
    track.uri = uri
    track.name = f"Track {item_id}"
    track.favorite = favorite

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
    cast("Any", plugin)._get_similar_tracks = AsyncMock(return_value=[])

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
    cast("Any", plugin)._get_similar_tracks = AsyncMock(return_value=[])

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
    cast("Any", plugin)._get_similar_tracks = AsyncMock(return_value=[])

    rules = SmartPlaylistRules(limit=5)
    result = await plugin._evaluate_rules(rules)
    assert len(result) <= 5


# ---------------------------------------------------------------------------
# New feature tests: seed_artist, exclusions, dedup, validation, count_tracks
# ---------------------------------------------------------------------------


class TestNewValidation:
    """Validate new mutual-exclusion and range checks."""

    def _make_plugin(self) -> SmartPlaylistProvider:
        mass = MagicMock()
        manifest = MagicMock()
        manifest.domain = "smart_playlist"
        config = MagicMock()
        config.get_value.return_value = "GLOBAL"
        return SmartPlaylistProvider(mass, manifest, config, set())

    def test_seed_track_and_seed_artist_mutually_exclusive(self) -> None:
        """Setting both seed_track_uri and seed_artist_uri raises InvalidDataError."""
        plugin = self._make_plugin()
        rules = SmartPlaylistRules(
            seed_track_uri="library://track/1",
            seed_artist_uri="library://artist/2",
        )
        with pytest.raises(InvalidDataError, match="mutually exclusive"):
            plugin._validate_rules(rules)

    def test_dedup_hours_out_of_range_raises(self) -> None:
        """dedup_hours outside 1-8760 raises InvalidDataError."""
        plugin = self._make_plugin()
        with pytest.raises(InvalidDataError, match="dedup_hours"):
            plugin._validate_rules(SmartPlaylistRules(dedup_hours=0))
        with pytest.raises(InvalidDataError, match="dedup_hours"):
            plugin._validate_rules(SmartPlaylistRules(dedup_hours=9000))

    def test_dedup_hours_valid_passes(self) -> None:
        """dedup_hours within 1-8760 does not raise."""
        plugin = self._make_plugin()
        plugin._validate_rules(SmartPlaylistRules(dedup_hours=24))  # should not raise


@pytest.mark.asyncio
async def test_seed_artist_uses_similar_artists_tracks() -> None:
    """seed_artist_uri calls _get_similar_artists_tracks instead of _get_similar_tracks."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    similar_tracks = [_make_mock_track("10", "library://track/10")]
    cast("Any", plugin)._get_similar_artists_tracks = AsyncMock(return_value=similar_tracks)
    cast("Any", plugin)._get_similar_tracks = AsyncMock(return_value=[])

    rules = SmartPlaylistRules(seed_artist_uri="library://artist/5", limit=10)
    result = await plugin._evaluate_rules(rules)

    cast("Any", plugin)._get_similar_artists_tracks.assert_awaited_once()
    cast("Any", plugin)._get_similar_tracks.assert_not_awaited()
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


@pytest.mark.asyncio
async def test_dedup_removes_recently_played() -> None:
    """Tracks played within dedup_hours are excluded; others are kept."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    recent = _make_mock_track("1", "library://track/1")
    recent.last_played = int(_time.time() - 60)  # 1 minute ago

    old = _make_mock_track("2", "library://track/2")
    old.last_played = int(_time.time() - 7200)  # 2 hours ago

    never = _make_mock_track("3", "library://track/3")
    never.last_played = 0  # never played

    cast("Any", plugin)._get_library_tracks = AsyncMock(return_value=[recent, old, never])

    rules = SmartPlaylistRules(dedup_hours=1, limit=2)  # limit <= non-recent count
    result = await plugin._evaluate_rules(rules)
    uris = {t.uri for t in result}
    assert "library://track/1" not in uris
    assert "library://track/2" in uris
    assert "library://track/3" in uris


@pytest.mark.asyncio
async def test_dedup_fallback_when_pool_exhausted() -> None:
    """When all tracks were recently played, dedup is ignored and the full pool is returned."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())

    tracks = []
    for i in range(5):
        t = _make_mock_track(str(i), f"library://track/{i}")
        t.last_played = int(_time.time() - 30)  # 30 sec ago
        tracks.append(t)

    cast("Any", plugin)._get_library_tracks = AsyncMock(return_value=tracks)

    rules = SmartPlaylistRules(dedup_hours=1, limit=5)
    result = await plugin._evaluate_rules(rules)
    # Pool exhausted → fallback to full pool
    assert len(result) == 5


@pytest.mark.asyncio
async def test_get_playlist_tracks_dynamic_limit_5(tmp_path: Any) -> None:
    """get_playlist_tracks uses limit=5 for dynamic playlists."""
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

    rules = SmartPlaylistRules(limit=100, is_dynamic=True)
    plugin._rules_store["abc"] = rules

    result = await plugin.get_playlist_tracks("abc")
    assert len(result) <= 5


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
