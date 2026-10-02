# ruff: noqa: RUF003
"""Tests for provider/dialogs_player.py — content resolver + play wrapper."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import QueueOption

from music_assistant.providers.yandex_alice.dialogs_nlu import ParsedCommand
from music_assistant.providers.yandex_alice.dialogs_player import play_for_alice, resolve_query

# ---------------------------------------------------------------------------
# resolve_query
# ---------------------------------------------------------------------------


@dataclass
class _SearchResults:
    artists: list[object] = field(default_factory=list)
    albums: list[object] = field(default_factory=list)
    tracks: list[object] = field(default_factory=list)
    playlists: list[object] = field(default_factory=list)


def _make_mass(search_results: _SearchResults | None = None) -> MagicMock:
    mass = MagicMock()
    mass.music = MagicMock()
    mass.music.search = AsyncMock(return_value=search_results or _SearchResults())
    mass.music_providers = []
    mass.providers = []
    mass.player_queues = MagicMock()
    mass.player_queues.play_media = AsyncMock()
    mass.players = MagicMock()
    mass.players.get_player = MagicMock(return_value=None)
    mass.players.cmd_power = AsyncMock()
    return mass


@pytest.mark.asyncio
class TestResolveQuery:
    """Tests for resolve_query — content resolver dispatching by ParsedCommand.kind."""

    async def test_track(self) -> None:
        """kind=track returns the first track search result."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(_SearchResults(tracks=[track]))
        result = await resolve_query(mass, ParsedCommand(kind="track", query="yesterday"))
        assert result is track
        mass.music.search.assert_awaited_once()

    async def test_artist(self) -> None:
        """kind=artist returns the first artist search result."""
        artist = MagicMock(uri="library://artist/1", spec_set=["uri"])
        mass = _make_mass(_SearchResults(artists=[artist]))
        result = await resolve_query(
            mass, ParsedCommand(kind="artist", query="metallica", radio_mode=True)
        )
        assert result is artist

    async def test_album(self) -> None:
        """kind=album returns the first album search result."""
        album = MagicMock(uri="library://album/1", spec_set=["uri"])
        mass = _make_mass(_SearchResults(albums=[album]))
        result = await resolve_query(mass, ParsedCommand(kind="album", query="black album"))
        assert result is album

    async def test_playlist(self) -> None:
        """kind=playlist returns the first playlist search result."""
        playlist = MagicMock(uri="library://playlist/1", spec_set=["uri"])
        mass = _make_mass(_SearchResults(playlists=[playlist]))
        result = await resolve_query(mass, ParsedCommand(kind="playlist", query="rock"))
        assert result is playlist

    async def test_search_kind_prefers_artist(self) -> None:
        """
        kind=search prefers artist over playlist/track for unqualified queries.

        Users typically say a band/artist name without a "плейлист" /
        "альбом" qualifier ("включи Iron Maiden"). Picking the artist
        (with radio_mode=True downstream) matches the intent better than
        starting an unrelated playlist that happens to contain the query.
        """
        artist = MagicMock(uri="library://artist/1", spec_set=["uri"])
        playlist = MagicMock(uri="library://playlist/1", spec_set=["uri"])
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(_SearchResults(artists=[artist], playlists=[playlist], tracks=[track]))
        result = await resolve_query(mass, ParsedCommand(kind="search", query="iron maiden"))
        assert result is artist

    async def test_search_no_results_returns_none(self) -> None:
        """Empty search results return None."""
        mass = _make_mass(_SearchResults())
        result = await resolve_query(mass, ParsedCommand(kind="search", query="nope"))
        assert result is None

    async def test_my_wave_no_provider_returns_none(self) -> None:
        """kind=my_wave without yandex_music provider returns None."""
        mass = _make_mass()
        result = await resolve_query(mass, ParsedCommand(kind="my_wave", query="", radio_mode=True))
        assert result is None

    async def test_my_wave_success_returns_provider_track_uri(self) -> None:
        """The Yandex Music rotor success path returns a playable provider URI."""
        mass = _make_mass()
        provider = MagicMock(domain="yandex_music", available=True, instance_id="ym-main")
        provider.domain = "yandex_music"
        provider.available = True
        provider.instance_id = "ym-main"
        provider.client.get_rotor_station_tracks = AsyncMock(
            return_value=([MagicMock(id="track-1")], "batch-1")
        )
        mass.music_providers = [provider]

        result = await resolve_query(mass, ParsedCommand(kind="my_wave", query="", radio_mode=True))

        assert result == "ym-main://track/track-1"
        provider.client.get_rotor_station_tracks.assert_awaited_once_with("user:onyourwave")

    async def test_genre_success_returns_provider_track_uri(self) -> None:
        """A genre rotor hit avoids the generic MA search fallback."""
        mass = _make_mass()
        provider = MagicMock(domain="yandex_music", available=True, instance_id="ym-main")
        provider.domain = "yandex_music"
        provider.available = True
        provider.instance_id = "ym-main"
        provider.client.get_rotor_station_tracks = AsyncMock(
            return_value=([MagicMock(id="genre-track")], "batch-2")
        )
        mass.music_providers = [provider]

        result = await resolve_query(
            mass, ParsedCommand(kind="genre", query="rock", radio_mode=True)
        )

        assert result == "ym-main://track/genre-track"
        provider.client.get_rotor_station_tracks.assert_awaited_once_with("genre:rock")
        mass.music.search.assert_not_awaited()

    async def test_search_failure_returns_none(self) -> None:
        """Search exception is swallowed and returns None."""
        mass = _make_mass()
        mass.music.search = AsyncMock(side_effect=RuntimeError("boom"))
        result = await resolve_query(mass, ParsedCommand(kind="track", query="x"))
        assert result is None

    async def test_cyrillic_query_retries_with_stemmed_form(self) -> None:
        """First search empty + Cyrillic query → retry with inflection stripped."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        empty = _SearchResults()
        hit = _SearchResults(tracks=[track])
        mass = _make_mass(empty)
        # First call returns empty, second returns the track.
        mass.music.search = AsyncMock(side_effect=[empty, hit])
        result = await resolve_query(mass, ParsedCommand(kind="track", query="металлику"))
        assert result is track
        # Two calls were made.
        assert mass.music.search.await_count == 2
        # Second call used the stemmed query ("металлик" — last `у` stripped).
        second_call_kwargs = mass.music.search.await_args_list[1].kwargs
        assert second_call_kwargs["search_query"] == "металлик"

    async def test_ascii_query_does_not_retry(self) -> None:
        """ASCII-only query is not retried — stemming has no effect."""
        empty = _SearchResults()
        mass = _make_mass(empty)
        mass.music.search = AsyncMock(return_value=empty)
        result = await resolve_query(mass, ParsedCommand(kind="track", query="metallica"))
        assert result is None
        assert mass.music.search.await_count == 1

    async def test_retry_skipped_when_stemmed_equals_original(self) -> None:
        """Russian query already in stemmed form (short word) doesn't trigger retry."""
        empty = _SearchResults()
        mass = _make_mass(empty)
        mass.music.search = AsyncMock(return_value=empty)
        # "рок" (3 chars) — too short for the suffix-strip to produce a different stem.
        result = await resolve_query(mass, ParsedCommand(kind="search", query="рок"))
        assert result is None
        assert mass.music.search.await_count == 1


