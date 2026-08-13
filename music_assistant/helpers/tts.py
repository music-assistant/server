"""Helpers to render text into playable speech audio through the plugin TTS engines."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from music_assistant_models.enums import StreamType
from music_assistant_models.errors import InvalidDataError, MusicAssistantError

from music_assistant.constants import MASS_LOGGER_NAME

if TYPE_CHECKING:
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.plugin import TTSEngine

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.helpers.tts")

# last-resort guard so a wedged engine fails the call instead of hanging its caller.
# Kept above the deadlines the engines apply themselves (120s in the OpenAI-compatible
# providers), so their own, more specific error is the one that surfaces.
TTS_QUERY_TIMEOUT_SECONDS = 180

REMOTE_STREAM_SCHEMES = ("http://", "https://", "rtsp://", "rtmp://")


async def query_tts_engine(
    engine: TTSEngine,
    message: str,
    language: str | None = None,
    timeout: float | None = None,
) -> StreamDetails:
    """
    Render a message through a TTS engine.

    :param engine: The TTS engine to speak the message.
    :param message: The text to speak.
    :param language: Optional language code, omit to use the engine's own default voice.
    :param timeout: Seconds to wait for the engine, defaults to TTS_QUERY_TIMEOUT_SECONDS.
        Lower it for a caller that is holding something up while it waits.
    """
    if timeout is None:
        timeout = TTS_QUERY_TIMEOUT_SECONDS
    try:
        async with asyncio.timeout(timeout) as query_timeout:
            return await engine.provider.get_tts_message(
                message, language=language, engine_id=engine.id
            )
    except TimeoutError as err:
        # expired() tells our own cap apart from a timeout raised inside the engine
        if not query_timeout.expired():
            raise
        raise MusicAssistantError(
            f"TTS engine '{engine.uid}' did not respond within {timeout}s"
        ) from err


async def query_tts_engine_with_language_fallback(
    engine: TTSEngine,
    message: str,
    language: str | None = None,
    timeout: float | None = None,
    logger: logging.Logger | None = None,
) -> StreamDetails:
    """
    Render a message through a TTS engine, retrying without the language if it is rejected.

    :param engine: The TTS engine to speak the message.
    :param message: The text to speak.
    :param language: Optional language code, omit to use the engine's own default voice.
    :param timeout: Seconds to wait for the engine, defaults to TTS_QUERY_TIMEOUT_SECONDS.
        Lower it for a caller that is holding something up while it waits.
    :param logger: Optional logger to report a rejected language on.
    """
    try:
        return await query_tts_engine(engine, message, language, timeout)
    except TimeoutError, MusicAssistantError:
        # a timeout or our own structured failure is not a language rejection, so a
        # language-less retry would not help and would only double the wait
        raise
    except Exception as err:
        if language is None:
            raise
        # some engines reject a language they don't support; fall back to the engine's
        # own default voice rather than losing the audio entirely
        (logger or LOGGER).warning(
            "TTS engine '%s' rejected language '%s' (%s), retrying with its default voice",
            engine.uid,
            language,
            err,
        )
        return await query_tts_engine(engine, message, None, timeout)


def resolve_tts_language(mass: MusicAssistant) -> str | None:
    """
    Return the language a TTS engine should speak in, as a hyphenated code like 'en-US'.

    Returns None when no language is configured, leaving the engine on its own default voice.

    :param mass: The Music Assistant instance holding the configured locale.
    """
    locale = mass.metadata.locale
    return locale.replace("_", "-") if locale else None


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
