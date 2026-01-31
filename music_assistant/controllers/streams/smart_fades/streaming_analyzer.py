# smart_fades_streaming_analyzer.py

import asyncio
from typing import Any

from music_assistant_models.media_items import AudioFormat

from music_assistant.controllers.streams.smart_fades.processors import (
    BeatThisStreamingProcessor,
    StreamingAnalyzerProcessor,
)


class SmartFadesStreamingAnalyzer:
    """
    Public streaming analyzer facade.
    StreamsController MUST NOT know about internal analyzers.
    """

    def __init__(self, audio_format: AudioFormat):
        self._processors: list[StreamingAnalyzerProcessor] = [
            BeatThisStreamingProcessor(audio_format=audio_format),
            # future processors go here
        ]

    async def process_pcm_chunk(self, pcm_chunk: bytes) -> None:
        await asyncio.gather(*(p.process_pcm_chunk(pcm_chunk) for p in self._processors))

    async def finalize(self) -> dict[str, Any]:
        results = await asyncio.gather(*(p.finalize() for p in self._processors))

        merged: dict[str, Any] = {}
        for r in results:
            merged.update(r)

        return merged

    async def reset(self) -> None:
        await asyncio.gather(*(p.reset() for p in self._processors))
