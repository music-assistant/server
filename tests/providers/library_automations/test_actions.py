"""Tests for the Library Automations action handlers (actions.py)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Album, Artist, Track

from music_assistant.providers.library_automations import actions
from music_assistant.providers.library_automations.models import (
    AutomationAction,
    AutomationRule,
    AutomationTrigger,
)


def _make_track(item_id: str) -> Track:
    track = Track(
        item_id=item_id, provider="library", name=f"Track {item_id}", provider_mappings=set()
    )
    track.media_type = MediaType.TRACK
    return track


def _make_rule(action_type: str, params: dict[str, object] | None = None) -> AutomationRule:
    return AutomationRule(
        id="rule-1",
        name="test rule",
        trigger=AutomationTrigger(type="media_item_unfavorited", media_types=["track"]),
        action=AutomationAction(type=action_type, params=params or {}),
    )


async def test_add_to_playlist_creates_missing_playlist_and_caches_id() -> None:
    """When the target playlist doesn't exist yet, it is created and the id cached on the rule."""
    provider = MagicMock()
    provider.persist_rule = AsyncMock()
    provider.mass.music.playlists.get_library_item = AsyncMock(
        side_effect=MediaNotFoundError("not found")
    )
    provider.mass.music.playlists.library_items = AsyncMock(return_value=[])
    provider.mass.music.playlists.create_playlist = AsyncMock(return_value=MagicMock(item_id="55"))
    provider.mass.music.playlists._handle_add_playlist_tracks = AsyncMock()

    rule = _make_rule("add_to_playlist", {"playlist_name": "Sorted Out"})
    track = _make_track("7")

    await actions.execute_action(provider, rule, track)

    provider.mass.music.playlists.create_playlist.assert_awaited_once_with("Sorted Out")
    provider.mass.music.playlists._handle_add_playlist_tracks.assert_awaited_once_with(
        55, [track.uri]
    )
    assert rule.action.params["playlist_id"] == "55"
    provider.persist_rule.assert_awaited_once_with(rule)


async def test_add_to_playlist_reuses_existing_playlist_by_name() -> None:
    """An existing playlist with a matching name is reused instead of creating a duplicate."""
    provider = MagicMock()
    provider.persist_rule = AsyncMock()
    provider.mass.music.playlists.get_library_item = AsyncMock(
        side_effect=MediaNotFoundError("not found")
    )
    existing = MagicMock(item_id="20")
    existing.name = "Sorted Out"
    provider.mass.music.playlists.library_items = AsyncMock(return_value=[existing])
    provider.mass.music.playlists.create_playlist = AsyncMock()
    provider.mass.music.playlists._handle_add_playlist_tracks = AsyncMock()

    rule = _make_rule("add_to_playlist", {"playlist_name": "Sorted Out"})
    track = _make_track("7")

    await actions.execute_action(provider, rule, track)

    provider.mass.music.playlists.create_playlist.assert_not_called()
    assert rule.action.params["playlist_id"] == "20"


async def test_add_to_playlist_skips_lookup_when_playlist_id_already_cached() -> None:
    """A previously-resolved playlist_id short-circuits the by-name lookup entirely."""
    provider = MagicMock()
    provider.persist_rule = AsyncMock()
    provider.mass.music.playlists.get_library_item = AsyncMock(return_value=MagicMock())
    provider.mass.music.playlists.library_items = AsyncMock()
    provider.mass.music.playlists._handle_add_playlist_tracks = AsyncMock()

    rule = _make_rule("add_to_playlist", {"playlist_id": "55", "playlist_name": "Sorted Out"})
    track = _make_track("8")

    await actions.execute_action(provider, rule, track)

    provider.mass.music.playlists.library_items.assert_not_called()
    provider.mass.music.playlists._handle_add_playlist_tracks.assert_awaited_once_with(
        55, [track.uri]
    )


