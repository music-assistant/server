"""Tests for the Smart Playlist plugin provider."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import ProviderMapping, Track

from music_assistant.constants import DYNAMIC_PLAYLIST_SAMPLE_SIZE
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
