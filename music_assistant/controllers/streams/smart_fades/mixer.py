"""Smart Fades Mixer - Mixes audio tracks using smart fades."""

from __future__ import annotations

import math
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from music_assistant_models.enums import CrossfadeMode

from music_assistant.controllers.streams.audio_analysis import SMART_FADES_ANALYSIS_DOMAIN
from music_assistant.controllers.streams.smart_fades.fades import (
    SmartCrossFade,
    SmartFade,
    SmartFadeNotApplicable,
    StandardCrossFade,
)
from music_assistant.controllers.streams.smart_fades.helpers import detect_effective_audio_end
from music_assistant.controllers.streams.smart_fades.vocal import (
    VocalMask,
    build_vocal_windows,
    parse_vocal_probabilities,
)
from music_assistant.helpers.audio import align_audio_to_frame_boundary, strip_silence
from music_assistant.models.audio_analysis import AudioAnalysisData

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
        mode: CrossfadeMode,
        fade_out_data: bytes,
        fade_in_bytes_len: int,
    ) -> SmartFade:
        """
        Pick the SmartFade implementation, prime its filters, and return it.

        For the standard crossfade path (explicit mode or smart-crossfade fallback)
        the trailing silence in ``fade_out_data`` is measured so that ``timing_info``
        reflects the audio that will actually be rendered.  The trim itself is deferred
        to ``apply()``, which executes it as a plain slice — no bytes are modified here.

        :param fade_in_streamdetails: Stream details for the incoming track.
        :param fade_out_streamdetails: Stream details for the outgoing track.
        :param pcm_format: Audio format of both input buffers (and mix output).
        :param standard_crossfade_duration: Duration in seconds for standard crossfade.
        :param mode: Smart fades mode (SMART_CROSSFADE or STANDARD_CROSSFADE).
        :param fade_out_data: PCM buffer of the outgoing track's tail.
        :param fade_in_bytes_len: Expected length in bytes of the fade-in input.
        """
        # degradation chain: smart-crossfade → standard; richer modes prepend their builder
        smart_fade: SmartFade | None = None
        fade_out_analysis: AudioAnalysisData | None = None
        if mode == CrossfadeMode.SMART_CROSSFADE:
            smart_fade, fade_out_analysis = await self._build_smart_crossfade(
                fade_in_streamdetails=fade_in_streamdetails,
                fade_out_streamdetails=fade_out_streamdetails,
                fade_out_bytes_len=len(fade_out_data),
                fade_in_bytes_len=fade_in_bytes_len,
                pcm_format=pcm_format,
            )
        if smart_fade is None:
            smart_fade = await self._build_standard_crossfade(
                fade_out_data=fade_out_data,
                fade_in_bytes_len=fade_in_bytes_len,
                pcm_format=pcm_format,
                standard_crossfade_duration=standard_crossfade_duration,
                fade_out_analysis=fade_out_analysis,
            )
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

    async def _build_standard_crossfade(
        self,
        fade_out_data: bytes,
        fade_in_bytes_len: int,
        pcm_format: AudioFormat,
        standard_crossfade_duration: int,
        fade_out_analysis: AudioAnalysisData | None = None,
    ) -> StandardCrossFade:
        """
        Build a StandardCrossFade — the tail of the degradation chain, never fails.

        Measures the trailing silence here so timing_info reflects the audio that
        will actually be rendered; apply() executes the cut as a plain slice.

        :param fade_out_data: PCM buffer of the outgoing track's tail.
        :param fade_in_bytes_len: Expected length in bytes of the fade-in input.
        :param pcm_format: Audio format of both input buffers.
        :param standard_crossfade_duration: Duration in seconds for standard crossfade.
        :param fade_out_analysis: Outgoing analysis retained from a failed smart
            build, or ``None`` for the regular standard path.
        """
        trailing_silence_bytes = 0
        try:
            stripped = align_audio_to_frame_boundary(
                await strip_silence(fade_out_data, pcm_format=pcm_format, reverse=True),
                pcm_format,
            )
            retained_bytes = max(
                len(stripped),
                self._get_vocal_retention_bytes(
                    fade_out_analysis,
                    len(fade_out_data),
                    pcm_format,
                ),
            )
            trailing_silence_bytes = max(0, len(fade_out_data) - retained_bytes)
        except Exception as err:
            # a failed measurement degrades to the old late-boundary bookkeeping
            # instead of killing the stream
            self.logger.warning("Measuring trailing silence failed: %s", err)
        smart_fade = StandardCrossFade(
            logger=self.logger,
            crossfade_duration=standard_crossfade_duration,
            trailing_silence_bytes=trailing_silence_bytes,
        )
        smart_fade.build(len(fade_out_data) - trailing_silence_bytes, fade_in_bytes_len, pcm_format)
        return smart_fade

    async def _build_smart_crossfade(
        self,
        fade_in_streamdetails: StreamDetails,
        fade_out_streamdetails: StreamDetails,
        fade_out_bytes_len: int,
        fade_in_bytes_len: int,
        pcm_format: AudioFormat,
    ) -> tuple[SmartFade | None, AudioAnalysisData | None]:
        """
        Attempt to build a SmartCrossFade and retain outgoing analysis for fallback.

        Returns the built fade (or ``None`` when fallback is needed) together
        with the outgoing analysis row, when available.
        """
        analyses = await self._load_analyses(fade_out_streamdetails, fade_in_streamdetails)
        fade_out_analysis, fade_in_analysis = analyses
        if not (
            fade_out_analysis
            and fade_in_analysis
            and fade_out_analysis.bpm
            and fade_in_analysis.bpm
            and fade_out_analysis.beats is not None
            and fade_in_analysis.beats is not None
        ):
            return None, fade_out_analysis
        try:
            smart_fade = SmartCrossFade(
                logger=self.logger,
                fade_out_analysis=fade_out_analysis,
                fade_in_analysis=fade_in_analysis,
            )
            smart_fade.build(fade_out_bytes_len, fade_in_bytes_len, pcm_format)
        except SmartFadeNotApplicable as e:
            self.logger.debug("Smart crossfade not applicable: %s - using standard crossfade", e)
            return None, fade_out_analysis
        except Exception as e:
            self.logger.warning(
                "Smart crossfade build failed: %s, falling back to standard crossfade", e
            )
            return None, fade_out_analysis
        return smart_fade, fade_out_analysis

    async def _load_analyses(
        self,
        fade_out_streamdetails: StreamDetails,
        fade_in_streamdetails: StreamDetails,
    ) -> tuple[AudioAnalysisData | None, AudioAnalysisData | None]:
        """
        Load both tracks' analysis rows for a planned fade.

        Rows are returned independently so a failed smart build can still use a
        valid outgoing row to protect vocals in the standard fallback.
        """
        fade_out_analysis = await self.streams.audio_analysis.get_audio_analysis(
            fade_out_streamdetails.item_id,
            fade_out_streamdetails.provider,
            priority=(SMART_FADES_ANALYSIS_DOMAIN,),
        )
        fade_in_analysis = await self.streams.audio_analysis.get_audio_analysis(
            fade_in_streamdetails.item_id,
            fade_in_streamdetails.provider,
            priority=(SMART_FADES_ANALYSIS_DOMAIN,),
        )
        return fade_out_analysis, fade_in_analysis

    @staticmethod
    def _get_vocal_retention_bytes(
        analysis: AudioAnalysisData | None,
        fade_out_bytes_len: int,
        pcm_format: AudioFormat,
    ) -> int:
        """
        Return the frame-aligned outgoing length needed to retain an audible vocal.

        Invalid or stale vocal data, missing/invalid RMS data, and vocals beyond
        the RMS-audible boundary return zero so standard silence removal keeps its
        existing behavior.

        :param analysis: Outgoing track analysis retained from the smart path.
        :param fade_out_bytes_len: Full outgoing PCM buffer length in bytes.
        :param pcm_format: Audio format of the outgoing buffer.
        """
        if analysis is None or analysis.rms_energy is None:
            return 0
        rms_energy = analysis.rms_energy
        if len(rms_energy) < 2 or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0.0
            for value in rms_energy
        ):
            return 0
        timeline = parse_vocal_probabilities(analysis)
        if timeline is None:
            return 0

        buffer_duration = fade_out_bytes_len / pcm_format.pcm_sample_size
        track_duration = analysis.duration
        assert track_duration is not None  # guaranteed by the validated timeline
        buffer_offset = max(0.0, track_duration - buffer_duration)
        vocal_mask = build_vocal_windows(
            timeline.probabilities,
            timeline.frame_duration,
            buffer_offset,
            track_duration,
            beat_duration=60.0 / analysis.bpm if analysis.bpm and analysis.bpm > 0.0 else None,
        )
        if not vocal_mask.windows:
            return 0

        audio_end = detect_effective_audio_end(
            rms_energy,
            track_duration,
            buffer_duration,
        )
        buffer_local_mask = VocalMask(
            windows=[
                (left - buffer_offset, right - buffer_offset) for left, right in vocal_mask.windows
            ]
        ).clamped_to(audio_end)
        retained_seconds = buffer_local_mask.last_end()
        frame_size = pcm_format.channels * pcm_format.bit_depth // 8
        retained_frames = math.ceil(retained_seconds * pcm_format.pcm_sample_size / frame_size)
        return min(fade_out_bytes_len, retained_frames * frame_size)
