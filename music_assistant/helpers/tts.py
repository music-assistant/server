"""Helpers to render text into playable speech audio through the plugin TTS engines."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from music_assistant_models.enums import StreamType
from music_assistant_models.errors import InvalidDataError, MusicAssistantError

if TYPE_CHECKING:
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.models.plugin import TTSEngine

# last-resort guard so a wedged engine fails the call instead of hanging its caller.
# Kept above the deadlines the engines apply themselves (120s in the OpenAI-compatible
# providers), so their own, more specific error is the one that surfaces.
TTS_QUERY_TIMEOUT_SECONDS = 180

REMOTE_STREAM_SCHEMES = ("http://", "https://", "rtsp://", "rtmp://")


async def query_tts_engine(
    engine: TTSEngine, message: str, language: str | None = None
) -> StreamDetails:
    """
    Render a message through a TTS engine.

    :param engine: The TTS engine to speak the message.
    :param message: The text to speak.
    :param language: Optional language code, omit to use the engine's own default voice.
    """
    try:
        async with asyncio.timeout(TTS_QUERY_TIMEOUT_SECONDS) as query_timeout:
            return await engine.provider.get_tts_message(
                message, language=language, engine_id=engine.id
            )
    except TimeoutError as err:
        # expired() tells our own cap apart from a timeout raised inside the engine
        if not query_timeout.expired():
            raise
        raise MusicAssistantError(
            f"TTS engine '{engine.uid}' did not respond within {TTS_QUERY_TIMEOUT_SECONDS}s"
        ) from err


async def resolve_tts_stream_path(
    engine: TTSEngine, stream_details: StreamDetails
) -> tuple[str, StreamType]:
    """
    Return the playable path of a rendered clip and the way to stream it.

    :param engine: The TTS engine that produced the clip, named in the error raised when it
        did not return anything playable.
    :param stream_details: The StreamDetails the engine returned.
    """
    path = str(stream_details.path or "").strip()
    if path.startswith(REMOTE_STREAM_SCHEMES):
        return path, StreamType.HTTP
    if path and Path(path).is_absolute() and await asyncio.to_thread(Path(path).is_file):
        return path, StreamType.LOCAL_FILE
    raise InvalidDataError(
        f"TTS engine '{engine.uid}' returned an unusable stream path: "
        f"{path or '<empty>'}. StreamDetails.path must be a fetchable "
        "http(s)/rtsp/rtmp URL or the absolute path of an existing local file."
    )
