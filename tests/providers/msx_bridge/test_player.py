"""Tests for MSXPlayer."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, Mock, patch

from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.msx_bridge.player import MSXPlayer
from music_assistant.providers.msx_bridge.provider import MSXBridgeProvider

# --- Initialization and properties ---


def test_init_defaults(player: MSXPlayer) -> None:
    """MSXPlayer should have correct default attributes."""
    assert player._attr_name == "Test TV"
    assert player._attr_type == PlayerType.PLAYER
    assert PlayerFeature.PAUSE in player._attr_supported_features
    assert PlayerFeature.SET_MEMBERS in player._attr_supported_features
    assert PlayerFeature.VOLUME_SET in player._attr_supported_features
    assert player._attr_available is True
    assert player._attr_powered is True
    assert player._attr_volume_level == 100
    assert player.output_format == "mp3"


def test_init_custom_params(provider: MSXBridgeProvider) -> None:
    """MSXPlayer should accept custom name and output_format."""
    p = MSXPlayer(provider, "msx_custom", name="Living Room TV", output_format="flac")
    object.__setattr__(p, "update_state", Mock())
    assert p._attr_name == "Living Room TV"
    assert p.output_format == "flac"


def test_needs_poll_always_true(player: MSXPlayer) -> None:
    """needs_poll should always return True."""
    assert player.needs_poll is True


def test_poll_interval_playing(player: MSXPlayer) -> None:
    """poll_interval should return 5 when playing."""
    player._attr_playback_state = PlaybackState.PLAYING
    assert player.poll_interval == 5


def test_poll_interval_not_playing(player: MSXPlayer) -> None:
    """poll_interval should return 30 when idle or paused."""
    player._attr_playback_state = PlaybackState.IDLE
    assert player.poll_interval == 30
    player._attr_playback_state = PlaybackState.PAUSED
    assert player.poll_interval == 30


# --- Playback ---


async def test_play_media(player: MSXPlayer) -> None:
    """play_media should store stream URL, set state to PLAYING, and reset elapsed."""
    media = Mock(spec=PlayerMedia)
    media.uri = "http://ma-server/stream/12345"

    await player.play_media(media)

    assert player.current_stream_url == "http://ma-server/stream/12345"
    assert player._attr_playback_state == PlaybackState.PLAYING
    assert player._attr_elapsed_time == 0
    assert player._attr_elapsed_time_last_updated is not None
    assert player._attr_current_media is media
    cast("Mock", player.update_state).assert_called()


async def test_play_resume(player: MSXPlayer) -> None:
    """play() should set state to PLAYING and update timestamp without touching elapsed."""
    player._attr_playback_state = PlaybackState.PAUSED
    player._attr_elapsed_time = 42.0

    await player.play()

    assert player._attr_playback_state == PlaybackState.PLAYING
    assert player._attr_elapsed_time == 42.0  # untouched
    assert player._attr_elapsed_time_last_updated is not None
    cast("Mock", player.update_state).assert_called()


async def test_pause_accumulates_time(player: MSXPlayer) -> None:
    """pause() should accumulate elapsed time from last update."""
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_elapsed_time = 10.0
    player._attr_elapsed_time_last_updated = 100.0

    with patch("music_assistant.providers.msx_bridge.player.time") as mock_time:
        mock_time.time.return_value = 115.0
        await player.pause()

    assert player._attr_playback_state == PlaybackState.PAUSED
    assert player._attr_elapsed_time == 25.0  # 10 + (115 - 100)
    cast("Mock", player.update_state).assert_called()


async def test_pause_none_elapsed(player: MSXPlayer) -> None:
    """pause() should not crash when elapsed_time is None."""
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_elapsed_time = None
    player._attr_elapsed_time_last_updated = None

    await player.pause()

    assert player._attr_playback_state == PlaybackState.PAUSED
    # elapsed stays None since there was nothing to accumulate
    assert player._attr_elapsed_time is None


async def test_stop_clears_all(player: MSXPlayer) -> None:
    """stop() should reset state, media, elapsed, and stream URL."""
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_current_media = Mock()
    player._attr_elapsed_time = 42.0
    player.current_stream_url = "http://something"

    await player.stop()

    assert player._attr_playback_state == PlaybackState.IDLE
    assert player._attr_current_media is None
    assert player._attr_elapsed_time is None  # type: ignore[unreachable]
    assert player._attr_elapsed_time_last_updated is None
    assert player.current_stream_url is None
    cast("Mock", player.update_state).assert_called()


async def test_stop_idempotent(player: MSXPlayer) -> None:
    """Calling stop() on an idle player should not raise."""
    player._attr_playback_state = PlaybackState.IDLE
    await player.stop()
    assert player._attr_playback_state == PlaybackState.IDLE


# --- Volume and polling ---


async def test_volume_set(player: MSXPlayer) -> None:
    """volume_set should update volume level and call update_state."""
    await player.volume_set(75)
    assert player._attr_volume_level == 75
    cast("Mock", player.update_state).assert_called()


async def test_poll_updates_elapsed(player: MSXPlayer) -> None:
    """poll() should accumulate elapsed time during PLAYING."""
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_elapsed_time = 10.0
    player._attr_elapsed_time_last_updated = 200.0

    with patch("music_assistant.providers.msx_bridge.player.time") as mock_time:
        mock_time.time.return_value = 205.0
        await player.poll()

    assert player._attr_elapsed_time == 15.0  # 10 + (205 - 200)
    assert player._attr_elapsed_time_last_updated == 205.0
    cast("Mock", player.update_state).assert_called()


async def test_poll_noop_when_paused(player: MSXPlayer) -> None:
    """poll() should not update anything when paused."""
    player._attr_playback_state = PlaybackState.PAUSED
    player._attr_elapsed_time = 42.0
    cast("Mock", player.update_state).reset_mock()

    await player.poll()

    assert player._attr_elapsed_time == 42.0
    cast("Mock", player.update_state).assert_not_called()


async def test_poll_noop_when_idle(player: MSXPlayer) -> None:
    """poll() should not update anything when idle."""
    player._attr_playback_state = PlaybackState.IDLE
    cast("Mock", player.update_state).reset_mock()

    await player.poll()

    cast("Mock", player.update_state).assert_not_called()


# --- Grouping ---


async def test_set_members_add_and_remove(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """set_members should add and remove group members."""
    leader = MSXPlayer(provider, "msx_leader", name="Leader TV", output_format="mp3")
    object.__setattr__(leader, "update_state", Mock())
    member = MSXPlayer(provider, "msx_member", name="Member TV", output_format="mp3")
    object.__setattr__(member, "update_state", Mock())
    mass_mock.players.get = Mock(side_effect=lambda pid: member if pid == "msx_member" else None)

    await leader.set_members(player_ids_to_add=["msx_member"])

    assert "msx_member" in leader._attr_group_members
    cast("Mock", leader.update_state).assert_called()

    await leader.set_members(player_ids_to_remove=["msx_member"])

    assert "msx_member" not in leader._attr_group_members


async def test_set_members_ignores_self_and_non_msx(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """set_members should not add self or non-MSX players."""
    leader = MSXPlayer(provider, "msx_leader", name="Leader TV", output_format="mp3")
    object.__setattr__(leader, "update_state", Mock())
    mass_mock.players.get = Mock(return_value=None)

    await leader.set_members(player_ids_to_add=["msx_leader", "msx_other", "sendspin_123"])

    assert leader._attr_group_members == []


async def test_play_media_propagates_to_group_members(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """play_media should propagate to group members when leader (direct member.play_media)."""
    leader = MSXPlayer(provider, "msx_leader", name="Leader TV", output_format="mp3")
    object.__setattr__(leader, "update_state", Mock())
    leader._attr_group_members = ["msx_member"]
    member = MSXPlayer(provider, "msx_member", name="Member TV", output_format="mp3")
    object.__setattr__(member, "update_state", Mock())
    object.__setattr__(member, "play_media", AsyncMock())
    mass_mock.players.get = Mock(return_value=member)

    media = Mock(spec=PlayerMedia)
    media.uri = "library://track/123"
    media.title = None
    media.artist = None
    media.image_url = None
    media.duration = None
    media.source_id = None
    media.queue_item_id = None

    with patch.object(leader.provider, "notify_play_started", Mock()):
        await leader.play_media(media)

    # We call member.play_media directly (not mass.players.play_media) to avoid redirect
    cast("Mock", member.play_media).assert_called_once_with(media)


async def test_play_media_no_propagation_when_empty_group(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """play_media with empty group_members should not call mass.players.play_media."""
    leader = MSXPlayer(provider, "msx_leader", name="Leader TV", output_format="mp3")
    object.__setattr__(leader, "update_state", Mock())
    leader._attr_group_members = []
    mass_mock.players.play_media = AsyncMock()

    media = Mock(spec=PlayerMedia)
    media.uri = "library://track/123"
    media.title = None
    media.artist = None
    media.image_url = None
    media.duration = None
    media.source_id = None
    media.queue_item_id = None

    with patch.object(leader.provider, "notify_play_started", Mock()):
        await leader.play_media(media)

    mass_mock.players.play_media.assert_not_called()


async def test_stop_propagates_to_group_members(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """stop() should propagate to group members when leader."""
    leader = MSXPlayer(provider, "msx_leader", name="Leader TV", output_format="mp3")
    object.__setattr__(leader, "update_state", Mock())
    leader._attr_group_members = ["msx_member"]
    member = MSXPlayer(provider, "msx_member", name="Member TV", output_format="mp3")
    object.__setattr__(member, "stop", AsyncMock())
    mass_mock.players.get = Mock(return_value=member)

    with patch.object(leader.provider, "notify_play_stopped", Mock()):
        await leader.stop()

    # group_members may include leader; we skip self and propagate only to members
    cast("Mock", member.stop).assert_called_once()
