"""Playback pipeline helpers for Sendspin players."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from aiosendspin.server.audio import AudioFormat as SendspinAudioFormat
from aiosendspin.server.channels import MAIN_CHANNEL
from aiosendspin.server.push_stream import StreamStoppedError
from music_assistant_models.enums import ContentType, PlaybackState
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import CONF_OUTPUT_CHANNELS
from music_assistant.helpers.audio import get_player_filter_params
from music_assistant.helpers.ffmpeg import FFMpeg
from music_assistant.models.player import PlayerMedia

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

    from .player import SendspinPlayer

# Use a frame size that matches ffmpeg's typical internal audio frame granularity
# (4096 samples at 48kHz => 16384 bytes for stereo s16le). This avoids startup
# "short read" behavior that can otherwise cause a permanent offset between the
# main and DSP channels when a DSP channel is (re)created mid-stream.
_FRAME_SAMPLES = 4096
_MAX_BUFFER_AHEAD_US = 5_000_000
_MAIN_PCM_CACHE_RETENTION_US = 10_000_000
_ENABLE_HISTORICAL_DSP_INJECTION = True


@dataclass
class _PreparedCommitFrame:
    """Prepared audio frame for a single commit."""

    seq: int
    main_pcm: bytes
    main_duration_us: int
    dsp_pcm: dict[UUID, tuple[bytes, SendspinAudioFormat]]


@dataclass
class _MainPCMCacheChunk:
    """Main channel chunk kept for late-join DSP preprocessing."""

    timestamp_us: int
    duration_us: int
    pcm_data: bytes


@dataclass
class _HistoricalInjection:
    """Historical DSP audio to inject on next commit."""

    channel_id: UUID
    audio_format: SendspinAudioFormat
    chunks: list[bytes]
    start_time_us: int | None = None


@dataclass
class _DSPChannel:
    """A DSP processing channel backed by an ffmpeg process."""

    channel_id: UUID
    filter_params: list[str]
    ffmpeg: FFMpeg
    output_channels: int  # 1 for mono (left/right mode), 2 for stereo
    pending: bytearray = field(default_factory=bytearray)
    chunks_processed: int = 0
    chunks_failed: int = 0
    input_bytes_total: int = 0
    input_us_total: int = 0
    raw_output_bytes_total: int = 0
    raw_output_us_total: int = 0
    delivered_output_bytes_total: int = 0
    delivered_output_us_total: int = 0
    pending_peak_bytes: int = 0
    pending_condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    stdout_reader_task: asyncio.Task[None] | None = None


def pcm_duration_us(byte_count: int, sample_rate: int, bit_depth: int, channels: int) -> int:
    """Calculate PCM duration from byte count and format."""
    frame_stride = (bit_depth // 8) * channels
    sample_count = byte_count // frame_stride if frame_stride else 0
    return int(sample_count * 1_000_000 / sample_rate) if sample_rate else 0


def _expected_dsp_bytes(
    input_pcm: bytes, bit_depth: int, input_channels: int, output_channels: int
) -> int:
    """Calculate expected DSP PCM byte length for preserved sample count."""
    bytes_per_sample = bit_depth // 8
    input_frame_stride = bytes_per_sample * input_channels
    sample_count = len(input_pcm) // input_frame_stride if input_frame_stride else 0
    return sample_count * bytes_per_sample * output_channels


def _normalize_pcm_size(pcm_data: bytes, expected_size: int) -> bytes:
    """Trim/pad PCM data to expected size."""
    if len(pcm_data) == expected_size:
        return pcm_data
    if len(pcm_data) > expected_size:
        return pcm_data[:expected_size]
    return pcm_data + (b"\x00" * (expected_size - len(pcm_data)))


def _get_dsp_filter_params(
    mass: MusicAssistant, player_id: str, pcm_format: AudioFormat
) -> list[str] | None:
    """Return ffmpeg filter params if player needs DSP, else None."""
    dsp_enabled = mass.config.get_player_dsp_config(player_id).enabled
    output_channels = mass.config.get_raw_player_config_value(
        player_id, CONF_OUTPUT_CHANNELS, "stereo"
    )
    if not dsp_enabled and output_channels == "stereo":
        return None
    filter_params = get_player_filter_params(mass, player_id, pcm_format, pcm_format)
    return filter_params if filter_params else None


async def _close_dsp_channel(dsp: _DSPChannel) -> None:
    """Close a DSP channel and release ffmpeg resources."""
    if dsp.stdout_reader_task is not None:
        dsp.stdout_reader_task.cancel()
        with suppress(asyncio.CancelledError):
            await dsp.stdout_reader_task
    with suppress(Exception):
        await dsp.ffmpeg.close()


async def _read_dsp_stdout(player: SendspinPlayer, dsp: _DSPChannel) -> None:
    """Continuously read ffmpeg stdout into dsp.pending.

    We need to drain stdout continuously; otherwise ffmpeg can block on its stdout
    pipe and/or our per-chunk reads can artificially lag behind real output.
    """
    try:
        while True:
            data = await dsp.ffmpeg.read(65536)
            if not data:
                return
            dsp.pending.extend(data)
            dsp.pending_peak_bytes = max(dsp.pending_peak_bytes, len(dsp.pending))
            async with dsp.pending_condition:
                dsp.pending_condition.notify_all()
    except asyncio.CancelledError:
        raise
    except Exception:
        player.logger.debug(
            "DSP stdout reader failed for channel %s", dsp.channel_id, exc_info=True
        )
    finally:
        async with dsp.pending_condition:
            dsp.pending_condition.notify_all()


async def _process_dsp_chunk(
    player: SendspinPlayer, dsp: _DSPChannel, chunk: bytes, pcm_format: AudioFormat
) -> bytes:
    """Process a PCM chunk through the DSP channel's ffmpeg.

    :param player: The player owning this DSP channel.
    :param dsp: DSP channel to process through.
    :param chunk: Raw PCM audio data.
    :param pcm_format: Input PCM format (used to calculate expected output size).
    """
    expected = len(chunk) // 2 if dsp.output_channels == 1 else len(chunk)
    input_duration_us = pcm_duration_us(
        len(chunk),
        pcm_format.sample_rate,
        pcm_format.bit_depth,
        pcm_format.channels,
    )
    output_duration_us = pcm_duration_us(
        expected,
        pcm_format.sample_rate,
        pcm_format.bit_depth,
        dsp.output_channels,
    )
    dsp.chunks_processed += 1
    dsp.input_bytes_total += len(chunk)
    dsp.input_us_total += input_duration_us
    try:
        await asyncio.wait_for(dsp.ffmpeg.write(chunk), timeout=5.0)
    except Exception:
        dsp.chunks_failed += 1
        dsp.delivered_output_bytes_total += expected
        dsp.delivered_output_us_total += output_duration_us
        player.logger.warning("DSP failed for channel %s", dsp.channel_id, exc_info=True)
        # Keep timeline continuity by filling this chunk with silence.
        return b"\x00" * expected

    deadline = time.monotonic() + 2.0
    while len(dsp.pending) < expected:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        async with dsp.pending_condition:
            if len(dsp.pending) >= expected:
                break
            if dsp.stdout_reader_task is not None and dsp.stdout_reader_task.done():
                break
            with suppress(TimeoutError):
                await asyncio.wait_for(dsp.pending_condition.wait(), timeout=remaining)

    raw_consumed_bytes = min(expected, len(dsp.pending))
    raw_consumed_us = 0
    if raw_consumed_bytes:
        raw_consumed_us = pcm_duration_us(
            raw_consumed_bytes,
            pcm_format.sample_rate,
            pcm_format.bit_depth,
            dsp.output_channels,
        )
        dsp.raw_output_bytes_total += raw_consumed_bytes
        dsp.raw_output_us_total += raw_consumed_us
    dsp.delivered_output_bytes_total += expected
    dsp.delivered_output_us_total += output_duration_us
    if raw_consumed_bytes >= expected:
        result = bytes(dsp.pending[:expected])
        del dsp.pending[:expected]
        return result

    # Still short: output silence for the missing tail to keep stream moving,
    # but record the fact that ffmpeg is lagging behind.
    result = bytes(dsp.pending) + b"\x00" * (expected - len(dsp.pending))
    dsp.pending.clear()
    return result


async def _create_dsp_channel(
    player: SendspinPlayer,
    player_id: str,
    pcm_format: AudioFormat,
) -> _DSPChannel:
    """Create a new DSP channel with an ffmpeg process.

    :param player: The leader player owning the DSP channels.
    :param player_id: Player ID this DSP channel belongs to.
    :param pcm_format: Input PCM audio format.
    """
    filter_params = _get_dsp_filter_params(player.mass, player_id, pcm_format) or []
    output_channels = 1 if any("pan=mono" in p for p in filter_params) else 2
    output_format = AudioFormat(
        content_type=pcm_format.content_type,
        sample_rate=pcm_format.sample_rate,
        bit_depth=pcm_format.bit_depth,
        channels=output_channels,
    )
    channel_id = player._player_channel_map[player_id]
    ffmpeg = FFMpeg(
        audio_input="-",
        input_format=pcm_format,
        output_format=output_format,
        filter_params=filter_params,
    )
    await ffmpeg.start()
    dsp = _DSPChannel(
        channel_id=channel_id,
        filter_params=filter_params,
        ffmpeg=ffmpeg,
        output_channels=output_channels,
    )
    dsp.stdout_reader_task = asyncio.create_task(
        _read_dsp_stdout(player, dsp),
        name=f"sendspin-dsp-stdout-{dsp.channel_id}",
    )
    return dsp


def _resolve_channel_for_player(
    player: SendspinPlayer, player_id: str, pcm_format: AudioFormat
) -> UUID:
    """Resolve channel ID for a player and update local maps."""
    filter_params = _get_dsp_filter_params(player.mass, player_id, pcm_format)
    if filter_params is None:
        player._player_channel_map[player_id] = MAIN_CHANNEL
        return MAIN_CHANNEL
    channel_id = uuid4()
    player._player_channel_map[player_id] = channel_id
    return channel_id


async def _release_player_channel(player: SendspinPlayer, player_id: str) -> None:
    """Release channel bookkeeping for a player and close its DSP channel."""
    player._player_channel_map.pop(player_id, None)
    player._pending_historical_injections.pop(player_id, None)
    dsp = player._dsp_channels.pop(player_id, None)
    if dsp is not None:
        await _close_dsp_channel(dsp)


async def _sync_live_dsp_channels(player: SendspinPlayer, pcm_format: AudioFormat) -> None:
    """Sync active DSP channels with current group members."""
    # Include pending joiners so we don't "drop" their DSP channel between the
    # set_members() call and the moment group membership state is updated.
    current_members = {
        player.player_id,
        *player._attr_group_members,
        *player._pending_join_members,
    }
    for member_id in current_members:
        if member_id not in player._player_channel_map:
            _resolve_channel_for_player(player, member_id, pcm_format)

    departed_members = (
        set(player._player_channel_map) - current_members - player._pending_join_members
    )
    for member_id in departed_members:
        await _release_player_channel(player, member_id)

    for member_id in current_members:
        if (
            member_id not in player._dsp_channels
            and player._player_channel_map.get(member_id) != MAIN_CHANNEL
        ):
            player._dsp_channels[member_id] = await _create_dsp_channel(
                player, member_id, pcm_format
            )


async def _iter_pcm_frames(
    audio_source: AsyncGenerator[bytes, None], pcm_format: AudioFormat
) -> AsyncGenerator[bytes, None]:
    """Split source PCM into fixed-size sample frames."""
    bytes_per_sample = pcm_format.bit_depth // 8
    frame_stride = bytes_per_sample * pcm_format.channels
    if frame_stride <= 0:
        async for chunk in audio_source:
            if chunk:
                yield chunk
        return

    frame_size = _FRAME_SAMPLES * frame_stride
    pending = bytearray()

    async for chunk in audio_source:
        if not chunk:
            continue
        pending.extend(chunk)
        while len(pending) >= frame_size:
            frame = bytes(pending[:frame_size])
            del pending[:frame_size]
            yield frame
    if pending:
        yield bytes(pending)


async def _prepare_commit_frames(
    player: SendspinPlayer,
    audio_source: AsyncGenerator[bytes, None],
    pcm_format: AudioFormat,
    prepared_queue: asyncio.Queue[_PreparedCommitFrame | None],
) -> None:
    """Read source PCM, process DSP channels, and queue commit-ready frames."""
    seq = 0
    try:
        async for frame in _iter_pcm_frames(audio_source, pcm_format):
            if player._push_stream is None or player._push_stream.is_stopped:
                break
            await _sync_live_dsp_channels(player, pcm_format)

            active_dsps = list(player._dsp_channels.values())
            processed_by_channel: dict[UUID, tuple[bytes, SendspinAudioFormat]] = {}
            if active_dsps:
                processed_frames = await asyncio.gather(
                    *(_process_dsp_chunk(player, dsp, frame, pcm_format) for dsp in active_dsps)
                )
                for dsp, processed in zip(active_dsps, processed_frames, strict=True):
                    expected_size = _expected_dsp_bytes(
                        frame, pcm_format.bit_depth, pcm_format.channels, dsp.output_channels
                    )
                    normalized = _normalize_pcm_size(processed, expected_size)
                    dsp_fmt = SendspinAudioFormat(
                        sample_rate=pcm_format.sample_rate,
                        bit_depth=pcm_format.bit_depth,
                        channels=dsp.output_channels,
                    )
                    processed_by_channel[dsp.channel_id] = (normalized, dsp_fmt)

            main_duration_us = pcm_duration_us(
                len(frame),
                pcm_format.sample_rate,
                pcm_format.bit_depth,
                pcm_format.channels,
            )
            await prepared_queue.put(
                _PreparedCommitFrame(
                    seq=seq,
                    main_pcm=frame,
                    main_duration_us=main_duration_us,
                    dsp_pcm=processed_by_channel,
                )
            )
            seq += 1
    finally:
        await prepared_queue.put(None)


def _inject_pending_historical_audio(player: SendspinPlayer) -> bool:
    """Queue pending historical DSP audio into PushStream before live prepare_audio."""
    if player._push_stream is None:
        return False
    injected = False
    for member_id, injection in list(player._pending_historical_injections.items()):
        if not injection.chunks:
            del player._pending_historical_injections[member_id]
            continue
        try:
            if not _ENABLE_HISTORICAL_DSP_INJECTION:
                continue

            for i, chunk in enumerate(injection.chunks):
                player._push_stream.prepare_historical_audio(
                    chunk,
                    injection.audio_format,
                    channel_id=injection.channel_id,
                    start_time_us=injection.start_time_us if i == 0 else None,
                )
            injected = True
        except Exception:
            player.logger.warning(
                "Failed to inject historical DSP audio for channel %s",
                injection.channel_id,
                exc_info=True,
            )
        finally:
            del player._pending_historical_injections[member_id]
    return injected


def _append_main_pcm_cache(
    player: SendspinPlayer, timestamp_us: int, duration_us: int, pcm_data: bytes
) -> None:
    """Store committed main-channel PCM for late-join DSP preprocessing."""
    player._main_pcm_cache.append(
        _MainPCMCacheChunk(timestamp_us=timestamp_us, duration_us=duration_us, pcm_data=pcm_data)
    )


def _prune_main_pcm_cache(player: SendspinPlayer) -> None:
    """Prune played/old main-channel PCM cache entries."""
    if player._push_stream is None:
        player._main_pcm_cache.clear()
        return
    while len(player._main_pcm_cache) > 1:
        oldest = player._main_pcm_cache[0]
        newest = player._main_pcm_cache[-1]
        window_us = (newest.timestamp_us + newest.duration_us) - oldest.timestamp_us
        if window_us <= _MAIN_PCM_CACHE_RETENTION_US:
            break
        player._main_pcm_cache.popleft()


async def prepare_dsp_for_join(player: SendspinPlayer, player_id: str) -> None:
    """Preprocess historical PCM for a joining player's new DSP channel."""
    if player._push_stream is None or player._pcm_format is None:
        return
    pcm_format = player._pcm_format
    channel_id = _resolve_channel_for_player(player, player_id, pcm_format)
    if channel_id == MAIN_CHANNEL:
        return

    dsp = await _create_dsp_channel(player, player_id, pcm_format)
    player._dsp_channels[player_id] = dsp

    if not _ENABLE_HISTORICAL_DSP_INJECTION:
        return

    target_us = player._push_stream.get_late_join_target_timestamp_us()
    historical_source = [
        chunk
        for chunk in player._main_pcm_cache
        if chunk.timestamp_us + chunk.duration_us > target_us
    ]
    if not historical_source:
        return

    first_chunk = historical_source[0]
    if first_chunk.timestamp_us > target_us + 200_000:
        return

    dsp_fmt = SendspinAudioFormat(
        sample_rate=pcm_format.sample_rate,
        bit_depth=pcm_format.bit_depth,
        channels=dsp.output_channels,
    )
    processed_chunks: list[bytes] = []
    for chunk in historical_source:
        processed = await _process_dsp_chunk(player, dsp, chunk.pcm_data, pcm_format)
        expected_size = _expected_dsp_bytes(
            chunk.pcm_data,
            pcm_format.bit_depth,
            pcm_format.channels,
            dsp.output_channels,
        )
        normalized = _normalize_pcm_size(processed, expected_size)
        processed_chunks.append(normalized)

    if processed_chunks:
        player._pending_historical_injections[player_id] = _HistoricalInjection(
            channel_id=dsp.channel_id,
            audio_format=dsp_fmt,
            chunks=processed_chunks,
            start_time_us=historical_source[0].timestamp_us,
        )


