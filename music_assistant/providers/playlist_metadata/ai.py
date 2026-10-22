"""
AI generated playlist covers and descriptions for the Playlist Metadata provider.

Covers are generated with an image generation engine exposed by an AI plugin (such as a
Home Assistant AI Task entity or an image model behind the OpenAI Compatible plugin), in one of
a few style presets or a custom prompt.

A cover is generated once per playlist and kept on disk, keyed by everything that shapes it
(the playlist name, the style, the prompt and the engines), so it is only regenerated, and only
paid for again, when one of those changes. Playlists can also get a short editorial description,
written by a text engine and kept in the cache the same way.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import os
import string
import tempfile
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

import aiohttp
from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ImageType
from music_assistant_models.errors import (
    ActionUnavailable,
    MediaNotFoundError,
    ProviderUnavailableError,
    RateLimited,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import MediaItemImage, Playlist, Track
from music_assistant_models.unique_list import UniqueList
from PIL import Image, ImageDraw, ImageFont, ImageOps

from music_assistant.helpers.json import json_dumps
from music_assistant.helpers.plugin_engines import (
    create_ai_engine_config_entries,
    create_image_engine_config_entries,
    select_ai_engine,
    select_image_engine,
)

from .ai_constants import (
    BASE_PROMPT,
    CACHE_CATEGORY_DESCRIPTIONS,
    CACHE_CATEGORY_FAILURES,
    CACHE_KEEP_SECONDS,
    CACHE_KEY_DESCRIBED_AT,
    CACHE_KEY_GENERATED_AT,
    CACHE_KEY_GENERATION,
    CANVAS_SIZE,
    CATCH_UP_DELAY_SECONDS,
    CONCEPT_FROM_ENGINE,
    CONCEPT_PROMPT,
    CONF_ACTION_PREVIEW,
    CONF_ACTION_REGENERATE,
    CONF_CUSTOM_PROMPT,
    CONF_DESCRIPTIONS,
    CONF_EXTRA_PLAYLISTS,
    CONF_IMAGE_ENGINE,
    CONF_MAX_PER_HOUR,
    CONF_PLAYLISTS,
    CONF_PREVIEW_CUSTOM_PROMPT,
    CONF_PREVIEW_DESCRIPTION,
    CONF_PREVIEW_IMAGE,
    CONF_PREVIEW_PLAYLIST,
    CONF_PREVIEW_STYLE,
    CONF_STYLE,
    CONF_TEXT_ENGINE,
    CONF_TITLE_OVERLAY,
    CONF_USE_PROMPT_ENGINE,
    CUSTOM_LAYOUT,
    DEFAULT_CONCEPT,
    DEFAULT_CUSTOM_PROMPT,
    DEFAULT_MAX_PER_HOUR,
    DESCRIPTION_PROMPT,
    ENGINE_RETRY_SECONDS,
    FAILURE_BACKOFF_SECONDS,
    FULL_BLEED,
    IMAGE_EXTENSION,
    IMAGE_PREFIX,
    IMAGE_TIMEOUT_SECONDS,
    MAX_CONCEPT_CHARS,
    MAX_CONTEXT_GENRES,
    MAX_CONTEXT_ITEMS,
    MAX_CONTEXT_TRACKS,
    MAX_DESCRIPTION_CHARS,
    MAX_OUTAGE_RETRIES,
    MAX_SOURCE_PIXELS,
    MAX_TITLE_LINES,
    MIN_TITLE_SCALE,
    NO_TEXT_RULES,
    PLAYLISTS_ALL,
    PLAYLISTS_SMART,
    PREVIEW_CONCURRENCY,
    PREVIEW_SIZE,
    PROMPT_TIMEOUT_SECONDS,
    SEED_SEPARATOR,
    STYLE_PRESETS,
    TEMPLATE_AI,
    TITLE_RULES,
    Style,
    TitleLayout,
)

if TYPE_CHECKING:
    from music_assistant.models.plugin import AIEngine, ImageEngine
    from music_assistant.providers.smart_playlist import SmartPlaylistProvider

    from . import PlaylistMetadataProvider

SMART_PLAYLIST_DOMAIN: Final = "smart_playlist"
BUILTIN_DOMAIN: Final = "builtin"
THROTTLE_WINDOW_SECONDS: Final = 3600

# what the worker generates for a playlist
KIND_COVER: Final = "cover"
KIND_DESCRIPTION: Final = "description"
# the engines that are left alone after they could not be reached, and what each one makes
ENGINE_IMAGE: Final = "image"
ENGINE_TEXT: Final = "text"
KIND_ENGINES: Final = {KIND_COVER: ENGINE_IMAGE, KIND_DESCRIPTION: ENGINE_TEXT}
# the smart playlist provider's plain summary of the rules, which a written description replaces
SMART_PLAYLIST_SUMMARY_PREFIX: Final = "[Smart Playlist] "
# the smart playlist rules that end up in the prompts, so a change to them makes a new cover
PROMPT_RULE_KEYS: Final = ("seed_names", "artist_names", "genre_names", "year_from", "year_to")
# errors of an engine that could not be reached, rather than of one that failed to generate
TRANSIENT_ERRORS: Final = (
    TimeoutError,
    ConnectionError,
    aiohttp.ClientError,
    ProviderUnavailableError,
    RateLimited,
    ResourceTemporarilyUnavailable,
)

# the settings only read by the preview button, saved as soon as they are changed
PREVIEW_KEYS: Final = frozenset(
    {
        CONF_PREVIEW_PLAYLIST,
        CONF_PREVIEW_STYLE,
        CONF_PREVIEW_CUSTOM_PROMPT,
    }
)


@dataclass(frozen=True, kw_only=True)
class CoverLook:
    """The settings that shape how a cover looks."""

    style: Style
    custom_prompt: str
    title_overlay: bool


@dataclass(kw_only=True)
class PlaylistContext:
    """What a playlist is about, as far as its cover is concerned."""

    # labels of the songs/playlists/artists that define the playlist, the title and artists split by SEED_SEPARATOR
    seeds: list[str] = field(default_factory=list)
    artists: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    era: str | None = None

    def describe(self) -> str:
        """Return the context as prompt sentences, each ending with a space."""
        text = ""
        if self.seeds:
            text += "Seed songs/playlists that define the vibe: " + "; ".join(self.seeds) + ". "
        if self.era:
            text += f"Era: {self.era}, evoke that decade's look. "
        if names := [*self.artists, *self.genres][:MAX_CONTEXT_ITEMS]:
            text += "Artists/genres: " + ", ".join(names) + ". "
        return text


class AIArtwork:
    """
    The AI generated covers and descriptions of the Playlist Metadata provider.

    A playlist's metadata refresh never waits for a generation: it applies what is ready and
    queues what is missing. A single background worker generates the queued covers and
    descriptions one playlist at a time, within the hourly limits, and then refreshes the
    playlist so they are applied to it as it is by then.
    """

    # one preview at a time, so repeated presses do not stack up paid generations
    _preview_lock: asyncio.Lock
    # stores the generation times one at a time, so an older list can not overwrite a newer one
    _store_lock: asyncio.Lock
    # one refresh pass at a time; a regenerate during one has it run again right after
    _refresh_lock: asyncio.Lock
    _refresh_again: bool
    # when each cover and description of the last hour was generated, to enforce the hourly limit
    _generated_at: deque[float]
    _described_at: deque[float]
    # bumped by the regenerate action, so every cover and description is keyed anew
    _generation: int
    # the library ids of the playlists waiting for the worker, with what each one is missing
    _pending: dict[str, set[str]]
    # wakes the worker from waiting for the hourly limit when new work is queued
    _wakeup: asyncio.Event
    _waiting: bool
    # until when an engine that could not be reached is left alone, per engine
    _paused_until: dict[str, float]
    # set when the provider unloads, so nothing queued afterwards starts a worker
    _stopped: bool
    # whether a worker runs; set before it is started, as an eagerly started task can queue
    # more work before Music Assistant has tracked it under its task id
    _working: bool

    def __init__(self, provider: PlaylistMetadataProvider) -> None:
        """
        Initialize the AI artwork of the provider.

        :param provider: The Playlist Metadata provider the covers and descriptions belong to.
        """
        self.provider = provider
        self.mass = provider.mass
        self.logger = provider.logger
        self._preview_lock = asyncio.Lock()
        self._store_lock = asyncio.Lock()
        self._preview_tasks: list[asyncio.Future[Any]] = []
        self._refresh_lock = asyncio.Lock()
        self._refresh_again = False
        self._generated_at = deque()
        self._described_at = deque()
        self._generation = 0
        self._pending = {}
        self._wakeup = asyncio.Event()
        self._waiting = False
        self._paused_until = {}
        self._stopped = False
        self._working = False

    @property
    def covers_enabled(self) -> bool:
        """Return whether covers are generated with AI."""
        return self.provider.config.get_value(self.provider.template_key) == TEMPLATE_AI

    @property
    def descriptions_enabled(self) -> bool:
        """Return whether descriptions are written with AI."""
        return bool(self.provider.config.get_value(CONF_DESCRIPTIONS))

    @property
    def waiting(self) -> bool:
        """Return whether the worker waits for the hourly limit, or for an engine, to allow more."""
        return self._waiting

    async def setup(self) -> None:
        """Restore the state kept across reloads and schedule the catch-up."""
        self._generated_at = await self._load_timestamps(CACHE_KEY_GENERATED_AT)
        self._described_at = await self._load_timestamps(CACHE_KEY_DESCRIBED_AT)
        generation = await self.mass.cache.get(
            CACHE_KEY_GENERATION, provider=self.provider.instance_id, default=0
        )
        self._generation = generation if isinstance(generation, int) else 0
        if self.covers_enabled or self.descriptions_enabled:
            # covers are otherwise only made on a playlist's (infrequent) metadata refresh, so
            # this catches up on the playlists in scope that have none yet
            self.mass.tasks.register_scheduled_task(
                task_id=self._task_id("refresh"),
                name="AI playlist artwork",
                handler=self.refresh,
                schedule=TaskSchedule.hourly(),
                initial_delay=CATCH_UP_DELAY_SECONDS,
            )
        # the schedule keeps its hourly rhythm across restarts and reloads, so changed settings
        # (which reload the provider) would otherwise wait for the next hourly run. This also
        # swaps the generated covers out once AI is turned off.
        self.mass.call_later(
            CATCH_UP_DELAY_SECONDS, self.refresh, task_id=self._task_id("catch_up")
        )

    def stop(self, is_removed: bool = False) -> None:
        """
        Stop the catch-up and the worker, and drop what is still waiting for it.

        :param is_removed: Whether the provider is being removed rather than reloaded.
        """
        self.mass.cancel_timer(self._task_id("catch_up"))
        for name in ("catch_up", "regenerate", "worker"):
            self.mass.cancel_task(self._task_id(name))
        for task in self._preview_tasks:
            task.cancel()
        self._stopped = True
        self.mass.tasks.unregister_scheduled_task(
            self._task_id("refresh"), clear_persisted_state=is_removed
        )
        self._pending.clear()

    async def config_entries(
        self, preview: tuple[ConfigEntry, ...] = ()
    ) -> tuple[ConfigEntry, ...]:
        """
        Return the config entries of the AI covers and descriptions.

        :param preview: The entries showing the preview covers and description, if any.
        """
        template_key = self.provider.template_key
        return (
            *await create_image_engine_config_entries(
                self.mass,
                CONF_IMAGE_ENGINE,
                depends_on=template_key,
                depends_on_value=TEMPLATE_AI,
                category="generic",
            ),
            ConfigEntry(
                key=CONF_STYLE,
                type=ConfigEntryType.STRING,
                required=True,
                default_value=Style.MUSIC_ASSISTANT.value,
                options=[ConfigValueOption(value=style.value) for style in Style],
                depends_on=template_key,
                depends_on_value=TEMPLATE_AI,
            ),
            ConfigEntry(
                key=CONF_CUSTOM_PROMPT,
                type=ConfigEntryType.STRING,
                required=False,
                default_value=DEFAULT_CUSTOM_PROMPT,
                depends_on=CONF_STYLE,
                depends_on_value=Style.CUSTOM.value,
            ),
            ConfigEntry(
                key=CONF_TITLE_OVERLAY,
                type=ConfigEntryType.BOOLEAN,
                required=False,
                default_value=False,
                depends_on=template_key,
                depends_on_value=TEMPLATE_AI,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_PREVIEW_PLAYLIST,
                type=ConfigEntryType.STRING,
                category="ai_preview",
                required=False,
                default_value=None,
                options=await self._preview_playlist_options(),
                immediate_apply=True,
                depends_on=template_key,
                depends_on_value=TEMPLATE_AI,
            ),
            ConfigEntry(
                key=CONF_PREVIEW_STYLE,
                type=ConfigEntryType.STRING,
                category="ai_preview",
                multi_value=True,
                required=False,
                default_value=[style.value for style in Style if style != Style.CUSTOM],
                options=[ConfigValueOption(value=style.value) for style in Style],
                immediate_apply=True,
                depends_on=template_key,
                depends_on_value=TEMPLATE_AI,
            ),
            ConfigEntry(
                key=CONF_PREVIEW_CUSTOM_PROMPT,
                type=ConfigEntryType.STRING,
                category="ai_preview",
                required=False,
                default_value=DEFAULT_CUSTOM_PROMPT,
                immediate_apply=True,
                depends_on=template_key,
                depends_on_value=TEMPLATE_AI,
            ),
            ConfigEntry(
                key=CONF_ACTION_PREVIEW,
                type=ConfigEntryType.ACTION,
                category="ai_preview",
                action=CONF_ACTION_PREVIEW,
                required=False,
                depends_on=template_key,
                depends_on_value=TEMPLATE_AI,
            ),
            *preview,
            ConfigEntry(
                key=CONF_DESCRIPTIONS,
                type=ConfigEntryType.BOOLEAN,
                category="ai",
                required=False,
                # off by default: it spends AI requests as soon as an engine is connected
                default_value=False,
            ),
            ConfigEntry(
                key=CONF_PLAYLISTS,
                type=ConfigEntryType.STRING,
                category="ai",
                required=True,
                default_value=PLAYLISTS_SMART,
                options=[
                    ConfigValueOption(value=PLAYLISTS_SMART),
                    ConfigValueOption(value=PLAYLISTS_ALL),
                ],
            ),
            ConfigEntry(
                key=CONF_EXTRA_PLAYLISTS,
                type=ConfigEntryType.STRING,
                category="ai",
                multi_value=True,
                required=False,
                default_value=[],
                options=await self._extra_playlist_options(),
                depends_on=CONF_PLAYLISTS,
                depends_on_value=PLAYLISTS_SMART,
            ),
            # writes the descriptions and, when asked, the visual concept of the covers
            *await create_ai_engine_config_entries(self.mass, CONF_TEXT_ENGINE, category="ai"),
            ConfigEntry(
                key=CONF_USE_PROMPT_ENGINE,
                type=ConfigEntryType.BOOLEAN,
                category="ai",
                required=False,
                default_value=False,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_MAX_PER_HOUR,
                type=ConfigEntryType.INTEGER,
                category="ai",
                required=False,
                default_value=DEFAULT_MAX_PER_HOUR,
                range=(1, 100),
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_ACTION_REGENERATE,
                type=ConfigEntryType.ACTION,
                category="ai",
                action=CONF_ACTION_REGENERATE,
                required=False,
                advanced=True,
            ),
        )

    async def preview(self) -> tuple[ConfigEntry, ...]:
        """Return the entries showing the picked playlist's covers and description, unstored."""
        playlist_id = self.provider.config.get_value(CONF_PREVIEW_PLAYLIST)
        if not playlist_id:
            raise ActionUnavailable("Pick a playlist to preview first")
        image_engine = await select_image_engine(self.provider, CONF_IMAGE_ENGINE)
        if image_engine is None:
            raise ActionUnavailable("Pick an image engine first")
        text_engine = (
            await select_ai_engine(self.provider, CONF_TEXT_ENGINE)
            if self.descriptions_enabled
            else None
        )
        playlist = await self.mass.music.playlists.get_library_item(str(playlist_id))
        looks = self._preview_looks()
        if self._preview_lock.locked():
            raise ActionUnavailable("A preview is already being generated, wait for it to finish")
        async with self._preview_lock:
            # previews count towards the hourly limit, like the covers they preview. The check
            # and the reservation of every slot run with nothing awaited in between, so a
            # background generation can not take a slot in between
            if (available := self._available(self._generated_at)) < len(looks):
                raise ActionUnavailable(
                    f"The hourly limit leaves {available} cover(s) for now, "
                    "pick fewer styles to preview or try again later"
                )
            await self._record(self._generated_at, CACHE_KEY_GENERATED_AT, count=len(looks))
            self.logger.info("Generating %d preview cover(s) for '%s'", len(looks), playlist.name)
            # what the playlist is about is the same for every style, so it is worked out once
            context = await self._playlist_context(playlist)
            if self._stopped:
                raise ActionUnavailable("The provider is unloading")
            concept = await self._concept(playlist, context)
            slots = asyncio.Semaphore(PREVIEW_CONCURRENCY)

            async def render(look: CoverLook) -> bytes:
                async with slots:
                    if self._stopped:
                        raise ActionUnavailable("The provider is unloading")
                    return await self._render(playlist, image_engine, look, context, concept)

            describe = asyncio.ensure_future(
                self._preview_description(playlist, text_engine, context)
            )
            renders = [asyncio.ensure_future(render(look)) for look in looks]
            # kept, so unloading cancels the description and the renders that are still
            # running or waiting
            self._preview_tasks = [describe, *renders]
            try:
                await asyncio.wait(self._preview_tasks)
            finally:
                # also stops them when the preview itself is cancelled
                for task in self._preview_tasks:
                    task.cancel()
                self._preview_tasks = []
        if self._stopped:
            raise ActionUnavailable("The provider is unloading")
        description = describe.result()
        results = [render.exception() or render.result() for render in renders]
        entries: list[ConfigEntry] = []
        for look, result in zip(looks, results, strict=True):
            if isinstance(result, BaseException):
                # the other styles are still worth showing
                self.logger.warning(
                    "Could not generate a %s preview for '%s': %s",
                    look.style.value,
                    playlist.name,
                    result,
                )
                continue
            entries.append(
                ConfigEntry(
                    key=f"{CONF_PREVIEW_IMAGE}_{look.style.value}",
                    type=ConfigEntryType.IMAGE,
                    translation_key=CONF_PREVIEW_IMAGE,
                    category="ai_preview",
                    required=False,
                    value=await asyncio.to_thread(
                        _preview_data_url, _style_name(look.style), result
                    ),
                )
            )
        if not entries:
            raise next(result for result in results if isinstance(result, BaseException))
        if description:
            entries.append(
                ConfigEntry(
                    key=CONF_PREVIEW_DESCRIPTION,
                    type=ConfigEntryType.LABEL,
                    category="ai_preview",
                    translation_params=[description],
                )
            )
        return tuple(entries)

    async def regenerate(self) -> None:
        """
        Regenerate all covers and descriptions.

        The current covers and descriptions stay in place until their replacement is ready.
        """
        # always moves forward, also for a second press within the same second, while staying
        # ahead of a counter that restarted with an emptied cache
        self._generation = max(self._generation + 1, int(time.time()))
        # one store at a time, of the counter as it is then, so the newest is stored last
        async with self._store_lock:
            await self.mass.cache.set(
                CACHE_KEY_GENERATION,
                self._generation,
                expiration=CACHE_KEEP_SECONDS,
                provider=self.provider.instance_id,
                persistent=True,
            )
        self.logger.info("Regenerating all AI covers and descriptions")
        if self._refresh_lock.locked():
            # the running pass may have checked playlists against the old keys already
            self._refresh_again = True
            return
        self.mass.create_task(self.refresh(), task_id=self._task_id("regenerate"))

    async def cover(self, playlist: Playlist) -> MediaItemImage | None:
        """
        Return the playlist's generated cover, queueing its generation when there is none yet.

        Returns None when the playlist gets no generated cover. Until a new cover is ready,
        the playlist keeps the generated cover it has, if any.

        :param playlist: The (library) playlist to return the cover for.
        """
        if (
            not self.covers_enabled
            or not self.in_scope(playlist)
            or await self._has_own_artwork(playlist)
        ):
            return None
        current = next(iter(self.own_images(playlist)), None)
        if current is not None and not await asyncio.to_thread(
            os.path.isfile, os.path.join(self._images_dir, current.path)
        ):
            current = None
        image_engine = await select_image_engine(self.provider, CONF_IMAGE_ENGINE)
        if image_engine is None:
            return current
        if (file_name := await self._file_name(playlist, image_engine)) is None:
            # what the cover should show is unknown for now, so the current one stays
            return current
        if await asyncio.to_thread(_touch, os.path.join(self._images_dir, file_name)):
            return MediaItemImage(
                type=ImageType.THUMB,
                path=file_name,
                provider=self.provider.instance_id,
                remotely_accessible=False,
            )
        if not await self._backing_off(KIND_COVER, playlist, file_name):
            self._enqueue(playlist, KIND_COVER)
        return current

    async def apply_description(self, playlist: Playlist) -> None:
        """
        Set the playlist's generated description, queueing it when there is none yet.

        It only replaces a description the playlist has no other source for: none at all, the
        one written here before, or the one the smart playlist provider generates. It is set on
        the playlist directly, as a returned description would not replace the current one.

        :param playlist: The (library) playlist to set the description of.
        """
        if not self.descriptions_enabled or not self.in_scope(playlist):
            return
        text_engine = await select_ai_engine(self.provider, CONF_TEXT_ENGINE)
        if text_engine is None or not await self._may_describe(playlist):
            return
        if (checksum := await self._description_checksum(playlist, text_engine)) is None:
            # what the description should say is unknown for now, so the current one stays
            return
        if (description := await self._stored_description(playlist, checksum)) is not None:
            playlist.metadata.description = description
            await self._remember_written(playlist, description)
            return
        if not await self._backing_off(KIND_DESCRIPTION, playlist, checksum):
            self._enqueue(playlist, KIND_DESCRIPTION)

    def in_scope(self, playlist: Playlist) -> bool:
        """Return whether the playlist gets a generated cover and description."""
        mappings = playlist.provider_mappings
        if any(m.provider_domain == BUILTIN_DOMAIN for m in mappings) and not playlist.is_editable:
            # generated by Music Assistant itself, with content that keeps changing
            return False
        return (
            _is_smart_playlist(playlist)
            or self.provider.config.get_value(CONF_PLAYLISTS) == PLAYLISTS_ALL
            or str(playlist.item_id) in self._extra_playlists()
        )

    def is_own_image(self, img: MediaItemImage) -> bool:
        """Return whether an image is a cover generated with AI."""
        return img.provider == self.provider.instance_id and Path(img.path).name.startswith(
            IMAGE_PREFIX
        )

    def own_images(self, playlist: Playlist) -> list[MediaItemImage]:
        """Return the generated covers of the playlist."""
        return [img for img in playlist.metadata.images or [] if self.is_own_image(img)]

    def remove_own_images(self, playlist: Playlist) -> None:
        """Take the generated covers off the playlist, leaving the files to the cleanup."""
        if not self.own_images(playlist):
            return
        playlist.metadata.images = UniqueList(
            [img for img in playlist.metadata.images or [] if not self.is_own_image(img)]
        )

    async def refresh(self) -> None:
        """
        Queue what the playlists lack, and refresh the ones whose generated cover must go.

        This catches up on covers and descriptions that are missing, also after a playlist
        changed or something else replaced its description, and takes generated covers off
        that are no longer wanted, such as when the playlist got artwork of its own.
        """
        if self._refresh_lock.locked():
            # the running pass covers it; only a regenerate has it run again
            return
        async with self._refresh_lock:
            self._refresh_again = True
            while self._refresh_again:
                self._refresh_again = False
                await self._refresh_playlists()

    async def _refresh_playlists(self) -> None:
        """Make one pass over the library playlists for the refresh."""
        image_engine = (
            await select_image_engine(self.provider, CONF_IMAGE_ENGINE)
            if self.covers_enabled
            else None
        )
        text_engine = (
            await select_ai_engine(self.provider, CONF_TEXT_ENGINE)
            if self.descriptions_enabled
            else None
        )
        ai_enabled = self.covers_enabled or self.descriptions_enabled
        async for playlist in self.mass.music.playlists.iter_library_items():
            own_images = self.own_images(playlist)
            if not ai_enabled and not own_images:
                continue
            in_scope = self.in_scope(playlist)
            has_own_artwork = await self._has_own_artwork(playlist)
            if own_images and (not self.covers_enabled or not in_scope or has_own_artwork):
                # the metadata refresh takes the generated cover off, for the playlist's own
                # artwork or the collage
                await self._refresh_playlist(playlist)
                continue
            if not in_scope:
                continue
            ready = False
            if image_engine is not None and not has_own_artwork:
                ready = await self._queue_missing_cover(playlist, image_engine)
            if text_engine is not None:
                ready = await self._queue_missing_description(playlist, text_engine) or ready
            if ready:
                # applying what was made before takes no generation, so it does not wait for
                # the hourly limit or the engine in the queue
                await self._refresh_playlist(playlist)

    async def _queue_missing_cover(self, playlist: Playlist, engine: ImageEngine) -> bool:
        """
        Queue the playlist's cover when it does not show the one it should.

        Returns whether that cover was made before, so a metadata refresh only has to apply it.
        """
        if (file_name := await self._file_name(playlist, engine)) is None:
            return False
        if await asyncio.to_thread(_touch, os.path.join(self._images_dir, file_name)):
            current = playlist.image
            shown = current is not None and self.is_own_image(current) and current.path == file_name
            return not shown
        if not await self._backing_off(KIND_COVER, playlist, file_name):
            self._enqueue(playlist, KIND_COVER)
        return False

    async def _queue_missing_description(self, playlist: Playlist, engine: AIEngine) -> bool:
        """
        Queue the playlist's description when it does not show the one it should.

        Returns whether that description was written before, so a metadata refresh only has to
        apply it.
        """
        if not await self._may_describe(playlist):
            return False
        if (checksum := await self._description_checksum(playlist, engine)) is None:
            return False
        if (stored := await self._stored_description(playlist, checksum)) is not None:
            return playlist.metadata.description != stored
        if not await self._backing_off(KIND_DESCRIPTION, playlist, checksum):
            self._enqueue(playlist, KIND_DESCRIPTION)
        return False

    def _enqueue(self, playlist: Playlist, kind: str) -> None:
        """Queue a cover or description of the playlist for the worker, starting it if needed."""
        if self._stopped:
            return
        kinds = self._pending.setdefault(str(playlist.item_id), set())
        if kind in kinds:
            return
        kinds.add(kind)
        # a running worker is kept and picks the playlist up; a waiting one is only woken for
        # work it can do now
        if self._blocked_for(kind) is None:
            self._wakeup.set()
        if not self._working:
            self._working = True
            self.mass.create_task(self._work(), task_id=self._task_id("worker"))

    async def _work(self) -> None:
        """Generate the queued covers and descriptions, one playlist at a time."""
        try:
            await self._work_queue()
        finally:
            self._working = False

    async def _work_queue(self) -> None:
        """Work through the queue until it is empty, waiting while everything left is blocked."""
        while self._pending and not self._stopped:
            self._wakeup.clear()
            blocked: dict[str, set[str]] = {}
            wait = 0
            for item_id in list(self._pending):
                kinds = self._pending.get(item_id, set())
                waits = [self._blocked_for(kind) for kind in kinds]
                if kinds and all(waits):
                    # nothing of it can run yet, so it is left as it is without fetching it
                    blocked[item_id] = set(kinds)
                    retry_in = min(w for w in waits if w)
                    wait = retry_in if not wait else min(wait, retry_in)
                    continue
                kinds = self._pending.pop(item_id, set())
                try:
                    waiting_kinds, retry_in = await self._process(item_id, kinds)
                except Exception as err:
                    # one playlist failing must not stop the others
                    self.logger.warning(
                        "Could not generate for playlist %s: %s", item_id, err, exc_info=err
                    )
                    continue
                if waiting_kinds:
                    blocked[item_id] = waiting_kinds
                    wait = retry_in if not wait else min(wait, retry_in)
            for item_id, kinds in blocked.items():
                self._pending.setdefault(item_id, set()).update(kinds)
            if blocked and set(self._pending) <= set(blocked):
                # everything left waits for the hourly limit to free a slot
                self.logger.info(
                    "Waiting for the hourly limit or the engine, continuing with %d playlist(s) "
                    "in %d minutes",
                    len(self._pending),
                    wait // 60 + 1,
                )
                self._waiting = True
                try:
                    with contextlib.suppress(TimeoutError):
                        async with asyncio.timeout(wait):
                            await self._wakeup.wait()
                finally:
                    self._waiting = False

    async def _process(self, item_id: str, kinds: set[str]) -> tuple[set[str], int]:
        """
        Generate what a queued playlist is missing, and refresh it to apply what was made.

        Returns what is left waiting for the hourly limit, and the seconds until a slot frees.

        :param item_id: The library id of the playlist.
        :param kinds: What to generate for it.
        """
        try:
            # fetched now, so the refresh applies the result to the playlist as it is now
            playlist = await self.mass.music.playlists.get_library_item(item_id)
        except MediaNotFoundError:
            return set(), 0
        waiting: set[str] = set()
        retry_in = 0
        made = False
        for kind, make in (
            (KIND_COVER, self._make_cover),
            (KIND_DESCRIPTION, self._make_description),
        ):
            if kind not in kinds:
                continue
            try:
                made = await make(playlist) or made
            except LimitReachedError as err:
                waiting.add(kind)
                retry_in = max(retry_in, err.retry_in)
        if made:
            await self._refresh_playlist(playlist)
        return waiting, retry_in

    async def _make_cover(self, playlist: Playlist) -> bool:
        """
        Generate and store the playlist's cover, returning whether it has one to apply.

        :raises LimitReachedError: When the hourly limit leaves no cover for now.
        """
        if (
            not self.covers_enabled
            or not self.in_scope(playlist)
            or await self._has_own_artwork(playlist)
        ):
            return False
        engine = await select_image_engine(self.provider, CONF_IMAGE_ENGINE)
        if engine is None:
            return False
        if (file_name := await self._file_name(playlist, engine)) is None:
            return False
        if await asyncio.to_thread(os.path.isfile, os.path.join(self._images_dir, file_name)):
            return True
        if await self._backing_off(KIND_COVER, playlist, file_name):
            return False
        self._check_paused(KIND_COVER)
        await self._reserve(self._generated_at, CACHE_KEY_GENERATED_AT)
        try:
            context = await self._playlist_context(playlist)
            concept = await self._concept(playlist, context)
            cover = await self._render(playlist, engine, self._look(), context, concept)
            await asyncio.to_thread(_write_file, self._images_dir, file_name, cover)
        except Exception as err:
            # an unreachable or misbehaving engine must not stop the worker
            self.logger.warning(
                "Could not generate a cover for '%s': %s", playlist.name, err, exc_info=err
            )
            await self._failed(KIND_COVER, playlist, file_name, err)
            return False
        self.logger.info("Generated a cover for '%s'", playlist.name)
        return True

    async def _make_description(self, playlist: Playlist) -> bool:
        """
        Write and store the playlist's description, returning whether it has one to apply.

        :raises LimitReachedError: When the hourly limit leaves no description for now.
        """
        if not self.descriptions_enabled or not self.in_scope(playlist):
            return False
        engine = await select_ai_engine(self.provider, CONF_TEXT_ENGINE)
        if engine is None or not await self._may_describe(playlist):
            return False
        if (checksum := await self._description_checksum(playlist, engine)) is None:
            return False
        if await self._stored_description(playlist, checksum) is not None:
            return True
        if await self._backing_off(KIND_DESCRIPTION, playlist, checksum):
            return False
        self._check_paused(KIND_DESCRIPTION)
        await self._reserve(self._described_at, CACHE_KEY_DESCRIBED_AT)
        try:
            description = await self._write_description(playlist, engine)
        except Exception as err:
            self.logger.warning(
                "Could not write a description for '%s': %s", playlist.name, err, exc_info=err
            )
            await self._failed(KIND_DESCRIPTION, playlist, checksum, err)
            return False
        if not description:
            await self._failed(KIND_DESCRIPTION, playlist, checksum, None)
            return False
        await self.mass.cache.set(
            self._description_key(playlist),
            description,
            expiration=CACHE_KEEP_SECONDS,
            provider=self.provider.instance_id,
            category=CACHE_CATEGORY_DESCRIPTIONS,
            checksum=checksum,
            persistent=True,
        )
        self.logger.info("Wrote a description for '%s': %s", playlist.name, description)
        return True

    async def _backing_off(self, kind: str, playlist: Playlist, key: str) -> bool:
        """
        Return whether a cover or description failed recently, or too often to try again.

        :param kind: Whether it is a cover or a description.
        :param playlist: The playlist it is for.
        :param key: What identifies it, so a changed one is tried afresh.
        """
        failure = await self.mass.cache.get(
            f"failed_{kind}_{playlist.item_id}",
            provider=self.provider.instance_id,
            category=CACHE_CATEGORY_FAILURES,
            checksum=key,
        )
        if not isinstance(failure, dict):
            return False
        retry_at = failure.get("retry_at")
        return not isinstance(retry_at, int | float) or time.time() < retry_at

    async def _failed(
        self, kind: str, playlist: Playlist, key: str, err: BaseException | None
    ) -> None:
        """
        Handle a failed cover or description: wait longer after each failure.

        An engine that could not be reached is counted apart, and given up on only after many
        more tries. It is also left alone for a while, so an outage does not take a slot of the
        limit per playlist.

        :param kind: Whether it is a cover or a description.
        :param playlist: The playlist it is for.
        :param key: What identifies it, so a changed one is tried afresh.
        :param err: The error, None for an engine that answered with nothing.
        """
        transient = self._pause_if_unreachable(KIND_ENGINES[kind], err)
        await self._record_failure(kind, playlist, key, transient=transient)

    def _pause_if_unreachable(self, engine: str, err: BaseException | None) -> bool:
        """
        Leave an engine alone for a while when the error shows it could not be reached.

        Returns whether it could not be reached.

        :param engine: Which engine failed, the image or the text engine.
        :param err: The error, None for an engine that answered with nothing.
        """
        if not isinstance(err, TRANSIENT_ERRORS):
            return False
        # as long as the service asked for, when it said how long
        pause = max(ENGINE_RETRY_SECONDS, getattr(err, "backoff_time", 0) or 0)
        until = time.time() + pause
        self._paused_until[engine] = max(self._paused_until.get(engine, 0), until)
        return True

    def _paused_for(self, kind: str) -> int:
        """
        Return the seconds the engines a cover or description needs are left alone, 0 if none.

        A cover also needs the text engine when that writes its visual concept.

        :param kind: Whether it is a cover or a description.
        """
        engines = [KIND_ENGINES[kind]]
        if kind == KIND_COVER and self.provider.config.get_value(CONF_USE_PROMPT_ENGINE):
            engines.append(ENGINE_TEXT)
        remaining = max(self._paused_until.get(engine, 0) for engine in engines) - time.time()
        return int(remaining) + 1 if remaining > 0 else 0

    def _blocked_for(self, kind: str) -> int | None:
        """
        Return the seconds until a cover or description may be generated, None when it may now.

        :param kind: Whether it is a cover or a description.
        """
        timestamps = self._generated_at if kind == KIND_COVER else self._described_at
        limited = self._throttle_delay(timestamps) or 0
        return max(self._paused_for(kind), limited) or None

    def _text_engine_paused(self) -> bool:
        """Return whether the text engine is left alone after it could not be reached."""
        return self._paused_until.get(ENGINE_TEXT, 0) > time.time()

    def _check_paused(self, kind: str) -> None:
        """
        Refuse to generate while an engine a kind needs is left alone after it was unreachable.

        :param kind: Whether it is a cover or a description.
        :raises LimitReachedError: While the engine is left alone.
        """
        if paused := self._paused_for(kind):
            raise LimitReachedError(paused)

    async def _record_failure(
        self, kind: str, playlist: Playlist, key: str, *, transient: bool = False
    ) -> None:
        """
        Record a failed cover or description, to wait longer after each failure.

        :param kind: Whether it is a cover or a description.
        :param playlist: The playlist it is for.
        :param key: What identifies it, so a changed one is tried afresh.
        :param transient: Whether the engine could not be reached, rather than failed to make it.
        """
        cache_key = f"failed_{kind}_{playlist.item_id}"
        previous = await self.mass.cache.get(
            cache_key,
            provider=self.provider.instance_id,
            category=CACHE_CATEGORY_FAILURES,
            checksum=key,
        )
        record = previous if isinstance(previous, dict) else {}
        failures = int(record.get("failures") or 0)
        outages = int(record.get("outages") or 0)
        # outages and failed generations are counted apart, and each gives up on its own count
        if transient:
            outages += 1
            attempts, give_up = outages, outages > MAX_OUTAGE_RETRIES
        else:
            failures += 1
            attempts, give_up = failures, failures > len(FAILURE_BACKOFF_SECONDS)
        retry_at: float | None = None
        if not give_up:
            retry_at = (
                time.time()
                + FAILURE_BACKOFF_SECONDS[min(attempts, len(FAILURE_BACKOFF_SECONDS)) - 1]
            )
        else:
            self.logger.warning(
                "Giving up on the %s of '%s' until the playlist or the settings change",
                kind,
                playlist.name,
            )
        await self.mass.cache.set(
            cache_key,
            {"failures": failures, "outages": outages, "retry_at": retry_at},
            expiration=CACHE_KEEP_SECONDS,
            provider=self.provider.instance_id,
            category=CACHE_CATEGORY_FAILURES,
            checksum=key,
            persistent=True,
        )

    async def _may_describe(self, playlist: Playlist) -> bool:
        """Return whether the playlist's description is one a generated one may replace."""
        current = playlist.metadata.description
        if not current or not current.strip():
            return True
        if _is_smart_playlist(playlist) and current.startswith(SMART_PLAYLIST_SUMMARY_PREFIX):
            # the plain summary of the rules; one the smart playlist provider wrote with AI is kept
            return True
        written = await self.mass.cache.get(
            self._written_key(playlist),
            provider=self.provider.instance_id,
            category=CACHE_CATEGORY_DESCRIPTIONS,
        )
        return isinstance(written, str) and current == written

    async def _remember_written(self, playlist: Playlist, description: str) -> None:
        """Remember the generated description put on the playlist, so it may be replaced later."""
        key = self._written_key(playlist)
        if (
            await self.mass.cache.get(
                key, provider=self.provider.instance_id, category=CACHE_CATEGORY_DESCRIPTIONS
            )
            == description
        ):
            return
        await self.mass.cache.set(
            key,
            description,
            expiration=CACHE_KEEP_SECONDS,
            provider=self.provider.instance_id,
            category=CACHE_CATEGORY_DESCRIPTIONS,
            persistent=True,
        )

    async def _reserve(self, timestamps: deque[float], key: str) -> None:
        """
        Take one slot of the hourly limit, as one step with checking that one is left.

        :param timestamps: When each generation of this kind ran, oldest first.
        :param key: The cache key the timestamps are stored under.
        :raises LimitReachedError: When no slot is left for now.
        """
        if (retry_in := self._throttle_delay(timestamps)) is not None:
            raise LimitReachedError(retry_in)
        await self._record(timestamps, key)

    async def _has_own_artwork(self, playlist: Playlist) -> bool:
        """Return whether the playlist has a cover of its own, which is never replaced."""
        for img in playlist.metadata.images or []:
            if img.type != ImageType.THUMB or self.provider._is_our_image(img):
                continue
            if not await self._is_unservable(img):
                return True
        return False

    async def _render(
        self,
        playlist: Playlist,
        engine: ImageEngine,
        look: CoverLook,
        context: PlaylistContext,
        concept: str,
    ) -> bytes:
        """Return a newly generated cover for the playlist, with the given look."""
        prompt = _build_prompt(playlist.name, look, context, concept)
        self.logger.debug("Generating a cover for '%s': %s", playlist.name, prompt)
        async with asyncio.timeout(IMAGE_TIMEOUT_SECONDS):
            image = await engine.provider.ai_generate_image(prompt, engine_id=engine.id)
        return await asyncio.to_thread(_render_cover, image, _overlay_layout(look), playlist.name)

    async def _preview_description(
        self, playlist: Playlist, engine: AIEngine | None, context: PlaylistContext
    ) -> str:
        """Return a description to preview for the playlist, empty when none could be written."""
        if (
            engine is None
            or self._text_engine_paused()
            or self._throttle_delay(self._described_at) is not None
        ):
            return ""
        await self._record(self._described_at, CACHE_KEY_DESCRIBED_AT)
        if self._stopped:
            return ""
        try:
            return await self._write_description(playlist, engine, context)
        except Exception as err:
            self._pause_if_unreachable(ENGINE_TEXT, err)
            # the covers are still worth showing
            self.logger.warning(
                "Could not write a preview description for '%s': %s",
                playlist.name,
                err,
                exc_info=err,
            )
            return ""

    async def _write_description(
        self, playlist: Playlist, engine: AIEngine, context: PlaylistContext | None = None
    ) -> str:
        """Return a newly written description for the playlist, empty when the engine gave none."""
        if context is None:
            context = await self._playlist_context(playlist)
        query = DESCRIPTION_PROMPT.format(
            name=playlist.name,
            seeds=_description_seeds(context),
        )
        async with asyncio.timeout(PROMPT_TIMEOUT_SECONDS):
            answer = await engine.provider.ai_query(query, engine_id=engine.id)
        return " ".join(answer.split()).strip().strip('"')[:MAX_DESCRIPTION_CHARS]

    async def _stored_description(self, playlist: Playlist, checksum: str) -> str | None:
        """
        Return the description written earlier for the playlist as it is now.

        :param playlist: The playlist to return the description of.
        :param checksum: What the stored description must match, see _description_checksum.
        """
        description = await self.mass.cache.get(
            self._description_key(playlist),
            provider=self.provider.instance_id,
            category=CACHE_CATEGORY_DESCRIPTIONS,
            checksum=checksum,
        )
        return description if isinstance(description, str) and description else None

    def _description_key(self, playlist: Playlist) -> str:
        """Return the cache key of the playlist's description."""
        return f"description_{playlist.item_id}"

    def _written_key(self, playlist: Playlist) -> str:
        """Return the cache key of the generated description last put on the playlist."""
        return f"written_{playlist.item_id}"

    async def _description_checksum(self, playlist: Playlist, engine: AIEngine) -> str | None:
        """
        Return what the stored description must match to still be the playlist's.

        Returns None while a smart playlist's rules can not be read.
        """
        if (rules_digest := await self._rules_digest(playlist)) is None:
            return None
        return json_dumps([playlist.name, engine.uid, self._generation, rules_digest])

    async def _rules_digest(self, playlist: Playlist) -> str | None:
        """
        Return a digest of what a smart playlist's rules put in the prompts.

        Returns an empty digest for any other playlist, and None while the rules can not be
        read, so nothing is made from a guess that would be paid for again once they can.
        """
        if not _is_smart_playlist(playlist):
            return ""
        if (rules := await self._smart_playlist_rules(playlist)) is None:
            return None
        shaping = {key: rules.get(key) for key in PROMPT_RULE_KEYS}
        return hashlib.sha1(json_dumps(shaping).encode(), usedforsecurity=False).hexdigest()

    async def _concept(self, playlist: Playlist, context: PlaylistContext) -> str:
        """Return the visual concept sentence, written by the prompt engine when one is used."""
        if not self.provider.config.get_value(CONF_USE_PROMPT_ENGINE):
            return DEFAULT_CONCEPT
        engine = await select_ai_engine(self.provider, CONF_TEXT_ENGINE)
        if engine is None or self._text_engine_paused():
            # the worker waits for a paused engine, so this serves previews and a cover that
            # was already under way when the engine was paused
            return DEFAULT_CONCEPT
        query = CONCEPT_PROMPT.format(name=playlist.name, context=context.describe())
        try:
            async with asyncio.timeout(PROMPT_TIMEOUT_SECONDS):
                concept = await engine.provider.ai_query(query, engine_id=engine.id)
        except Exception as err:
            self._pause_if_unreachable(ENGINE_TEXT, err)
            self.logger.warning(
                "The text engine could not write a visual concept for '%s': %s",
                playlist.name,
                err,
                exc_info=err,
            )
            return DEFAULT_CONCEPT
        concept = " ".join(concept.split()).strip().strip('"').rstrip(".")
        if not concept or len(concept) > MAX_CONCEPT_CHARS:
            return DEFAULT_CONCEPT
        return CONCEPT_FROM_ENGINE.format(concept=concept)

    async def _playlist_context(self, playlist: Playlist) -> PlaylistContext:
        """Return the context of a playlist: its smart playlist rules, else its tracks."""
        if (rules := await self._smart_playlist_rules(playlist)) is not None:
            return _context_from_rules(rules)
        artist_counts: Counter[str] = Counter()
        genre_counts: Counter[str] = Counter()
        first_track: dict[str, Track] = {}
        count = 0
        try:
            async for track in self.mass.music.playlists.iter_tracks(
                playlist.item_id, playlist.provider
            ):
                if not isinstance(track, Track):
                    continue
                if track.artists:
                    artist = track.artists[0].name
                    artist_counts[artist] += 1
                    first_track.setdefault(artist, track)
                if track.metadata and track.metadata.genres:
                    # a set, so sorted for the same pick among equally common genres every time
                    genre_counts.update(sorted(track.metadata.genres))
                count += 1
                if count >= MAX_CONTEXT_TRACKS:
                    break
        except Exception as err:
            self.logger.warning(
                "Could not read the tracks of '%s': %s", playlist.name, err, exc_info=err
            )
        top_artists = [artist for artist, _ in artist_counts.most_common(MAX_CONTEXT_ITEMS)]
        return PlaylistContext(
            seeds=[
                f"{first_track[artist].name}{SEED_SEPARATOR}"
                f"{', '.join(a.name for a in first_track[artist].artists)}"
                for artist in top_artists
            ],
            artists=top_artists,
            genres=[genre for genre, _ in genre_counts.most_common(MAX_CONTEXT_GENRES)],
        )

    async def _smart_playlist_rules(self, playlist: Playlist) -> dict[str, Any] | None:
        """Return the rules of a smart playlist, None for any other playlist."""
        for mapping in playlist.provider_mappings:
            if mapping.provider_domain != SMART_PLAYLIST_DOMAIN:
                continue
            provider = self.mass.get_provider(mapping.provider_instance)
            if provider is None or provider.domain != SMART_PLAYLIST_DOMAIN:
                continue
            try:
                return await cast("SmartPlaylistProvider", provider).get_smart_playlist_rules(
                    mapping.item_id
                )
            except Exception as err:
                self.logger.warning(
                    "Could not read the rules of '%s': %s", playlist.name, err, exc_info=err
                )
        return None

    async def _file_name(self, playlist: Playlist, image_engine: ImageEngine) -> str | None:
        """
        Return the cover's file name, which changes with everything that shapes the cover.

        Returns None while a smart playlist's rules can not be read, or while the text engine
        picked for the visual concept is unavailable.
        """
        if (rules_digest := await self._rules_digest(playlist)) is None:
            return None
        style = self._style()
        prompt_engine = ""
        if self.provider.config.get_value(CONF_USE_PROMPT_ENGINE):
            # the engine the concept is written with, also when it is only picked as it is used
            text_engine = await select_ai_engine(self.provider, CONF_TEXT_ENGINE)
            if text_engine is None and self.provider.config.get_value(CONF_TEXT_ENGINE):
                # the picked engine is gone for now, so the current cover stays
                return None
            prompt_engine = text_engine.uid if text_engine else ""
        key = json_dumps(
            [
                str(playlist.item_id),
                playlist.name,
                style.value,
                str(self.provider.config.get_value(CONF_CUSTOM_PROMPT) or "")
                if style == Style.CUSTOM
                else "",
                bool(self.provider.config.get_value(CONF_TITLE_OVERLAY)),
                image_engine.uid,
                prompt_engine,
                self._generation,
                rules_digest,
            ]
        )
        digest = hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()
        return f"{IMAGE_PREFIX}{playlist.item_id}_{digest[:16]}{IMAGE_EXTENSION}"

    async def _load_timestamps(self, key: str) -> deque[float]:
        """Return the stored times of the generations of one kind within the last hour."""
        stored = await self.mass.cache.get(key, provider=self.provider.instance_id, default=[])
        now = time.time()
        return deque(
            sorted(
                float(stamp)
                for stamp in (stored if isinstance(stored, list) else [])
                if isinstance(stamp, int | float) and now - stamp < THROTTLE_WINDOW_SECONDS
            )
        )

    async def _record(self, timestamps: deque[float], key: str, count: int = 1) -> None:
        """
        Record generations now, and store them so a reload or restart still counts them.

        The generations are recorded before anything is awaited, so a check of the limit
        right before this call and the recording happen as one step.

        :param timestamps: When each generation of this kind ran, oldest first.
        :param key: The cache key the timestamps are stored under.
        :param count: The number of generations to record.
        """
        now = time.time()
        timestamps.extend([now] * count)
        # one store at a time, each of the list as it is then, so the newest is stored last
        async with self._store_lock:
            await self.mass.cache.set(
                key,
                list(timestamps),
                expiration=THROTTLE_WINDOW_SECONDS,
                provider=self.provider.instance_id,
                persistent=True,
            )

    def _throttle_delay(self, timestamps: deque[float]) -> int | None:
        """
        Return the seconds until another generation may run, None when it may now.

        :param timestamps: When each generation of this kind ran, oldest first.
        """
        if self._available(timestamps):
            return None
        return int(timestamps[0] + THROTTLE_WINDOW_SECONDS - time.time()) + 5

    def _available(self, timestamps: deque[float]) -> int:
        """
        Return how many more generations of one kind the hourly limit allows now.

        :param timestamps: When each generation of this kind ran, oldest first.
        """
        now = time.time()
        while timestamps and now - timestamps[0] >= THROTTLE_WINDOW_SECONDS:
            timestamps.popleft()
        limit = self.provider.config.get_value(CONF_MAX_PER_HOUR)
        limit = limit if isinstance(limit, int) and limit > 0 else DEFAULT_MAX_PER_HOUR
        return max(0, limit - len(timestamps))

    def _style(self) -> Style:
        """Return the configured style."""
        try:
            return Style(str(self.provider.config.get_value(CONF_STYLE)))
        except ValueError:
            return Style.MUSIC_ASSISTANT

    def _look(self) -> CoverLook:
        """Return the configured look of the covers."""
        return CoverLook(
            style=self._style(),
            custom_prompt=str(self.provider.config.get_value(CONF_CUSTOM_PROMPT) or ""),
            title_overlay=bool(self.provider.config.get_value(CONF_TITLE_OVERLAY)),
        )

    def _preview_looks(self) -> list[CoverLook]:
        """Return a look per style picked to preview, every preset when none is picked."""
        value = self.provider.config.get_value(CONF_PREVIEW_STYLE)
        picked = {str(item) for item in value} if isinstance(value, list) else {str(value or "")}
        styles = [style for style in Style if style.value in picked]
        if not styles:
            styles = [style for style in Style if style != Style.CUSTOM]
        custom_prompt = str(self.provider.config.get_value(CONF_PREVIEW_CUSTOM_PROMPT) or "")
        title_overlay = self._look().title_overlay
        return [
            CoverLook(style=style, custom_prompt=custom_prompt, title_overlay=title_overlay)
            for style in styles
        ]

    def _extra_playlists(self) -> set[str]:
        """Return the library ids of the playlists opted in on top of the smart playlists."""
        value = self.provider.config.get_value(CONF_EXTRA_PLAYLISTS)
        return {str(item) for item in value} if isinstance(value, list) else set()

    async def _extra_playlist_options(self) -> list[ConfigValueOption]:
        """Return the library playlists that can be opted in, keeping the current selection."""
        selected = self._extra_playlists()
        options: list[ConfigValueOption] = []
        try:
            async for playlist in self.mass.music.playlists.iter_library_items():
                if any(
                    m.provider_domain == SMART_PLAYLIST_DOMAIN for m in playlist.provider_mappings
                ):
                    continue
                options.append(ConfigValueOption(str(playlist.item_id), title=playlist.name))
        except Exception as err:
            # the settings page still opens, with only the current selection to pick from
            self.logger.warning("Could not list the playlists: %s", err, exc_info=err)
        known = {option.value for option in options}
        options.extend(ConfigValueOption(item_id) for item_id in sorted(selected - known))
        return options

    async def _preview_playlist_options(self) -> list[ConfigValueOption]:
        """Return the library playlists that can be previewed, keeping the current selection."""
        selected = self.provider.config.get_value(CONF_PREVIEW_PLAYLIST)
        options: list[ConfigValueOption] = []
        try:
            async for playlist in self.mass.music.playlists.iter_library_items():
                options.append(ConfigValueOption(str(playlist.item_id), title=playlist.name))
        except Exception as err:
            # the settings page still opens, with only the current selection to pick from
            self.logger.warning("Could not list the playlists: %s", err, exc_info=err)
        if selected and str(selected) not in {option.value for option in options}:
            options.append(ConfigValueOption(str(selected)))
        return options

    async def _is_unservable(self, img: MediaItemImage) -> bool:
        """Return whether an image is a local file the built-in provider refuses to serve."""
        if img.remotely_accessible:
            return False
        provider = self.mass.get_provider(img.provider)
        if provider is None or provider.domain != BUILTIN_DOMAIN:
            return False
        # only built-in images are checked: it decides from the path alone, without reading
        try:
            await provider.resolve_image(img.path)
        except FileNotFoundError:
            return True
        return False

    async def _refresh_playlist(self, playlist: Playlist) -> None:
        """Refresh the playlist's metadata, by reference so the refresh reads it as it is now."""
        await self.mass.metadata.update_metadata(playlist.uri or playlist, force_refresh=True)

    def _task_id(self, name: str) -> str:
        """Return the id of one of the tasks or timers of the AI artwork."""
        return f"{self.provider.instance_id}_ai_{name}"

    @property
    def _images_dir(self) -> str:
        """Return the directory the covers are kept in, shared with the collages."""
        return self.provider._images_dir


