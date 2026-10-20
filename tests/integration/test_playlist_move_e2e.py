"""End-to-end test for moving a track inside a Music Assistant playlist."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.music_provider import MusicProvider


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
