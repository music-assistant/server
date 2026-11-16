"""Tests for the vocal removal of the Party plugin's karaoke mode."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import numpy.typing as npt
import pytest
from music_assistant_models.enums import ContentType, MediaType, PlaybackState, StreamType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.helpers.shared_playback import SharedPlaybackMode
from music_assistant.providers.party import (
    CONF_PARTY_MODE,
    CONF_PARTY_PLAYER,
    CONF_PARTY_PLAYER_AUTO,
    CONF_PARTY_VOCAL_VOLUME,
    PartyPlugin,
)
from music_assistant.providers.party.vocal_removal import (
    VocalRemoval,
    float_to_pcm,
    pcm_to_float,
    remove_vocals,
    supports_pcm_format,
)

type FloatAudio = npt.NDArray[np.float32]

SAMPLE_RATE = 8000
SEGMENT_SECONDS = 2.0
PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    sample_rate=SAMPLE_RATE,
    bit_depth=16,
    channels=2,
)


class _FakeSeparator:
    """Separator that treats a fixed share of the audio as vocals."""

    def __init__(self, vocal_share: float, fail_after: int | None = None) -> None:
        self.segment_seconds = SEGMENT_SECONDS
        self.vocal_share = vocal_share
        self.fail_after = fail_after
        self.segment_lengths: list[int] = []

    async def extract_vocals(self, audio: FloatAudio, sample_rate: int) -> FloatAudio:
        assert sample_rate == SAMPLE_RATE
        if self.fail_after is not None and len(self.segment_lengths) >= self.fail_after:
            msg = "model failure"
            raise RuntimeError(msg)
        self.segment_lengths.append(audio.shape[1])
        await asyncio.sleep(0)
        return audio * np.float32(self.vocal_share)


def _make_pcm(seconds: float, pcm_format: AudioFormat = PCM_FORMAT) -> bytes:
    """Return PCM audio of a stereo tone that differs per channel."""
    frames = int(seconds * pcm_format.sample_rate)
    time_axis = np.arange(frames, dtype=np.float32) / pcm_format.sample_rate
    left = 0.5 * np.sin(2 * np.pi * 220 * time_axis)
    right = 0.25 * np.sin(2 * np.pi * 330 * time_axis)
    audio = np.stack([left, right][: pcm_format.channels]).astype(np.float32)
    return float_to_pcm(audio, pcm_format)


async def _source(data: bytes, chunk_size: int) -> AsyncGenerator[bytes]:
    """Yield the data in chunks of the given size."""
    for offset in range(0, len(data), chunk_size):
        yield data[offset : offset + chunk_size]


async def _run(data: bytes, separator: Any, chunk_size: int | None = None) -> list[bytes]:
    """Return the chunks vocal removal yields for the given audio."""
    source = _source(data, chunk_size or PCM_FORMAT.pcm_sample_size)
    return [chunk async for chunk in remove_vocals(source, PCM_FORMAT, separator, MagicMock())]


@pytest.mark.parametrize(
    ("content_type", "bit_depth"),
    [
        (ContentType.PCM_S16LE, 16),
        (ContentType.PCM_S24LE, 24),
        (ContentType.PCM_S32LE, 32),
        (ContentType.PCM_F32LE, 32),
    ],
)
def test_pcm_conversion_round_trips(content_type: ContentType, bit_depth: int) -> None:
    """Decoding and encoding PCM returns the original samples in every supported format."""
    pcm_format = AudioFormat(
        content_type=content_type, sample_rate=SAMPLE_RATE, bit_depth=bit_depth, channels=2
    )
    assert supports_pcm_format(pcm_format)
    quiet_and_loud = np.array([[0.0, 0.5, -0.5, 0.999], [-1.0, 0.25, -0.25, 0.001]], np.float32)
    data = float_to_pcm(quiet_and_loud, pcm_format)

    decoded = pcm_to_float(data, pcm_format)

    assert len(data) == 4 * 2 * bit_depth // 8
    assert decoded.shape == (2, 4)
    np.testing.assert_allclose(decoded, quiet_and_loud, atol=2**-15)
    assert float_to_pcm(decoded, pcm_format) == data


def test_unsupported_pcm_formats_are_rejected() -> None:
    """Audio with more than two channels is left alone."""
    surround = AudioFormat(
        content_type=ContentType.PCM_S16LE, sample_rate=SAMPLE_RATE, bit_depth=16, channels=6
    )
    assert not supports_pcm_format(surround)


@pytest.mark.parametrize("seconds", [0.5, 2.0, 3.5, 9.3])
async def test_audio_without_vocals_passes_unchanged(seconds: float) -> None:
    """Without vocals the output is the input, cut into 1-second chunks."""
    data = _make_pcm(seconds)
    separator = _FakeSeparator(vocal_share=0.0)

    chunks = await _run(data, separator, chunk_size=1000)

    assert b"".join(chunks) == data
    assert all(len(chunk) == PCM_FORMAT.pcm_sample_size for chunk in chunks[:-1])
    assert len(chunks[-1]) <= PCM_FORMAT.pcm_sample_size
    assert max(separator.segment_lengths) <= int(SEGMENT_SECONDS * SAMPLE_RATE)


async def test_vocals_are_subtracted_across_segment_overlaps() -> None:
    """The blended estimate removes the same share of the audio everywhere."""
    data = _make_pcm(9.3)
    separator = _FakeSeparator(vocal_share=0.75)

    output = b"".join(await _run(data, separator))

    assert len(output) == len(data)
    expected = pcm_to_float(data, PCM_FORMAT) * 0.25
    np.testing.assert_allclose(pcm_to_float(output, PCM_FORMAT), expected, atol=2**-14)
    assert len(separator.segment_lengths) > 3


async def test_partial_trailing_frame_is_kept() -> None:
    """Bytes that do not make up a whole frame are passed on, keeping the length."""
    data = _make_pcm(1.2) + b"\x01"

    output = b"".join(await _run(data, _FakeSeparator(vocal_share=0.0)))

    assert output == data


async def test_separator_failure_falls_back_to_the_original_audio() -> None:
    """When the model fails mid-track the rest of the audio plays untouched."""
    data = _make_pcm(9.3)
    separator = _FakeSeparator(vocal_share=1.0, fail_after=2)

    output = b"".join(await _run(data, separator))

    assert len(output) == len(data)
    tail_start = len(data) // 2
    assert output[tail_start:] == data[tail_start:]
    # the first segment was processed before the failure
    assert not np.any(pcm_to_float(output[:4000], PCM_FORMAT))


def _create_plugin(config_values: dict[str, Any], *, ready: bool = True) -> PartyPlugin:
    """Create a party plugin with a stand-in vocal separator."""
    plugin = PartyPlugin.__new__(PartyPlugin)
    plugin.mass = MagicMock()
    plugin.mass.players.resolve_player_id.side_effect = lambda player_id: player_id
    plugin.logger = MagicMock()
    plugin.config = MagicMock()
    plugin.config.get_value.side_effect = config_values.get
    plugin._session = None
    plugin._vocal_separator = MagicMock(ready=ready)
    return plugin


def _stream_details(queue_id: str | None, media_type: MediaType = MediaType.TRACK) -> StreamDetails:
    """Build stream details of an item on the given queue."""
    return StreamDetails(
        provider="builtin",
        item_id="item-1",
        audio_format=PCM_FORMAT,
        media_type=media_type,
        stream_type=StreamType.HTTP,
        path="http://example.com/audio.mp3",
        queue_id=queue_id,
    )


def test_vocal_removal_applies_to_tracks_on_the_party_player() -> None:
    """Only tracks on the configured party player get their vocals removed."""
    plugin = _create_plugin(
        {CONF_PARTY_MODE: SharedPlaybackMode.VENUE.value, CONF_PARTY_PLAYER: "party_player"}
    )

    assert plugin._resolve_vocal_removal(_stream_details("party_player")) is not None
    assert plugin._resolve_vocal_removal(_stream_details("other_player")) is None
    assert plugin._resolve_vocal_removal(_stream_details(None)) is None
    assert plugin._resolve_vocal_removal(_stream_details("party_player", MediaType.RADIO)) is None


def test_vocal_removal_follows_the_playing_queue_with_automatic_player_selection() -> None:
    """Automatic player selection settles on one queue: the first one that plays."""
    plugin = _create_plugin(
        {CONF_PARTY_MODE: SharedPlaybackMode.VENUE.value, CONF_PARTY_PLAYER: CONF_PARTY_PLAYER_AUTO}
    )
    idle = MagicMock(queue_id="idle_player", active=True, state=PlaybackState.IDLE)
    playing = MagicMock(queue_id="playing_player", active=True, state=PlaybackState.PLAYING)
    also_playing = MagicMock(queue_id="second_player", active=True, state=PlaybackState.PLAYING)

    player_queues = cast("MagicMock", plugin.mass.player_queues)
    player_queues.__iter__.side_effect = lambda: iter([idle])
    # nothing plays yet, so the queue that starts is the party queue
    assert plugin._resolve_vocal_removal(_stream_details("idle_player")) is not None

    player_queues.__iter__.side_effect = lambda: iter([idle, playing, also_playing])
    assert plugin._resolve_vocal_removal(_stream_details("playing_player")) is not None
    assert plugin._resolve_vocal_removal(_stream_details("second_player")) is None
    assert plugin._resolve_vocal_removal(_stream_details("idle_player")) is None


def test_vocal_removal_waits_for_the_model() -> None:
    """Tracks play untouched while the model is still loading."""
    plugin = _create_plugin(
        {CONF_PARTY_MODE: SharedPlaybackMode.VENUE.value, CONF_PARTY_PLAYER: "party_player"},
        ready=False,
    )

    assert plugin._resolve_vocal_removal(_stream_details("party_player")) is None


def test_vocal_removal_uses_the_session_queue_in_remote_mode() -> None:
    """In remote mode only the queue of the guest session is the party queue."""
    plugin = _create_plugin({CONF_PARTY_MODE: SharedPlaybackMode.REMOTE.value})

    assert plugin._resolve_vocal_removal(_stream_details("session_queue")) is None

    plugin._session = MagicMock(queue_id="session_queue")
    assert plugin._resolve_vocal_removal(_stream_details("session_queue")) is not None
    assert plugin._resolve_vocal_removal(_stream_details("other_player")) is None


async def test_vocal_volume_keeps_a_share_of_the_vocals() -> None:
    """A guide vocal leaves the given share of the vocals in the audio."""
    data = _make_pcm(9.3)
    separator = _FakeSeparator(vocal_share=0.8)
    source = _source(data, PCM_FORMAT.pcm_sample_size)
    removal = VocalRemoval(cast("Any", separator), MagicMock(), vocal_volume=0.25)

    output = b"".join([chunk async for chunk in removal(source, PCM_FORMAT)])

    # 0.8 of the audio is vocals, of which a quarter stays: 0.2 + 0.8 * 0.25
    expected = pcm_to_float(data, PCM_FORMAT) * 0.4
    np.testing.assert_allclose(pcm_to_float(output, PCM_FORMAT), expected, atol=2**-14)


def test_vocal_removals_compare_by_the_audio_they_give() -> None:
    """A reloaded plugin's removal equals the old one unless the vocal volume differs."""
    assert VocalRemoval(MagicMock(), MagicMock(), 0.2) == VocalRemoval(
        MagicMock(), MagicMock(), 0.2
    )
    assert VocalRemoval(MagicMock(), MagicMock(), 0.2) != VocalRemoval(
        MagicMock(), MagicMock(), 0.0
    )