async def test_add_to_playlist_expands_album_to_its_tracks() -> None:
    """An album trigger item is resolved to its library tracks before adding to the playlist."""
    provider = MagicMock()
    provider.persist_rule = AsyncMock()
    provider.mass.music.playlists.get_library_item = AsyncMock(return_value=MagicMock())
    album_tracks = [_make_track("1"), _make_track("2")]
    provider.mass.music.albums.tracks = AsyncMock(return_value=album_tracks)
    provider.mass.music.playlists._handle_add_playlist_tracks = AsyncMock()

    rule = _make_rule("add_to_playlist", {"playlist_id": "99"})
    album = Album(item_id="42", provider="library", name="Some Album", provider_mappings=set())
    album.media_type = MediaType.ALBUM

    await actions.execute_action(provider, rule, album)

    provider.mass.music.albums.tracks.assert_awaited_once_with("42", "library")
    provider.mass.music.playlists._handle_add_playlist_tracks.assert_awaited_once_with(
        99, [t.uri for t in album_tracks]
    )


async def test_add_to_playlist_expands_artist_to_its_tracks() -> None:
    """An artist trigger item is resolved to its library tracks before adding to the playlist."""
    provider = MagicMock()
    provider.persist_rule = AsyncMock()
    provider.mass.music.playlists.get_library_item = AsyncMock(return_value=MagicMock())
    artist_tracks = [_make_track("3")]
    provider.mass.music.artists.tracks = AsyncMock(return_value=artist_tracks)
    provider.mass.music.playlists._handle_add_playlist_tracks = AsyncMock()

    rule = _make_rule("add_to_playlist", {"playlist_id": "99"})
    artist = Artist(item_id="17", provider="library", name="Some Artist", provider_mappings=set())
    artist.media_type = MediaType.ARTIST

    await actions.execute_action(provider, rule, artist)

    provider.mass.music.artists.tracks.assert_awaited_once_with("17", "library")
    provider.mass.music.playlists._handle_add_playlist_tracks.assert_awaited_once_with(
        99, [t.uri for t in artist_tracks]
    )


async def test_remove_from_playlist_removes_matching_positions() -> None:
    """remove_from_playlist finds the triggering track's position(s) and removes them."""
    provider = MagicMock()
    provider.persist_rule = AsyncMock()
    provider.mass.music.playlists.get_library_item = AsyncMock(return_value=MagicMock())

    track_to_remove = _make_track("5")
    other_track = _make_track("6")

    async def fake_tracks(_item_id: str, _domain: str) -> AsyncGenerator[Track]:
        for t in (other_track, track_to_remove, other_track):
            yield t

    provider.mass.music.playlists.tracks = fake_tracks
    provider.mass.music.playlists._handle_remove_playlist_tracks = AsyncMock()

    rule = _make_rule("remove_from_playlist", {"playlist_id": "12"})
    await actions.execute_action(provider, rule, track_to_remove)

    provider.mass.music.playlists._handle_remove_playlist_tracks.assert_awaited_once_with(12, (1,))


async def test_remove_from_playlist_noop_when_track_not_present() -> None:
    """remove_from_playlist does nothing when the track isn't in the playlist."""
    provider = MagicMock()
    provider.persist_rule = AsyncMock()
    provider.mass.music.playlists.get_library_item = AsyncMock(return_value=MagicMock())

    async def fake_tracks(_item_id: str, _domain: str) -> AsyncGenerator[Track]:
        for t in (_make_track("100"),):
            yield t

    provider.mass.music.playlists.tracks = fake_tracks
    provider.mass.music.playlists._handle_remove_playlist_tracks = AsyncMock()

    rule = _make_rule("remove_from_playlist", {"playlist_id": "12"})
    await actions.execute_action(provider, rule, _make_track("5"))

    provider.mass.music.playlists._handle_remove_playlist_tracks.assert_not_called()


async def test_remove_from_library() -> None:
    """remove_from_library removes the triggering item using its own media_type."""
    provider = MagicMock()
    provider.mass.music.remove_item_from_library = AsyncMock()

    rule = _make_rule("remove_from_library")
    track = _make_track("9")
    await actions.execute_action(provider, rule, track)

    provider.mass.music.remove_item_from_library.assert_awaited_once_with(MediaType.TRACK, "9")


async def test_unknown_action_type_logs_and_does_not_raise() -> None:
    """An unknown action type is logged and swallowed rather than crashing the dispatcher."""
    provider = MagicMock()
    rule = _make_rule("does_not_exist")
    track = _make_track("1")

    await actions.execute_action(provider, rule, track)  # should not raise

    provider.logger.warning.assert_called_once()
