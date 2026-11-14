"""Tests for external BluOS playback sources that have no input ID."""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlaybackState

from music_assistant.providers.bluesound.const import PLAYER_SOURCE_MAP
from music_assistant.providers.bluesound.player import BluesoundPlayer
from tests.common import MockPlayer, MockProvider


def _player(input_id: str | None = None, service: str | None = "LocalMusic") -> MagicMock:
    """Create a player status without contacting a device."""
    fake = MagicMock()
    fake.mass.streams.base_url = "http://mass.example:8097"
    fake.client.base_url = "http://player.example:11000"
    fake.status.input_id = input_id
    fake.status.service = service
    fake.status.stream_url = None
    fake.status.name = "Kashmir"
    fake.status.artist = "Led Zeppelin"
    fake.status.album = "Mothership"
    fake.status.image = None
    fake.status.total_seconds = 508
    fake.status.can_seek = True
    fake._attr_source_list = []
    fake._attr_active_source = None
    return fake


@pytest.mark.parametrize(
    ("input_id", "service", "expected"),
    [
        (None, "LocalMusic", "LocalMusic"),
        (None, "UnknownService", "UnknownService"),
        ("custom-input", "LocalMusic", "custom-input"),
        (None, None, "bluos_external"),
    ],
)
def test_external_source_has_stable_identifier(
    input_id: str | None, service: str | None, expected: str
) -> None:
    """Library and unknown services must not create a source with a null identifier."""
    fake = _player(input_id, service)

    BluesoundPlayer._resolve_source(cast("BluesoundPlayer", fake))

    assert fake._attr_active_source == expected
    source = fake._attr_source_list[-1]
    assert source.id == expected
    assert source.name == expected
    assert source.passive is True


@pytest.mark.parametrize("service", list(PLAYER_SOURCE_MAP))
def test_known_service_keeps_its_source(service: str) -> None:
    """Known services retain their existing source definitions."""
    fake = _player(service=service)

    BluesoundPlayer._resolve_source(cast("BluesoundPlayer", fake))

    assert fake._attr_active_source == service
    assert fake._attr_source_list == [PLAYER_SOURCE_MAP[service]]


def test_library_track_change_keeps_source_and_updates_metadata() -> None:
    """A new track changes the media without changing the library source identifier."""
    fake = _player()
    player = cast("BluesoundPlayer", fake)
    BluesoundPlayer._resolve_source(player)
    BluesoundPlayer._resolve_media(player)
    assert fake._attr_current_media.title == "Kashmir"
    assert fake._attr_current_media.artist == "Led Zeppelin"
    assert fake._attr_current_media.album == "Mothership"

    fake.status.name = "Teardrop"
    fake.status.artist = "Massive Attack"
    fake.status.album = "Collected"
    fake._attr_source_list = []
    BluesoundPlayer._resolve_source(player)
    BluesoundPlayer._resolve_media(player)

    assert fake._attr_active_source == "LocalMusic"
    assert fake._attr_source_list[-1].id == "LocalMusic"
    assert fake._attr_current_media.title == "Teardrop"
    assert fake._attr_current_media.artist == "Massive Attack"
    assert fake._attr_current_media.album == "Collected"


def test_ma_stream_is_not_classified_as_external() -> None:
    """An MA stream remains managed by the MA queue even if a service is reported."""
    fake = _player()
    fake.status.stream_url = "http://mass.example:8097/flow/test.flac"

    BluesoundPlayer._resolve_source(cast("BluesoundPlayer", fake))

    assert fake._attr_active_source is None
    assert fake._attr_source_list == []


@pytest.mark.parametrize("own_stream", [False, True], ids=["library", "ma_stream"])
def test_published_source_after_ma_to_library_switch(own_stream: bool) -> None:
    """Publish library metadata despite the remembered MA queue, but trust no MA stream."""
    mass = MagicMock()
    mass.closing = False
    mass.config.get_raw_player_config_value = MagicMock(
        side_effect=lambda _player_id, _key, default=None: default
    )
    mass.player_queues.get = MagicMock(return_value=None)
    mass.players.get_audio_source_session = MagicMock(return_value=None)
    mass.players.scale_volume_from_device = MagicMock(side_effect=lambda _pid, volume: volume)
    mass.streams.base_url = "http://mass.example:8097"
    player_id = "bluesound_player"
    player = cast(
        "BluesoundPlayer", MockPlayer(MockProvider("bluesound", mass=mass), player_id, "BluOS")
    )
    player._attr_trusts_reported_source = BluesoundPlayer._attr_trusts_reported_source
    player._attr_playback_state = PlaybackState.PLAYING
    player.set_active_mass_source(player_id)
    player.update_state(signal_event=False)
    assert player.state.active_source == player_id

    status = _player().status
    if own_stream:
        status.stream_url = "http://mass.example:8097/flow/test.flac"
    player.status = status
    player.client = MagicMock(base_url="http://player.example:11000")
    BluesoundPlayer._resolve_source(player)
    BluesoundPlayer._resolve_media(player)
    player.update_state(signal_event=False)

    assert player.state.active_source == (player_id if own_stream else "LocalMusic")
    if not own_stream:
        assert player.state.current_media is not None
        assert player.state.current_media.title == "Kashmir"
        assert player.state.current_media.artist == "Led Zeppelin"
        assert player.state.current_media.album == "Mothership"
