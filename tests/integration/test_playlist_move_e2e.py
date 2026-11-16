"""End-to-end test for moving a track inside a Music Assistant playlist."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest
from music_assistant_models.errors import ResourceTemporarilyUnavailable

from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from music_assistant.mass import MusicAssistant


@pytest.mark.asyncio
async def test_move_command_reorders_a_builtin_playlist(e2e_mass: MusicAssistant) -> None:
    """After the command returns, a plain read of the playlist shows the new order."""
    test_prov = cast("MusicProvider", e2e_mass.get_provider("test"))
    seeds = [await test_prov.get_track(f"0_0_{index}") for index in range(4)]
    playlists = e2e_mass.music.playlists
    playlist = await playlists.create_playlist("Reordered", provider_instance_or_domain="builtin")
    for seed in seeds:
        assert seed.uri is not None
        await playlists.add_playlist_track(playlist.item_id, seed.uri)

    async def _names() -> list[str]:
        items = await playlists.tracks(playlist.item_id, "library")
        return [item.name for item in items]

    assert await _names() == [seed.name for seed in seeds]

    move = cast(
        "Callable[..., Awaitable[None]]",
        e2e_mass.command_handlers["music/playlists/move_playlist_track"].target,
    )
    await move(db_playlist_id=playlist.item_id, from_position=4, to_position=1)
    assert await _names() == [seeds[3].name, seeds[0].name, seeds[1].name, seeds[2].name]


@pytest.mark.asyncio
async def test_batch_move_command_reorders_a_builtin_playlist(e2e_mass: MusicAssistant) -> None:
    """A block move lands the tracks together, in their playlist order."""
    test_prov = cast("MusicProvider", e2e_mass.get_provider("test"))
    seeds = [await test_prov.get_track(f"0_0_{index}") for index in range(4)]
    playlists = e2e_mass.music.playlists
    playlist = await playlists.create_playlist("Block", provider_instance_or_domain="builtin")
    for seed in seeds:
        assert seed.uri is not None
        await playlists.add_playlist_track(playlist.item_id, seed.uri)

    move = cast(
        "Callable[..., Awaitable[None]]",
        e2e_mass.command_handlers["music/playlists/move_playlist_tracks"].target,
    )
    await move(db_playlist_id=playlist.item_id, positions=[4, 2], to_position=1)
    items = await playlists.tracks(playlist.item_id, "library")
    assert [item.name for item in items] == [
        seeds[1].name,
        seeds[3].name,
        seeds[0].name,
        seeds[2].name,
    ]

    await move(db_playlist_id=playlist.item_id, positions=[1, 2])
    items = await playlists.tracks(playlist.item_id, "library")
    assert [item.name for item in items] == [
        seeds[0].name,
        seeds[2].name,
        seeds[1].name,
        seeds[3].name,
    ]


@pytest.mark.asyncio
async def test_batch_move_that_fails_part_way_reads_back_the_partial_order(
    e2e_mass: MusicAssistant,
) -> None:
    """When a replayed single move fails, a reload shows the moves that did land."""
    test_prov = cast("MusicProvider", e2e_mass.get_provider("test"))
    seeds = [await test_prov.get_track(f"0_0_{index}") for index in range(4)]
    playlists = e2e_mass.music.playlists
    playlist = await playlists.create_playlist("Partial", provider_instance_or_domain="builtin")
    for seed in seeds:
        assert seed.uri is not None
        await playlists.add_playlist_track(playlist.item_id, seed.uri)
    builtin = cast("MusicProvider", e2e_mass.get_provider("builtin"))
    single_move = builtin.move_playlist_track
    calls = 0

    async def _fail_second_move(prov_playlist_id: str, from_pos: int, to_pos: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ResourceTemporarilyUnavailable("second move")
        await single_move(prov_playlist_id, from_pos, to_pos)

    async def _replay_block(prov_playlist_id: str, positions: list[int], to_pos: int) -> None:
        # the default block move, which replays single moves, instead of builtin's own
        await MusicProvider.move_playlist_tracks(builtin, prov_playlist_id, positions, to_pos)

    move = cast(
        "Callable[..., Awaitable[None]]",
        e2e_mass.command_handlers["music/playlists/move_playlist_tracks"].target,
    )
    with (
        patch.object(builtin, "move_playlist_track", _fail_second_move),
        patch.object(builtin, "move_playlist_tracks", _replay_block),
        pytest.raises(ResourceTemporarilyUnavailable),
    ):
        await move(db_playlist_id=playlist.item_id, positions=[2, 4], to_position=1)

    items = await playlists.tracks(playlist.item_id, "library")
    assert [item.name for item in items] == [
        seeds[1].name,
        seeds[0].name,
        seeds[2].name,
        seeds[3].name,
    ]
