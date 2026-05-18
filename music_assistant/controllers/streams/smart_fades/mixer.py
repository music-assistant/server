"""Smart Fades Mixer - Mixes audio tracks using smart fades."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from music_assistant.controllers.streams.smart_fades.fades import (
    SmartCrossFade,
    SmartFade,
    StandardCrossFade,
)
from music_assistant.helpers.audio import align_audio_to_frame_boundary, strip_silence
from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.models.smart_fades import (
    SmartFadesMode,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.controllers.streams.controller import StreamsController


class SmartFadesMixer:
    """Smart fades mixer class that mixes tracks based on analysis data."""

    def __init__(self, streams: StreamsController) -> None:
        """Initialize smart fades mixer."""
        self.streams = streams
        self.logger = streams.logger.getChild("smart_fades_mixer")

    async def mix(
        self,
        fade_in_part: bytes | AsyncGenerator[bytes, None],
        fade_out_part: bytes,
        fade_in_streamdetails: StreamDetails,
        fade_out_streamdetails: StreamDetails,
        pcm_format: AudioFormat,
        standard_crossfade_duration: int = 10,
        mode: SmartFadesMode = SmartFadesMode.SMART_CROSSFADE,
    ) -> AsyncGenerator[bytes, None]:
        """
        Apply crossfade, yielding mixed PCM audio chunks as they become available.

        :param fade_in_part: Raw PCM bytes or async generator for the incoming track's head.
        :param fade_out_part: Raw PCM bytes for the outgoing track's tail.
        :param fade_in_streamdetails: Stream details for the incoming track.
        :param fade_out_streamdetails: Stream details for the outgoing track.
        :param pcm_format: Audio format of both input parts and the output.
        :param standard_crossfade_duration: Duration in seconds for standard crossfade fallback.
        :param mode: Smart fades mode to use.
        """
        if mode == SmartFadesMode.DISABLED:
            # No crossfade, just concatenate
            # Note that this should not happen since we check this before calling mix()
            # but just to be sure...
            fade_in_bytes = await self._ensure_bytes(fade_in_part)
            yield fade_out_part + fade_in_bytes
            return

        if mode == SmartFadesMode.STANDARD_CROSSFADE:
            # strip silence from end of outgoing track (common in song outros)
            fade_out_part = await strip_silence(
                fade_out_part,
                pcm_format=pcm_format,
                reverse=True,
            )
            # Ensure frame alignment after silence stripping
            fade_out_part = align_audio_to_frame_boundary(fade_out_part, pcm_format)
            smart_fade: SmartFade = StandardCrossFade(
                logger=self.logger,
                crossfade_duration=standard_crossfade_duration,
            )
            async for chunk in smart_fade.apply(
                fade_out_part,
                fade_in_part,
                pcm_format,
            ):
                yield chunk
            return

        # Attempt smart crossfade with analysis data from audio analysis providers
        fade_out_analysis: (
            AudioAnalysisData | None
        ) = await self.streams.audio_analysis.get_audio_analysis(
            fade_out_streamdetails.item_id,
            fade_out_streamdetails.provider,
        )
        fade_in_analysis: (
            AudioAnalysisData | None
        ) = await self.streams.audio_analysis.get_audio_analysis(
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
            and mode == SmartFadesMode.SMART_CROSSFADE
        ):
            try:
                smart_fade = SmartCrossFade(
                    logger=self.logger,
                    fade_out_analysis=fade_out_analysis,
                    fade_in_analysis=fade_in_analysis,
                )
                got_chunks = False
                async for chunk in smart_fade.apply(
                    fade_out_part,
                    fade_in_part,
                    pcm_format,
                ):
                    got_chunks = True
                    yield chunk
                return
            except Exception as e:
                if got_chunks:
                    # partial output already yielded, can't fall back safely
                    raise
                self.logger.warning(
                    "Smart crossfade failed: %s, falling back to standard crossfade", e
                )

        # Always fallback to Standard Crossfade in case something goes wrong
        # StandardCrossFade needs full bytes for slicing
        fade_in_bytes = await self._ensure_bytes(fade_in_part)
        smart_fade = StandardCrossFade(
            logger=self.logger,
            crossfade_duration=standard_crossfade_duration,
        )
        async for chunk in smart_fade.apply(
            fade_out_part,
            fade_in_bytes,
            pcm_format,
        ):
            yield chunk

    @staticmethod
    async def _ensure_bytes(
        data: bytes | AsyncGenerator[bytes, None],
    ) -> bytes:
        """Consume an async generator into bytes, or return bytes as-is."""
        if isinstance(data, bytes):
            return data
        buf = bytearray()
        async for chunk in data:
            buf.extend(chunk)
        return bytes(buf)
