"""Tests for the AmpliPi zone player."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlaybackState, PlayerFeature
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.providers.amplipi.constants import SOURCE_DISCONNECTED, ZONE_OFF
from music_assistant.providers.amplipi.player import AmpliPiZonePlayer


def _zone(
    zone_id: int, source_id: int = SOURCE_DISCONNECTED, disabled: bool = False
) -> SimpleNamespace:
    """Build a lightweight stand-in for a pyamplipi Zone."""
    return SimpleNamespace(
        id=zone_id,
        source_id=source_id,
        disabled=disabled,
        mute=False,
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
    provider.api.play_media = AsyncMock()
    provider.api.play_stream = AsyncMock()
    provider.api.pause_stream = AsyncMock()
    provider.api.stop_stream = AsyncMock()
    provider.api.get_source = AsyncMock()

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
        """Power, play_media, grouping, volume and pause should be supported."""
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


class TestStateMapping:
    """Test mapping of AmpliPi source state to MA playback state."""

    def test_playing(self) -> None:
        """A playing source maps to PLAYING."""
        assert AmpliPiZonePlayer._map_state(_source(0, state="playing")) == PlaybackState.PLAYING

    def test_paused(self) -> None:
        """A paused source maps to PAUSED."""
        assert AmpliPiZonePlayer._map_state(_source(0, state="paused")) == PlaybackState.PAUSED

    def test_stopped(self) -> None:
        """A stopped source maps to IDLE."""
        assert AmpliPiZonePlayer._map_state(_source(0, state="stopped")) == PlaybackState.IDLE

    def test_no_source(self) -> None:
        """A missing source maps to IDLE."""
        assert AmpliPiZonePlayer._map_state(None) == PlaybackState.IDLE

    def test_unknown_state(self) -> None:
        """An unrecognised source state falls back to IDLE."""
        assert AmpliPiZonePlayer._map_state(_source(0, state="buffering")) == PlaybackState.IDLE


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
        mock_provider.api.set_zones.assert_awaited_once()
        multi = mock_provider.api.set_zones.await_args.args[0]
        assert multi.zones == [0]
        assert multi.update.source_id == 0

        mock_provider.api.play_media.assert_awaited_once()
        play = mock_provider.api.play_media.await_args.args[0]
        assert play.source_id == 0
        assert play.media == "http://ma/stream.flac"

        assert player.active_source == player.player_id
        assert player.playback_state == PlaybackState.PLAYING
        assert player.powered is True

    async def test_play_media_raises_when_no_source(self, mock_provider: MagicMock) -> None:
        """play_media should raise when no source can be acquired."""
        mock_provider.status = _status(
            zones=[_zone(i, source_id=i) for i in range(4)] + [_zone(4)],
            sources=[_source(i, input_str=f"stream={i}") for i in range(4)],
        )
        player = _make_player(mock_provider, 4)
        with pytest.raises(PlayerCommandFailed):
            await player.play_media(MagicMock())


class TestVolume:
    """Test volume handling."""

    async def test_volume_set_scales_to_fraction(self, mock_provider: MagicMock) -> None:
        """Volume 0..100 should be sent to AmpliPi as a 0.0..1.0 fraction."""
        player = _make_player(mock_provider, 0)
        await player.volume_set(80)
        mock_provider.api.set_zone.assert_awaited_once()
        _zone_id, update = mock_provider.api.set_zone.await_args.args
        assert update.vol_f == pytest.approx(0.8)
        assert player.volume_level == 80


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
