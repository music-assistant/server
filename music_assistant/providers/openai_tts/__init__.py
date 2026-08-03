"""
OpenAI Text-to-speech provider for Music Assistant.

Renders speech through the OpenAI speech API and any self-hostable server implementing
the same endpoint (Kokoro-FastAPI, LocalAI, Speaches, ...), exposing each voice as a TTS
engine. Rendered clips are cached on disk and served from a route on the streamserver.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

import aiofiles
from aiofiles.os import makedirs, remove, replace
from aiofiles.os import path as aiopath
from aiohttp import ClientTimeout, web
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import AudioError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.models.plugin import PluginProvider, TTSEngine

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiohttp import ClientSession
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_BASE_URL = "base_url"
CONF_API_KEY = "api_key"
CONF_MODEL = "model"
CONF_VOICES = "voices"

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "tts-1"
# the voices the tts-1 model supports, used when the backend does not advertise its own
DEFAULT_VOICES = ("alloy", "echo", "fable", "nova", "onyx", "shimmer")
# the one response format all compatible backends implement
RESPONSE_FORMAT = "mp3"
# rendered clips older than this are removed from the on-disk cache
CACHE_MAX_AGE = 24 * 3600
# the shared http session has no default timeout: discovery runs during provider load so
# it must give up quickly, while rendering can legitimately be slow on a cpu-bound backend
VOICES_TIMEOUT = ClientTimeout(total=10)
SPEECH_TIMEOUT = ClientTimeout(total=120)

# clip file names are sha256 hexdigests; anything else in the cache directory is not ours
FILE_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")

SUPPORTED_FEATURES = {ProviderFeature.TTS}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return OpenAITTSProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def fetch_backend_voices(
    http_session: ClientSession, base_url: str, api_key: str = ""
) -> list[str]:
    """
    Return the voices the backend advertises, empty when it does not advertise any.

    Only some of the self-hostable servers implement a voice listing; the OpenAI cloud
    API does not. Never raises: an unreachable or silent backend yields an empty list.

    :param http_session: The HTTP session to request with.
    :param base_url: The API endpoint, without trailing slash.
    :param api_key: The API key to authenticate with, empty for backends without auth.
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with http_session.get(
            f"{base_url}/audio/voices", headers=headers, timeout=VOICES_TIMEOUT
        ) as response:
            response.raise_for_status()
            data = await response.json()
    except Exception:
        return []
    items = data.get("voices") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    # per item, so one unexpected entry does not discard the voices around it
    voices: list[str] = []
    for item in items:
        name: object
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = item.get("id") or item.get("name")
        else:
            continue
        if isinstance(name, str) and name:
            voices.append(name)
    return voices