async def run_playback(player: SendspinPlayer, media: PlayerMedia) -> None:  # noqa: PLR0915
    """Run Sendspin playback in a background task."""
    audio_source: AsyncGenerator[bytes, None] | None = None
    prepared_queue: asyncio.Queue[_PreparedCommitFrame | None] | None = None
    prepare_task: asyncio.Task[None] | None = None
    commit_count = 0
    stream_position_us = 0
    first_main_start_us: int | None = None
    try:
        pcm_format = AudioFormat(
            content_type=ContentType.PCM_S16LE,
            sample_rate=48000,
            bit_depth=16,
            channels=2,
        )
        player._pcm_format = pcm_format
        sendspin_fmt = SendspinAudioFormat(
            sample_rate=pcm_format.sample_rate,
            bit_depth=pcm_format.bit_depth,
            channels=pcm_format.channels,
        )

        player._player_channel_map.clear()
        player._pending_historical_injections.clear()
        player._main_pcm_cache.clear()

        def channel_resolver(player_id: str) -> UUID:
            if player._pcm_format is None:
                return MAIN_CHANNEL
            if cached_channel_id := player._player_channel_map.get(player_id):
                return cached_channel_id
            return _resolve_channel_for_player(player, player_id, player._pcm_format)

        player._push_stream = player.api.group.start_stream(channel_resolver=channel_resolver)
        audio_source = player.mass.streams.get_stream(media, pcm_format)
        prepared_queue = asyncio.Queue(maxsize=4)
        prepare_task = asyncio.create_task(
            _prepare_commit_frames(player, audio_source, pcm_format, prepared_queue)
        )
        while True:
            prepared = await prepared_queue.get()
            if prepared is None:
                break
            if player._push_stream is None or player._push_stream.is_stopped:
                break

            stream_position_us += prepared.main_duration_us
            _inject_pending_historical_audio(player)
            player._push_stream.prepare_audio(prepared.main_pcm, sendspin_fmt)
            for channel_id, (processed_pcm, dsp_fmt) in prepared.dsp_pcm.items():
                player._push_stream.prepare_audio(processed_pcm, dsp_fmt, channel_id=channel_id)

            commit_start_us = await player._push_stream.commit_audio()
            main_start_us = commit_start_us
            if first_main_start_us is None:
                first_main_start_us = main_start_us
            _append_main_pcm_cache(
                player,
                timestamp_us=main_start_us,
                duration_us=prepared.main_duration_us,
                pcm_data=prepared.main_pcm,
            )
            commit_count += 1
            _prune_main_pcm_cache(player)
            await player._push_stream.sleep_to_limit_buffer(_MAX_BUFFER_AHEAD_US)

        if prepare_task is not None:
            await prepare_task

    except asyncio.CancelledError:
        player.logger.debug("Playback cancelled for player %s", player.display_name)
        raise
    except StreamStoppedError:
        player.logger.debug("Playback stream stopped for player %s", player.display_name)
    except Exception:
        player.logger.exception("Error during playback for player %s", player.display_name)
        raise
    finally:
        if player._push_stream is not None and not player._push_stream.is_stopped:
            with suppress(Exception):
                await player.api.group.stop()
        if prepare_task is not None and not prepare_task.done():
            prepare_task.cancel()
            with suppress(asyncio.CancelledError):
                await prepare_task
        player._push_stream = None
        player._pcm_format = None
        if audio_source is not None:
            with suppress(Exception):
                await audio_source.aclose()
        for dsp in player._dsp_channels.values():
            with suppress(Exception):
                await dsp.ffmpeg.close()
        player._dsp_channels.clear()
        player._player_channel_map.clear()
        player._pending_join_members.clear()
        player._pending_historical_injections.clear()
        player._main_pcm_cache.clear()
        if player._playback_task is asyncio.current_task():
            player._playback_task = None
        player._attr_playback_state = PlaybackState.IDLE
        player._attr_elapsed_time = 0
        player._attr_elapsed_time_last_updated = time.time()
        player._attr_current_media = None
        player.update_state()
