"""Tests for the AmpliPi zone player."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlaybackState, PlayerFeature
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.providers.amplipi.constants import SOURCE_DISCONNECTED, ZONE_OFF
from music_assistant.providers.amplipi.player import AmpliPiZonePlayer
from music_assistant.providers.amplipi.provider import AmpliPiPlayerProvider


def _zone(
    zone_id: int, source_id: int = SOURCE_DISCONNECTED, disabled: bool = False
) -> SimpleNamespace:
    """Build a lightweight stand-in for a pyamplipi Zone."""
    return SimpleNamespace(
        id=zone_id,
        name=f"Zone {zone_id}",
        source_id=source_id,
        disabled=disabled,
        mute=False,
        vol=-30,
        vol_f=0.5,
    )


def _source(
    source_id: int, input_str: str | None = "", state: str | None = None
) -> SimpleNamespace:
    """Build a lightweight stand-in for a pyamplipi Source."""
    info = SimpleNamespace(state=state) if state is not None else None
    return SimpleNamespace(id=source_id, input=input_str, info=info)


def _status(zones: list[SimpleNamespace], sources: list[SimpleNamespace]) -> SimpleNamespace:
    """Build a lightweight stand-in for a pyamplipi Status."""
    return SimpleNamespace(zones=zones, sources=sources)


@pytest.fixture
def mock_provider() -> MagicMock:
    """Create a mock AmpliPiPlayerProvider with an async API."""
    provider = MagicMock()
    provider.instance_id = "amplipi_test"
    provider.domain = "amplipi"
    provider.manifest = MagicMock()
    provider.manifest.domain = "amplipi"

    provider.api = MagicMock()
    provider.api.set_zone = AsyncMock()
    provider.api.set_zones = AsyncMock()
    provider.api.set_source = AsyncMock()
    provider.api.play_stream = AsyncMock()
    provider.api.pause_stream = AsyncMock()
    provider.api.stop_stream = AsyncMock()
    provider.api.get_source = AsyncMock()
    # the provider creates/reuses one internetradio stream per source for MA playback
    provider.ensure_stream = AsyncMock(return_value=42)
    # selectable AmpliPi sources (native streams + RCA inputs); empty by default
    provider.selectable_streams = MagicMock(return_value=[])

    # default status: 4 sources (all free), zones 0..3 disconnected
    provider.status = _status(
        zones=[_zone(i) for i in range(4)],
        sources=[_source(i, input_str="") for i in range(4)],
    )
    # map player_id -> zone id
    provider.zone_id_for = MagicMock(
        side_effect=lambda pid: (
            int(pid.rsplit("_", 1)[1]) if pid.startswith("amplipi_test_zone_") else None
        )
    )

    provider.mass = MagicMock()
    provider.mass.players = MagicMock()
    provider.mass.streams.resolve_stream_url = AsyncMock(return_value="http://ma/stream.flac")
    config = MagicMock()
    config.name = None
    config.default_name = "Zone"
    config.enabled = True
    config.player_type = None
    config.get_value = MagicMock(return_value=None)
    provider.mass.config.get_base_player_config.return_value = config
    return provider


def _make_player(provider: MagicMock, zone_id: int) -> AmpliPiZonePlayer:
    """Build a player with update_state patched out (heavy state calc)."""
    player = AmpliPiZonePlayer(provider=provider, zone_id=zone_id)
    player.update_state = MagicMock()  # type: ignore[misc,method-assign]
    return player


class TestSupportedFeatures:
    """Verify the player advertises the expected features."""

    def test_required_features(self, mock_provider: MagicMock) -> None:
        """Power, play_media, grouping and volume should be supported."""
        player = _make_player(mock_provider, 0)
        for feature in (
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.PAUSE,
            PlayerFeature.POWER,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.SET_MEMBERS,
        ):
            assert feature in player.supported_features

    def test_player_id_includes_instance_and_zone(self, mock_provider: MagicMock) -> None:
        """Player id should be namespaced by provider instance and zone id."""
        player = _make_player(mock_provider, 3)
        assert player.player_id == "amplipi_test_zone_3"
        assert player.zone_id == 3

    def test_player_name_uses_zone_name(self, mock_provider: MagicMock) -> None:
        """The player name should be the AmpliPi zone's friendly name, not the raw id."""
        mock_provider.status = _status(
            zones=[SimpleNamespace(id=2, name="Living Room")],
            sources=[],
        )
        player = _make_player(mock_provider, 2)
        assert player.name == "Living Room"

    def test_player_name_falls_back_when_unset(self, mock_provider: MagicMock) -> None:
        """An unnamed AmpliPi zone gets a sensible 1-based default name."""
        mock_provider.status = _status(
            zones=[SimpleNamespace(id=1, name="")],
            sources=[],
        )
        player = _make_player(mock_provider, 1)
        assert player.name == "AmpliPi Zone 2"

    def test_player_name_tracks_zone_rename(self, mock_provider: MagicMock) -> None:
        """A zone renamed in AmpliPi propagates to the player name on the next poll."""
        player = _make_player(mock_provider, 0)
        status = _status(
            zones=[SimpleNamespace(id=0, name="Kitchen", source_id=-1, mute=False, vol=-30)],
            sources=[],
        )
        player.update_from_status(status)
        assert player.name == "Kitchen"


