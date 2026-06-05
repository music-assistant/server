"""Smart Fades Mixer - Mixes audio tracks using smart fades."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from music_assistant.controllers.streams.audio_analysis import SMART_FADES_ANALYSIS_DOMAIN
from music_assistant.controllers.streams.smart_fades.fades import (
    SmartCrossFade,
    SmartFade,
    StandardCrossFade,
)
from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.models.smart_fades import SmartFadesMode

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

    async def build(
        self,
        fade_in_streamdetails: StreamDetails,
        fade_out_streamdetails: StreamDetails,
        pcm_format: AudioFormat,
        standard_crossfade_duration: int,
        mode: SmartFadesMode,
        fade_out_bytes_len: int,
        fade_in_bytes_len: int,
    ) -> SmartFade:
        """Pick the SmartFade implementation, prime its filters, return it.

        :param fade_in_streamdetails: Stream details for the incoming track.
        :param fade_out_streamdetails: Stream details for the outgoing track.
        :param pcm_format: Audio format of both input buffers (and mix output).
        :param standard_crossfade_duration: Duration in seconds for standard crossfade.
        :param mode: Smart fades mode (SMART_CROSSFADE or STANDARD_CROSSFADE).
        :param fade_out_bytes_len: Expected length in bytes of the fade-out input.
        :param fade_in_bytes_len: Expected length in bytes of the fade-in input.
        """
        smart_fade: SmartFade | None = None
        if mode == SmartFadesMode.SMART_CROSSFADE:
            smart_fade = await self._build_smart_crossfade(
                fade_in_streamdetails=fade_in_streamdetails,
                fade_out_streamdetails=fade_out_streamdetails,
                fade_out_bytes_len=fade_out_bytes_len,
                fade_in_bytes_len=fade_in_bytes_len,
                pcm_format=pcm_format,
            )
        if smart_fade is None:
            smart_fade = StandardCrossFade(
                logger=self.logger,
                crossfade_duration=standard_crossfade_duration,
            )
            smart_fade._build(fade_out_bytes_len, fade_in_bytes_len, pcm_format)
        return smart_fade

    async def mix(
        self,
        smart_fade: SmartFade,
        fade_in_part: bytes | AsyncGenerator[bytes],
        fade_out_part: bytes,
        pcm_format: AudioFormat,
    ) -> AsyncGenerator[bytes]:
        """Run the already-built SmartFade and yield mixed PCM audio chunks."""
        async for chunk in smart_fade.apply(fade_out_part, fade_in_part, pcm_format):
            yield chunk

    async def _build_smart_crossfade(
        self,
        fade_in_streamdetails: StreamDetails,
        fade_out_streamdetails: StreamDetails,
        fade_out_bytes_len: int,
        fade_in_bytes_len: int,
        pcm_format: AudioFormat,
    ) -> SmartFade | None:
        """Attempt to build a SmartCrossFade. Returns None when fallback is needed."""
        fade_out_analysis: (
            AudioAnalysisData | None
        ) = await self.streams.audio_analysis.get_audio_analysis(
            fade_out_streamdetails.item_id,
            fade_out_streamdetails.provider,
            priority=(SMART_FADES_ANALYSIS_DOMAIN,),
        )
        fade_in_analysis: (
            AudioAnalysisData | None
        ) = await self.streams.audio_analysis.get_audio_analysis(
            fade_in_streamdetails.item_id,
            fade_in_streamdetails.provider,
            priority=(SMART_FADES_ANALYSIS_DOMAIN,),
        )
        if not (
            fade_out_analysis
            and fade_in_analysis
            and fade_out_analysis.bpm
            and fade_in_analysis.bpm
            and fade_out_analysis.beats is not None
            and fade_in_analysis.beats is not None
        ):
            return None
        try:
            smart_fade = SmartCrossFade(
                logger=self.logger,
                fade_out_analysis=fade_out_analysis,
                fade_in_analysis=fade_in_analysis,
            )
            smart_fade._build(fade_out_bytes_len, fade_in_bytes_len, pcm_format)
        except Exception as e:
            self.logger.warning(
                "Smart crossfade build failed: %s, falling back to standard crossfade", e
            )
            return None
        return smart_fade
