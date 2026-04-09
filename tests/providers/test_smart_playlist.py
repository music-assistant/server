"""Tests for the Smart Playlist plugin provider."""

from __future__ import annotations

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
    plugin._rules_dir = str(rules_dir)
    plugin._rules_store = {}
    plugin._names_store = {}

    rules = SmartPlaylistRules(genre_ids=[1, 2], favorites_only=True)
    await plugin._save_rules("42", rules)

    # Simulate reload
    plugin2 = SmartPlaylistProvider(mass, manifest, config, set())
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