# ---------------------------------------------------------------------------
# play_for_alice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPlayForAlice:
    """Tests for play_for_alice — power-on + play_media orchestration."""

    async def test_no_player_object_still_plays(self) -> None:
        """When player object is missing, play_media is called without cmd_power."""
        mass = _make_mass()
        await play_for_alice(mass, "p1", "library://track/1", radio_mode=False)
        mass.players.cmd_power.assert_not_awaited()
        mass.player_queues.play_media.assert_awaited_once_with(
            queue_id="p1", media="library://track/1", radio_mode=False
        )

    async def test_powers_on_when_off(self) -> None:
        """Player with power feature and powered=False gets cmd_power before play."""
        mass = _make_mass()
        player = MagicMock()
        player.supported_features = {"power"}
        player.powered = False
        mass.players.get_player = MagicMock(return_value=player)
        await play_for_alice(mass, "p1", "library://track/1", radio_mode=False)
        mass.players.cmd_power.assert_awaited_once_with("p1", True)
        mass.player_queues.play_media.assert_awaited_once()

    async def test_skips_power_when_already_on(self) -> None:
        """Player already powered=True does not get cmd_power."""
        mass = _make_mass()
        player = MagicMock()
        player.supported_features = {"power"}
        player.powered = True
        mass.players.get_player = MagicMock(return_value=player)
        await play_for_alice(mass, "p1", "library://track/1", radio_mode=False)
        mass.players.cmd_power.assert_not_awaited()

    async def test_radio_mode_passed_through(self) -> None:
        """radio_mode=True is forwarded to play_media."""
        mass = _make_mass()
        await play_for_alice(mass, "p1", "library://artist/1", radio_mode=True)
        mass.player_queues.play_media.assert_awaited_once_with(
            queue_id="p1", media="library://artist/1", radio_mode=True
        )

    @pytest.mark.parametrize(
        ("enqueue_option", "expected"),
        [
            ("add", QueueOption.ADD),
            ("next", QueueOption.NEXT),
            ("replace", QueueOption.REPLACE),
        ],
    )
    async def test_enqueue_option_mapping(self, enqueue_option: str, expected: QueueOption) -> None:
        """Every supported manifest enqueue value maps to the MA QueueOption enum."""
        mass = _make_mass()

        await play_for_alice(
            mass,
            "p1",
            "library://track/1",
            enqueue_option=enqueue_option,
        )

        assert mass.player_queues.play_media.await_args.kwargs["option"] == expected

    async def test_unknown_enqueue_option_is_not_forwarded(self) -> None:
        """An unknown future manifest value cannot leak into the MA controller call."""
        mass = _make_mass()

        await play_for_alice(
            mass,
            "p1",
            "library://track/1",
            enqueue_option="unsupported",
        )

        assert "option" not in mass.player_queues.play_media.await_args.kwargs
