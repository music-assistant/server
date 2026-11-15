"""Tests for the crossfade of item streams on players whose protocols have ids of their own."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

from music_assistant_models.enums import CrossfadeMode, IdentifierType

from tests.controllers.streams.test_crossfade_transition import (
    STANDARD_CROSSFADE_DURATION,
    TEST_PCM_FORMAT,
    _queue_item,
)
from tests.controllers.streams.test_voice_over_transition import _drain

# the real server with fake providers, so the players and their ids are the real ones
from tests.scenarios.conftest import scenario  # noqa: F401
from tests.scenarios.test_lifecycle import DEN, PRE_SPLIT_DEN_TV

if TYPE_CHECKING:
    import pytest

    from tests.scenarios.harness import Scenario

KITCHEN = {IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0"}


async def test_item_stream_crossfades_on_a_player_with_a_minted_id(
    scenario: Scenario,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device found after the split gets a player id of its own; its tracks still crossfade."""
    sonos = await scenario.add_provider("sonos")
    kitchen = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.register(kitchen)
    await scenario.idle()
    player_id = scenario.single_player(kitchen).player_id
    assert player_id != kitchen.protocol_id

    assert await _crossfades(scenario, monkeypatch, player_id, kitchen.protocol_id)


async def test_item_stream_crossfades_through_an_output_with_another_id(
    scenario: Scenario,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A player that plays through an output of another id still crossfades its tracks."""
    airplay = await scenario.add_provider("airplay")
    chromecast = await scenario.add_provider("chromecast")
    airplay.protocol("airplay_den", "Den", role="audio", identifiers=DEN)
    chromecast.protocol("cast_den", "Den", role="audio", identifiers=DEN)
    scenario.set_present("airplay_den", True)
    scenario.set_present("cast_den", True)
    await scenario.seed_pre_split_config(PRE_SPLIT_DEN_TV)
    await scenario.idle()

    assert await _crossfades(scenario, monkeypatch, "upf3eba71c", "airplay_den")


async def _crossfades(
    scene: Scenario, monkeypatch: pytest.MonkeyPatch, player_id: str, protocol_id: str
) -> bool:
    """
    Stream a track of the player's queue the way the protocol's item stream request does.

    Only the audio is stubbed: the players, their config and the queue are the server's own.
    Returns whether the track was mixed into the next one.

    :param scene: The running scenario.
    :param monkeypatch: Pytest's monkeypatch fixture.
    :param player_id: The player whose queue plays.
    :param protocol_id: The protocol of the player that fetches the stream.
    """
    mass = scene.mass
    audio = mass.streams.audio
    # a protocol fetches its stream under its own id
    protocol = mass.players.get_output_protocol(protocol_id)
    assert protocol is not None
    assert protocol.player is mass.players.get_player(player_id)
    current = _queue_item("item-1", "Current")
    following = _queue_item("item-2", "Following")
    for item in (current, following):
        item.queue_id = player_id
        item.origin = None

    async def _item_stream(
        queue_item: SimpleNamespace, *_args: object, **_kwargs: object
    ) -> AsyncGenerator[bytes]:
        for _ in range(queue_item.streamdetails.duration):
            yield bytes(TEST_PCM_FORMAT.pcm_sample_size)
            await asyncio.sleep(0)

    async def _mix(
        *_args: object, fade_out_part: bytes, **_kwargs: object
    ) -> AsyncGenerator[bytes]:
        yield fade_out_part

    build = AsyncMock(
        return_value=SimpleNamespace(
            timing_info=SimpleNamespace(
                fadein_trimmed_duration=0.0,
                crossfade_duration=float(STANDARD_CROSSFADE_DURATION),
                pre_crossfade_duration=0.0,
                post_crossfade_duration=0.0,
            )
        )
    )
    monkeypatch.setattr(audio, "get_queue_item_stream", _item_stream)
    monkeypatch.setattr(audio, "select_pcm_format", AsyncMock(return_value=TEST_PCM_FORMAT))
    monkeypatch.setattr(audio.smart_fades_mixer, "build", build)
    monkeypatch.setattr(audio.smart_fades_mixer, "mix", _mix)
    monkeypatch.setattr(
        mass.player_queues, "load_next_queue_item", AsyncMock(return_value=following)
    )
    monkeypatch.setattr(mass.player_queues, "index_by_id", lambda *_args: 1)
    monkeypatch.setattr(mass.player_queues, "prepare_next_audio_buffer", lambda *_args: None)

    await _drain(
        audio.get_queue_item_stream_with_smartfade(
            protocol,
            cast("Any", current),
            TEST_PCM_FORMAT,
            crossfade_mode=CrossfadeMode.STANDARD_CROSSFADE,
            standard_crossfade_duration=STANDARD_CROSSFADE_DURATION,
        )
    )
    if handover := audio._crossfade_handover.pop(player_id, None):
        await handover.close()
    return build.await_count == 1