class LimitReachedError(Exception):
    """No generation of a kind is allowed for now: the hourly limit, or a paused engine."""

    def __init__(self, retry_in: int) -> None:
        """
        Initialize the error.

        :param retry_in: The seconds until a generation may be tried again.
        """
        super().__init__(f"no generation allowed for now, retry in {retry_in} seconds")
        self.retry_in = retry_in


def _is_smart_playlist(playlist: Playlist) -> bool:
    """Return whether the playlist comes from the smart playlist provider."""
    return any(m.provider_domain == SMART_PLAYLIST_DOMAIN for m in playlist.provider_mappings)


def _touch(path: str) -> bool:
    """
    Mark a cover file as just used, returning whether it exists.

    The cleanup deletes files that no playlist refers to once they are a few minutes old, so a
    file about to be referenced again is touched first.

    :param path: The path of the cover file.
    """
    try:
        os.utime(path)
    except FileNotFoundError:
        return False
    return True


def _build_prompt(name: str, look: CoverLook, context: PlaylistContext, concept: str) -> str:
    """
    Return the image prompt for a playlist, with the given look.

    :param name: The playlist name.
    :param look: The look of the cover.
    :param context: What the playlist is about.
    :param concept: The visual concept sentence.
    """
    values = {
        "name": name,
        "title": name,
        "context": context.describe(),
        "concept": concept,
        "description": concept,
        "artists": ", ".join(context.artists[:MAX_CONTEXT_ITEMS]),
        "genres": ", ".join(context.genres),
        "era": context.era or "",
    }
    if look.style == Style.CUSTOM:
        template = look.custom_prompt or DEFAULT_CUSTOM_PROMPT
        return _format_template(template, values).strip()
    preset = STYLE_PRESETS[look.style]
    if look.title_overlay:
        text_rules = NO_TEXT_RULES.format(area=preset.title_area)
    else:
        text_rules = f"{preset.typography.format(title=name)} {TITLE_RULES}"
    values["style"] = f"{preset.art} {text_rules} {FULL_BLEED}"
    return BASE_PROMPT.format(**values)


