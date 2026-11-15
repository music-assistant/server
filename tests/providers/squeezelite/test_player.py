"""Tests for the Squeezelite player."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock, call, patch

import pytest
from aioslimproto.models import EventType as SlimEventType
from aioslimproto.models import PlayerState as SlimPlayerState
from aioslimproto.models import SlimEvent
from music_assistant_models.enums import (
    MediaType,
    PlaybackState,
    PlayerFeature,
    ProtocolRole,
    RepeatMode,
)
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import (
    CONF_ENTRY_PREFER_WAV_FOR_LIVE_SOURCES_DEFAULT_ENABLED,
    CONF_OUTPUT_CODEC,
    CONF_PREFER_WAV_FOR_LIVE_SOURCES,
)
from music_assistant.providers.squeezelite.constants import (
    CONF_ENTRY_DISPLAY,
    CONF_ENTRY_VISUALIZATION,
    CONF_STORED_VOLUME,
    DEFAULT_PLAYER_VOLUME,
)
from music_assistant.providers.squeezelite.player import SqueezelitePlayer, is_protocol_only_device
from tests.common import use_real_create_task

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry


async def test_squeezelite_prefers_wav_for_live_sources_by_default() -> None:
    """Squeezelite players default to the known-compatible low-latency WAV path."""

    async def _no_library_items(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
        return
        yield  # pragma: no cover - makes this an async generator

    player = SqueezelitePlayer.__new__(SqueezelitePlayer)
    player.mass = mass = MagicMock()
    mass.music.playlists.iter_library_items = _no_library_items
    mass.music.radio.iter_library_items = _no_library_items

    entries = await player.get_config_entries()
    entry = next(entry for entry in entries if entry.key == CONF_PREFER_WAV_FOR_LIVE_SOURCES)

    assert (
        entry.default_value == CONF_ENTRY_PREFER_WAV_FOR_LIVE_SOURCES_DEFAULT_ENABLED.default_value
    )


def _member_player(*, prefer_wav_for_live_sources: bool) -> MagicMock:
    """Return a mocked member player with the given effective WAV preference."""
    member = MagicMock()
    member.config.get_value.side_effect = lambda key, default=None: (
        prefer_wav_for_live_sources if key == CONF_PREFER_WAV_FOR_LIVE_SOURCES else default
    )
    return member


def _player_with_mocked_mass() -> tuple[SqueezelitePlayer, MagicMock]:
    """Return an uninitialized SqueezelitePlayer alongside its mocked mass instance."""
    player = SqueezelitePlayer.__new__(SqueezelitePlayer)
    player.mass = mass = MagicMock()
    player._protocol_id = "p1"
    player._player = None
    player._provider = MagicMock(get_protocol=lambda pid: mass.players.get_protocol(pid))
    return player, mass


def test_group_audio_source_uses_wav_when_member_prefers_it() -> None:
    """A sync group member that opted into low-latency WAV gets WAV for AudioSource media."""
    player, mass = _player_with_mocked_mass()
    mass.players.get_protocol.return_value = _member_player(prefer_wav_for_live_sources=True)

    codec = player._get_member_output_codec(
        "member_1", PlayerMedia(uri="fake://x", media_type=MediaType.AUDIO_SOURCE)
    )

    assert codec == "wav"
    mass.config.get_protocol_value.assert_not_called()


def test_group_audio_source_falls_back_to_output_codec_when_disabled() -> None:
    """A sync group member that disabled the WAV preference uses its configured output codec."""
    player, mass = _player_with_mocked_mass()
    mass.players.get_protocol.return_value = _member_player(prefer_wav_for_live_sources=False)
    mass.config.get_protocol_value.return_value = "aac"

    codec = player._get_member_output_codec(
        "member_1", PlayerMedia(uri="fake://x", media_type=MediaType.AUDIO_SOURCE)
    )

    assert codec == "aac"
    mass.config.get_protocol_value.assert_called_once_with("member_1", CONF_OUTPUT_CODEC, "flac")


def test_group_regular_track_ignores_wav_preference() -> None:
    """Regular track playback in a sync group always uses the member's configured codec."""
    player, mass = _player_with_mocked_mass()
    mass.config.get_protocol_value.return_value = "flac"

    codec = player._get_member_output_codec(
        "member_1", PlayerMedia(uri="fake://x", media_type=MediaType.TRACK)
    )

    assert codec == "flac"
    mass.players.get_protocol.assert_not_called()


