"""Smart Fades Mixer - Mixes audio tracks using smart fades."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.controllers.streams.smart_fades.fades import (
    SmartCrossFade,
    SmartFade,
    StandardCrossFade,
)
from music_assistant.helpers.audio import (
    align_audio_to_frame_boundary,
    strip_silence,
)
from music_assistant.models.smart_fades import (
    SmartFadesAnalysis,
    SmartFadesAnalysisFragment,
    SmartFadesMode,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.controllers.streams.streams_controller import StreamsController


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
        if mode == SmartFadesMode.STANDARD_CROSSFADE:
            smart_fade: SmartFade = StandardCrossFade(
                logger=self.logger,
                crossfade_duration=standard_crossfade_duration,
            )
            return await smart_fade.apply(
                fade_out_part,
                fade_in_part,
                pcm_format,
            )
        # Attempt smart crossfade with analysis data
        fade_out_analysis: SmartFadesAnalysis | None
        if stored_analysis := await self.streams.mass.music.get_smart_fades_analysis(
            fade_out_streamdetails.item_id,
            fade_out_streamdetails.provider,
            SmartFadesAnalysisFragment.OUTRO,
        ):
            fade_out_analysis = stored_analysis
        else:
            fade_out_analysis = await self.streams.mass.streams.smart_fades_analyzer.analyze(
                fade_out_streamdetails.item_id,
                fade_out_streamdetails.provider,
                SmartFadesAnalysisFragment.OUTRO,
                fade_out_part,
                pcm_format,
            )

        fade_in_analysis: SmartFadesAnalysis | None
        if stored_analysis := await self.streams.mass.music.get_smart_fades_analysis(
            fade_in_streamdetails.item_id,
            fade_in_streamdetails.provider,
            SmartFadesAnalysisFragment.INTRO,
        ):
            fade_in_analysis = stored_analysis
        else:
            fade_in_analysis = await self.streams.mass.streams.smart_fades_analyzer.analyze(
                fade_in_streamdetails.item_id,
                fade_in_streamdetails.provider,
                SmartFadesAnalysisFragment.INTRO,
                fade_in_part,
                pcm_format,
            )
        if (
            fade_out_analysis
            and fade_in_analysis
            and fade_out_analysis.confidence > 0.3
            and fade_in_analysis.confidence > 0.3
            and mode == SmartFadesMode.SMART_CROSSFADE
        ):
            try:
                smart_fade = SmartCrossFade(
                    logger=self.logger,
                    fade_out_analysis=fade_out_analysis,
                    fade_in_analysis=fade_in_analysis,
                )
                return await smart_fade.apply(
                    fade_out_part,
                    fade_in_part,
                    pcm_format,
                )
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