def _context_from_rules(rules: dict[str, Any]) -> PlaylistContext:
    """Return the context of a smart playlist from its rules."""

    def _names(key: str) -> list[str]:
        value = rules.get(key)
        return [str(name) for name in value.values()] if isinstance(value, dict) else []

    seeds = _names("seed_names")[:MAX_CONTEXT_ITEMS]
    # seed labels read "<title><SEED_SEPARATOR><artist A>, <artist B>"
    seed_artists = [
        artist.strip()
        for label in seeds
        if SEED_SEPARATOR in label
        for artist in label.split(SEED_SEPARATOR, 1)[1].split(",")
    ]
    year_from, year_to = rules.get("year_from"), rules.get("year_to")
    era = f"{year_from or ''}-{year_to or ''}" if year_from or year_to else None
    return PlaylistContext(
        seeds=seeds,
        artists=list(dict.fromkeys([*_names("artist_names"), *seed_artists])),
        genres=_names("genre_names")[:MAX_CONTEXT_ITEMS],
        era=era,
    )


def _description_seeds(context: PlaylistContext) -> str:
    """Return what defines the playlist, for its description prompt."""
    seeds = "; ".join(context.seeds or context.artists[:MAX_CONTEXT_ITEMS]) or "n/a"
    if context.genres:
        seeds += f" (genres {', '.join(context.genres)})"
    if context.era:
        seeds += f" (years {context.era})"
    return seeds