def _playing_queue(streamdetails: StreamDetails, buffered_volume: float | None) -> MagicMock:
    """Build a queue that plays an item buffered with the given vocal volume, or untouched."""
    transform = None
    if buffered_volume is not None:
        transform = VocalRemoval(MagicMock(), MagicMock(), buffered_volume)
    streamdetails.buffer = MagicMock(pcm_transform=transform)
    queue = MagicMock(
        queue_id=streamdetails.queue_id,
        active=True,
        state=PlaybackState.PLAYING,
        corrected_elapsed_time=42.7,
    )
    queue.current_item.streamdetails = streamdetails
    return queue


@pytest.mark.parametrize(
    ("buffered_volume", "wanted_volume", "restarts"),
    [
        (None, 0, True),
        (0.0, None, True),
        (0.0, 30, True),
        (0.3, 30, False),
        (None, None, False),
    ],
)
async def test_playing_track_restarts_only_when_the_settings_ask_for_other_audio(
    buffered_volume: float | None, wanted_volume: int | None, restarts: bool
) -> None:
    """
    Changing vocal removal restarts the playing party track at its position.

    A wanted volume of None stands for vocal removal that is switched off.
    """
    plugin = _create_plugin(
        {
            CONF_PARTY_MODE: SharedPlaybackMode.VENUE.value,
            CONF_PARTY_PLAYER: "party_player",
            CONF_PARTY_VOCAL_VOLUME: wanted_volume,
        }
    )
    if wanted_volume is None:
        plugin._vocal_separator = None
    other = _playing_queue(_stream_details("other_player"), None)
    party = _playing_queue(_stream_details("party_player"), buffered_volume)
    player_queues = cast("MagicMock", plugin.mass.player_queues)
    player_queues.__iter__.side_effect = lambda: iter([other, party])
    player_queues.seek = AsyncMock()

    await plugin._sync_playing_track()

    if restarts:
        player_queues.seek.assert_awaited_once_with("party_player", 42)
    else:
        player_queues.seek.assert_not_awaited()
