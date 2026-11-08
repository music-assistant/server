"""Tests for DSD over PCM (DoP) handling in the flow stream format and restart logic."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import (
    CONF_DSD_DOP,
    CONF_FLOW_MODE_SAMPLE_RATE,
    CONF_OUTPUT_CHANNELS,
    FLOW_MODE_SAMPLE_RATE_SMART,
)
from music_assistant.controllers.streams.audio import StreamsAudio

DOP_RATES = [(44100, 24), (48000, 24), (96000, 24), (176400, 24), (192000, 24), (352800, 24)]


def _make_streams_audio(*, dop: bool) -> StreamsAudio:
    """Return a StreamsAudio whose players have DoP switched on or off."""
    audio = StreamsAudio(MagicMock())
    audio.mass.config.get_player_dsp_config = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(enabled=False)
    )
    audio.mass.config.get_raw_player_config_value = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda _player_id, key, default=None: dop if key == CONF_DSD_DOP else default
    )
    return audio


def _make_player(*, supported: list[tuple[int, int]] = DOP_RATES, members: int = 1) -> MagicMock:
    player = MagicMock()
    player.player_id = "player"
    player.group_members = [f"member-{index}" for index in range(members)]
    player.get_supported_sample_rates = MagicMock(return_value=supported)
    player.config.get_value = MagicMock(
        side_effect=lambda key, default=None: (
            FLOW_MODE_SAMPLE_RATE_SMART if key == CONF_FLOW_MODE_SAMPLE_RATE else default
        )
    )
    return player


def _dsd_streamdetails(probed_rate: int = 352800) -> MagicMock:
    """Stream details of a local DSF file, as the library reports it (DSD rate / 8)."""
    streamdetails = MagicMock()
    streamdetails.audio_format = AudioFormat(
        content_type=ContentType.DSF, sample_rate=probed_rate, bit_depth=8, channels=2
    )
    streamdetails.stream_type = StreamType.LOCAL_FILE
    streamdetails.path = "/music/track.dsf"
    streamdetails.decoded_audio_format = None
    streamdetails.media_type = MediaType.TRACK
    return streamdetails


def _queue_track(streamdetails: MagicMock) -> MagicMock:
    track = MagicMock()
    track.queue_item_id = "item-2"
    track.name = "DSD Track"
    track.media_type = MediaType.TRACK
    track.streamdetails = streamdetails
    return track


def _pcm_format(rate: int) -> AudioFormat:
    return AudioFormat(
        content_type=ContentType.PCM_F32LE, sample_rate=rate, bit_depth=32, channels=2
    )


@pytest.mark.asyncio
async def test_flow_format_runs_at_dop_rate_for_dsd_start() -> None:
    """A DSD64 first track anchors the flow at its DoP rate (176.4 kHz), in F32."""
    audio = _make_streams_audio(dop=True)
    fmt = await audio.select_flow_pcm_format(
        _make_player(), start_streamdetails=_dsd_streamdetails()
    )
    assert fmt.sample_rate == 176400
    assert fmt.content_type == ContentType.PCM_F32LE


@pytest.mark.asyncio
async def test_flow_format_ignores_dop_when_disabled() -> None:
    """Without the DoP setting a DSD track is decoded to PCM at the snapped source rate."""
    audio = _make_streams_audio(dop=False)
    fmt = await audio.select_flow_pcm_format(
        _make_player(), start_streamdetails=_dsd_streamdetails()
    )
    assert fmt.sample_rate == 352800


@pytest.mark.asyncio
async def test_flow_format_ignores_dop_for_unsupported_rate() -> None:
    """
    A player that does not take the DoP rate gets the regular PCM conversion.

    The regular conversion keeps the 44.1 kHz family of the source.
    """
    audio = _make_streams_audio(dop=True)
    player = _make_player(supported=[(44100, 24), (96000, 24), (192000, 24)])
    fmt = await audio.select_flow_pcm_format(player, start_streamdetails=_dsd_streamdetails())
    assert fmt.sample_rate == 44100


@pytest.mark.asyncio
async def test_flow_format_ignores_dop_for_groups() -> None:
    """A grouped player never gets DoP: the group would alter the shared stream."""
    audio = _make_streams_audio(dop=True)
    fmt = await audio.select_flow_pcm_format(
        _make_player(members=2), start_streamdetails=_dsd_streamdetails()
    )
    assert fmt.sample_rate == 352800


@pytest.mark.asyncio
async def test_flow_format_ignores_dop_for_a_16_bit_output() -> None:
    """A client that takes the DoP rate only at 16 bits would truncate the DSD payload."""
    audio = _make_streams_audio(dop=True)
    player = _make_player()
    player.supports_dop_output = MagicMock(return_value=False)
    fmt = await audio.select_flow_pcm_format(player, start_streamdetails=_dsd_streamdetails())
    assert fmt.sample_rate == 352800
    player.supports_dop_output.assert_called_with(176400)


@pytest.mark.asyncio
async def test_flow_format_ignores_dop_with_dsp() -> None:
    """DSP filters the stream, so a player with DSP on gets decoded PCM."""
    audio = _make_streams_audio(dop=True)
    audio.mass.config.get_player_dsp_config = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(enabled=True)
    )
    fmt = await audio.select_flow_pcm_format(
        _make_player(), start_streamdetails=_dsd_streamdetails()
    )
    assert fmt.sample_rate == 352800


@pytest.mark.asyncio
@pytest.mark.parametrize("output_channels", ["mono", "left", "right"])
async def test_flow_format_ignores_dop_with_channel_mapping(output_channels: str) -> None:
    """A non-stereo channel mapping mixes the channels, so DoP is not used."""
    audio = _make_streams_audio(dop=True)
    audio.mass.config.get_raw_player_config_value = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda _player_id, key, default=None: {
            CONF_DSD_DOP: True,
            CONF_OUTPUT_CHANNELS: output_channels,
        }.get(key, default)
    )
    fmt = await audio.select_flow_pcm_format(
        _make_player(), start_streamdetails=_dsd_streamdetails()
    )
    assert fmt.sample_rate == 352800


@pytest.mark.asyncio
async def test_flow_format_ignores_dop_with_an_overlay() -> None:
    """An audio overlay is mixed into the flow, so DoP is not used."""
    audio = _make_streams_audio(dop=True)
    fmt = await audio.select_flow_pcm_format(
        _make_player(), start_streamdetails=_dsd_streamdetails(), overlay_active=True
    )
    assert fmt.sample_rate == 352800


@pytest.mark.parametrize(
    ("flow_rate", "probed_rate", "expected"),
    [
        (44100, 352800, True),  # PCM flow -> DSD64 needs the 176.4 kHz DoP rate
        (176400, 352800, False),  # already at the DSD64 DoP rate
        (176400, 705600, True),  # DSD128 needs 352.8 kHz
    ],
)
def test_restart_on_dop_rate_change(flow_rate: int, probed_rate: int, expected: bool) -> None:
    """The flow restarts whenever a DSD track needs a different DoP rate."""
    audio = _make_streams_audio(dop=True)
    assert (
        audio._flow_stream_needs_restart(
            _queue_track(_dsd_streamdetails(probed_rate)),
            _pcm_format(flow_rate),
            [rate for rate, _ in DOP_RATES],
            FLOW_MODE_SAMPLE_RATE_SMART,
            is_first_track=False,
            dop_enabled=True,
        )
        is expected
    )


def test_no_dop_restart_when_disabled() -> None:
    """Without DoP the regular smart-mode rule applies (352.8 kHz > 176.4 kHz restarts)."""
    audio = _make_streams_audio(dop=False)
    assert audio._flow_stream_needs_restart(
        _queue_track(_dsd_streamdetails()),
        _pcm_format(176400),
        [rate for rate, _ in DOP_RATES],
        FLOW_MODE_SAMPLE_RATE_SMART,
        is_first_track=False,
        dop_enabled=False,
    )


def test_dop_transition_detects_dsd_before_resolving() -> None:
    """A DSD track is recognised from its provider mapping before streamdetails exist."""
    audio = _make_streams_audio(dop=True)
    pcm_item = MagicMock()
    pcm_item.queue_item_id = "item-1"
    pcm_item.streamdetails = None
    pcm_item.media_item = None
    from music_assistant_models.media_items import ProviderMapping, Track  # noqa: PLC0415

    dsd_item = MagicMock()
    dsd_item.streamdetails = None
    dsd_item.media_item = Track(
        item_id="1",
        provider="filesystem",
        name="DSD",
        provider_mappings={
            ProviderMapping(
                item_id="1",
                provider_domain="filesystem",
                provider_instance="filesystem",
                audio_format=AudioFormat(
                    content_type=ContentType.DSF, sample_rate=352800, bit_depth=8
                ),
            )
        },
    )
    with patch.object(audio.mass.player_queues, "get_next_item", return_value=dsd_item):
        assert audio._dop_transition("queue", pcm_item)
    with patch.object(audio.mass.player_queues, "get_next_item", return_value=None):
        assert not audio._dop_transition("queue", pcm_item)


def _queue_item_audio(
    *,
    dop_player_ids: set[str],
    queue_player: MagicMock,
    protocol_players: dict[str, MagicMock] | None = None,
) -> StreamsAudio:
    """Return a StreamsAudio resolving players by id, with DoP on for the given ids."""
    audio = StreamsAudio(MagicMock())
    players = {queue_player.player_id: queue_player, **(protocol_players or {})}
    audio.mass.players.get_player = MagicMock(side_effect=players.get)  # type: ignore[method-assign]
    audio.mass.player_queues.get = MagicMock(return_value=None)  # type: ignore[method-assign]
    audio.mass.config.get_player_dsp_config = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(enabled=False)
    )
    audio.mass.config.get_raw_player_config_value = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda player_id, key, default=None: (
            player_id in dop_player_ids if key == CONF_DSD_DOP else default
        )
    )
    return audio


def _universal_player(
    output: MagicMock | None = None, linked_ids: tuple[str, ...] = ()
) -> MagicMock:
    """Return a queue player that renders through ``output`` (itself when None)."""
    player = _make_player()
    player.player_id = "queue"
    player.resolve_output_player = MagicMock(return_value=output or player)
    player.linked_output_protocols = [MagicMock(output_protocol_id=pid) for pid in linked_ids]
    return player


def _dsd_queue_item(streamdetails: MagicMock | None = None) -> MagicMock:
    item = _queue_track(streamdetails if streamdetails is not None else _dsd_streamdetails())
    item.queue_id = "queue"
    return item


def _protocol_player(player_id: str, supported: list[tuple[int, int]] = DOP_RATES) -> MagicMock:
    player = _make_player(supported=supported)
    player.player_id = player_id
    return player


def test_dop_item_on_active_dop_protocol_player_skips_buffer() -> None:
    """The active Sendspin protocol player has DoP on: the item plays as DoP."""
    sendspin = _protocol_player("sendspin")
    audio = _queue_item_audio(
        dop_player_ids={"sendspin"},
        queue_player=_universal_player(output=sendspin, linked_ids=("sendspin",)),
        protocol_players={"sendspin": sendspin},
    )
    assert audio.queue_item_plays_as_dop(_dsd_queue_item()) is True


def test_dop_item_before_protocol_selection_uses_linked_players() -> None:
    """No active protocol yet: a linked DoP protocol player is enough."""
    sendspin = _protocol_player("sendspin")
    audio = _queue_item_audio(
        dop_player_ids={"sendspin"},
        queue_player=_universal_player(linked_ids=("airplay", "sendspin")),
        protocol_players={"sendspin": sendspin, "airplay": _protocol_player("airplay")},
    )
    assert audio.queue_item_plays_as_dop(_dsd_queue_item()) is True


def test_active_non_dop_protocol_keeps_buffer() -> None:
    """A different protocol is active: the item gets decoded, so it keeps its buffer."""
    airplay = _protocol_player("airplay")
    audio = _queue_item_audio(
        dop_player_ids={"sendspin"},
        queue_player=_universal_player(output=airplay, linked_ids=("sendspin", "airplay")),
        protocol_players={"sendspin": _protocol_player("sendspin"), "airplay": airplay},
    )
    assert audio.queue_item_plays_as_dop(_dsd_queue_item()) is False


def test_unsupported_dop_rate_keeps_buffer() -> None:
    """DSD128 needs 352.8 kHz: a player that tops out at 192 kHz decodes it."""
    sendspin = _protocol_player("sendspin", supported=[(176400, 24), (192000, 24)])
    audio = _queue_item_audio(
        dop_player_ids={"sendspin"},
        queue_player=_universal_player(output=sendspin),
        protocol_players={"sendspin": sendspin},
    )
    assert audio.queue_item_plays_as_dop(_dsd_queue_item(_dsd_streamdetails(705600))) is False


def test_grouped_queue_player_keeps_buffer() -> None:
    """A group mixes the stream for its members, so DoP is never used."""
    queue_player = _universal_player()
    queue_player.group_members = ["queue", "other"]
    audio = _queue_item_audio(dop_player_ids={"queue"}, queue_player=queue_player)
    assert audio.queue_item_plays_as_dop(_dsd_queue_item()) is False


def test_pcm_item_keeps_buffer() -> None:
    """Only local DSF files play as DoP."""
    streamdetails = _dsd_streamdetails()
    streamdetails.audio_format = AudioFormat(
        content_type=ContentType.FLAC, sample_rate=44100, bit_depth=16, channels=2
    )
    audio = _queue_item_audio(dop_player_ids={"queue"}, queue_player=_universal_player())
    assert audio.queue_item_plays_as_dop(_dsd_queue_item(streamdetails)) is False


def test_unresolved_item_keeps_buffer() -> None:
    """Without streamdetails nothing is known yet."""
    item = _dsd_queue_item()
    item.streamdetails = None
    audio = _queue_item_audio(dop_player_ids={"queue"}, queue_player=_universal_player())
    assert audio.queue_item_plays_as_dop(item) is False


@pytest.mark.asyncio
async def test_dop_stream_releases_a_warmed_buffer() -> None:
    """A decoded buffer warmed before DoP was decided is released as DoP starts."""
    audio = StreamsAudio(MagicMock())
    warmed = MagicMock()
    warmed.clear = AsyncMock()
    streamdetails = _dsd_streamdetails()
    streamdetails.buffer = warmed
    streamdetails.duration = 300
    item = _dsd_queue_item(streamdetails)

    async def _no_audio(*_args: object, **_kwargs: object) -> AsyncGenerator[bytes]:
        chunks: tuple[bytes, ...] = ()
        for chunk in chunks:
            yield chunk

    with patch("music_assistant.controllers.streams.audio.get_dop_stream", _no_audio):
        async for _ in audio._get_dop_queue_item_stream(item, _pcm_format(176400), 0, True):
            pass

    warmed.clear.assert_awaited_once()
    assert streamdetails.buffer is None


@pytest.mark.asyncio
async def test_dop_stream_reads_the_validated_descriptor(tmp_path: Path) -> None:
    """DoP reads the descriptor the provider opened and checked, and closes it afterwards."""
    audio = StreamsAudio(MagicMock())
    item = _dsd_queue_item()
    item.streamdetails.buffer = None
    item.streamdetails.duration = 300
    fd = os.open(tmp_path / "track.dsf", os.O_RDONLY | os.O_CREAT)
    audio.open_local_files = AsyncMock(  # type: ignore[method-assign]
        return_value=([f"/proc/self/fd/{fd}"], (fd,))
    )
    sources: list[object] = []
    streaming: list[bool] = []

    async def _capture(source: object, *_args: object, **_kwargs: object) -> AsyncGenerator[bytes]:
        sources.append(source)
        streaming.append(audio.is_streaming_dop(item.queue_id))
        yield b"\x00" * 8

    with patch("music_assistant.controllers.streams.audio.get_dop_stream", _capture):
        async for _ in audio._get_dop_queue_item_stream(item, _pcm_format(176400), 0, True):
            pass

    assert sources == [fd]
    assert streaming == [True]
    assert not audio.is_streaming_dop(item.queue_id)
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(fd)
