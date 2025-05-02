"""Various helpers for audio streaming and manipulation."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import struct
import time
from collections.abc import AsyncGenerator
from io import BytesIO
from typing import TYPE_CHECKING, Final, cast

import aiofiles
import shortuuid
from aiohttp import ClientTimeout
from music_assistant_models.dsp import DSPConfig, DSPDetails, DSPState
from music_assistant_models.enums import (
    ContentType,
    MediaType,
    PlayerFeature,
    PlayerType,
    StreamType,
    VolumeNormalizationMode,
)
from music_assistant_models.errors import (
    AudioError,
    InvalidDataError,
    MediaNotFoundError,
    MusicAssistantError,
    ProviderUnavailableError,
)
from music_assistant_models.streamdetails import AudioFormat

from music_assistant.constants import (
    CONF_ALLOW_AUDIO_CACHE,
    CONF_ENTRY_OUTPUT_LIMITER,
    CONF_OUTPUT_CHANNELS,
    CONF_VOLUME_NORMALIZATION,
    CONF_VOLUME_NORMALIZATION_RADIO,
    CONF_VOLUME_NORMALIZATION_TARGET,
    CONF_VOLUME_NORMALIZATION_TRACKS,
    MASS_LOGGER_NAME,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.helpers.json import JSON_DECODE_EXCEPTIONS, json_loads
from music_assistant.helpers.throttle_retry import BYPASS_THROTTLER
from music_assistant.helpers.util import clean_stream_title, remove_file

from .datetime import utc
from .dsp import filter_to_ffmpeg_params
from .ffmpeg import FFMpeg, get_ffmpeg_stream
from .playlists import IsHLSPlaylist, PlaylistItem, fetch_playlist, parse_m3u
from .process import AsyncProcess, communicate
from .util import detect_charset, has_enough_space

if TYPE_CHECKING:
    from music_assistant_models.config_entries import CoreConfig, PlayerConfig
    from music_assistant_models.player import Player
    from music_assistant_models.player_queue import QueueItem
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant import MusicAssistant

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.audio")

# ruff: noqa: PLR0915

HTTP_HEADERS = {"User-Agent": "Lavf/60.16.100.MusicAssistant"}
HTTP_HEADERS_ICY = {**HTTP_HEADERS, "Icy-MetaData": "1"}

SLOW_PROVIDERS = ("tidal", "ytmusic", "apple_music")

CACHE_BASE_KEY: Final[str] = "audio_cache_path"
CACHE_FILES_IN_USE: set[str] = set()


class StreamCache:
    """
    StreamCache.

    Basic class to handle caching of audio streams to a (semi) temporary file.
    Useful in case of slow or unreliable network connections, faster seeking,
    or when the audio stream is slow itself.
    """

    async def create(self) -> None:
        """Create the cache file (if needed)."""
        if self._cache_file is None:
            if cached_cache_path := await self.mass.cache.get(
                self.streamdetails.uri, base_key=CACHE_BASE_KEY
            ):
                # we have a mapping stored for this uri, prefer that
                self._cache_file = cached_cache_path
                if await asyncio.to_thread(os.path.exists, self._cache_file):
                    # cache file already exists from a previous session,
                    # we can simply use that, there is nothing to create
                    CACHE_FILES_IN_USE.add(self._cache_file)
                    self._all_data_written = True
                    return
            else:
                # create new cache file
                cache_id = shortuuid.random(30)
                self._cache_file = cache_file = os.path.join(
                    self.mass.streams.audio_cache_dir, cache_id
                )
                await self.mass.cache.set(
                    self.streamdetails.uri, cache_file, base_key=CACHE_BASE_KEY
                )
        # mark file as in-use to prevent it being deleted
        CACHE_FILES_IN_USE.add(self._cache_file)
        # start fetch task if its not already running
        if self._fetch_task is None:
            self._fetch_task = self.mass.create_task(self._create_cache_file())
        # wait until the first part of the file is received
        await self._first_part_received.wait()
        if self._stream_error:
            # an error occurred while creating the cache file
            # remove the cache file and raise an error
            raise AudioError(self._stream_error)

    def release(self) -> None:
        """Release the cache file."""
        self._subscribers -= 1
        if self._subscribers <= 0:
            CACHE_FILES_IN_USE.discard(self._cache_file)

    async def get_audio_stream(self) -> str | AsyncGenerator[bytes, None]:
        """
        Get the cached audio stream.

        Returns a string with the path of the cachefile if the file is ready.
        If the file is not yet ready, it will return an async generator that will
        stream the (intermediate) audio data from the cache file.
        """
        self._subscribers += 1
        assert self._cache_file is not None  # type guard
        # mark file as in-use to prevent it being deleted
        CACHE_FILES_IN_USE.add(self._cache_file)

        async def _stream_from_cache() -> AsyncGenerator[bytes, None]:
            chunksize = get_chunksize(self.streamdetails.audio_format, 1)
            wait_loops = 0
            assert self._cache_file is not None  # type guard
            async with aiofiles.open(self._cache_file, "rb") as file:
                while wait_loops < 2000:
                    chunk = await file.read(chunksize)
                    if chunk:
                        yield chunk
                        await asyncio.sleep(0)  # yield to eventloop
                        del chunk
                    elif self._all_data_written:
                        # reached EOF
                        break
                    else:
                        # data is not yet available, wait a bit
                        await asyncio.sleep(0.05)
                        # prevent an infinite loop in case of an error
                        wait_loops += 1

        if self._all_data_written:
            # cache file is ready
            return self._cache_file

        # cache file does not exist at all (or is still being written)
        await self.create()
        return _stream_from_cache()

    async def _create_cache_file(self) -> None:
        time_start = time.time()
        self.logger.debug("Creating audio cache for %s", self.streamdetails.uri)
        CACHE_FILES_IN_USE.add(self._cache_file)
        self._first_part_received.clear()
        self._all_data_written = False
        extra_input_args = ["-y", *(self.org_extra_input_args or [])]
        if self.org_stream_type == StreamType.CUSTOM:
            audio_source = self.mass.get_provider(self.streamdetails.provider).get_audio_stream(
                self.streamdetails,
            )
        elif self.org_stream_type == StreamType.ICY:
            raise NotImplementedError("Caching of this streamtype is not supported!")
        elif self.org_stream_type == StreamType.HLS:
            if self.streamdetails.media_type == MediaType.RADIO:
                raise NotImplementedError("Caching of this streamtype is not supported!")
            substream = await get_hls_substream(self.mass, self.org_path)
            audio_source = substream.path
        elif self.org_stream_type == StreamType.ENCRYPTED_HTTP:
            audio_source = self.org_path
            extra_input_args += ["-decryption_key", self.streamdetails.decryption_key]
        elif self.org_stream_type == StreamType.MULTI_FILE:
            audio_source = get_multi_file_stream(self.mass, self.streamdetails)
        else:
            audio_source = self.org_path

        # we always use ffmpeg to fetch the original audio source
        # this may feel a bit redundant, but it's the most reliable way to fetch the audio
        # because ffmpeg has all logic to handle different audio formats, codecs, etc.
        # and it also accounts for complicated cases such as encrypted streams or
        # m4a/mp4 streams with the moov atom at the end of the file.
        # ffmpeg will produce a lossless copy of the original codec.
        try:
            ffmpeg_proc = FFMpeg(
                audio_input=audio_source,
                input_format=self.org_audio_format,
                output_format=self.streamdetails.audio_format,
                extra_input_args=extra_input_args,
                audio_output=self._cache_file,
                collect_log_history=True,
            )
            await ffmpeg_proc.start()
            # wait until the first data is written to the cache file
            while ffmpeg_proc.returncode is None:
                await asyncio.sleep(0.1)
                if not await asyncio.to_thread(os.path.exists, self._cache_file):
                    continue
                if await asyncio.to_thread(os.path.getsize, self._cache_file) > 64000:
                    break

            # set 'first part received' event to signal that the first part of the file is ready
            # this is useful for the get_audio_stream method to know when it can start streaming
            # we do guard for the returncode here, because if ffmpeg exited abnormally, we should
            # not signal that the first part is ready
            if ffmpeg_proc.returncode in (None, 0):
                self._first_part_received.set()
                self.logger.debug(
                    "First part received for %s after %.2fs",
                    self.streamdetails.uri,
                    time.time() - time_start,
                )
            # wait until ffmpeg is done
            await ffmpeg_proc.wait()

            # raise an error if ffmpeg exited with a non-zero code
            if ffmpeg_proc.returncode != 0:
                ffmpeg_proc.logger.warning("\n".join(ffmpeg_proc.log_history))
                raise AudioError(f"FFMpeg error {ffmpeg_proc.returncode}")

            # set 'all data written' event to signal that the entire file is ready
            self._all_data_written = True
            self.logger.debug(
                "Writing all data for %s done in %.2fs",
                self.streamdetails.uri,
                time.time() - time_start,
            )
        except BaseException as err:
            self.logger.error("Error while creating cache for %s: %s", self.streamdetails.uri, err)
            # make sure that the (corrupted/incomplete) cache file is removed
            await self._remove_cache_file()
            # unblock the waiting tasks by setting the event
            # this will allow the tasks to continue and handle the error
            self._stream_error = str(err) or err.__qualname__
            self._first_part_received.set()
        finally:
            await ffmpeg_proc.close()

    async def _remove_cache_file(self) -> None:
        self._first_part_received.clear()
        self._all_data_written = False
        self._fetch_task = None
        await remove_file(self._cache_file)

    def __init__(self, mass: MusicAssistant, streamdetails: StreamDetails) -> None:
        """Initialize the StreamCache."""
        self.mass = mass
        self.streamdetails = streamdetails
        self.logger = LOGGER.getChild("cache")
        self._cache_file: str | None = None
        self._fetch_task: asyncio.Task | None = None
        self._subscribers: int = 0
        self._first_part_received = asyncio.Event()
        self._all_data_written: bool = False
        self._stream_error: str | None = None
        self.org_path: str | None = streamdetails.path
        self.org_stream_type: StreamType | None = streamdetails.stream_type
        self.org_extra_input_args: list[str] | None = streamdetails.extra_input_args
        self.org_audio_format = streamdetails.audio_format
        streamdetails.audio_format = AudioFormat(
            content_type=ContentType.NUT,
            codec_type=streamdetails.audio_format.codec_type,
            sample_rate=streamdetails.audio_format.sample_rate,
            bit_depth=streamdetails.audio_format.bit_depth,
            channels=streamdetails.audio_format.channels,
        )
        streamdetails.path = "-"
        streamdetails.stream_type = StreamType.CACHE
        streamdetails.can_seek = True
        streamdetails.allow_seek = True
        streamdetails.extra_input_args = []


async def crossfade_pcm_parts(
    fade_in_part: bytes,
    fade_out_part: bytes,
    pcm_format: AudioFormat,
    fade_out_pcm_format: AudioFormat | None = None,
) -> bytes:
    """Crossfade two chunks of pcm/raw audio using ffmpeg."""
    if fade_out_pcm_format is None:
        fade_out_pcm_format = pcm_format

    # calculate the fade_length from the smallest chunk
    fade_length = min(
        len(fade_in_part) / pcm_format.pcm_sample_size,
        len(fade_out_part) / fade_out_pcm_format.pcm_sample_size,
    )
    # write the fade_out_part to a temporary file
    fadeout_filename = f"/tmp/{shortuuid.random(20)}.pcm"  # noqa: S108
    async with aiofiles.open(fadeout_filename, "wb") as outfile:
        await outfile.write(fade_out_part)

    args = [
        # generic args
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "quiet",
        # fadeout part (as file)
        "-acodec",
        fade_out_pcm_format.content_type.name.lower(),
        "-ac",
        str(fade_out_pcm_format.channels),
        "-ar",
        str(fade_out_pcm_format.sample_rate),
        "-channel_layout",
        "mono" if fade_out_pcm_format.channels == 1 else "stereo",
        "-f",
        fade_out_pcm_format.content_type.value,
        "-i",
        fadeout_filename,
        # fade_in part (stdin)
        "-acodec",
        pcm_format.content_type.name.lower(),
        "-ac",
        str(pcm_format.channels),
        "-channel_layout",
        "mono" if pcm_format.channels == 1 else "stereo",
        "-ar",
        str(pcm_format.sample_rate),
        "-f",
        pcm_format.content_type.value,
        "-i",
        "-",
        # filter args
        "-filter_complex",
        f"[0][1]acrossfade=d={fade_length}",
        # output args
        "-acodec",
        pcm_format.content_type.name.lower(),
        "-ac",
        str(pcm_format.channels),
        "-channel_layout",
        "mono" if pcm_format.channels == 1 else "stereo",
        "-ar",
        str(pcm_format.sample_rate),
        "-f",
        pcm_format.content_type.value,
        "-",
    ]
    _, crossfaded_audio, _ = await communicate(args, fade_in_part)
    await remove_file(fadeout_filename)
    if crossfaded_audio:
        LOGGER.log(
            VERBOSE_LOG_LEVEL,
            "crossfaded 2 pcm chunks. fade_in_part: %s - "
            "fade_out_part: %s - fade_length: %s seconds",
            len(fade_in_part),
            len(fade_out_part),
            fade_length,
        )
        return crossfaded_audio
    # no crossfade_data, return original data instead
    LOGGER.debug(
        "crossfade of pcm chunks failed: not enough data? - fade_in_part: %s - fade_out_part: %s",
        len(fade_in_part),
        len(fade_out_part),
    )
    if fade_out_pcm_format.sample_rate != pcm_format.sample_rate:
        # Edge case: the sample rates are different,
        # we need to resample the fade_out part to the same sample rate as the fade_in part
        async with FFMpeg(
            audio_input="-",
            input_format=fade_out_pcm_format,
            output_format=pcm_format,
        ) as ffmpeg:
            res = await ffmpeg.communicate(fade_out_part)
            return res[0] + fade_in_part
    return fade_out_part + fade_in_part


async def strip_silence(
    mass: MusicAssistant,  # noqa: ARG001
    audio_data: bytes,
    pcm_format: AudioFormat,
    reverse: bool = False,
) -> bytes:
    """Strip silence from begin or end of pcm audio using ffmpeg."""
    args = ["ffmpeg", "-hide_banner", "-loglevel", "quiet"]
    args += [
        "-acodec",
        pcm_format.content_type.name.lower(),
        "-f",
        pcm_format.content_type.value,
        "-ac",
        str(pcm_format.channels),
        "-ar",
        str(pcm_format.sample_rate),
        "-i",
        "-",
    ]
    # filter args
    if reverse:
        args += [
            "-af",
            "areverse,atrim=start=0.2,silenceremove=start_periods=1:start_silence=0.1:start_threshold=0.02,areverse",
        ]
    else:
        args += [
            "-af",
            "atrim=start=0.2,silenceremove=start_periods=1:start_silence=0.1:start_threshold=0.02",
        ]
    # output args
    args += ["-f", pcm_format.content_type.value, "-"]
    _returncode, stripped_data, _stderr = await communicate(args, audio_data)

    # return stripped audio
    bytes_stripped = len(audio_data) - len(stripped_data)
    if LOGGER.isEnabledFor(VERBOSE_LOG_LEVEL):
        seconds_stripped = round(bytes_stripped / pcm_format.pcm_sample_size, 2)
        location = "end" if reverse else "begin"
        LOGGER.log(
            VERBOSE_LOG_LEVEL,
            "stripped %s seconds of silence from %s of pcm audio. bytes stripped: %s",
            seconds_stripped,
            location,
            bytes_stripped,
        )
    return stripped_data


def get_player_dsp_details(
    mass: MusicAssistant, player: Player, group_preventing_dsp=False
) -> DSPDetails:
    """Return DSP details of single a player.

    This will however not check if the queried player is part of a group.
    The caller is responsible for passing the result of is_grouping_preventing_dsp of
    the leader/PlayerGroup as the group_preventing_dsp argument in such cases.
    """
    dsp_config = mass.config.get_player_dsp_config(player.player_id)
    dsp_state = DSPState.ENABLED if dsp_config.enabled else DSPState.DISABLED
    if dsp_state == DSPState.ENABLED and (
        group_preventing_dsp or is_grouping_preventing_dsp(player)
    ):
        dsp_state = DSPState.DISABLED_BY_UNSUPPORTED_GROUP
        dsp_config = DSPConfig(enabled=False)
    elif dsp_state == DSPState.DISABLED:
        # DSP is disabled by the user, remove all filters
        dsp_config = DSPConfig(enabled=False)

    # remove disabled filters
    dsp_config.filters = [x for x in dsp_config.filters if x.enabled]

    output_limiter = is_output_limiter_enabled(mass, player)
    return DSPDetails(
        state=dsp_state,
        input_gain=dsp_config.input_gain,
        filters=dsp_config.filters,
        output_gain=dsp_config.output_gain,
        output_limiter=output_limiter,
        output_format=player.output_format,
    )


def get_stream_dsp_details(
    mass: MusicAssistant,
    queue_id: str,
) -> dict[str, DSPDetails]:
    """Return DSP details of all players playing this queue, keyed by player_id."""
    player = mass.players.get(queue_id)
    dsp: dict[str, DSPDetails] = {}
    group_preventing_dsp = is_grouping_preventing_dsp(player)
    output_format = None
    is_external_group = False

    if player.provider.startswith("player_group"):
        if group_preventing_dsp:
            try:
                # We need a bit of a hack here since only the leader knows the correct output format
                provider = mass.get_provider(player.provider)
                if provider:
                    output_format = provider._get_sync_leader(player).output_format
            except RuntimeError:
                # _get_sync_leader will raise a RuntimeError if this group has no players
                # just ignore this and continue without output_format
                LOGGER.warning("Unable to get the sync group leader for %s", queue_id)
    else:
        # We only add real players (so skip the PlayerGroups as they only sync containing players)
        details = get_player_dsp_details(mass, player)
        dsp[player.player_id] = details
        if group_preventing_dsp:
            # The leader is responsible for sending the (combined) audio stream, so get
            # the output format from the leader.
            output_format = player.output_format
        is_external_group = player.type in (PlayerType.GROUP, PlayerType.STEREO_PAIR)

    # We don't enumerate all group members in case this group is externally created
    # (e.g. a Chromecast group from the Google Home app)
    if player and player.group_childs and not is_external_group:
        # grouped playback, get DSP details for each player in the group
        for child_id in player.group_childs:
            # skip if we already have the details (so if it's the group leader)
            if child_id in dsp:
                continue
            if child_player := mass.players.get(child_id):
                dsp[child_id] = get_player_dsp_details(
                    mass, child_player, group_preventing_dsp=group_preventing_dsp
                )
                if group_preventing_dsp:
                    # Use the correct format from the group leader, since
                    # this player is part of a group that does not support
                    # multi device DSP processing.
                    dsp[child_id].output_format = output_format
    return dsp


async def get_stream_details(
    mass: MusicAssistant,
    queue_item: QueueItem,
    seek_position: int = 0,
    fade_in: bool = False,
    prefer_album_loudness: bool = False,
) -> StreamDetails:
    """
    Get streamdetails for the given QueueItem.

    This is called just-in-time when a PlayerQueue wants a MediaItem to be played.
    Do not try to request streamdetails too much in advance as this is expiring data.
    """
    BYPASS_THROTTLER.set(True)
    time_start = time.time()
    LOGGER.debug("Getting streamdetails for %s", queue_item.uri)
    if seek_position and (queue_item.media_type == MediaType.RADIO or not queue_item.duration):
        LOGGER.warning("seeking is not possible on duration-less streams!")
        seek_position = 0

    if not queue_item.media_item and not queue_item.streamdetails:
        # in case of a non-media item queue item, the streamdetails should already be provided
        # this should not happen, but guard it just in case
        raise MediaNotFoundError(
            f"Unable to retrieve streamdetails for {queue_item.name} ({queue_item.uri})"
        )
    if queue_item.streamdetails and (utc() - queue_item.streamdetails.created_at).seconds < 1800:
        # already got a fresh/unused (or cached) streamdetails
        # we assume that the streamdetails are valid for max 30 minutes
        streamdetails = queue_item.streamdetails
    else:
        # retrieve streamdetails from provider
        media_item = queue_item.media_item
        # sort by quality and check item's availability
        for prov_media in sorted(
            media_item.provider_mappings, key=lambda x: x.quality or 0, reverse=True
        ):
            if not prov_media.available:
                LOGGER.debug(f"Skipping unavailable {prov_media}")
                continue
            # guard that provider is available
            music_prov = mass.get_provider(prov_media.provider_instance)
            if not music_prov:
                LOGGER.debug(f"Skipping {prov_media} - provider not available")
                continue  # provider not available ?
            # get streamdetails from provider
            try:
                streamdetails: StreamDetails = await music_prov.get_stream_details(
                    prov_media.item_id, media_item.media_type
                )
            except MusicAssistantError as err:
                LOGGER.warning(str(err))
            else:
                break
        else:
            msg = f"Unable to retrieve streamdetails for {queue_item.name} ({queue_item.uri})"
            raise MediaNotFoundError(msg)

        # work out how to handle radio stream
        if (
            streamdetails.stream_type in (StreamType.ICY, StreamType.HLS, StreamType.HTTP)
            and streamdetails.media_type == MediaType.RADIO
        ):
            resolved_url, stream_type = await resolve_radio_stream(mass, streamdetails.path)
            streamdetails.path = resolved_url
            streamdetails.stream_type = stream_type

    # set queue_id on the streamdetails so we know what is being streamed
    streamdetails.queue_id = queue_item.queue_id
    # handle skip/fade_in details
    streamdetails.seek_position = seek_position
    streamdetails.fade_in = fade_in
    if not streamdetails.duration:
        streamdetails.duration = queue_item.duration

    # handle volume normalization details
    if result := await mass.music.get_loudness(
        streamdetails.item_id,
        streamdetails.provider,
        media_type=queue_item.media_type,
    ):
        streamdetails.loudness = result[0]
        streamdetails.loudness_album = result[1]
    streamdetails.prefer_album_loudness = prefer_album_loudness
    player_settings = await mass.config.get_player_config(streamdetails.queue_id)
    core_config = await mass.config.get_core_config("streams")
    streamdetails.target_loudness = float(
        player_settings.get_value(CONF_VOLUME_NORMALIZATION_TARGET)
    )
    streamdetails.volume_normalization_mode = _get_normalization_mode(
        core_config, player_settings, streamdetails
    )

    # attach the DSP details of all group members
    streamdetails.dsp = get_stream_dsp_details(mass, streamdetails.queue_id)

    LOGGER.debug(
        "retrieved streamdetails for %s in %s milliseconds",
        queue_item.uri,
        int((time.time() - time_start) * 1000),
    )

    # determine if we may use caching for the audio stream
    if streamdetails.enable_cache is None:
        streamdetails.enable_cache = await _is_cache_allowed(mass, streamdetails)

    # handle temporary cache support of audio stream
    if streamdetails.enable_cache:
        if streamdetails.cache is None:
            streamdetails.cache = StreamCache(mass, streamdetails)
        else:
            streamdetails.cache = cast("StreamCache", streamdetails.cache)
        # create cache (if needed) and wait until the cache is available
        await streamdetails.cache.create()
        LOGGER.debug(
            "streamdetails cache ready for %s in %s milliseconds",
            queue_item.uri,
            int((time.time() - time_start) * 1000),
        )

    return streamdetails


async def _is_cache_allowed(mass: MusicAssistant, streamdetails: StreamDetails) -> bool:
    """Check if caching is allowed for the given streamdetails."""
    if streamdetails.media_type not in (
        MediaType.TRACK,
        MediaType.AUDIOBOOK,
        MediaType.PODCAST_EPISODE,
    ):
        return False
    if streamdetails.stream_type in (StreamType.ICY, StreamType.LOCAL_FILE, StreamType.UNKNOWN):
        return False
    if streamdetails.stream_type == StreamType.LOCAL_FILE:
        # no need to cache local files
        return False
    allow_cache = mass.config.get_raw_core_config_value(
        "streams", CONF_ALLOW_AUDIO_CACHE, mass.streams.allow_cache_default
    )
    if allow_cache == "disabled":
        return False
    if not await has_enough_space(mass.streams.audio_cache_dir, 5):
        return False
    if allow_cache == "always":
        return True
    # auto mode
    if streamdetails.stream_type == StreamType.ENCRYPTED_HTTP:
        # always prefer cache for encrypted streams
        return True
    if not streamdetails.duration:
        # we can't determine filesize without duration so play it safe and dont allow cache
        return False
    estimated_filesize = get_chunksize(streamdetails.audio_format, streamdetails.duration)
    if streamdetails.stream_type == StreamType.MULTI_FILE:
        # prefer cache to speedup multi-file streams
        # (if total filesize smaller than 2GB)
        max_filesize = 2 * 1024 * 1024 * 1024
    elif streamdetails.stream_type == StreamType.CUSTOM:
        # prefer cache for custom streams (to speedup seeking)
        max_filesize = 250 * 1024 * 1024  # 250MB
    elif streamdetails.stream_type == StreamType.HLS:
        # prefer cache for HLS streams (to speedup seeking)
        max_filesize = 250 * 1024 * 1024  # 250MB
    elif streamdetails.media_type in (
        MediaType.AUDIOBOOK,
        MediaType.PODCAST_EPISODE,
    ):
        # prefer cache for audiobooks and episodes (to speedup seeking)
        max_filesize = 2 * 1024 * 1024 * 1024  # 2GB
    elif streamdetails.provider in SLOW_PROVIDERS:
        # prefer cache for slow providers
        max_filesize = 2 * 1024 * 1024 * 1024  # 2GB
    else:
        max_filesize = 50 * 1024 * 1024

    return estimated_filesize < max_filesize


async def get_media_stream(
    mass: MusicAssistant,
    streamdetails: StreamDetails,
    pcm_format: AudioFormat,
    filter_params: list[str] | None = None,
) -> AsyncGenerator[bytes, None]:
    """Get PCM audio stream for given media details."""
    logger = LOGGER.getChild("media_stream")
    logger.log(VERBOSE_LOG_LEVEL, "Starting media stream for %s", streamdetails.uri)
    extra_input_args = streamdetails.extra_input_args or []
    strip_silence_begin = streamdetails.strip_silence_begin
    strip_silence_end = streamdetails.strip_silence_end
    if streamdetails.fade_in:
        filter_params.append("afade=type=in:start_time=0:duration=3")
        strip_silence_begin = False

    # work out audio source for these streamdetails
    stream_type = streamdetails.stream_type
    if stream_type == StreamType.CACHE:
        cache = cast("StreamCache", streamdetails.cache)
        audio_source = await cache.get_audio_stream()
    elif stream_type == StreamType.MULTI_FILE:
        audio_source = get_multi_file_stream(mass, streamdetails)
    elif stream_type == StreamType.CUSTOM:
        audio_source = mass.get_provider(streamdetails.provider).get_audio_stream(
            streamdetails,
            seek_position=streamdetails.seek_position if streamdetails.can_seek else 0,
        )
    elif stream_type == StreamType.ICY:
        audio_source = get_icy_radio_stream(mass, streamdetails.path, streamdetails)
    elif stream_type == StreamType.HLS:
        substream = await get_hls_substream(mass, streamdetails.path)
        audio_source = substream.path
        if streamdetails.media_type == MediaType.RADIO:
            # HLS streams (especially the BBC) struggle when they're played directly
            # with ffmpeg, where they just stop after some minutes,
            # so we tell ffmpeg to loop around in this case.
            extra_input_args += ["-stream_loop", "-1", "-re"]
    elif stream_type == StreamType.ENCRYPTED_HTTP:
        audio_source = streamdetails.path
        extra_input_args += ["-decryption_key", streamdetails.decryption_key]
    else:
        audio_source = streamdetails.path

    # handle seek support
    if (
        streamdetails.seek_position
        and streamdetails.duration
        and streamdetails.allow_seek
        # allow seeking for custom streams,
        # but only for custom streams that can't seek theirselves
        and not (stream_type == StreamType.CUSTOM and streamdetails.can_seek)
    ):
        extra_input_args += ["-ss", str(int(streamdetails.seek_position))]

    bytes_sent = 0
    chunk_number = 0
    buffer: bytes = b""
    finished = False
    cancelled = False
    ffmpeg_proc = FFMpeg(
        audio_input=audio_source,
        input_format=streamdetails.audio_format,
        output_format=pcm_format,
        filter_params=filter_params,
        extra_input_args=extra_input_args,
        collect_log_history=True,
        loglevel="debug" if LOGGER.isEnabledFor(VERBOSE_LOG_LEVEL) else "info",
    )

    try:
        await ffmpeg_proc.start()
        logger.debug(
            "Started media stream for %s"
            " - using streamtype: %s"
            " - volume normalization: %s"
            " - pcm format: %s"
            " - ffmpeg PID: %s",
            streamdetails.uri,
            streamdetails.stream_type,
            streamdetails.volume_normalization_mode,
            pcm_format.content_type.value,
            ffmpeg_proc.proc.pid,
        )
        # use 1 second chunks
        chunk_size = pcm_format.pcm_sample_size
        async for chunk in ffmpeg_proc.iter_chunked(chunk_size):
            if chunk_number == 1:
                # At this point ffmpeg has started and should now know the codec used
                # for encoding the audio.
                streamdetails.audio_format.codec_type = ffmpeg_proc.input_format.codec_type

            # for non-tracks we just yield all chunks directly
            if streamdetails.media_type != MediaType.TRACK:
                yield chunk
                bytes_sent += len(chunk)
                continue

            chunk_number += 1
            # determine buffer size dynamically
            if chunk_number < 5 and strip_silence_begin:
                req_buffer_size = int(pcm_format.pcm_sample_size * 5)
            elif chunk_number > 240 and strip_silence_end:
                req_buffer_size = int(pcm_format.pcm_sample_size * 10)
            elif chunk_number > 120 and strip_silence_end:
                req_buffer_size = int(pcm_format.pcm_sample_size * 8)
            elif chunk_number > 60:
                req_buffer_size = int(pcm_format.pcm_sample_size * 6)
            elif chunk_number > 20 and strip_silence_end:
                req_buffer_size = int(pcm_format.pcm_sample_size * 4)
            else:
                req_buffer_size = pcm_format.pcm_sample_size * 2

            # always append to buffer
            buffer += chunk
            del chunk

            if len(buffer) < req_buffer_size:
                # buffer is not full enough, move on
                continue

            if strip_silence_begin:
                # strip silence from begin of audio
                strip_silence_begin = False
                chunk = await strip_silence(  # noqa: PLW2901
                    mass, buffer, pcm_format=pcm_format
                )
                bytes_sent += len(chunk)
                yield chunk
                buffer = b""
                continue

            #### OTHER: enough data in buffer, feed to output
            while len(buffer) > req_buffer_size:
                yield buffer[: pcm_format.pcm_sample_size]
                bytes_sent += pcm_format.pcm_sample_size
                buffer = buffer[pcm_format.pcm_sample_size :]

        # end of audio/track reached
        logger.log(VERBOSE_LOG_LEVEL, "End of stream reached.")
        if strip_silence_end and buffer:
            # strip silence from end of audio
            buffer = await strip_silence(
                mass,
                buffer,
                pcm_format=pcm_format,
                reverse=True,
            )
        # send remaining bytes in buffer
        bytes_sent += len(buffer)
        yield buffer
        del buffer
        # wait until stderr also completed reading
        await ffmpeg_proc.wait_with_timeout(5)
        if bytes_sent == 0:
            # edge case: no audio data was sent
            raise AudioError("No audio was received")
        elif ffmpeg_proc.returncode not in (0, None):
            raise AudioError(f"FFMpeg exited with code {ffmpeg_proc.returncode}")
        finished = True
    except (Exception, GeneratorExit) as err:
        if isinstance(err, asyncio.CancelledError | GeneratorExit):
            # we were cancelled, just raise
            cancelled = True
            raise
        logger.error("Error while streaming %s: %s", streamdetails.uri, err)
        # dump the last 10 lines of the log in case of an unclean exit
        logger.warning("\n".join(list(ffmpeg_proc.log_history)[-10:]))
        streamdetails.stream_error = True
    finally:
        # always ensure close is called which also handles all cleanup
        await ffmpeg_proc.close()
        # try to determine how many seconds we've streamed
        seconds_streamed = bytes_sent / pcm_format.pcm_sample_size if bytes_sent else 0
        logger.debug(
            "stream %s (with code %s) for %s - seconds streamed: %s",
            "cancelled" if cancelled else "finished" if finished else "aborted",
            ffmpeg_proc.returncode,
            streamdetails.uri,
            seconds_streamed,
        )
        streamdetails.seconds_streamed = seconds_streamed
        # store accurate duration
        if finished and not streamdetails.seek_position and seconds_streamed:
            streamdetails.duration = seconds_streamed

        # release cache if needed
        if cache := streamdetails.cache:
            cache = cast("StreamCache", streamdetails.cache)
            cache.release()

        # parse loudnorm data if we have that collected (and enabled)
        if (
            (streamdetails.loudness is None or finished)
            and streamdetails.volume_normalization_mode == VolumeNormalizationMode.DYNAMIC
            and (finished or (seconds_streamed >= 300))
        ):
            # if dynamic volume normalization is enabled and the entire track is streamed
            # the loudnorm filter will output the measurement in the log,
            # so we can use those directly instead of analyzing the audio
            logger.log(VERBOSE_LOG_LEVEL, "Collecting loudness measurement...")
            if loudness_details := parse_loudnorm(" ".join(ffmpeg_proc.log_history)):
                logger.debug(
                    "Loudness measurement for %s: %s dB",
                    streamdetails.uri,
                    loudness_details,
                )
                streamdetails.loudness = loudness_details
                mass.create_task(
                    mass.music.set_loudness(
                        streamdetails.item_id,
                        streamdetails.provider,
                        loudness_details,
                        media_type=streamdetails.media_type,
                    )
                )
        elif (
            streamdetails.loudness is None
            and streamdetails.volume_normalization_mode
            not in (
                VolumeNormalizationMode.DISABLED,
                VolumeNormalizationMode.FIXED_GAIN,
            )
            and (finished or (seconds_streamed >= 30))
        ):
            # dynamic mode not allowed and no measurement known, we need to analyze the audio
            # add background task to start analyzing the audio
            task_id = f"analyze_loudness_{streamdetails.uri}"
            mass.call_later(5, analyze_loudness, mass, streamdetails, task_id=task_id)

        # report stream to provider
        if (finished or seconds_streamed >= 30) and (
            music_prov := mass.get_provider(streamdetails.provider)
        ):
            mass.create_task(music_prov.on_streamed(streamdetails))


def create_wave_header(samplerate=44100, channels=2, bitspersample=16, duration=None):
    """Generate a wave header from given params."""
    file = BytesIO()

    # Generate format chunk
    format_chunk_spec = b"<4sLHHLLHH"
    format_chunk = struct.pack(
        format_chunk_spec,
        b"fmt ",  # Chunk id
        16,  # Size of this chunk (excluding chunk id and this field)
        1,  # Audio format, 1 for PCM
        channels,  # Number of channels
        int(samplerate),  # Samplerate, 44100, 48000, etc.
        int(samplerate * channels * (bitspersample / 8)),  # Byterate
        int(channels * (bitspersample / 8)),  # Blockalign
        bitspersample,  # 16 bits for two byte samples, etc.
    )
    # Generate data chunk
    # duration = 3600*6.7
    data_chunk_spec = b"<4sL"
    if duration is None:
        # use max value possible
        datasize = 4254768000  # = 6,7 hours at 44100/16
    else:
        # calculate from duration
        numsamples = samplerate * duration
        datasize = int(numsamples * channels * (bitspersample / 8))
    data_chunk = struct.pack(
        data_chunk_spec,
        b"data",  # Chunk id
        int(datasize),  # Chunk size (excluding chunk id and this field)
    )
    sum_items = [
        # "WAVE" string following size field
        4,
        # "fmt " + chunk size field + chunk size
        struct.calcsize(format_chunk_spec),
        # Size of data chunk spec + data size
        struct.calcsize(data_chunk_spec) + datasize,
    ]
    # Generate main header
    all_chunks_size = int(sum(sum_items))
    main_header_spec = b"<4sL4s"
    main_header = struct.pack(main_header_spec, b"RIFF", all_chunks_size, b"WAVE")
    # Write all the contents in
    file.write(main_header)
    file.write(format_chunk)
    file.write(data_chunk)

    # return file.getvalue(), all_chunks_size + 8
    return file.getvalue()


async def resolve_radio_stream(mass: MusicAssistant, url: str) -> tuple[str, StreamType]:
    """
    Resolve a streaming radio URL.

    Unwraps any playlists if needed.
    Determines if the stream supports ICY metadata.

    Returns tuple;
    - unfolded URL as string
    - StreamType to determine ICY (radio) or HLS stream.
    """
    cache_base_key = "resolved_radio_info"
    if cache := await mass.cache.get(url, base_key=cache_base_key):
        return cache
    stream_type = StreamType.HTTP
    resolved_url = url
    timeout = ClientTimeout(total=0, connect=10, sock_read=5)
    try:
        async with mass.http_session.get(
            url, headers=HTTP_HEADERS_ICY, allow_redirects=True, timeout=timeout
        ) as resp:
            headers = resp.headers
            resp.raise_for_status()
            if not resp.headers:
                raise InvalidDataError("no headers found")
        if headers.get("icy-metaint") is not None:
            stream_type = StreamType.ICY
        if (
            url.endswith((".m3u", ".m3u8", ".pls"))
            or ".m3u?" in url
            or ".m3u8?" in url
            or ".pls?" in url
            or "audio/x-mpegurl" in headers.get("content-type")
            or "audio/x-scpls" in headers.get("content-type", "")
        ):
            # url is playlist, we need to unfold it
            try:
                substreams = await fetch_playlist(mass, url)
                if not any(x for x in substreams if x.length):
                    for line in substreams:
                        if not line.is_url:
                            continue
                        # unfold first url of playlist
                        return await resolve_radio_stream(mass, line.path)
                    raise InvalidDataError("No content found in playlist")
            except IsHLSPlaylist:
                stream_type = StreamType.HLS

    except Exception as err:
        LOGGER.warning("Error while parsing radio URL %s: %s", url, str(err))
        return (url, stream_type)

    result = (resolved_url, stream_type)
    cache_expiration = 3600 * 3
    await mass.cache.set(url, result, expiration=cache_expiration, base_key=cache_base_key)
    return result


async def get_icy_radio_stream(
    mass: MusicAssistant, url: str, streamdetails: StreamDetails
) -> AsyncGenerator[bytes, None]:
    """Get (radio) audio stream from HTTP, including ICY metadata retrieval."""
    timeout = ClientTimeout(total=0, connect=30, sock_read=5 * 60)
    LOGGER.debug("Start streaming radio with ICY metadata from url %s", url)
    async with mass.http_session.get(
        url, allow_redirects=True, headers=HTTP_HEADERS_ICY, timeout=timeout
    ) as resp:
        headers = resp.headers
        meta_int = int(headers["icy-metaint"])
        while True:
            try:
                yield await resp.content.readexactly(meta_int)
                meta_byte = await resp.content.readexactly(1)
                if meta_byte == b"\x00":
                    continue
                meta_length = ord(meta_byte) * 16
                meta_data = await resp.content.readexactly(meta_length)
            except asyncio.exceptions.IncompleteReadError:
                break
            if not meta_data:
                continue
            meta_data = meta_data.rstrip(b"\0")
            stream_title = re.search(rb"StreamTitle='([^']*)';", meta_data)
            if not stream_title:
                continue
            try:
                # in 99% of the cases the stream title is utf-8 encoded
                stream_title = stream_title.group(1).decode("utf-8")
            except UnicodeDecodeError:
                # fallback to iso-8859-1
                stream_title = stream_title.group(1).decode("iso-8859-1", errors="replace")
            cleaned_stream_title = clean_stream_title(stream_title)
            if cleaned_stream_title != streamdetails.stream_title:
                LOGGER.log(
                    VERBOSE_LOG_LEVEL,
                    "ICY Radio streamtitle original: %s",
                    stream_title,
                )
                LOGGER.log(
                    VERBOSE_LOG_LEVEL,
                    "ICY Radio streamtitle cleaned: %s",
                    cleaned_stream_title,
                )
                streamdetails.stream_title = cleaned_stream_title


async def get_hls_substream(
    mass: MusicAssistant,
    url: str,
) -> PlaylistItem:
    """Select the (highest quality) HLS substream for given HLS playlist/URL."""
    timeout = ClientTimeout(total=0, connect=30, sock_read=5 * 60)
    # fetch master playlist and select (best) child playlist
    # https://datatracker.ietf.org/doc/html/draft-pantos-http-live-streaming-19#section-10
    async with mass.http_session.get(
        url, allow_redirects=True, headers=HTTP_HEADERS, timeout=timeout
    ) as resp:
        resp.raise_for_status()
        raw_data = await resp.read()
        encoding = resp.charset or await detect_charset(raw_data)
        master_m3u_data = raw_data.decode(encoding)
    substreams = parse_m3u(master_m3u_data)
    # There is a chance that we did not get a master playlist with subplaylists
    # but just a single master/sub playlist with the actual audio stream(s)
    # so we need to detect if the playlist child's contain audio streams or
    # sub-playlists.
    if any(
        x
        for x in substreams
        if (x.length or x.path.endswith((".mp4", ".aac")))
        and not x.path.endswith((".m3u", ".m3u8"))
    ):
        return PlaylistItem(path=url, key=substreams[0].key)
    # sort substreams on best quality (highest bandwidth) when available
    if any(x for x in substreams if x.stream_info):
        substreams.sort(key=lambda x: int(x.stream_info.get("BANDWIDTH", "0")), reverse=True)
    substream = substreams[0]
    if not substream.path.startswith("http"):
        # path is relative, stitch it together
        base_path = url.rsplit("/", 1)[0]
        substream.path = base_path + "/" + substream.path
    return substream


async def get_http_stream(
    mass: MusicAssistant,
    url: str,
    streamdetails: StreamDetails,
    seek_position: int = 0,
) -> AsyncGenerator[bytes, None]:
    """Get audio stream from HTTP."""
    LOGGER.debug("Start HTTP stream for %s (seek_position %s)", streamdetails.uri, seek_position)
    if seek_position:
        assert streamdetails.duration, "Duration required for seek requests"
    # try to get filesize with a head request
    seek_supported = streamdetails.can_seek
    if seek_position or not streamdetails.size:
        async with mass.http_session.head(url, allow_redirects=True, headers=HTTP_HEADERS) as resp:
            resp.raise_for_status()
            if size := resp.headers.get("Content-Length"):
                streamdetails.size = int(size)
            seek_supported = resp.headers.get("Accept-Ranges") == "bytes"
    # headers
    headers = {**HTTP_HEADERS}
    timeout = ClientTimeout(total=0, connect=30, sock_read=5 * 60)
    skip_bytes = 0
    if seek_position and streamdetails.size:
        skip_bytes = int(streamdetails.size / streamdetails.duration * seek_position)
        headers["Range"] = f"bytes={skip_bytes}-{streamdetails.size}"

    # seeking an unknown or container format is not supported due to the (moov) headers
    if seek_position and (
        not seek_supported
        or streamdetails.audio_format.content_type
        in (
            ContentType.UNKNOWN,
            ContentType.M4A,
            ContentType.M4B,
        )
    ):
        LOGGER.warning(
            "Seeking in %s (%s) not possible.",
            streamdetails.uri,
            streamdetails.audio_format.output_format_str,
        )
        seek_position = 0
        streamdetails.seek_position = 0

    # start the streaming from http
    bytes_received = 0
    async with mass.http_session.get(
        url, allow_redirects=True, headers=headers, timeout=timeout
    ) as resp:
        is_partial = resp.status == 206
        if seek_position and not is_partial:
            raise InvalidDataError("HTTP source does not support seeking!")
        resp.raise_for_status()
        async for chunk in resp.content.iter_any():
            bytes_received += len(chunk)
            yield chunk

    # store size on streamdetails for later use
    if not streamdetails.size:
        streamdetails.size = bytes_received
    LOGGER.debug(
        "Finished HTTP stream for %s (transferred %s/%s bytes)",
        streamdetails.uri,
        bytes_received,
        streamdetails.size,
    )


async def get_file_stream(
    mass: MusicAssistant,  # noqa: ARG001
    filename: str,
    streamdetails: StreamDetails,
    seek_position: int = 0,
) -> AsyncGenerator[bytes, None]:
    """Get audio stream from local accessible file."""
    if seek_position:
        assert streamdetails.duration, "Duration required for seek requests"
    if not streamdetails.size:
        stat = await asyncio.to_thread(os.stat, filename)
        streamdetails.size = stat.st_size

    # seeking an unknown or container format is not supported due to the (moov) headers
    if seek_position and (
        streamdetails.audio_format.content_type
        in (
            ContentType.UNKNOWN,
            ContentType.M4A,
            ContentType.M4B,
            ContentType.MP4,
        )
    ):
        LOGGER.warning(
            "Seeking in %s (%s) not possible.",
            streamdetails.uri,
            streamdetails.audio_format.output_format_str,
        )
        seek_position = 0
        streamdetails.seek_position = 0

    chunk_size = get_chunksize(streamdetails.audio_format)
    async with aiofiles.open(streamdetails.data, "rb") as _file:
        if seek_position:
            seek_pos = int((streamdetails.size / streamdetails.duration) * seek_position)
            await _file.seek(seek_pos)
        # yield chunks of data from file
        while True:
            data = await _file.read(chunk_size)
            if not data:
                break
            yield data


async def get_multi_file_stream(
    mass: MusicAssistant,  # noqa: ARG001
    streamdetails: StreamDetails,
) -> AsyncGenerator[bytes, None]:
    """Return audio stream for a concatenation of multiple files."""
    files_list: list[str] = streamdetails.data
    # concat input files
    temp_file = f"/tmp/{shortuuid.random(20)}.txt"  # noqa: S108
    async with aiofiles.open(temp_file, "w") as f:
        for path in files_list:
            await f.write(f"file '{path}'\n")

    try:
        async for chunk in get_ffmpeg_stream(
            audio_input=temp_file,
            input_format=streamdetails.audio_format,
            output_format=AudioFormat(
                content_type=ContentType.NUT,
                sample_rate=streamdetails.audio_format.sample_rate,
                bit_depth=streamdetails.audio_format.bit_depth,
                channels=streamdetails.audio_format.channels,
            ),
            extra_input_args=["-safe", "0", "-f", "concat", "-i", temp_file],
        ):
            yield chunk
    finally:
        await remove_file(temp_file)


async def get_preview_stream(
    mass: MusicAssistant,
    provider_instance_id_or_domain: str,
    item_id: str,
    media_type: MediaType = MediaType.TRACK,
) -> AsyncGenerator[bytes, None]:
    """Create a 30 seconds preview audioclip for the given streamdetails."""
    if not (music_prov := mass.get_provider(provider_instance_id_or_domain)):
        raise ProviderUnavailableError
    streamdetails = await music_prov.get_stream_details(item_id, media_type)
    async for chunk in get_ffmpeg_stream(
        audio_input=music_prov.get_audio_stream(streamdetails, 30)
        if streamdetails.stream_type == StreamType.CUSTOM
        else streamdetails.path,
        input_format=streamdetails.audio_format,
        output_format=AudioFormat(content_type=ContentType.AAC),
        extra_input_args=["-to", "30"],
    ):
        yield chunk


async def get_silence(
    duration: int,
    output_format: AudioFormat,
) -> AsyncGenerator[bytes, None]:
    """Create stream of silence, encoded to format of choice."""
    if output_format.content_type.is_pcm():
        # pcm = just zeros
        for _ in range(duration):
            yield b"\0" * int(output_format.sample_rate * (output_format.bit_depth / 8) * 2)
        return
    if output_format.content_type == ContentType.WAV:
        # wav silence = wave header + zero's
        yield create_wave_header(
            samplerate=output_format.sample_rate,
            channels=2,
            bitspersample=output_format.bit_depth,
            duration=duration,
        )
        for _ in range(duration):
            yield b"\0" * int(output_format.sample_rate * (output_format.bit_depth / 8) * 2)
        return
    # use ffmpeg for all other encodings
    args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "quiet",
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r={output_format.sample_rate}:cl={'stereo'}",
        "-t",
        str(duration),
        "-f",
        output_format.output_format_str,
        "-",
    ]
    async with AsyncProcess(args, stdout=True) as ffmpeg_proc:
        async for chunk in ffmpeg_proc.iter_chunked():
            yield chunk


def get_chunksize(
    fmt: AudioFormat,
    seconds: int = 1,
) -> int:
    """Get a default chunk/file size for given contenttype in bytes."""
    pcm_size = int(fmt.sample_rate * (fmt.bit_depth / 8) * fmt.channels * seconds)
    if fmt.content_type.is_pcm() or fmt.content_type == ContentType.WAV:
        return pcm_size
    if fmt.content_type in (ContentType.WAV, ContentType.AIFF, ContentType.DSF):
        return pcm_size
    if fmt.bit_rate and fmt.bit_rate < 10000:
        return int(((fmt.bit_rate * 1000) / 8) * seconds)
    if fmt.content_type in (ContentType.FLAC, ContentType.WAVPACK, ContentType.ALAC):
        # assume 74.7% compression ratio (level 0)
        # source: https://z-issue.com/wp/flac-compression-level-comparison/
        return int(pcm_size * 0.747)
    if fmt.content_type in (ContentType.MP3, ContentType.OGG):
        return int((320000 / 8) * seconds)
    if fmt.content_type in (ContentType.AAC, ContentType.M4A):
        return int((256000 / 8) * seconds)
    return int((320000 / 8) * seconds)


def is_grouping_preventing_dsp(player: Player) -> bool:
    """Check if grouping is preventing DSP from being applied to this leader/PlayerGroup.

    If this returns True, no DSP should be applied to the player.
    This function will not check if the Player is in a group, the caller should do that first.
    """
    # We require the caller to handle non-leader cases themselves since player.synced_to
    # can be unreliable in some edge cases
    multi_device_dsp_supported = PlayerFeature.MULTI_DEVICE_DSP in player.supported_features
    child_count = len(player.group_childs) if player.group_childs else 0

    is_multiple_devices: bool
    if player.provider.startswith("player_group"):
        # PlayerGroups have no leader, so having a child count of 1 means
        # the group actually contains only a single player.
        is_multiple_devices = child_count > 1
    elif player.type == PlayerType.GROUP:
        # This is an group player external to Music Assistant.
        is_multiple_devices = True
    else:
        is_multiple_devices = child_count > 0
    return is_multiple_devices and not multi_device_dsp_supported


def is_output_limiter_enabled(mass: MusicAssistant, player: Player) -> bool:
    """Check if the player has the output limiter enabled.

    Unlike DSP, the limiter is still configurable when synchronized without MULTI_DEVICE_DSP.
    So in grouped scenarios without MULTI_DEVICE_DSP, the permanent sync group or the leader gets
    decides if the limiter should be turned on or not.
    """
    deciding_player_id = player.player_id
    if player.active_group:
        # Syncgroup, get from the group player
        deciding_player_id = player.active_group
    elif player.synced_to:
        # Not in sync group, but synced, get from the leader
        deciding_player_id = player.synced_to
    return mass.config.get_raw_player_config_value(
        deciding_player_id,
        CONF_ENTRY_OUTPUT_LIMITER.key,
        CONF_ENTRY_OUTPUT_LIMITER.default_value,
    )


def get_player_filter_params(
    mass: MusicAssistant,
    player_id: str,
    input_format: AudioFormat,
    output_format: AudioFormat,
) -> list[str]:
    """Get player specific filter parameters for ffmpeg (if any)."""
    filter_params = []

    dsp = mass.config.get_player_dsp_config(player_id)
    limiter_enabled = True

    if player := mass.players.get(player_id):
        if is_grouping_preventing_dsp(player):
            # We can not correctly apply DSP to a grouped player without multi-device DSP support,
            # so we disable it.
            dsp.enabled = False
        elif player.provider.startswith("player_group") and (
            PlayerFeature.MULTI_DEVICE_DSP not in player.supported_features
        ):
            # This is a special case! We have a player group where:
            # - The group leader does not support MULTI_DEVICE_DSP
            # - But only contains a single player (since nothing is preventing DSP)
            # We can still apply the DSP of that single player.
            if player.group_childs:
                child_player = mass.players.get(player.group_childs[0])
                dsp = mass.config.get_player_dsp_config(child_player)
            else:
                # This should normally never happen, but if it does, we disable DSP.
                dsp.enabled = False

        # We here implicitly know what output format is used for the player
        # in the audio processing steps. We save this information to
        # later be able to show this to the user in the UI.
        player.output_format = output_format

        limiter_enabled = is_output_limiter_enabled(mass, player)

    if dsp.enabled:
        # Apply input gain
        if dsp.input_gain != 0:
            filter_params.append(f"volume={dsp.input_gain}dB")

        # Process each DSP filter sequentially
        for f in dsp.filters:
            if not f.enabled:
                continue

            # Apply filter
            filter_params.extend(filter_to_ffmpeg_params(f, input_format))

        # Apply output gain
        if dsp.output_gain != 0:
            filter_params.append(f"volume={dsp.output_gain}dB")

    conf_channels = mass.config.get_raw_player_config_value(
        player_id, CONF_OUTPUT_CHANNELS, "stereo"
    )

    # handle output mixing only left or right
    if conf_channels == "left":
        filter_params.append("pan=mono|c0=FL")
    elif conf_channels == "right":
        filter_params.append("pan=mono|c0=FR")

    # Add safety limiter at the end
    if limiter_enabled:
        filter_params.append("alimiter=limit=-2dB:level=false:asc=true")

    LOGGER.debug("Generated ffmpeg params for player %s: %s", player_id, filter_params)
    return filter_params


def parse_loudnorm(raw_stderr: bytes | str) -> float | None:
    """Parse Loudness measurement from ffmpeg stderr output."""
    stderr_data = raw_stderr.decode() if isinstance(raw_stderr, bytes) else raw_stderr
    if "[Parsed_loudnorm_0 @" not in stderr_data:
        return None
    for jsun_chunk in stderr_data.split(" { "):
        try:
            stderr_data = "{" + jsun_chunk.rsplit("}")[0].strip() + "}"
            loudness_data = json_loads(stderr_data)
            return float(loudness_data["input_i"])
        except (*JSON_DECODE_EXCEPTIONS, KeyError, ValueError, IndexError):
            continue
    return None


async def analyze_loudness(
    mass: MusicAssistant,
    streamdetails: StreamDetails,
) -> None:
    """Analyze media item's audio, to calculate EBU R128 loudness."""
    if result := await mass.music.get_loudness(
        streamdetails.item_id,
        streamdetails.provider,
        media_type=streamdetails.media_type,
    ):
        # only when needed we do the analyze job
        streamdetails.loudness = result[0]
        streamdetails.loudness_album = result[1]
        return

    logger = LOGGER.getChild("analyze_loudness")
    logger.debug("Start analyzing audio for %s", streamdetails.uri)

    extra_input_args = [
        # limit to 10 minutes to reading too much in memory
        "-t",
        "600",
    ]
    if streamdetails.stream_type == StreamType.CACHE:
        cache = cast("StreamCache", streamdetails.cache)
        audio_source = await cache.get_audio_stream()
    elif streamdetails.stream_type == StreamType.MULTI_FILE:
        audio_source = get_multi_file_stream(mass, streamdetails)
    elif streamdetails.stream_type == StreamType.CUSTOM:
        audio_source = mass.get_provider(streamdetails.provider).get_audio_stream(
            streamdetails,
        )
    elif streamdetails.stream_type == StreamType.HLS:
        substream = await get_hls_substream(mass, streamdetails.path)
        audio_source = substream.path
    elif streamdetails.stream_type == StreamType.ENCRYPTED_HTTP:
        audio_source = streamdetails.path
        extra_input_args += ["-decryption_key", streamdetails.decryption_key]
    else:
        audio_source = streamdetails.path

    # calculate BS.1770 R128 integrated loudness with ffmpeg
    async with FFMpeg(
        audio_input=audio_source,
        input_format=streamdetails.audio_format,
        output_format=streamdetails.audio_format,
        audio_output="NULL",
        filter_params=["ebur128=framelog=verbose"],
        extra_input_args=extra_input_args,
        collect_log_history=True,
        loglevel="info",
    ) as ffmpeg_proc:
        await ffmpeg_proc.wait()
        log_lines = ffmpeg_proc.log_history
        log_lines_str = "\n".join(log_lines)
        try:
            loudness_str = (
                log_lines_str.split("Integrated loudness")[1].split("I:")[1].split("LUFS")[0]
            )
            loudness = float(loudness_str.strip())
        except (IndexError, ValueError, AttributeError):
            LOGGER.warning(
                "Could not determine integrated loudness of %s - %s",
                streamdetails.uri,
                log_lines_str or "received empty value",
            )
        else:
            streamdetails.loudness = loudness
            await mass.music.set_loudness(
                streamdetails.item_id,
                streamdetails.provider,
                loudness,
                media_type=streamdetails.media_type,
            )
            logger.debug(
                "Integrated loudness of %s is: %s",
                streamdetails.uri,
                loudness,
            )
        finally:
            # release cache if needed
            if cache := streamdetails.cache:
                cache = cast("StreamCache", streamdetails.cache)
                cache.release()