class OpenAITTSProvider(PluginProvider):
    """Text-to-speech provider backed by the OpenAI (compatible) speech API."""

    _cache_dir: str
    _render_lock: asyncio.Lock
    _voices: list[str]
    # rendered clips by file id; the route only serves paths from this index
    _clips: dict[str, str]
    _unregister_route: Callable[[], None] | None = None

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # scoped per instance: different endpoints can render the same input differently
        self._cache_dir = os.path.join(self.mass.cache_path, self.domain, self.instance_id)
        self._render_lock = asyncio.Lock()
        self._voices = await self._resolve_voices()
        # clips rendered before a restart stay playable
        self._clips = await self._index_cache()
        self._unregister_route = self.mass.streams.register_dynamic_route(
            self._route_path, self._handle_speech_request
        )

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        is_removed will be set to True when the provider is removed from the configuration.
        """
        await super().unload(is_removed)
        if unregister := self._unregister_route:
            self._unregister_route = None
            unregister()
        if is_removed:
            await asyncio.to_thread(shutil.rmtree, self._cache_dir, ignore_errors=True)

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return the (options) config entries for this provider instance.

        The connection details (endpoint, api key and model) are collected by the setup
        flow (see setup_flow.py); the only option here overrides the list of voices that
        is exposed as TTS engines.
        """
        return (
            ConfigEntry(
                key=CONF_VOICES,
                type=ConfigEntryType.STRING,
                required=False,
                default_value=None,
                category="features",
                requires_reload=True,
            ),
        )

    async def get_tts_engines(self) -> list[TTSEngine]:
        """Return one TTS engine per available voice."""
        return [TTSEngine(id=voice, name=voice, provider=self) for voice in self._voices]

    async def get_tts_message(
        self, message: str, language: str | None = None, engine_id: str | None = None
    ) -> StreamDetails:
        """
        Render the given message as speech.

        :param message: The text to convert to speech.
        :param language: Ignored: the speech endpoint has no language parameter, the
            backend infers the language from the message itself.
        :param engine_id: The voice to render with; defaults to the first available voice.
        :return: StreamDetails for the (cached) audio clip.
        """
        voice = engine_id or self._voices[0]
        file_id = await self._render_speech(message, voice)
        return StreamDetails(
            provider=self.instance_id,
            item_id=file_id,
            audio_format=AudioFormat(content_type=ContentType.MP3),
            media_type=MediaType.SOUND_EFFECT,
            stream_type=StreamType.HTTP,
            path=f"{self.mass.streams.base_url}{self._route_path}?id={file_id}",
        )

    @property
    def _route_path(self) -> str:
        """Return the path of this instance's dynamic route on the streamserver."""
        return f"/{self.instance_id}_speech"

    @property
    def _base_url(self) -> str:
        """Return the configured API endpoint, without trailing slash."""
        return str(self.get_setup_value(CONF_BASE_URL, DEFAULT_BASE_URL)).rstrip("/")

    async def _handle_speech_request(self, request: web.Request) -> web.FileResponse | web.Response:
        """Serve a rendered speech clip by its file id."""
        if not (file_id := request.query.get("id")):
            return web.Response(status=400, text="Missing id")
        # the id from the url is only a lookup key, never a path component
        if not (file_path := self._clips.get(file_id)):
            raise web.HTTPNotFound
        if not await aiopath.isfile(file_path):
            self._clips.pop(file_id, None)
            raise web.HTTPNotFound
        return web.FileResponse(file_path)

    async def _render_speech(self, message: str, voice: str) -> str:
        """
        Render the message to a file in the cache directory and return its file id.

        :param message: The text to convert to speech.
        :param voice: The voice to render with.
        """
        model = str(self.get_setup_value(CONF_MODEL, DEFAULT_MODEL))
        # the endpoint is part of the identity: repointing an instance must not reuse clips
        digest = f"{self._base_url}\0{model}\0{voice}\0{message}"
        file_id = hashlib.sha256(digest.encode()).hexdigest()
        file_path = os.path.join(self._cache_dir, f"{file_id}.{RESPONSE_FORMAT}")
        async with self._render_lock:
            if await aiopath.isfile(file_path):
                # a reused clip is played right away, so keep it out of reach of the reaper
                with suppress(OSError):
                    await asyncio.to_thread(os.utime, file_path, None)
                self._clips[file_id] = file_path
                return file_id
            await makedirs(self._cache_dir, exist_ok=True)
            audio_data = await self._request_speech(message, voice)
            # write to a temp file first so an interrupted render leaves no partial clip
            tmp_path = f"{file_path}.tmp"
            try:
                async with aiofiles.open(tmp_path, "wb") as _file:
                    await _file.write(audio_data)
                await replace(tmp_path, file_path)
            except OSError:
                with suppress(OSError):
                    await remove(tmp_path)
                raise
            self._clips[file_id] = file_path
        await self._reap_cache()
        return file_id

    async def _request_speech(self, message: str, voice: str) -> bytes:
        """
        Request the rendered audio for the message from the speech API.

        :param message: The text to convert to speech.
        :param voice: The voice to render with.
        """
        api_key = self.get_setup_value(CONF_API_KEY)
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        payload = {
            "model": str(self.get_setup_value(CONF_MODEL, DEFAULT_MODEL)),
            "voice": voice,
            "input": message,
            "response_format": RESPONSE_FORMAT,
        }
        async with self.mass.http_session.post(
            f"{self._base_url}/audio/speech",
            headers=headers,
            json=payload,
            timeout=SPEECH_TIMEOUT,
        ) as response:
            if response.status != 200:
                detail = (await response.text())[:200]
                raise AudioError(f"Speech request failed (HTTP {response.status}): {detail}")
            return await response.read()

    async def _reap_cache(self) -> None:
        """Remove rendered clips that are older than the maximum cache age."""

        def _reap() -> list[str]:
            reaped: list[str] = []
            cutoff = time.time() - CACHE_MAX_AGE
            with os.scandir(self._cache_dir) as entries:
                for entry in entries:
                    with suppress(OSError):
                        if (
                            entry.is_file(follow_symlinks=False)
                            and entry.stat(follow_symlinks=False).st_mtime < cutoff
                        ):
                            Path(entry.path).unlink()
                            reaped.append(Path(entry.name).stem)
            return reaped

        # reaping is opportunistic housekeeping and must never break playback
        with suppress(OSError):
            for file_id in await asyncio.to_thread(_reap):
                self._clips.pop(file_id, None)

    async def _index_cache(self) -> dict[str, str]:
        """Return the clips present in the cache directory, keyed by file id."""

        def _index() -> dict[str, str]:
            # only adopt what this provider could have written itself: anything else that
            # ended up in the directory must never become reachable through the route
            with os.scandir(self._cache_dir) as entries:
                return {
                    Path(entry.name).stem: entry.path
                    for entry in entries
                    if entry.is_file(follow_symlinks=False)
                    and entry.name.endswith(f".{RESPONSE_FORMAT}")
                    and FILE_ID_PATTERN.match(Path(entry.name).stem)
                }

        try:
            return await asyncio.to_thread(_index)
        except OSError:
            # nothing rendered yet: the cache directory is created on the first render
            return {}

    async def _resolve_voices(self) -> list[str]:
        """Return the voices to expose, from the config override, the backend or the defaults."""
        if override := self.get_config_value(CONF_VOICES):
            voices = [voice.strip() for voice in str(override).split(",") if voice.strip()]
            if voices := list(dict.fromkeys(voices)):
                self.logger.debug("Using %s voice(s) from the config override", len(voices))
                return voices
        api_key = str(self.get_setup_value(CONF_API_KEY) or "")
        if voices := await fetch_backend_voices(self.mass.http_session, self._base_url, api_key):
            self.logger.debug("Discovered %s voice(s) on the backend", len(voices))
            return voices
        self.logger.debug("Falling back to the default voices")
        return list(DEFAULT_VOICES)