class TestStatePolling:
    """Test that polling reconciles state without trusting AmpliPi's unreliable source state."""

    def test_poll_keeps_commanded_state_while_connected(self, mock_provider: MagicMock) -> None:
        """While the zone is connected, our commanded playback_state must be preserved."""
        player = _make_player(mock_provider, 0)
        player._attr_playback_state = PlaybackState.PLAYING
        # AmpliPi reports the fileplayer as "stopped" even though audio is playing
        status = _status(
            zones=[_zone(0, source_id=0)],
            sources=[_source(0, input_str="stream=42", state="stopped")],
        )
        player.update_from_status(status)
        assert player.playback_state == PlaybackState.PLAYING

    def test_poll_idle_when_disconnected(self, mock_provider: MagicMock) -> None:
        """A disconnected zone reports IDLE regardless of prior state."""
        player = _make_player(mock_provider, 0)
        player._attr_playback_state = PlaybackState.PLAYING
        status = _status(
            zones=[_zone(0, source_id=SOURCE_DISCONNECTED)],
            sources=[_source(0, input_str="")],
        )
        player.update_from_status(status)
        assert player.playback_state == PlaybackState.IDLE
        assert player.active_source is None


class TestPower:
    """Test the power command transitions (the bug from the issue)."""

    async def test_power_on_connects_disconnected(self, mock_provider: MagicMock) -> None:
        """Powering on should set the zone to SOURCE_DISCONNECTED (on, idle)."""
        player = _make_player(mock_provider, 1)
        await player.power(True)
        mock_provider.api.set_zone.assert_awaited_once()
        zone_id, update = mock_provider.api.set_zone.await_args.args
        assert zone_id == 1
        assert update.source_id == SOURCE_DISCONNECTED
        assert player.powered is True

    async def test_power_off_sets_zone_off(self, mock_provider: MagicMock) -> None:
        """Powering off should set the zone (and members) to ZONE_OFF."""
        player = _make_player(mock_provider, 1)
        await player.power(False)
        mock_provider.api.set_zones.assert_awaited_once()
        multi = mock_provider.api.set_zones.await_args.args[0]
        assert multi.zones == [1]
        assert multi.update.source_id == ZONE_OFF
        assert player.powered is False


