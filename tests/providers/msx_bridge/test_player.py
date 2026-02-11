"""Tests for MSXPlayer."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.msx_bridge.player import MSXPlayer

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


def test_init_custom_params(provider: Any) -> None:
    """MSXPlayer should accept custom name and output_format."""
    p = MSXPlayer(provider, "msx_custom", name="Living Room TV", output_format="flac")
    p.update_state = Mock()  # type: ignore[misc,method-assign]
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
    player.update_state.assert_called()  # type: ignore[attr-defined]


async def test_play_resume(player: MSXPlayer, mass_mock: Mock) -> None:
    """play() when PAUSED should call queue resume to re-send the track to MSX."""
    player._attr_playback_state = PlaybackState.PAUSED
    player._attr_elapsed_time = 42.0

    await player.play()

    mass_mock.player_queues.resume.assert_awaited_once_with(player.player_id)


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
    player.update_state.assert_called()  # type: ignore[attr-defined]


async def test_pause_none_elapsed(player: MSXPlayer) -> None:
    """pause() should not crash when elapsed_time is None."""
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_elapsed_time = None
    player._attr_elapsed_time_last_updated = None

    await player.pause()

    assert player._attr_playback_state == PlaybackState.PAUSED
    # elapsed stays None since there was nothing to accumulate
    assert player._attr_elapsed_time is None


async def test_pause_notifies_stop_on_msx(player: MSXPlayer) -> None:
    """pause() should call provider.notify_play_stopped so MSX closes the player."""
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_elapsed_time = 10.0
    player._attr_elapsed_time_last_updated = 100.0

    with patch.object(player.provider, "notify_play_stopped") as mock_notify:
        await player.pause()

    assert player._attr_playback_state == PlaybackState.PAUSED
    mock_notify.assert_called_once_with(player.player_id)


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
    player.update_state.assert_called()


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
    player.update_state.assert_called()  # type: ignore[attr-defined]


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
    player.update_state.assert_called()  # type: ignore[attr-defined]


async def test_poll_noop_when_paused(player: MSXPlayer) -> None:
    """poll() should not update anything when paused."""
    player._attr_playback_state = PlaybackState.PAUSED
    player._attr_elapsed_time = 42.0
    player.update_state.reset_mock()  # type: ignore[attr-defined]

    await player.poll()

    assert player._attr_elapsed_time == 42.0
    player.update_state.assert_not_called()  # type: ignore[attr-defined]


async def test_poll_noop_when_idle(player: MSXPlayer) -> None:
    """poll() should not update anything when idle."""
    player._attr_playback_state = PlaybackState.IDLE
    player.update_state.reset_mock()  # type: ignore[attr-defined]

    await player.poll()

    player.update_state.assert_not_called()  # type: ignore[attr-defined]


# --- Grouping ---


async def test_set_members_add_and_remove(provider: Any, mass_mock: Mock) -> None:
    """set_members should add and remove group members."""
    leader = MSXPlayer(provider, "msx_leader", name="Leader TV", output_format="mp3")
    leader.update_state = Mock()  # type: ignore[misc,method-assign]
    member = MSXPlayer(provider, "msx_member", name="Member TV", output_format="mp3")
    member.update_state = Mock()  # type: ignore[misc,method-assign]
    mass_mock.players.get = Mock(side_effect=lambda pid: member if pid == "msx_member" else None)

    await leader.set_members(player_ids_to_add=["msx_member"])

    assert "msx_member" in leader._attr_group_members
    leader.update_state.assert_called()

    await leader.set_members(player_ids_to_remove=["msx_member"])

    assert "msx_member" not in leader._attr_group_members


async def test_set_members_ignores_self_and_non_msx(provider: Any, mass_mock: Mock) -> None:
    """set_members should not add self or non-MSX players."""
    leader = MSXPlayer(provider, "msx_leader", name="Leader TV", output_format="mp3")
    leader.update_state = Mock()  # type: ignore[misc,method-assign]
    mass_mock.players.get = Mock(return_value=None)

    await leader.set_members(player_ids_to_add=["msx_leader", "msx_other", "sendspin_123"])

    assert leader._attr_group_members == []


async def test_play_media_propagates_to_group_members(provider: Any, mass_mock: Mock) -> None:
    """play_media should propagate to group members when leader (direct member.play_media)."""
    leader = MSXPlayer(provider, "msx_leader", name="Leader TV", output_format="mp3")
    leader.update_state = Mock()  # type: ignore[misc,method-assign]
    leader._attr_group_members = ["msx_member"]
    member = MSXPlayer(provider, "msx_member", name="Member TV", output_format="mp3")
    member.update_state = Mock()  # type: ignore[misc,method-assign]
    member.play_media = AsyncMock()  # type: ignore[method-assign]
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
    member.play_media.assert_called_once_with(media)


async def test_play_media_no_propagation_when_empty_group(provider: Any, mass_mock: Mock) -> None:
    """play_media with empty group_members should not call mass.players.play_media."""
    leader = MSXPlayer(provider, "msx_leader", name="Leader TV", output_format="mp3")
    leader.update_state = Mock()  # type: ignore[misc,method-assign]
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


async def test_stop_propagates_to_group_members(provider: Any, mass_mock: Mock) -> None:
    """stop() should propagate to group members when leader."""
    leader = MSXPlayer(provider, "msx_leader", name="Leader TV", output_format="mp3")
    leader.update_state = Mock()  # type: ignore[misc,method-assign]
    leader._attr_group_members = ["msx_member"]
    member = MSXPlayer(provider, "msx_member", name="Member TV", output_format="mp3")
    member.stop = AsyncMock()  # type: ignore[method-assign]
    mass_mock.players.get = Mock(return_value=member)

    with patch.object(leader.provider, "notify_play_stopped", Mock()):
        await leader.stop()

    # group_members may include leader; we skip self and propagate only to members
    member.stop.assert_called_once()


# --- Grouping: disable and recursion guard ---


async def test_propagation_skipped_when_grouping_disabled(provider: Any, mass_mock: Mock) -> None:
    """play_media should NOT propagate to members when grouping is disabled."""
    provider.grouping_enabled = False
    leader = MSXPlayer(
        provider,
        "msx_leader",
        name="Leader TV",
        output_format="mp3",
        grouping_enabled=False,
    )
    leader.update_state = Mock()  # type: ignore[misc,method-assign]
    leader._attr_group_members = ["msx_member"]
    member = MSXPlayer(
        provider,
        "msx_member",
        name="Member TV",
        output_format="mp3",
        grouping_enabled=False,
    )
    member.play_media = AsyncMock()  # type: ignore[method-assign]
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

    member.play_media.assert_not_called()


def test_no_set_members_feature_when_grouping_disabled(provider: Any) -> None:
    """MSXPlayer should not declare SET_MEMBERS when grouping is disabled."""
    p = MSXPlayer(
        provider,
        "msx_nogrouping",
        name="Solo TV",
        output_format="mp3",
        grouping_enabled=False,
    )
    p.update_state = Mock()  # type: ignore[misc,method-assign]
    assert PlayerFeature.SET_MEMBERS not in p._attr_supported_features
    assert p._attr_can_group_with == set()


async def test_propagation_recursion_guard(provider: Any, mass_mock: Mock) -> None:
    """Propagation should not recurse when member.play_media triggers propagation."""
    leader = MSXPlayer(provider, "msx_leader", name="Leader TV", output_format="mp3")
    leader.update_state = Mock()  # type: ignore[misc,method-assign]
    leader._attr_group_members = ["msx_member"]

    # Create a member whose play_media calls back into leader's propagation
    member = MSXPlayer(provider, "msx_member", name="Member TV", output_format="mp3")
    member.update_state = Mock()  # type: ignore[misc,method-assign]
    member._attr_group_members = ["msx_leader"]  # would cause recursion without guard

    mass_mock.players.get = Mock(
        side_effect=lambda pid: member
        if pid == "msx_member"
        else leader
        if pid == "msx_leader"
        else None
    )

    media = Mock(spec=PlayerMedia)
    media.uri = "library://track/123"
    media.title = None
    media.artist = None
    media.image_url = None
    media.duration = None
    media.source_id = None
    media.queue_item_id = None

    with patch.object(leader.provider, "notify_play_started", Mock()):
        # This should NOT infinitely recurse
        await leader.play_media(media)

    # Leader played successfully (no exception from recursion)
    assert leader._attr_playback_state == PlaybackState.PLAYING


# --- Queue-backed playlist playback ---


async def test_play_media_queue_sends_playlist(player: MSXPlayer, mass_mock: Mock) -> None:
    """play_media with queue context should send playlist via WS instead of stream."""
    media = Mock(spec=PlayerMedia)
    media.uri = "http://ma-server/stream/12345"
    media.title = "Track 1"
    media.artist = "Artist 1"
    media.image_url = None
    media.duration = 180
    media.source_id = "msx_test"
    media.queue_item_id = "qi1"

    queue = Mock()
    queue.current_index = 2
    mass_mock.player_queues.get.return_value = queue
    mass_mock.player_queues.get_item.return_value = None

    with (
        patch.object(player.provider, "notify_play_playlist") as mock_playlist,
        patch.object(player.provider, "notify_play_started") as mock_play,
    ):
        await player.play_media(media)

    mock_playlist.assert_called_once_with("msx_test", 2)
    mock_play.assert_not_called()
    assert player._playing_from_queue is True


async def test_play_media_skips_ws_when_playing_from_queue(
    player: MSXPlayer, mass_mock: Mock
) -> None:
    """play_media should skip WS notification when _playing_from_queue is True."""
    player._playing_from_queue = True

    media = Mock(spec=PlayerMedia)
    media.uri = "http://ma-server/stream/12345"
    media.title = None
    media.artist = None
    media.image_url = None
    media.duration = None
    media.source_id = "msx_test"
    media.queue_item_id = "qi2"

    mass_mock.player_queues.get_item.return_value = None

    with (
        patch.object(player.provider, "notify_play_playlist") as mock_playlist,
        patch.object(player.provider, "notify_play_started") as mock_play,
    ):
        await player.play_media(media)

    mock_playlist.assert_not_called()
    mock_play.assert_not_called()


async def test_play_media_non_queue_sends_broadcast_play(
    player: MSXPlayer,
) -> None:
    """play_media without queue context should use broadcast_play as before."""
    media = Mock(spec=PlayerMedia)
    media.uri = "http://ma-server/stream/12345"
    media.title = "Track 1"
    media.artist = "Artist 1"
    media.image_url = None
    media.duration = 180
    media.source_id = None
    media.queue_item_id = None

    with (
        patch.object(player.provider, "notify_play_playlist") as mock_playlist,
        patch.object(player.provider, "notify_play_started") as mock_play,
    ):
        await player.play_media(media)

    mock_playlist.assert_not_called()
    mock_play.assert_called_once()


async def test_stop_resets_playing_from_queue(player: MSXPlayer) -> None:
    """stop() should reset _playing_from_queue flag."""
    player._playing_from_queue = True
    await player.stop()
    assert player._playing_from_queue is False