def _format_template(template: str, values: dict[str, str]) -> str:
    """Fill the placeholders of a user template, leaving anything unknown as typed."""
    result = ""
    try:
        for literal, field_name, _spec, _conversion in string.Formatter().parse(template):
            result += literal
            if field_name is None:
                continue
            result += values.get(field_name, f"{{{field_name}}}")
    except ValueError:
        # unbalanced braces: the template is meant literally
        return template
    return result


def _overlay_layout(look: CoverLook) -> TitleLayout | None:
    """Return how to draw the title over the image, None when the image carries it."""
    if not look.title_overlay:
        return None
    return CUSTOM_LAYOUT if look.style == Style.CUSTOM else STYLE_PRESETS[look.style].layout


def _write_file(directory: str, file_name: str, data: bytes) -> None:
    """Write a file in full or not at all, so a failed write never leaves a partial cover."""
    fd, tmp_name = tempfile.mkstemp(dir=directory, suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
        tmp_path.replace(Path(directory, file_name))
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _render_cover(image: bytes, layout: TitleLayout | None, title: str) -> bytes:
    """Return the generated image as a square JPEG cover, with the title drawn when asked."""
    with Image.open(BytesIO(image)) as source:
        # the size is known before the pixels are decoded
        if source.width * source.height > MAX_SOURCE_PIXELS:
            msg = f"the engine returned an image of {source.width}x{source.height} pixels"
            raise ValueError(msg)
        canvas = ImageOps.fit(source.convert("RGB"), (CANVAS_SIZE, CANVAS_SIZE))
    if layout is not None:
        canvas = _draw_title(canvas, layout, title)
    buffer = BytesIO()
    canvas.save(buffer, "JPEG", optimize=True, quality=90)
    return buffer.getvalue()


def _style_name(style: Style) -> str:
    """Return the name of a style to label its preview with."""
    return style.value.replace("_", " ").title()


def _preview_data_url(name: str, cover: bytes) -> str:
    """Return the cover scaled down to a preview with its style named above it, as a data url."""
    gap = PREVIEW_SIZE // 24
    label = PREVIEW_SIZE // 8
    preview = Image.new("RGB", (PREVIEW_SIZE, label + PREVIEW_SIZE), (18, 22, 32))
    draw = ImageDraw.Draw(preview)
    draw.text(
        (gap, label // 2), name, fill=(255, 255, 255), font=_get_font(label // 2), anchor="lm"
    )
    with Image.open(BytesIO(cover)) as image:
        preview.paste(image.convert("RGB").resize((PREVIEW_SIZE, PREVIEW_SIZE)), (0, label))
    buffer = BytesIO()
    preview.save(buffer, "JPEG", optimize=True, quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def _draw_title(canvas: Image.Image, layout: TitleLayout, title: str) -> Image.Image:
    """Draw the title over the cover, with a shade behind it for legibility."""
    size = canvas.width
    margin = int(size * 0.07)
    if layout == TitleLayout.BOTTOM_LEFT_SMALL:
        font_size = int(size * 0.065)
    elif layout == TitleLayout.CENTER:
        font_size = int(size * 0.09)
    else:
        font_size = int(size * 0.12)
    font, font_size, lines = _fit_title(title, font_size, size - 2 * margin)
    line_height = int(font_size * 1.08)
    block_height = line_height * len(lines)

    shade = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw_shade = ImageDraw.Draw(shade)
    if layout == TitleLayout.CENTER:
        # an even dim keeps the centered title legible on any background
        draw_shade.rectangle((0, 0, size, size), fill=(0, 0, 0, 70))
        top = (size - block_height) // 2
    elif layout == TitleLayout.BOTTOM_LEFT_SMALL:
        _vertical_gradient(draw_shade, size, from_top=False, height=size // 2)
        top = size - margin - block_height
    else:
        _vertical_gradient(draw_shade, size, from_top=True, height=size // 2)
        top = margin
    canvas = Image.alpha_composite(canvas.convert("RGBA"), shade)

    draw = ImageDraw.Draw(canvas)
    # thickens the strokes into a heavy weight, also when only a regular font is installed
    weight = 0 if layout == TitleLayout.BOTTOM_LEFT_SMALL else max(1, font_size // 40)
    for index, line in enumerate(lines):
        y = top + index * line_height
        if layout == TitleLayout.CENTER:
            x = (size - int(draw.textlength(line, font=font))) // 2
        else:
            x = margin
        draw.text(
            (x + 3, y + 3),
            line,
            fill=(0, 0, 0, 120),
            font=font,
            stroke_width=weight,
            stroke_fill=(0, 0, 0, 120),
        )
        draw.text(
            (x, y),
            line,
            fill=(255, 255, 255, 255),
            font=font,
            stroke_width=weight,
            stroke_fill=(255, 255, 255, 255),
        )
    return canvas.convert("RGB")


def _fit_title(
    title: str, font_size: int, max_width: int
) -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, int, list[str]]:
    """
    Return the font, its size and the lines that fit the whole title within the width.

    The font shrinks until the title takes at most a few lines; nothing of it is ever dropped.

    :param title: The title to fit.
    :param font_size: The size to start from.
    :param max_width: The width each line has to fit in, in pixels.
    """
    measure = ImageDraw.Draw(Image.new("L", (1, 1)))
    min_size = max(1, int(font_size * MIN_TITLE_SCALE))
    while True:
        font = _get_font(font_size)
        lines = _wrap_title(title, font, max_width, measure)
        if len(lines) <= MAX_TITLE_LINES or font_size <= min_size:
            return font, font_size, lines
        font_size = max(min_size, int(font_size * 0.9))


def _wrap_title(
    title: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
    measure: ImageDraw.ImageDraw,
) -> list[str]:
    """Return the title wrapped into lines that fit the width, breaking only too long words."""
    lines: list[str] = []
    current = ""
    for word in title.split():
        candidate = f"{current} {word}" if current else word
        if measure.textlength(candidate, font=font) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
        # a word wider than the line on its own is split over as many lines as it needs
        current = ""
        for char in word:
            if current and measure.textlength(current + char, font=font) > max_width:
                lines.append(current)
                current = ""
            current += char
    if current:
        lines.append(current)
    return lines or [title]


def _vertical_gradient(
    draw: ImageDraw.ImageDraw, size: int, *, from_top: bool, height: int
) -> None:
    """Draw a dark gradient fading out from the top or bottom edge."""
    for offset in range(height):
        alpha = int(170 * (1 - offset / height) ** 1.5)
        y = offset if from_top else size - 1 - offset
        draw.line([(0, y), (size, y)], fill=(0, 0, 0, alpha))


def _get_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Return a bold TrueType font at the given size, falling back to Pillow's built-in."""
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial Bold.ttf",
        "/Windows/Fonts/arialbd.ttf",
    ):
        if Path(path).exists():
            with contextlib.suppress(OSError):
                return ImageFont.truetype(path, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()