class TestAcquireSource:
    """Test the source acquisition policy (4 sources for 6+ zones)."""

    async def test_reuses_current_source(self, mock_provider: MagicMock) -> None:
        """If the zone already holds a source, reuse it."""
        player = _make_player(mock_provider, 0)
        player._source_id = 2
        source = await player._acquire_source()
        assert source is not None
        assert source.id == 2

    async def test_claims_free_source(self, mock_provider: MagicMock) -> None:
        """A free source (not bound to a zone) should be claimed."""
        mock_provider.status = _status(
            zones=[_zone(0, source_id=0), _zone(1)],
            sources=[
                _source(0, input_str="stream=10"),
                _source(1, input_str=""),
                _source(2, input_str="None"),
                _source(3, input_str="stream=11"),
            ],
        )
        player = _make_player(mock_provider, 1)
        source = await player._acquire_source()
        assert source is not None
        assert source.id == 1

    async def test_all_sources_in_use_returns_none(self, mock_provider: MagicMock) -> None:
        """With all 4 sources bound to zones, acquisition fails."""
        mock_provider.status = _status(
            zones=[_zone(i, source_id=i) for i in range(4)] + [_zone(4)],
            sources=[_source(i, input_str=f"stream={i}") for i in range(4)],
        )
        player = _make_player(mock_provider, 4)
        assert await player._acquire_source() is None

    async def test_falls_back_to_unbound_busy_source(self, mock_provider: MagicMock) -> None:
        """A source with a non-free input but no zone attached is claimed as a fallback."""
        mock_provider.status = _status(
            zones=[_zone(0, source_id=0)],
            sources=[_source(0, input_str="stream=1"), _source(1, input_str="stream=99")],
        )
        player = _make_player(mock_provider, 5)
        player._source_id = None
        source = await player._acquire_source()
        assert source is not None
        assert source.id == 1


class TestPlayMedia:
    """Test play_media wiring."""

    async def test_play_media_acquires_and_plays(self, mock_provider: MagicMock) -> None:
        """play_media should resolve a url, claim a source, connect the zone and play."""
        mock_provider.status = _status(
            zones=[_zone(0)],
            sources=[_source(0, input_str="")],
        )
        player = _make_player(mock_provider, 0)
        media = MagicMock()
        await player.play_media(media)

        mock_provider.mass.streams.resolve_stream_url.assert_awaited_once()
        # an internetradio stream is created/reused for the source and pointed at the url
        mock_provider.ensure_stream.assert_awaited_once_with(0, "http://ma/stream.flac")

        mock_provider.api.set_zones.assert_awaited_once()
        multi = mock_provider.api.set_zones.await_args.args[0]
        assert multi.zones == [0]
        assert multi.update.source_id == 0
        # the zone must be unmuted, otherwise AmpliPi plays silently
        assert multi.update.mute is False

        assert player.active_source == player.player_id
        assert player.playback_state == PlaybackState.PLAYING
        assert player.powered is True
        # AmpliPi reports no position, so play_media must seed the self-clock: without it
        # the queue elapsed time stays pinned at the stream start and pause/seek-resume
        # would always jump back to the start (see _get_flow_queue_stream_index).
        assert player.elapsed_time == 0
        assert player.elapsed_time_last_updated is not None

    async def test_play_media_binds_source_to_stream_and_plays(
        self, mock_provider: MagicMock
    ) -> None:
        """play_media must point the source at the MA stream and start it."""
        mock_provider.status = _status(
            zones=[_zone(0)],
            sources=[_source(0, input_str="")],
        )
        player = _make_player(mock_provider, 0)
        await player.play_media(MagicMock())

        mock_provider.api.set_source.assert_awaited_once()
        source_id, update = mock_provider.api.set_source.await_args.args
        assert source_id == 0
        assert update.input == "stream=42"
        mock_provider.api.play_stream.assert_awaited_once_with(42)

    async def test_play_media_detaches_stray_zones(self, mock_provider: MagicMock) -> None:
        """play_media must detach zones left on the source that are not in the group."""
        # our zone 0 owns source 0, but zones 2 and 3 are stale leftovers on it too
        mock_provider.status = _status(
            zones=[_zone(0, source_id=0), _zone(2, source_id=0), _zone(3, source_id=0)],
            sources=[_source(0, input_str="stream=42")],
        )
        player = _make_player(mock_provider, 0)
        player._source_id = 0
        await player.play_media(MagicMock())

        calls = mock_provider.api.set_zones.await_args_list
        # first the strays are disconnected, then our group is attached
        detach = calls[0].args[0]
        assert set(detach.zones) == {2, 3}
        assert detach.update.source_id == SOURCE_DISCONNECTED
        attach = calls[1].args[0]
        assert attach.zones == [0]
        assert attach.update.source_id == 0
        assert attach.update.mute is False

    async def test_play_media_attaches_group_members(self, mock_provider: MagicMock) -> None:
        """play_media must attach the leader's grouped members alongside its own zone."""
        mock_provider.status = _status(
            zones=[_zone(0), _zone(2)],
            sources=[_source(0, input_str="")],
        )
        player = _make_player(mock_provider, 0)
        player._attr_group_members = [player.player_id, "amplipi_test_zone_2"]
        await player.play_media(MagicMock())
        attach = mock_provider.api.set_zones.await_args.args[0]
        assert set(attach.zones) == {0, 2}

    async def test_play_media_raises_when_no_source(self, mock_provider: MagicMock) -> None:
        """play_media should raise when no source can be acquired."""
        mock_provider.status = _status(
            zones=[_zone(i, source_id=i) for i in range(4)] + [_zone(4)],
            sources=[_source(i, input_str=f"stream={i}") for i in range(4)],
        )
        player = _make_player(mock_provider, 4)
        with pytest.raises(PlayerCommandFailed):
            await player.play_media(MagicMock())


