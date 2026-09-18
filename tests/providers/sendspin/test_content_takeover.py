"""Regression tests for new-content Sendspin group takeover snapshots."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from aiosendspin.noise.keys import Identity
from aiosendspin.noise.trust_store import InMemoryServerPairingStore
from aiosendspin.server import SendspinServer
from aiosendspin.server.client import SendspinClient
from aiosendspin.server.group import SendspinGroup
from aiosendspin.server.roles.metadata.state import Metadata
from PIL import Image

from music_assistant.providers.sendspin.player import SendspinPlayer

if TYPE_CHECKING:
    from music_assistant_models.queue_item import QueueItem


def _make_metadata_player() -> MagicMock:
    """Create a player mock with the shared takeover state."""
    player = MagicMock()
    player._metadata_generation = 0
    player._content_takeover_pending = False
    player._metadata_lock = asyncio.Lock()
    return player


async def test_new_content_takeover_clears_snapshot_before_member_replay() -> None:
    """A joining client cannot receive the previous content snapshot."""
    loop = asyncio.get_running_loop()
    server = SendspinServer(
        loop,
        Identity.generate(),
        "test",
        pairing_store=InMemoryServerPairingStore(),
    )
    leader = SendspinClient(server, "leader")
    joining = SendspinClient(server, "joining")
    group = SendspinGroup(server, leader)
    metadata_role = cast("Any", group.group_role("metadata"))
    artwork_role = cast("Any", group.group_role("artwork"))

    metadata_role.set_metadata(Metadata(title="The Band", artwork_url="old-art"))
    old_art = Image.new("RGB", (2, 2), "red")
    await artwork_role.set_album_artwork(old_art)
    assert metadata_role.metadata is not None
    assert artwork_role.get_album_artwork() is old_art

    player = _make_metadata_player()
    player._clear_current_media_metadata = SendspinPlayer._clear_current_media_metadata.__get__(
        player, SendspinPlayer
    )
    player._metadata_role = metadata_role
    player._artwork_role = artwork_role
    player._visualizer_role = None
    player._color_role = None
    player._controller_role = None
    player._cancel_beat_retry = MagicMock()
    player.last_sent_artwork_url = "old-art"
    player.last_sent_artist_artwork_url = None
    player._last_beat_queue_item_id = None
    player._last_beat_anchor_us = None

    await SendspinPlayer.on_group_content_takeover(player)

    await group.add_client(joining)
    assert metadata_role.metadata is None
    assert artwork_role.get_album_artwork() is not old_art
    await server.close()


async def test_old_takeover_cleanup_does_not_clear_new_generation() -> None:
    """Finishing an older transaction cannot release a newer pending generation."""
    player = _make_metadata_player()
    player._clear_current_media_metadata = AsyncMock()

    first = await SendspinPlayer.on_group_content_takeover(player)
    second = await SendspinPlayer.on_group_content_takeover(player)
    await SendspinPlayer.on_group_content_takeover_finished(player, first)
    assert player._content_takeover_pending is True
    await SendspinPlayer.on_group_content_takeover_finished(player, second)
    assert player._content_takeover_pending is False


async def test_takeover_completion_republishes_current_media() -> None:
    """Finishing the current takeover publishes media that arrived while it was pending."""
    player = _make_metadata_player()
    player._metadata_generation = 1
    player._content_takeover_pending = True
    player.state.current_media = object()
    player.send_current_media_metadata = AsyncMock()
    scheduled: list[asyncio.Task[None]] = []

    def _create_task(coro: Any, **_kwargs: object) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        scheduled.append(task)
        return task

    player.mass.create_task = _create_task

    await SendspinPlayer.on_group_content_takeover_finished(player, 1)
    assert player._content_takeover_pending is False
    assert len(scheduled) == 1
    await scheduled[0]
    player.send_current_media_metadata.assert_awaited_once_with()


@pytest.mark.parametrize("artist", [False, True])
async def test_publish_artwork_updates_cache_after_set_and_clear(artist: bool) -> None:
    """Update only the selected artwork cache after set and clear succeed."""
    role = SimpleNamespace(
        set_album_artwork=AsyncMock(),
        set_artist_artwork=AsyncMock(),
    )
    player = _make_metadata_player()
    player._artwork_role = role
    player._metadata_publish_allowed = SendspinPlayer._metadata_publish_allowed.__get__(
        player, SendspinPlayer
    )
    player.last_sent_artwork_url = "old-album"
    player.last_sent_artist_artwork_url = "old-artist"
    image = Image.new("RGB", (2, 2), "red")

    await SendspinPlayer._publish_artwork(player, image, "new-art", 0, artist=artist)
    await SendspinPlayer._publish_artwork(player, None, None, 0, artist=artist)

    setter = role.set_artist_artwork if artist else role.set_album_artwork
    setter.assert_has_awaits([call(image), call(None)])
    assert player.last_sent_artist_artwork_url == (None if artist else "old-artist")
    assert player.last_sent_artwork_url == (None if not artist else "old-album")


@pytest.mark.parametrize("artist", [False, True])
@pytest.mark.parametrize("failure", ["stale", "missing_role", "setter"])
async def test_publish_artwork_does_not_poison_cache(artist: bool, failure: str) -> None:
    """Leave caches unchanged when artwork publication cannot complete."""
    role = SimpleNamespace(
        set_album_artwork=AsyncMock(),
        set_artist_artwork=AsyncMock(),
    )
    if failure == "setter":
        setter = role.set_artist_artwork if artist else role.set_album_artwork
        setter.side_effect = RuntimeError("setter failed")
    player = _make_metadata_player()
    player._metadata_generation = 1 if failure == "stale" else 0
    player._artwork_role = None if failure == "missing_role" else role
    player._metadata_publish_allowed = SendspinPlayer._metadata_publish_allowed.__get__(
        player, SendspinPlayer
    )
    player.last_sent_artwork_url = "old-album"
    player.last_sent_artist_artwork_url = "old-artist"

    call = SendspinPlayer._publish_artwork(
        player, Image.new("RGB", (2, 2)), "new-art", 0, artist=artist
    )
    if failure == "setter":
        with pytest.raises(RuntimeError, match="setter failed"):
            await call
    else:
        await call

    assert player.last_sent_artist_artwork_url == "old-artist"
    assert player.last_sent_artwork_url == "old-album"


async def test_stale_artist_artwork_does_not_poison_cache() -> None:
    """An artwork fetch superseded during await is retried by the next generation."""
    thumbnail_started = asyncio.Event()
    release_thumbnail = asyncio.Event()

    async def get_thumbnail(*_args: object, **_kwargs: object) -> bytes:
        thumbnail_started.set()
        await release_thumbnail.wait()
        return b"art"

    role = SimpleNamespace(set_artist_artwork=AsyncMock())
    player = _make_metadata_player()
    player._clear_current_media_metadata = AsyncMock()
    player.last_sent_artist_artwork_url = None
    player._artwork_role = role
    player._publish_artwork = SendspinPlayer._publish_artwork.__get__(player, SendspinPlayer)
    player.mass.music.get_library_item_by_prov_id = AsyncMock(return_value=None)
    player.mass.metadata.get_image_url.return_value = "artist-art"
    player.mass.metadata.get_thumbnail = get_thumbnail
    player._decode_artwork = AsyncMock(return_value=object())
    player._metadata_publish_allowed = SendspinPlayer._metadata_publish_allowed.__get__(
        player, SendspinPlayer
    )
    item = cast(
        "QueueItem",
        SimpleNamespace(
            name="Track",
            media_item=SimpleNamespace(
                artists=[SimpleNamespace(item_id="artist", provider="test", image=object())]
            ),
        ),
    )

    with patch("music_assistant.providers.sendspin.player.is_track", return_value=True):
        first = asyncio.create_task(SendspinPlayer._send_artist_artwork(player, item, generation=0))
        await thumbnail_started.wait()
        await SendspinPlayer.on_group_content_takeover(player)
        release_thumbnail.set()
        await first

        assert player.last_sent_artist_artwork_url is None
        await SendspinPlayer.on_group_content_takeover_finished(player, 1)
        player._content_takeover_pending = False
        await SendspinPlayer._send_artist_artwork(player, item, generation=1)

    assert player.last_sent_artist_artwork_url == "artist-art"