@pytest.mark.parametrize(
    ("media_type", "expected"),
    [
        (MediaType.RADIO, (64, 1)),
        (MediaType.AUDIO_SOURCE, (64, 1)),
        (MediaType.ANNOUNCEMENT, (64, 1)),
        (MediaType.TRACK, (200, 20)),
    ],
)
async def test_live_streams_start_on_a_smaller_buffer(
    media_type: MediaType, expected: tuple[int, int]
) -> None:
    """Radio, AudioSource and announcement streams start on a smaller buffer, also when repeated."""
    player, mass = _player_with_mocked_mass()
    mass.player_queues.get.return_value = MagicMock(
        repeat_mode=RepeatMode.ONE, shuffle_enabled=False
    )
    slimplayer = MagicMock()
    slimplayer.play_url = AsyncMock()

    await player._handle_play_url_for_slimplayer(
        slimplayer,
        url="http://127.0.0.1:8097/stream.flac",
        media=PlayerMedia(uri="fake://x", media_type=media_type, source_id="queue_1"),
    )
    # the repeat-one re-enqueue is scheduled, not awaited here
    mass.call_later.call_args.args[1].close()

    assert slimplayer.play_url.call_count == 2
    for play_call in slimplayer.play_url.call_args_list:
        thresholds = (play_call.kwargs["stream_threshold"], play_call.kwargs["output_threshold"])
        assert thresholds == expected


@pytest.mark.parametrize(
    ("device_model", "expected"),
    [
        # generic squeezelite (software) players are full players
        ("SqueezeLite", False),
        ("SqueezeLite-HA-Addon", False),
        ("SqueezePlay", False),
        ("SqueezeESP32", False),
        # hardware players are full players
        ("Squeezebox Boom", False),
        ("Transporter", False),
        # unknown/absent model info defaults to a full player
        ("", False),
        ("Unknown", False),
        # WiiM/LinkPlay devices use squeezelite as a secondary protocol
        ("WiiM Player", True),
        ("wiim mini", True),
        # LMS bridge tools represent devices that are already players themselves
        ("RaopBridge", True),
        ("CastBridge", True),
        ("UPnPBridge", True),
    ],
)
def test_is_protocol_only_device(device_model: str, expected: bool) -> None:
    """Test protocol-only device detection based on the reported device model."""
    assert is_protocol_only_device(device_model) is expected


@pytest.mark.parametrize(
    ("url", "mime_type", "expected"),
    [
        # sync group member urls carry the codec in the query string, not the path
        (
            "http://127.0.0.1:8097/slimproto/multi?player_id=x&fmt=flac&child_player_id=y",
            "audio/flac",
            "audio/flac",
        ),
        (
            "http://127.0.0.1:8097/slimproto/multi?player_id=x&fmt=mp3&child_player_id=y",
            "audio/mpeg",
            "audio/mpeg",
        ),
        # without an explicit mime type it is derived from the url extension
        ("http://127.0.0.1:8097/stream.flac", None, "audio/flac"),
    ],
)
async def test_play_url_mime_type_is_explicit_for_sync_members(
    url: str, mime_type: str | None, expected: str
) -> None:
    """Sync group member urls pass the member codec mime type instead of deriving it."""
    player, mass = _player_with_mocked_mass()
    mass.player_queues.get.return_value = None
    slimplayer = MagicMock()
    slimplayer.play_url = AsyncMock()

    await player._handle_play_url_for_slimplayer(
        slimplayer,
        url=url,
        media=PlayerMedia(uri="fake://x", media_type=MediaType.TRACK, source_id="queue_1"),
        mime_type=mime_type,
    )

    assert slimplayer.play_url.call_args.kwargs["mime_type"] == expected


@pytest.mark.parametrize(
    ("url", "is_group_playback", "duration"),
    [
        ("http://127.0.0.1:8097/single/s1/queue_1/q2/p1.flac", False, 4),
        ("http://127.0.0.1:8097/flow/s1/queue_1/q2/p1.flac", False, 198),
        (
            "http://127.0.0.1:8097/slimproto/multi?player_id=x&fmt=flac&child_player_id=y",
            True,
            198,
        ),
    ],
    ids=["single_item", "flow", "sync_group"],
)
async def test_only_a_single_item_stream_gets_the_seconds_left_after_a_seek(
    url: str, is_group_playback: bool, duration: int
) -> None:
    """A flow stream, also the one a sync group plays, carries on into the next track."""
    player, mass = _player_with_mocked_mass()
    mass.player_queues.get.return_value = None
    slimplayer = MagicMock()
    slimplayer.play_url = AsyncMock()
    slimplayer.pause = AsyncMock()
    media = PlayerMedia(
        uri="fake://x",
        media_type=MediaType.TRACK,
        source_id="queue_1",
        duration=198,
        stream_duration=4,
    )

    await player._handle_play_url_for_slimplayer(
        slimplayer, url=url, media=media, is_group_playback=is_group_playback
    )

    assert slimplayer.play_url.call_args.kwargs["metadata"]["duration"] == duration