class TestSelectableStreams:
    """Test the provider's selectable-source filtering (verified against live hardware shape)."""

    def test_excludes_ma_streams_and_fileplayer(self) -> None:
        """Native streams + RCA inputs are selectable; MA's own streams and fileplayer are not."""
        prov = AmpliPiPlayerProvider.__new__(AmpliPiPlayerProvider)
        prov._streams = [
            SimpleNamespace(id=996, name="Input 1", type="rca"),
            SimpleNamespace(id=1000, name="Groove Salad", type="internetradio"),
            SimpleNamespace(id=1003, name="AmpliPro 1", type="airplay"),
            SimpleNamespace(id=1008, name="External Media", type="fileplayer"),
            SimpleNamespace(id=1009, name="Music Assistant 3", type="internetradio"),
        ]
        ids = {s.id for s in AmpliPiPlayerProvider.selectable_streams(prov)}
        assert ids == {996, 1000, 1003}


class TestSelectSource:
    """Test SELECT_SOURCE for AmpliPi-side sources (native streams + RCA/line inputs)."""

    def test_select_source_feature_advertised(self, mock_provider: MagicMock) -> None:
        """The player should advertise SELECT_SOURCE."""
        player = _make_player(mock_provider, 0)
        assert PlayerFeature.SELECT_SOURCE in player.supported_features

    def test_source_list_built_from_selectable_streams(self, mock_provider: MagicMock) -> None:
        """source_list should expose selectable AmpliPi streams as 'stream=<id>' sources."""
        mock_provider.selectable_streams = MagicMock(
            return_value=[
                SimpleNamespace(id=3, name="Turntable", type="rca"),
                SimpleNamespace(id=7, name="AirPlay", type="airplay"),
            ]
        )
        mock_provider.status = _status(zones=[_zone(0, source_id=SOURCE_DISCONNECTED)], sources=[])
        player = _make_player(mock_provider, 0)
        player.update_from_status(mock_provider.status)
        by_id = {s.id: s for s in player.source_list}
        assert set(by_id) == {"stream=3", "stream=7"}
        assert all(not s.passive for s in player.source_list)
        # routing-only sources: no transport advertised
        assert all(
            not (s.can_play_pause or s.can_seek or s.can_next_previous) for s in player.source_list
        )
        # physical inputs keep their name; native streams get a disambiguating type suffix
        assert by_id["stream=3"].name == "Turntable"
        assert by_id["stream=7"].name == "AirPlay (AirPlay)"

    async def test_select_source_sets_input_and_attaches(self, mock_provider: MagicMock) -> None:
        """Selecting a source should point an AmpliPi source at the stream and attach the zone."""
        mock_provider.status = _status(
            zones=[_zone(0)],
            sources=[_source(0, input_str="")],
        )
        player = _make_player(mock_provider, 0)
        await player.select_source("stream=3")

        source_id, update = mock_provider.api.set_source.await_args.args
        assert source_id == 0
        assert update.input == "stream=3"
        attach = mock_provider.api.set_zones.await_args.args[0]
        assert attach.zones == [0]
        assert attach.update.source_id == 0
        assert attach.update.mute is False
        assert player.active_source == "stream=3"
        assert player.playback_state == PlaybackState.PLAYING

    async def test_select_source_rejects_unknown(self, mock_provider: MagicMock) -> None:
        """An id that is not a 'stream=<id>' source should be rejected."""
        player = _make_player(mock_provider, 0)
        with pytest.raises(PlayerCommandFailed):
            await player.select_source("hdmi1")

    async def test_select_source_raises_when_no_source(self, mock_provider: MagicMock) -> None:
        """Selecting a source must fail when no AmpliPi source can be acquired."""
        mock_provider.status = _status(
            zones=[_zone(i, source_id=i) for i in range(4)] + [_zone(4)],
            sources=[_source(i, input_str=f"stream={i}") for i in range(4)],
        )
        player = _make_player(mock_provider, 4)
        with pytest.raises(PlayerCommandFailed):
            await player.select_source("stream=3")


