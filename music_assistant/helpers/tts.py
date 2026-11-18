"""Helpers to render text into playable speech audio through the plugin TTS engines."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import StreamType
from music_assistant_models.errors import InvalidDataError, MusicAssistantError

from music_assistant.constants import MASS_LOGGER_NAME

if TYPE_CHECKING:
    from collections.abc import Iterable

    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.plugin import TTSEngine

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.helpers.tts")

# last-resort guard so a wedged engine fails the call instead of hanging its caller.
# Kept above the deadlines the engines apply themselves (120s in the OpenAI-compatible
# providers), so their own, more specific error is the one that surfaces.
TTS_QUERY_TIMEOUT_SECONDS = 180

REMOTE_STREAM_SCHEMES = ("http://", "https://", "rtsp://", "rtmp://")

# codes that name the same language as another: written Norwegian and the old Hebrew code
LANGUAGE_ALIASES = {"nb": "no", "iw": "he"}


class TTSLanguageNotSupportedError(MusicAssistantError):
    """The TTS engine rejected (or likely rejected) the requested language."""


async def query_tts_engine(
    engine: TTSEngine,
    message: str,
    language: str | None = None,
    timeout: float | None = None,
    options: dict[str, Any] | None = None,
) -> StreamDetails:
    """
    Render a message through a TTS engine.

    :param engine: The TTS engine to speak the message.
    :param message: The text to speak.
    :param language: Optional language code, omit to use the engine's own default voice.
    :param timeout: Seconds to wait for the engine, defaults to TTS_QUERY_TIMEOUT_SECONDS.
        Lower it for a caller that is holding something up while it waits.
    :param options: Optional integration-specific TTS options, passed through to the
        engine as-is. Ignored by engines that have none.
    """
    if timeout is None:
        timeout = TTS_QUERY_TIMEOUT_SECONDS
    query_timeout = asyncio.timeout(timeout)
    try:
        async with query_timeout:
            return await engine.provider.get_tts_message(
                message, language=language, engine_id=engine.id, options=options
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
    options: dict[str, Any] | None = None,
) -> StreamDetails:
    """
    Render a message through a TTS engine, retrying without the language if the engine rejects it.

    :param engine: The TTS engine to speak the message.
    :param message: The text to speak.
    :param language: Optional language code, omit to use the engine's own default voice.
    :param timeout: Seconds to wait for the engine, defaults to TTS_QUERY_TIMEOUT_SECONDS.
        Lower it for a caller that is holding something up while it waits.
    :param logger: Optional logger to report a rejected language on.
    :param options: Optional integration-specific TTS options, passed through to the
        engine as-is. Ignored by engines that have none.
    """
    try:
        return await query_tts_engine(engine, message, language, timeout, options)
    except TTSLanguageNotSupportedError as err:
        if not language:
            raise
        # the error message carries the classifier's certainty, so it is logged as-is
        (logger or LOGGER).warning("%s, retrying with the engine's default voice", err)
        return await query_tts_engine(engine, message, None, timeout, options)


def match_tts_language(
    language: str, supported_languages: Iterable[str], country: str | None = None
) -> str | None:
    """
    Return the entry of an engine's supported languages that fits the requested language.

    Returns None when the engine supports no variant of the language at all.

    :param language: The requested language, a bare code like 'nl' or a regional one
        like 'nl-NL' or 'nl_NL'.
    :param supported_languages: The languages the engine accepts, in its own spelling.
    :param country: Optional region to prefer when several variants fit, like 'NL'.
    """
    wanted_language, _, wanted_region = language.replace("_", "-").partition("-")
    wanted_language = wanted_language.casefold()
    wanted_language = LANGUAGE_ALIASES.get(wanted_language, wanted_language)
    candidates: list[tuple[str, str]] = []
    for supported in supported_languages:
        base, _, region = supported.replace("_", "-").partition("-")
        base = base.casefold()
        if LANGUAGE_ALIASES.get(base, base) == wanted_language:
            candidates.append((supported, region.upper()))
    if not candidates:
        return None
    # regions in order of preference: the requested one, the bare code, the caller's country,
    # U.S. English (as Home Assistant prefers it) and the region spelled like the language
    regions = [wanted_region.upper(), ""]
    if country:
        regions.append(country.upper())
    if wanted_language == "en":
        regions.append("US")
    regions.append(wanted_language.upper())
    for region in regions:
        for supported, supported_region in candidates:
            if supported_region == region:
                return supported
    return candidates[0][0]


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