async def test_grouped_play_media_passes_per_member_mime_type() -> None:
    """Grouped play_media gives each sync member its own codec's mime type, not a derived one."""
    player, mass = _player_with_mocked_mass()
    player._protocol_id = "leader"
    player._attr_group_members = ["leader", "member_flac", "member_mp3"]
    player.multi_client_stream = None
    player.client = MagicMock(elapsed_seconds=0)
    use_real_create_task(mass)

    mass.player_queues.get_item.return_value = None
    mass.player_queues.get.return_value = None
    mass.streams.audio.select_flow_pcm_format = AsyncMock()
    mass.streams.base_url = "http://127.0.0.1:8097"

    member_codecs = {"member_flac": "flac", "member_mp3": "mp3"}
    clients = []
    for member_id in ("member_flac", "member_mp3"):
        client = MagicMock()
        client.player_id = member_id
        client.play_url = AsyncMock()
        client.pause = AsyncMock()
        clients.append(client)

    media = PlayerMedia(
        uri="fake://x",
        media_type=MediaType.TRACK,
        source_id="queue_1",
        queue_item_id="item_1",
    )

    with (
        patch.object(SqueezelitePlayer, "synced_to", new_callable=PropertyMock, return_value=None),
        patch("music_assistant.providers.squeezelite.player.MultiClientStream"),
        patch.object(player, "_get_sync_clients", return_value=clients),
        patch.object(
            player,
            "_get_member_output_codec",
            side_effect=lambda member_player_id, _media: member_codecs[member_player_id],
        ),
    ):
        await player.play_media(media)

    flac_client, mp3_client = clients
    assert flac_client.play_url.call_args.kwargs["mime_type"] == "audio/flac"
    assert mp3_client.play_url.call_args.kwargs["mime_type"] == "audio/mpeg"


async def test_regroup_supersedes_pending_resume() -> None:
    """A member removed and re-added while the leader is busy restarts the leader once."""
    player, mass = _player_with_mocked_mass()
    player._protocol_id = "leader"
    player._attr_group_members = ["leader", "member"]
    player._attr_playback_state = PlaybackState.PLAYING
    # the protocol is attached to a player with an id of its own, whose queue plays
    player._player = MagicMock(player_id="pl_leader")
    player._player.current_media = PlayerMedia(uri="fake://x")
    use_real_create_task(mass)
    # the member as the slimproto client of the group and as the protocol it is attached as
    member = MagicMock()
    member.player_id = "member"
    member.state = SlimPlayerState.STOPPED
    member.client.state = SlimPlayerState.STOPPED
    mass.players.get_protocol.return_value = member
    # the playback lock an announcement holds on the leader while it plays
    leader_busy = asyncio.Event()
    resumed: list[str] = []

    async def _cmd_resume(player_id: str) -> None:
        await leader_busy.wait()
        resumed.append(player_id)

    mass.players.cmd_resume = _cmd_resume

    with (
        patch.object(SqueezelitePlayer, "synced_to", new_callable=PropertyMock, return_value=None),
        patch.object(player, "_get_sync_clients", return_value=[member]),
        patch.object(player, "update_state"),
    ):
        await player.set_members(player_ids_to_remove=["member"])
        await player.set_members(player_ids_to_add=["member"])
        leader_busy.set()
        await asyncio.gather(*mass._tracked_tasks.values(), return_exceptions=True)

    assert resumed == ["pl_leader"]


async def test_set_members_removes_several_members_at_once() -> None:
    """All members passed in one removal call leave the group and are stopped."""
    player, _mass = _player_with_mocked_mass()
    player._protocol_id = "leader"
    player._attr_group_members = ["leader", "member_1", "member_2", "member_3"]
    player.client = MagicMock(player_id="leader")
    members = {
        member_id: MagicMock(player_id=member_id, state=SlimPlayerState.PLAYING, stop=AsyncMock())
        for member_id in ("member_1", "member_2", "member_3")
    }
    player._provider = MagicMock()
    player._provider.slimproto.get_player.side_effect = members.get

    with (
        patch.object(SqueezelitePlayer, "synced_to", new_callable=PropertyMock, return_value=None),
        patch.object(player, "update_state"),
    ):
        await player.set_members(player_ids_to_remove=["member_1", "member_2"])

    assert player.group_members == ["leader", "member_3"]
    members["member_1"].stop.assert_awaited_once()
    members["member_2"].stop.assert_awaited_once()
    members["member_3"].stop.assert_not_called()


async def test_play_media_resets_elapsed_anchor() -> None:
    """A new stream does not carry the previous stream's position."""
    player, mass = _player_with_mocked_mass()
    player.multi_client_stream = None
    player.client = MagicMock(elapsed_seconds=0)
    player._provider = MagicMock(mass=mass)
    player._attr_elapsed_time = 3.9
    player._attr_elapsed_time_last_updated = time.time() - 4
    mass.streams.resolve_stream_url = AsyncMock(return_value="http://127.0.0.1:8097/x.flac")

    with (
        patch.object(SqueezelitePlayer, "synced_to", new_callable=PropertyMock, return_value=None),
        patch.object(
            SqueezelitePlayer, "group_members", new_callable=PropertyMock, return_value=[]
        ),
        patch.object(player, "_handle_play_url_for_slimplayer", new=AsyncMock()),
        patch.object(player, "update_attributes"),
        patch.object(player, "update_state"),
    ):
        await player.play_media(
            PlayerMedia(uri="fake://x", media_type=MediaType.TRACK, source_id="queue_1")
        )

    assert player._attr_elapsed_time == 0
    assert time.time() - player._attr_elapsed_time_last_updated < 1