class TestVolume:
    """Test volume handling."""

    async def test_volume_set_maps_to_usable_db_window(self, mock_provider: MagicMock) -> None:
        """Volume 0..100 maps onto a usable dB window (0 -> floor, 100 -> 0 dB)."""
        player = _make_player(mock_provider, 0)
        await player.volume_set(80)
        mock_provider.api.set_zone.assert_awaited_once()
        _zone_id, update = mock_provider.api.set_zone.await_args.args
        # 80% across a -60..0 dB window -> -12 dB
        assert update.vol == -12
        assert player.volume_level == 80

    async def test_volume_mute(self, mock_provider: MagicMock) -> None:
        """Muting should write mute=True to the zone and reflect it locally."""
        player = _make_player(mock_provider, 0)
        await player.volume_mute(True)
        _zone_id, update = mock_provider.api.set_zone.await_args.args
        assert update.mute is True
        assert player.volume_muted is True

    async def test_volume_db_roundtrip(self, mock_provider: MagicMock) -> None:
        """Mapping a volume to dB and back should return the original value."""
        for level in (0, 25, 50, 80, 100):
            assert AmpliPiZonePlayer._db_to_volume(AmpliPiZonePlayer._volume_to_db(level)) == level


class TestPlayStop:
    """Test the play/stop transport against the active stream."""

    def test_requires_flow_mode(self, mock_provider: MagicMock) -> None:
        """AmpliPi plays a single stream url, so flow mode is required."""
        assert _make_player(mock_provider, 0).requires_flow_mode is True

    async def test_play_starts_active_stream(self, mock_provider: MagicMock) -> None:
        """play() should resume the stream currently bound to this zone's source."""
        player = _make_player(mock_provider, 0)
        player._source_id = 0
        mock_provider.api.get_source = AsyncMock(return_value=SimpleNamespace(input="stream=5"))
        await player.play()
        mock_provider.api.play_stream.assert_awaited_once_with(5)
        assert player.playback_state == PlaybackState.PLAYING

    async def test_play_without_active_stream(self, mock_provider: MagicMock) -> None:
        """With no source bound, play() still reports PLAYING but issues no stream command."""
        player = _make_player(mock_provider, 0)
        player._source_id = None
        # an inactive queue must not divert play() into the resume path
        mock_provider.mass.player_queues.get.return_value = SimpleNamespace(active=False)
        await player.play()
        mock_provider.api.play_stream.assert_not_awaited()
        assert player.playback_state == PlaybackState.PLAYING

    async def test_play_resumes_from_pause_via_queue(self, mock_provider: MagicMock) -> None:
        """Unpausing a paused MA queue re-resolves from the saved position, not the stale stream."""
        player = _make_player(mock_provider, 0)
        player._source_id = 0
        player._attr_playback_state = PlaybackState.PAUSED
        mock_provider.mass.player_queues.get.return_value = SimpleNamespace(active=True)
        mock_provider.mass.player_queues.resume = AsyncMock()
        await player.play()
        # AmpliPi cannot unpause in place, so play() delegates to the queue's resume
        mock_provider.mass.player_queues.resume.assert_awaited_once_with(player.player_id)
        # it must NOT replay the stale (stopped) stream
        mock_provider.api.play_stream.assert_not_awaited()

    async def test_stop_stops_active_stream(self, mock_provider: MagicMock) -> None:
        """stop() should stop the bound stream and mark the zone idle."""
        player = _make_player(mock_provider, 0)
        player._source_id = 0
        mock_provider.api.get_source = AsyncMock(return_value=SimpleNamespace(input="stream=5"))
        await player.stop()
        mock_provider.api.stop_stream.assert_awaited_once_with(5)
        assert player.playback_state == PlaybackState.IDLE
        assert player.stop_called is True

    async def test_pause_stops_active_stream(self, mock_provider: MagicMock) -> None:
        """pause() should stop the bound stream but report PAUSED (not idle)."""
        player = _make_player(mock_provider, 0)
        player._source_id = 0
        mock_provider.api.get_source = AsyncMock(return_value=SimpleNamespace(input="stream=5"))
        await player.pause()
        mock_provider.api.stop_stream.assert_awaited_once_with(5)
        assert player.playback_state == PlaybackState.PAUSED
        # unlike stop(), pause() must not mark the queue as stopped (resume re-plays it)
        assert player.stop_called is False

    async def test_pause_without_active_stream(self, mock_provider: MagicMock) -> None:
        """With no source bound, pause() still reports PAUSED but issues no stream command."""
        player = _make_player(mock_provider, 0)
        player._source_id = None
        await player.pause()
        mock_provider.api.stop_stream.assert_not_awaited()
        assert player.playback_state == PlaybackState.PAUSED

    async def test_active_stream_id_non_stream_input(self, mock_provider: MagicMock) -> None:
        """A source input that is not 'stream=<id>' yields no active stream."""
        player = _make_player(mock_provider, 0)
        player._source_id = 0
        mock_provider.api.get_source = AsyncMock(return_value=SimpleNamespace(input="rca"))
        assert await player._active_stream_id() is None

    async def test_active_stream_id_unparsable(self, mock_provider: MagicMock) -> None:
        """A non-numeric stream id is ignored rather than raising."""
        player = _make_player(mock_provider, 0)
        player._source_id = 0
        mock_provider.api.get_source = AsyncMock(return_value=SimpleNamespace(input="stream=x"))
        assert await player._active_stream_id() is None