def _get_normalization_mode(
    core_config: CoreConfig, player_config: PlayerConfig, streamdetails: StreamDetails
) -> VolumeNormalizationMode:
    if not player_config.get_value(CONF_VOLUME_NORMALIZATION):
        # disabled for this player
        return VolumeNormalizationMode.DISABLED
    if streamdetails.target_loudness is None:
        # no target loudness set, disable normalization
        return VolumeNormalizationMode.DISABLED
    # work out preference for track or radio
    preference = VolumeNormalizationMode(
        core_config.get_value(
            CONF_VOLUME_NORMALIZATION_RADIO
            if streamdetails.media_type == MediaType.RADIO
            else CONF_VOLUME_NORMALIZATION_TRACKS,
        )
    )

    # handle no measurement available but fallback to dynamic mode is allowed
    if streamdetails.loudness is None and preference == VolumeNormalizationMode.FALLBACK_DYNAMIC:
        return VolumeNormalizationMode.DYNAMIC

    # handle no measurement available and no fallback allowed
    if streamdetails.loudness is None and preference == VolumeNormalizationMode.MEASUREMENT_ONLY:
        return VolumeNormalizationMode.DISABLED

    # handle no measurement available and fallback to fixed gain is allowed
    if streamdetails.loudness is None and preference == VolumeNormalizationMode.FALLBACK_FIXED_GAIN:
        return VolumeNormalizationMode.FIXED_GAIN

    # handle measurement available - chosen mode is measurement
    if streamdetails.loudness is not None and preference not in (
        VolumeNormalizationMode.DISABLED,
        VolumeNormalizationMode.FIXED_GAIN,
        VolumeNormalizationMode.DYNAMIC,
    ):
        return VolumeNormalizationMode.MEASUREMENT_ONLY

    # simply return the preference
    return preference