async def test_play_media_clears_a_stale_resume_state() -> None:
    """A new stream neither resumes from an older pause nor restarts on its first resume."""
    player, mass = _player_with_mocked_mass()
    player.multi_client_stream = None
    player.client = MagicMock(elapsed_seconds=0)
    player._provider = MagicMock(mass=mass)
    player._paused_at = 42.0
    player._restream_needed = True
    mass.streams.resolve_stream_url = AsyncMock(return_value="http://127.0.0.1:8097/x.flac")

    with (
        patch.object(SqueezelitePlayer, "synced_to", new_callable=PropertyMock, return_value=None),
        patch.object(
            SqueezelitePlayer, "group_members", new_callable=PropertyMock, return_value=[]
        ),
        patch.object(player, "_handle_play_url_for_slimplayer", new=AsyncMock()),
        patch.object(player, "update_attributes"),
        patch.object(player, "update_state"),
    ):
        await player.play_media(
            PlayerMedia(uri="fake://x", media_type=MediaType.TRACK, source_id="queue_1")
        )

    assert player._paused_at == 0.0
    assert player._restream_needed is False


async def test_a_paused_group_resumes_in_sync_from_its_own_pause_only() -> None:
    """The pause position is used for the resume after that pause, not for a later one."""
    player, mass = _player_with_mocked_mass()
    use_real_create_task(mass)
    player._restream_needed = False
    player._paused_at = 42.0
    sync_group = MagicMock(resume=AsyncMock())
    player._sync_group = sync_group
    clients = [MagicMock(play=AsyncMock()), MagicMock(play=AsyncMock())]

    with (
        patch.object(
            SqueezelitePlayer,
            "group_members",
            new_callable=PropertyMock,
            return_value=["leader", "member"],
        ),
        patch.object(player, "_group_needs_restream", return_value=False),
        patch.object(player, "_get_sync_clients", return_value=clients),
    ):
        await player.play()
        # a pause from a device button bypasses pause(), so no new pause position
        await player.play()

    sync_group.resume.assert_awaited_once_with(clients, 42.0)
    for client in clients:
        client.play.assert_awaited_once()


async def test_repeated_play_while_restreaming_restarts_the_group_once() -> None:
    """A second play before the new stream exists replaces the pending restart."""
    player, mass = _player_with_mocked_mass()
    player._protocol_id = "leader"
    # the protocol is attached to a player with an id of its own, whose queue restarts
    player._player = MagicMock(player_id="pl_leader")
    player._restream_needed = True

    with patch.object(
        SqueezelitePlayer,
        "group_members",
        new_callable=PropertyMock,
        return_value=["leader", "member"],
    ):
        await player.play()

    mass.players.cmd_resume.assert_called_once_with("pl_leader")
    assert mass.create_task.call_args.kwargs == {
        "task_id": "squeezelite_regroup_resume_leader",
        "abort_existing": True,
    }


async def test_a_group_changed_while_paused_restarts_the_player_of_its_leader() -> None:
    """A play after a member joined a paused group restarts the leader's player, not a resume."""
    player, mass = _player_with_mocked_mass()
    player._protocol_id = "leader"
    player._attr_group_members = ["leader", "member"]
    player._attr_playback_state = PlaybackState.PAUSED
    player._restream_needed = False
    player._paused_at = 42.0
    # the protocol is attached to a player with an id of its own, whose queue is paused
    player._player = MagicMock(player_id="pl_leader")
    player._player.current_media = PlayerMedia(uri="fake://x")
    sync_group = MagicMock(resume=AsyncMock())
    player._sync_group = sync_group
    joining = MagicMock()
    joining.client.state = SlimPlayerState.STOPPED
    mass.players.get_protocol.return_value = joining

    with (
        patch.object(SqueezelitePlayer, "synced_to", new_callable=PropertyMock, return_value=None),
        patch.object(player, "_group_needs_restream", return_value=False),
        patch.object(player, "_get_sync_clients", return_value=[]),
        patch.object(player, "update_state"),
    ):
        await player.set_members(player_ids_to_add=["joining"])
        await player.play()

    mass.players.cmd_resume.assert_called_once_with("pl_leader")
    sync_group.resume.assert_not_called()


@pytest.mark.parametrize(("sync_adjust", "play_delay"), [(30, 30), (None, 0)])
async def test_sync_delay_setting_is_the_play_delay(
    sync_adjust: int | None, play_delay: int
) -> None:
    """The per-player sync delay setting becomes the player's LMS play delay."""
    player, _mass = _player_with_mocked_mass()
    player.client = MagicMock(play_delay=0)
    config = MagicMock(get_value=MagicMock(return_value=sync_adjust))

    with (
        patch.object(SqueezelitePlayer, "config", new_callable=PropertyMock, return_value=config),
        patch.object(player, "_set_preset_items", new=AsyncMock()),
        patch.object(player, "_set_display", new=AsyncMock()),
    ):
        await player.on_config_updated()

    assert player.client.play_delay == play_delay


