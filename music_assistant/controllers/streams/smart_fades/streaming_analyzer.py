# smart_fades_streaming_analyzer.py

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import numpy as np
from music_assistant_models.media_items import AudioFormat

from music_assistant.controllers.streams.smart_fades.processors import (
    BeatThisStreamingProcessor,
    StreamingAnalyzerProcessor,
)
from music_assistant.models.smart_fades import SmartFadesAnalysis, SmartFadesAnalysisFragment

if TYPE_CHECKING:
    from music_assistant.controllers.streams.streams_controller import StreamsController


class SmartFadesStreamingAnalyzer:
    """
    Public streaming analyzer facade.
    StreamsController MUST NOT know about internal analyzers.
    """

    def __init__(
        self,
        streams: StreamsController,
        item_id: str,
        provider_instance_id_or_domain: str,
        audio_format: AudioFormat,
    ):
        self._processors: list[StreamingAnalyzerProcessor] = [
            BeatThisStreamingProcessor(audio_format=audio_format),
            # future processors go here
        ]
        self.streams = streams
        self.item_id = item_id
        self.provider_instance_id_or_domain = provider_instance_id_or_domain

    async def process_pcm_chunk(self, pcm_chunk: bytes) -> None:
        await asyncio.gather(*(p.process_pcm_chunk(pcm_chunk) for p in self._processors))

    async def finalize(self) -> SmartFadesAnalysis:
        results = await asyncio.gather(*(p.finalize() for p in self._processors))

        merged: dict[str, Any] = {}
        for r in results:
            merged.update(r)

        analysis = self._create_analysis(merged)
        self.streams.mass.create_task(
            self.streams.mass.music.set_smart_fades_analysis(
                self.item_id, self.provider_instance_id_or_domain, analysis
            )
        )
        return analysis

    def _create_analysis(self, results: dict[str, Any]) -> SmartFadesAnalysis:
        beats = results["beats"]
        downbeats = results["downbeats"]
        duration = results["duration"]

        # Calculate BPM from beat intervals
        beat_intervals = np.diff(beats)
        bpm = 60.0 / np.mean(beat_intervals)

        # Confidence based on beat interval consistency (lower CV = higher confidence)
        cv = float(np.std(beat_intervals) / np.mean(beat_intervals))
        confidence = max(0.0, min(1.0, 1.0 - cv))

        return SmartFadesAnalysis(
            fragment=SmartFadesAnalysisFragment.FULL_SONG,
            bpm=float(bpm),
            beats=beats,
            downbeats=downbeats,
            confidence=confidence,
            duration=duration,
        )

    async def reset(self) -> None:
        await asyncio.gather(*(p.reset() for p in self._processors))