class TestAvailability:
    """Test the unavailable / status-reconciliation edge cases."""

    def test_set_unavailable_is_idempotent(self, mock_provider: MagicMock) -> None:
        """Marking an already-unavailable player must not re-emit a state update."""
        player = _make_player(mock_provider, 0)
        player._attr_available = True
        player.set_unavailable()
        assert player.available is False
        player.update_state.reset_mock()  # type: ignore[attr-defined]
        player.set_unavailable()
        player.update_state.assert_not_called()  # type: ignore[attr-defined]

    def test_update_from_status_missing_zone(self, mock_provider: MagicMock) -> None:
        """If the zone is absent from the status the player goes unavailable."""
        player = _make_player(mock_provider, 0)
        player._attr_available = True
        player.update_from_status(_status(zones=[_zone(1)], sources=[]))
        assert player.available is False

    def test_reflects_external_active_source(self, mock_provider: MagicMock) -> None:
        """A connected user-selectable AmpliPi source is surfaced as the active source."""
        mock_provider.selectable_streams = MagicMock(
            return_value=[SimpleNamespace(id=7, name="AirPlay", type="airplay")]
        )
        mock_provider.status = _status(
            zones=[_zone(0, source_id=0)],
            sources=[_source(0, input_str="stream=7")],
        )
        player = _make_player(mock_provider, 0)
        player.update_from_status(mock_provider.status)
        assert player.active_source == "stream=7"

    def test_connected_non_stream_source_defaults_to_ma(self, mock_provider: MagicMock) -> None:
        """A connected source that is not a selectable external stream defaults to MA playback."""
        mock_provider.status = _status(
            zones=[_zone(0, source_id=0)],
            sources=[_source(0, input_str="")],
        )
        player = _make_player(mock_provider, 0)
        player.update_from_status(mock_provider.status)
        assert player.active_source == player.player_id

    def test_external_active_source_cleared_when_input_changes(
        self, mock_provider: MagicMock
    ) -> None:
        """A previously-reflected external source must not stay selected after the input changes."""
        mock_provider.selectable_streams = MagicMock(
            return_value=[SimpleNamespace(id=7, name="AirPlay", type="airplay")]
        )
        player = _make_player(mock_provider, 0)
        # external source selected and reflected
        mock_provider.status = _status(
            zones=[_zone(0, source_id=0)], sources=[_source(0, input_str="stream=7")]
        )
        player.update_from_status(mock_provider.status)
        assert player.active_source == "stream=7"
        # input changes back to our own (non-selectable) MA stream: must revert to MA playback
        mock_provider.status = _status(
            zones=[_zone(0, source_id=0)], sources=[_source(0, input_str="stream=42")]
        )
        player.update_from_status(mock_provider.status)
        assert player.active_source == player.player_id