async def test_buffer_ready_starts_the_group_in_sync() -> None:
    """Once every member has buffered, the group starts at a common instant."""
    player, _mass = _player_with_mocked_mass()
    clients = [MagicMock(state=SlimPlayerState.BUFFER_READY) for _ in range(2)]
    sync_group = MagicMock(start=AsyncMock())
    player._sync_group = sync_group

    with (
        patch.object(SqueezelitePlayer, "synced_to", new_callable=PropertyMock, return_value=None),
        patch.object(
            SqueezelitePlayer,
            "group_members",
            new_callable=PropertyMock,
            return_value=["leader", "member"],
        ),
        patch.object(player, "_get_sync_clients", side_effect=lambda: iter(clients)),
        patch("music_assistant.providers.squeezelite.player.asyncio.sleep", new=AsyncMock()),
    ):
        await player._handle_buffer_ready()

    sync_group.start.assert_awaited_once_with(clients)


async def test_buffer_ready_starts_the_leader_when_its_members_dropped_out() -> None:
    """A leader left on its own while buffering starts instead of waiting forever."""
    player, _mass = _player_with_mocked_mass()
    player.client = leader = MagicMock(
        state=SlimPlayerState.BUFFER_READY, jiffies=1234, unpause_at=AsyncMock()
    )
    sync_group = MagicMock(start=AsyncMock())
    player._sync_group = sync_group

    with (
        patch.object(SqueezelitePlayer, "synced_to", new_callable=PropertyMock, return_value=None),
        patch.object(
            SqueezelitePlayer,
            "group_members",
            new_callable=PropertyMock,
            return_value=["leader", "member"],
        ),
        patch.object(player, "_get_sync_clients", side_effect=lambda: iter([leader])),
        patch("music_assistant.providers.squeezelite.player.asyncio.sleep", new=AsyncMock()),
    ):
        await player._handle_buffer_ready()

    leader.unpause_at.assert_awaited_once_with(1234)
    sync_group.start.assert_not_called()


def test_sync_members_need_the_stream_of_their_leader() -> None:
    """Members are moved along when their leader renders through another protocol."""
    player = SqueezelitePlayer.__new__(SqueezelitePlayer)

    assert player.native_grouping_requires_own_stream is True


def test_a_heartbeat_checks_the_group_sync() -> None:
    """The leader's heartbeat runs the group drift check on all members."""
    player, mass = _player_with_mocked_mass()
    clients = [MagicMock(), MagicMock()]
    sync_group = MagicMock()
    player._sync_group = sync_group

    with (
        patch.object(
            SqueezelitePlayer,
            "group_members",
            new_callable=PropertyMock,
            return_value=["leader", "member"],
        ),
        patch.object(player, "_get_sync_clients", return_value=clients),
    ):
        player._handle_sync()

    sync_group.check.assert_called_once_with(clients)
    mass.create_task.assert_called_once_with(sync_group.check.return_value)


async def test_play_media_drops_heartbeat_from_before_the_flush() -> None:
    """An old-stream heartbeat that lands before the flush does not survive the restart."""
    player, mass = _player_with_mocked_mass()
    player.multi_client_stream = None
    player.client = client = MagicMock(elapsed_seconds=0)
    client.play_url = AsyncMock()
    player._provider = MagicMock(mass=mass)
    mass.player_queues.get.return_value = None

    async def _resolve_with_heartbeat(*_args: object) -> str:
        player._attr_elapsed_time = 3.9
        return "http://127.0.0.1:8097/x.flac"

    mass.streams.resolve_stream_url = _resolve_with_heartbeat

    with (
        patch.object(SqueezelitePlayer, "synced_to", new_callable=PropertyMock, return_value=None),
        patch.object(
            SqueezelitePlayer, "group_members", new_callable=PropertyMock, return_value=[]
        ),
    ):
        await player.play_media(
            PlayerMedia(uri="fake://x", media_type=MediaType.TRACK, source_id="queue_1")
        )

    assert player._attr_elapsed_time == 0


def _player_for_volume_restore(
    stored: dict[str, object], legacy_cache: list[object] | None, *, known: bool = True
) -> tuple[SqueezelitePlayer, MagicMock]:
    """
    Return a protocol whose stored config values and legacy cache entry are mocked.

    :param stored: The values stored on the record of protocol ``p1``.
    :param legacy_cache: The legacy cache entry of the protocol.
    :param known: Whether the protocol has a record before its attach creates one.
    """
    player, mass = _player_with_mocked_mass()
    player._provider = MagicMock(mass=mass, instance_id="squeezelite_1")
    player.logger = MagicMock()
    player.client = MagicMock(player_id="p1", muted=False, volume_level=20)
    player.client.stop = AsyncMock()
    player.client.mute = AsyncMock()
    player.client.volume_set = AsyncMock(
        side_effect=lambda level: setattr(player.client, "volume_level", level)
    )

    def _get_protocol_value(protocol_id: str, key: str, default: object = None) -> object:
        return stored.get(key, default) if protocol_id == "p1" else default

    def _set_protocol_value(protocol_id: str, key: str, value: object) -> None:
        # like the config controller: a protocol without a record that is not attached
        if protocol_id != "p1" or not (known or player._player is not None):
            raise KeyError(protocol_id)
        stored[key] = value

    async def _attach(protocol: SqueezelitePlayer) -> None:
        protocol._player = MagicMock(player_id="pl_p1")

    mass.config.get_protocol_value.side_effect = _get_protocol_value
    mass.config.set_protocol_value.side_effect = _set_protocol_value
    mass.cache.get = AsyncMock(return_value=legacy_cache)
    mass.players.register = AsyncMock(side_effect=_attach)
    return player, mass


