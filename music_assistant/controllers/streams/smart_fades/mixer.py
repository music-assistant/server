"""Smart Fades Mixer - Mixes audio tracks using smart fades."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Track

from music_assistant.controllers.streams.smart_fades.fades import (
    SmartCrossFade,
    SmartFade,
    StandardCrossFade,
)
from music_assistant.helpers.audio import (
    align_audio_to_frame_boundary,
    strip_silence,
)
from music_assistant.helpers.process import communicate
from music_assistant.models.smart_fades import (
    SmartFadesMode,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.controllers.streams.streams_controller import StreamsController

_SMART_FADES_DEBUG_DIR = (
    Path(__file__).parent.parent.parent.parent.parent.parent / "smart_fades_test" / "smart_fades"
)


class SmartFadesMixer:
    """Smart fades mixer class that mixes tracks based on analysis data."""

    def __init__(self, streams: StreamsController) -> None:
        """Initialize smart fades mixer."""
        self.streams = streams
        self.logger = streams.logger.getChild("smart_fades_mixer")

    async def mix(
        self,
        fade_in_part: bytes,
        fade_out_part: bytes,
        fade_in_streamdetails: StreamDetails,
        fade_out_streamdetails: StreamDetails,
        pcm_format: AudioFormat,
        standard_crossfade_duration: int = 10,
        mode: SmartFadesMode = SmartFadesMode.SMART_CROSSFADE,
    ) -> bytes:
        """Apply crossfade with internal state management and smart/standard fallback logic."""
        if mode == SmartFadesMode.DISABLED:
            # No crossfade, just concatenate
            # Note that this should not happen since we check this before calling mix()
            # but just to be sure...
            return fade_out_part + fade_in_part

        if mode == SmartFadesMode.STANDARD_CROSSFADE:
            # strip silence from end of audio of fade_out_part
            fade_out_part = await strip_silence(
                self.streams.mass,
                fade_out_part,
                pcm_format=pcm_format,
                reverse=True,
            )
            # Ensure frame alignment after silence stripping
            fade_out_part = align_audio_to_frame_boundary(fade_out_part, pcm_format)
            # strip silence from begin of audio of fade_in_part
            fade_in_part = await strip_silence(
                self.streams.mass,
                fade_in_part,
                pcm_format=pcm_format,
                reverse=False,
            )
            # Ensure frame alignment after silence stripping
            fade_in_part = align_audio_to_frame_boundary(fade_in_part, pcm_format)
            smart_fade: SmartFade = StandardCrossFade(
                logger=self.logger,
                crossfade_duration=standard_crossfade_duration,
            )
            return await smart_fade.apply(
                fade_out_part,
                fade_in_part,
                pcm_format,
            )
        # Attempt smart crossfade with stored audio analysis data
        fade_out_analysis = await self.streams.mass.music.get_audio_analysis(
            fade_out_streamdetails.item_id,
            fade_out_streamdetails.provider,
        )
        fade_in_analysis = await self.streams.mass.music.get_audio_analysis(
            fade_in_streamdetails.item_id,
            fade_in_streamdetails.provider,
        )

        if (
            fade_out_analysis
            and fade_in_analysis
            and fade_out_analysis.bpm
            and fade_in_analysis.bpm
            and fade_out_analysis.beats is not None
            and fade_in_analysis.beats is not None
        ):
            try:
                smart_fade = SmartCrossFade(
                    logger=self.logger,
                    fade_out_analysis=fade_out_analysis,
                    fade_in_analysis=fade_in_analysis,
                )
                result = await smart_fade.apply(
                    fade_out_part,
                    fade_in_part,
                    pcm_format,
                )
                await self._save_crossfade_to_mp3(
                    result,
                    pcm_format,
                    fade_out_streamdetails,
                    fade_in_streamdetails,
                )
                return result
            except Exception as e:
                self.logger.warning(
                    "Smart crossfade failed: %s, falling back to standard crossfade", e
                )

        # Always fallback to Standard Crossfade in case something goes wrong
        smart_fade = StandardCrossFade(
            logger=self.logger,
            crossfade_duration=standard_crossfade_duration,
        )
        return await smart_fade.apply(
            fade_out_part,
            fade_in_part,
            pcm_format,
        )

    async def _track_display_name(self, streamdetails: StreamDetails) -> str:
        """Return 'Artist - Title' for a track streamdetails, falling back to item_id."""
        try:
            item = await self.streams.mass.music.get_item(
                MediaType.TRACK,
                streamdetails.item_id,
                streamdetails.provider,
            )
            if isinstance(item, Track) and item.name:
                return f"{item.artist_str} - {item.name}"
        except Exception as e:
            self.logger.debug("Could not resolve track display name: %s", e)
        return streamdetails.stream_title or streamdetails.item_id

    async def _save_crossfade_to_mp3(
        self,
        pcm_bytes: bytes,
        pcm_format: AudioFormat,
        fade_out_streamdetails: StreamDetails,
        fade_in_streamdetails: StreamDetails,
    ) -> None:
        """Save a SmartCrossFade result to an MP3 file in the debug output directory.

        :param pcm_bytes: Raw PCM bytes returned by SmartCrossFade.apply().
        :param pcm_format: Audio format describing the PCM encoding.
        :param fade_out_streamdetails: StreamDetails for the outgoing track.
        :param fade_in_streamdetails: StreamDetails for the incoming track.
        """
        if not _SMART_FADES_DEBUG_DIR.is_dir():
            return

        out_title, in_title = await asyncio.gather(
            self._track_display_name(fade_out_streamdetails),
            self._track_display_name(fade_in_streamdetails),
        )
        raw_name = f"{out_title} -> {in_title}"
        # Strip characters that are invalid in filenames
        safe_name = re.sub(r'[\\/*?:"<>|]', "_", raw_name)
        output_path = _SMART_FADES_DEBUG_DIR / f"{safe_name}.mp3"

        args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-acodec",
            pcm_format.content_type.name.lower(),
            "-ac",
            str(pcm_format.channels),
            "-ar",
            str(pcm_format.sample_rate),
            "-channel_layout",
            "mono" if pcm_format.channels == 1 else "stereo",
            "-f",
            pcm_format.content_type.value,
            "-i",
            "-",
            "-q:a",
            "2",
            str(output_path),
        ]
        try:
            returncode, _, stderr = await communicate(args, pcm_bytes)
            if returncode != 0:
                stderr_msg = stderr.decode() if stderr else "(no stderr)"
                self.logger.warning("Failed to save crossfade MP3: %s", stderr_msg)
            else:
                self.logger.debug("Saved SmartCrossFade to %s", output_path)
        except Exception as e:
            self.logger.warning("Failed to save crossfade MP3: %s", e)