class TestGrouping:
    """Test set_members grouping behaviour."""

    async def test_add_member_connects_to_leader_source(self, mock_provider: MagicMock) -> None:
        """Adding a member should connect its zone to the leader's source."""
        leader = _make_player(mock_provider, 0)
        leader._source_id = 1
        await leader.set_members(player_ids_to_add=["amplipi_test_zone_2"])
        mock_provider.api.set_zones.assert_awaited()
        multi = mock_provider.api.set_zones.await_args.args[0]
        assert multi.zones == [2]
        assert multi.update.source_id == 1
        # the added zone must be unmuted, otherwise it joins the group silently
        assert multi.update.mute is False
        assert leader.group_members == [leader.player_id, "amplipi_test_zone_2"]

    async def test_remove_member_disconnects(self, mock_provider: MagicMock) -> None:
        """Removing the last member should dissolve the group view."""
        leader = _make_player(mock_provider, 0)
        leader._source_id = 1
        leader._attr_group_members = [leader.player_id, "amplipi_test_zone_2"]
        await leader.set_members(player_ids_to_remove=["amplipi_test_zone_2"])
        multi = mock_provider.api.set_zones.await_args.args[0]
        assert multi.zones == [2]
        assert multi.update.source_id == SOURCE_DISCONNECTED
        assert leader.group_members == []

    async def test_add_member_acquires_source_for_leader(self, mock_provider: MagicMock) -> None:
        """A sourceless leader claims a source (and connects its own zone) before grouping."""
        mock_provider.status = _status(
            zones=[_zone(0), _zone(2)],
            sources=[_source(0, input_str="")],
        )
        leader = _make_player(mock_provider, 0)
        # leader has no source yet (the default after construction)
        await leader.set_members(player_ids_to_add=["amplipi_test_zone_2"])
        assert leader._source_id == 0
        # the leader's own zone is connected/unmuted to the acquired source
        zone_id, update = mock_provider.api.set_zone.await_args.args
        assert zone_id == 0
        assert update.source_id == 0
        assert update.mute is False
        assert leader.group_members == [leader.player_id, "amplipi_test_zone_2"]

    async def test_add_member_raises_when_no_source(self, mock_provider: MagicMock) -> None:
        """Grouping fails if the sourceless leader cannot acquire a source."""
        mock_provider.status = _status(
            zones=[_zone(i, source_id=i) for i in range(4)] + [_zone(5)],
            sources=[_source(i, input_str=f"stream={i}") for i in range(4)],
        )
        leader = _make_player(mock_provider, 5)
        leader._source_id = None
        with pytest.raises(PlayerCommandFailed):
            await leader.set_members(player_ids_to_add=["amplipi_test_zone_2"])

    async def test_add_unmapped_member_skips_api(self, mock_provider: MagicMock) -> None:
        """Adding only unknown/unmapped player ids must not issue an empty set_zones call."""
        leader = _make_player(mock_provider, 0)
        leader._source_id = 1
        await leader.set_members(player_ids_to_add=["nonexistent"])
        mock_provider.api.set_zones.assert_not_awaited()

    async def test_remove_unmapped_member_skips_api(self, mock_provider: MagicMock) -> None:
        """Removing only unknown/unmapped player ids must not issue an empty set_zones call."""
        leader = _make_player(mock_provider, 0)
        leader._source_id = 1
        leader._attr_group_members = [leader.player_id, "nonexistent"]
        await leader.set_members(player_ids_to_remove=["nonexistent"])
        mock_provider.api.set_zones.assert_not_awaited()
        assert leader.group_members == []

    def test_poll_prunes_disconnected_members(self, mock_provider: MagicMock) -> None:
        """A polled member no longer on the leader's source is pruned from the group."""
        mock_provider.status = _status(
            zones=[_zone(0, source_id=0), _zone(2, source_id=SOURCE_DISCONNECTED)],
            sources=[_source(0, input_str="stream=42")],
        )
        leader = _make_player(mock_provider, 0)
        leader._attr_group_members = [leader.player_id, "amplipi_test_zone_2"]
        leader.update_from_status(mock_provider.status)
        assert leader.group_members == []

    def test_poll_keeps_connected_members(self, mock_provider: MagicMock) -> None:
        """A polled member still on the leader's source is retained in the group."""
        mock_provider.status = _status(
            zones=[_zone(0, source_id=0), _zone(2, source_id=0)],
            sources=[_source(0, input_str="stream=42")],
        )
        leader = _make_player(mock_provider, 0)
        leader._attr_group_members = [leader.player_id, "amplipi_test_zone_2"]
        leader.update_from_status(mock_provider.status)
        assert leader.group_members == [leader.player_id, "amplipi_test_zone_2"]

    def test_poll_dissolves_group_when_disconnected(self, mock_provider: MagicMock) -> None:
        """When the leader zone itself disconnects, its group is dissolved entirely."""
        mock_provider.status = _status(
            zones=[_zone(0, source_id=SOURCE_DISCONNECTED)],
            sources=[],
        )
        leader = _make_player(mock_provider, 0)
        leader._attr_group_members = [leader.player_id, "amplipi_test_zone_2"]
        leader.update_from_status(mock_provider.status)
        assert leader.group_members == []
        mock_provider.mass.players.trigger_player_update.assert_any_call("amplipi_test_zone_2")