@pytest.mark.parametrize(
    ("stored", "legacy_cache", "expected_volume"),
    [
        # volume mapped to an AVR: the provider never sends volume commands, so the
        # cache entry expired, but the stored value must still be restored
        ({CONF_STORED_VOLUME: 100}, None, 100),
        ({CONF_STORED_VOLUME: 35}, [False, 80], 35),
        # stored before the volume moved out of the cache
        ({}, [True, 55], 55),
        ({}, None, DEFAULT_PLAYER_VOLUME),
    ],
)
async def test_setup_restores_volume_without_a_valid_cache_entry(
    stored: dict[str, object],
    legacy_cache: list[object] | None,
    expected_volume: int,
) -> None:
    """Reconnecting restores the last volume even after the cache entry expired."""
    player, mass = _player_for_volume_restore(stored, legacy_cache)

    with patch.object(player, "update_attributes"):
        await player.setup()

    client = cast("MagicMock", player.client)
    client.mute.assert_not_called()
    client.volume_set.assert_awaited_once_with(expected_volume)
    mass.players.register.assert_awaited_once_with(player)
    assert stored[CONF_STORED_VOLUME] == expected_volume


async def test_a_first_connect_stores_the_volume_once_attached() -> None:
    """A new device has no record until its attach creates one, so its volume is stored after."""
    stored: dict[str, object] = {}
    player, mass = _player_for_volume_restore(stored, None, known=False)

    with patch.object(player, "update_attributes"):
        await player.setup()

    mass.players.register.assert_awaited_once_with(player)
    assert stored[CONF_STORED_VOLUME] == DEFAULT_PLAYER_VOLUME


async def test_volume_changed_on_the_player_is_stored() -> None:
    """A volume change made on the player itself (e.g. JiveLite) is kept for the next connect."""
    stored: dict[str, object] = {}
    player, mass = _player_for_volume_restore(stored, None)
    with patch.object(player, "update_attributes"), patch.object(player, "update_state"):
        await player.setup()
        mass.config.set_protocol_value.reset_mock()
        event = SlimEvent(type=SlimEventType.PLAYER_UPDATED, player_id="p1")

        cast("MagicMock", player.client).volume_level = 65
        player.handle_slim_event(event)
        assert stored[CONF_STORED_VOLUME] == 65

        player.handle_slim_event(event)
        mass.config.set_protocol_value.assert_called_once()


@pytest.mark.parametrize("entry", [CONF_ENTRY_DISPLAY, CONF_ENTRY_VISUALIZATION])
def test_the_settings_entries_of_a_player_can_be_serialized(entry: ConfigEntry) -> None:
    """The settings page of a player turns its entries into a dict, which a bad default breaks."""
    entry.to_dict()
    if entry.options:
        assert entry.default_value in {option.value for option in entry.options}


@pytest.mark.parametrize(
    ("device_model", "role", "powered_by_client"),
    [("SqueezeLite", ProtocolRole.NATIVE, True), ("WiiM Player", ProtocolRole.AUDIO, False)],
)
async def test_the_role_follows_the_device_model(
    device_model: str, role: ProtocolRole, powered_by_client: bool
) -> None:
    """A device of its own is an audio protocol, powered with the stream; a player has power."""
    provider = MagicMock(instance_id="squeezelite")
    provider.mass.config.get_base_protocol_config.return_value = MagicMock()
    use_real_create_task(provider.mass)
    client = MagicMock(device_model=device_model, max_sample_rate=96000)
    client.stop = AsyncMock()
    client.power = AsyncMock()
    player = SqueezelitePlayer(provider, "aa:bb:cc:dd:ee:ff", client)
    player.update_state = MagicMock()  # type: ignore[misc,method-assign]

    await player.stop()

    assert player.role == role
    assert (PlayerFeature.POWER in player.supported_features) is powered_by_client
    if powered_by_client:
        client.stop.assert_awaited_once()
        client.power.assert_not_awaited()
    else:
        client.power.assert_awaited_once_with(False)
        client.stop.assert_not_awaited()


# ---------------------------------------------------------------- Sendspin bridge


def _player_with_bridge(*, owns_stream: bool = True) -> tuple[SqueezelitePlayer, MagicMock]:
    """Return a player whose Sendspin bridge has, or does not have, its stream on the player."""
    player, _mass = _player_with_mocked_mass()
    player.client = MagicMock(player_id="p1")
    player.sendspin_bridge = bridge = MagicMock(owns_stream=owns_stream)
    return player, bridge


@pytest.mark.parametrize("owns_stream", [True, False])
def test_a_stream_of_the_bridge_is_started_by_the_bridge(owns_stream: bool) -> None:
    """The bridge starts its stream at an instant of its timeline, the player would start it now."""
    player, _bridge = _player_with_bridge(owns_stream=owns_stream)
    event = SlimEvent(type=SlimEventType.PLAYER_BUFFER_READY, player_id="p1")

    with patch.object(player, "_handle_buffer_ready", new=MagicMock()) as handle_buffer_ready:
        player.handle_slim_event(event)

    assert handle_buffer_ready.call_count == (0 if owns_stream else 1)


@pytest.mark.parametrize("members_left", [1, 2])
async def test_a_group_start_that_waits_leaves_the_player_to_its_bridge(members_left: int) -> None:
    """A bridge that takes the player over while its sync group buffers decides its start."""
    player, bridge = _player_with_bridge(owns_stream=False)
    player.client = leader = MagicMock(
        state=SlimPlayerState.BUFFER_READY, jiffies=1234, unpause_at=AsyncMock()
    )
    clients = [leader, MagicMock(state=SlimPlayerState.BUFFER_READY)][:members_left]
    player._sync_group = sync_group = MagicMock(start=AsyncMock())

    async def taken_over(_delay: float) -> None:
        bridge.owns_stream = True

    with (
        patch.object(SqueezelitePlayer, "synced_to", new_callable=PropertyMock, return_value=None),
        patch.object(
            SqueezelitePlayer,
            "group_members",
            new_callable=PropertyMock,
            return_value=["leader", "member"],
        ),
        patch.object(player, "_get_sync_clients", side_effect=lambda: iter(clients)),
        patch("music_assistant.providers.squeezelite.player.asyncio.sleep", new=taken_over),
    ):
        await player._handle_buffer_ready()

    leader.unpause_at.assert_not_awaited()
    sync_group.start.assert_not_called()


@pytest.mark.parametrize("owns_stream", [True, False])
def test_the_position_of_a_bridge_stream_is_not_that_of_the_protocol(owns_stream: bool) -> None:
    """Neither the elapsed time nor the sync check of the sync group follows a bridge stream."""
    player, _bridge = _player_with_bridge(owns_stream=owns_stream)
    cast("MagicMock", player.client).elapsed_seconds = 42.0
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_elapsed_time = 0
    event = SlimEvent(type=SlimEventType.PLAYER_HEARTBEAT, player_id="p1")

    with (
        patch.object(
            SqueezelitePlayer, "group_members", new_callable=PropertyMock, return_value=["p1", "m"]
        ),
        patch.object(player, "_handle_sync") as handle_sync,
        patch.object(player, "update_state"),
    ):
        player.handle_slim_event(event)

    assert player._attr_elapsed_time == (0 if owns_stream else 42.0)
    assert handle_sync.call_count == (0 if owns_stream else 1)


@pytest.mark.parametrize("owns_stream", [True, False])
def test_a_bridge_stream_is_no_media_of_the_protocol(owns_stream: bool) -> None:
    """The protocol reports that it plays, but not what: that is what Sendspin shows."""
    provider = MagicMock(instance_id="squeezelite")
    provider.mass.config.get_base_protocol_config.return_value = MagicMock()
    client = MagicMock(
        device_model="SqueezeLite",
        max_sample_rate=96000,
        state=SlimPlayerState.PLAYING,
        volume_level=20,
        muted=False,
    )
    client.current_media.metadata = {"item_id": "library://track/1", "title": "Before"}
    player = SqueezelitePlayer(provider, "aa:bb:cc:dd:ee:ff", client)
    player.sendspin_bridge = MagicMock(owns_stream=owns_stream)

    player.update_attributes()

    assert player.playback_state == PlaybackState.PLAYING
    if owns_stream:
        assert player.current_media is None
    else:
        assert player.current_media is not None
        assert player.current_media.title == "Before"


async def test_a_stream_of_the_protocol_takes_the_players_back_from_their_bridges() -> None:
    """Before the protocol plays on its players, a bridge that plays on one of them lets go."""
    player, bridge = _player_with_bridge()
    mass = cast("MagicMock", player.mass)
    player.multi_client_stream = None
    member_client = MagicMock(player_id="member")
    member = MagicMock()
    without_bridge = MagicMock(player_id="plain")
    player._provider = MagicMock(mass=mass)
    player._provider.get_protocol.side_effect = {
        "p1": player,
        "member": member,
        "plain": MagicMock(sendspin_bridge=None),
    }.get
    mass.streams.resolve_stream_url = AsyncMock(return_value="http://127.0.0.1:8097/x.flac")
    steps = MagicMock()
    steps.attach_mock(bridge.stop_streaming, "own_bridge")
    steps.attach_mock(member.sendspin_bridge.stop_streaming, "member_bridge")
    play_url = AsyncMock()
    steps.attach_mock(play_url, "play_url")

    with (
        patch.object(SqueezelitePlayer, "synced_to", new_callable=PropertyMock, return_value=None),
        patch.object(
            SqueezelitePlayer, "group_members", new_callable=PropertyMock, return_value=[]
        ),
        patch.object(
            player,
            "_get_sync_clients",
            side_effect=lambda: iter([player.client, member_client, without_bridge]),
        ),
        patch.object(player, "_handle_play_url_for_slimplayer", new=play_url),
        patch.object(player, "update_state"),
    ):
        await player.play_media(
            PlayerMedia(uri="fake://x", media_type=MediaType.TRACK, source_id="queue_1")
        )

    assert [step[0] for step in steps.mock_calls] == ["own_bridge", "member_bridge", "play_url"]


async def test_a_stop_of_the_protocol_ends_the_stream_of_its_bridge() -> None:
    """The bridge gives the player back and leaves its session, then the player is stopped."""
    provider = MagicMock(instance_id="squeezelite")
    provider.mass.config.get_base_protocol_config.return_value = MagicMock()
    use_real_create_task(provider.mass)
    client = MagicMock(device_model="SqueezeLite", max_sample_rate=96000)
    client.stop = AsyncMock()
    player = SqueezelitePlayer(provider, "aa:bb:cc:dd:ee:ff", client)
    player.update_state = MagicMock()  # type: ignore[misc,method-assign]
    player.sendspin_bridge = bridge = MagicMock()
    steps = MagicMock()
    steps.attach_mock(bridge.stop_streaming, "bridge")
    steps.attach_mock(client.stop, "client")

    await player.stop()

    assert steps.mock_calls == [call.bridge(), call.client()]


async def test_a_stream_that_takes_the_player_over_ends_what_the_protocol_plays() -> None:
    """The player leaves the sync group of its leader and ends the stream of its own group."""
    player, _mass = _player_with_mocked_mass()
    leader = MagicMock()
    player._provider = MagicMock()
    player._provider.get_protocol.side_effect = {"leader": leader}.get
    player.multi_client_stream = stream = MagicMock(stop=AsyncMock())
    player._paused_at = 12.5
    player._attr_elapsed_time = 80.0

    with patch.object(
        SqueezelitePlayer, "synced_to", new_callable=PropertyMock, return_value="leader"
    ):
        await player.release_native_playback()

    leader.release_member.assert_called_once_with("p1")
    stream.stop.assert_awaited_once()
    assert vars(player)["multi_client_stream"] is None
    assert player._paused_at == 0.0
    assert player._attr_elapsed_time == 0


@pytest.mark.parametrize(
    ("target", "queue_of"), [("living_room", "living_room"), (None, "kitchen")]
)
async def test_buttons_act_on_the_player_that_leads_the_sendspin_group(
    target: str | None, queue_of: str
) -> None:
    """While the player plays for its bridge, next on it skips for its Sendspin group."""
    player, bridge = _player_with_bridge()
    mass = cast("MagicMock", player.mass)
    player._player = MagicMock(player_id="kitchen")
    player.logger = MagicMock()
    bridge.transport_command_target.return_value = target
    mass.player_queues.next = AsyncMock()
    queue = mass.player_queues.get_active_queue.return_value
    event = SlimEvent(type=SlimEventType.PLAYER_BTN_EVENT, player_id="p1", data="button fwd")

    await player._handle_player_cli_event(event)

    mass.player_queues.get_active_queue.assert_called_once_with(queue_of)
    mass.player_queues.next.assert_awaited_once_with(queue.queue_id)


async def test_the_bridge_is_evaluated_when_the_config_of_the_protocol_is_loaded() -> None:
    """Every attach and config change brings the Sendspin bridge in line."""
    player, mass = _player_with_mocked_mass()
    use_real_create_task(mass)
    player.client = MagicMock(play_delay=0)
    config = MagicMock(get_value=MagicMock(return_value=0))

    with (
        patch.object(SqueezelitePlayer, "config", new_callable=PropertyMock, return_value=config),
        patch.object(player, "_set_preset_items", new=AsyncMock()),
        patch.object(player, "_set_display", new=AsyncMock()),
        patch(
            "music_assistant.providers.squeezelite.player.evaluate_bridge_of", new=AsyncMock()
        ) as evaluate,
    ):
        await player.on_config_updated()
        await asyncio.sleep(0)

    evaluate.assert_awaited_once_with(mass, "p1")


async def test_a_protocol_that_is_removed_takes_its_bridge_client_along() -> None:
    """The Sendspin protocol of the bridge is removed with the protocol it rides on."""
    player, _mass = _player_with_mocked_mass()
    player._provider = MagicMock()
    player._provider.bridge_manager.remove_bridge = remove_bridge = AsyncMock()

    await player.on_remove()

    remove_bridge.assert_awaited_once_with("p1", permanent=True)
