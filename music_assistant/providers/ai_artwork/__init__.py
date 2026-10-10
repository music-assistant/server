"""
AI Artwork Provider for Music Assistant.

Generates playlist covers with an image generation engine exposed by an AI plugin (such as a
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
import textwrap
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ImageType, ProviderFeature
from music_assistant_models.errors import ActionUnavailable
from music_assistant_models.media_items import MediaItemImage, Playlist, Track
from music_assistant_models.unique_list import UniqueList
from PIL import Image, ImageDraw, ImageFont, ImageOps

from music_assistant.constants import DB_TABLE_ARTISTS, DB_TABLE_TRACK_ARTISTS, DB_TABLE_TRACKS
from music_assistant.helpers.json import json_dumps
from music_assistant.helpers.plugin_engines import (
    create_ai_engine_config_entries,
    create_image_engine_config_entries,
    select_ai_engine,
    select_image_engine,
)
from music_assistant.models.metadata_provider import MetadataProvider

from .constants import (
    BASE_PROMPT,
    CACHE_CATEGORY_DESCRIPTIONS,
    CACHE_KEEP_SECONDS,
    CACHE_KEY_GENERATION,
    CANVAS_SIZE,
    CONCEPT_FROM_ENGINE,
    CONCEPT_PROMPT,
    CONF_ACTION_PREVIEW,
    CONF_ACTION_REGENERATE,
    CONF_CUSTOM_PROMPT,
    CONF_DESCRIPTIONS,
    CONF_EXTRA_PLAYLISTS,
    CONF_IMAGE_ENGINE,
    CONF_KEEP_EXISTING_ARTWORK,
    CONF_MAX_PER_HOUR,
    CONF_PLAYLISTS,
    CONF_PREVIEW_IMAGE,
    CONF_PREVIEW_PLAYLIST,
    CONF_STYLE,
    CONF_TEXT_ENGINE,
    CONF_TITLE_OVERLAY,
    CONF_USE_PROMPT_ENGINE,
    CUSTOM_LAYOUT,
    DEFAULT_CONCEPT,
    DEFAULT_CUSTOM_PROMPT,
    DEFAULT_MAX_PER_HOUR,
    DESCRIPTION_PROMPT,
    FULL_BLEED,
    IMAGE_EXTENSION,
    IMAGE_TIMEOUT_SECONDS,
    IMAGES_DIR,
    MAX_CONCEPT_CHARS,
    MAX_CONTEXT_GENRES,
    MAX_CONTEXT_ITEMS,
    MAX_CONTEXT_TRACKS,
    MAX_DESCRIPTION_CHARS,
    MAX_FAVORITES,
    NO_TEXT_RULES,
    ORPHAN_GRACE_SECONDS,
    PLAYLISTS_ALL,
    PLAYLISTS_SMART,
    PREVIEW_SIZE,
    PROMPT_TIMEOUT_SECONDS,
    REPLACEABLE_IMAGE_DOMAINS,
    SEED_SEPARATOR,
    STYLE_PRESETS,
    TITLE_RULES,
    Style,
    TitleLayout,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant.models.plugin import AIEngine, ImageEngine
    from music_assistant.providers.smart_playlist import SmartPlaylistProvider

SUPPORTED_FEATURES: Final[set[ProviderFeature]] = {ProviderFeature.PLAYLIST_METADATA}

SMART_PLAYLIST_DOMAIN: Final = "smart_playlist"
THROTTLE_WINDOW_SECONDS: Final = 3600


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return AIArtworkProvider(mass, manifest, config, SUPPORTED_FEATURES)


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


class AIArtworkProvider(MetadataProvider):
    """Metadata provider that generates playlist covers with an AI image engine."""

    _images_dir: str
    _lock: asyncio.Lock
    _description_lock: asyncio.Lock
    # when each cover and description of the last hour was generated, to enforce the hourly limit
    _generated_at: deque[float]
    _described_at: deque[float]
    # bumped by the regenerate action, so every cover and description is keyed anew
    _generation: int

    @property
    def priority(self) -> int:
        """Priority for this provider (lower = more preferred)."""
        # ahead of the playlist_metadata collage, which stays behind the cover as a fallback
        return 80

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the (options) config entries to configure this provider instance."""
        return await self._config_entries()

    async def handle_config_action(self, action: str) -> tuple[ConfigEntry, ...] | None:
        """
        Regenerate all covers and descriptions, or preview the cover of one playlist.

        The current covers and descriptions stay in place until their replacement is ready.
        A preview is only shown on the settings page and never replaces a cover.

        :param action: The action id of the pressed button.
        """
        if action == CONF_ACTION_PREVIEW:
            return await self._config_entries(preview=await self._preview())
        if action != CONF_ACTION_REGENERATE:
            raise ActionUnavailable(f"Unknown action: {action}")
        self._generation = int(time.time())
        await self.mass.cache.set(
            CACHE_KEY_GENERATION,
            self._generation,
            expiration=CACHE_KEEP_SECONDS,
            provider=self.instance_id,
            persistent=True,
        )
        self.logger.info("Regenerating all covers and descriptions")
        self.mass.create_task(
            self._refresh_covers(),
            task_id=f"{self.instance_id}_regenerate",
            abort_existing=True,
        )
        return None

    async def update_config(self, config: ProviderConfig, changed_keys: set[str]) -> None:
        """
        Handle a config update, reloading unless only the playlist to preview changed.

        :param config: The updated provider config.
        :param changed_keys: The keys of the config that changed.
        """
        if changed_keys == {f"values/{CONF_PREVIEW_PLAYLIST}"}:
            # saved on its own as soon as it is picked, and only read by the preview button
            self.config = config
            return
        await super().update_config(config, changed_keys)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._images_dir = os.path.join(self.mass.storage_path, IMAGES_DIR)
        await asyncio.to_thread(os.makedirs, self._images_dir, exist_ok=True)
        self._lock = asyncio.Lock()
        self._description_lock = asyncio.Lock()
        self._generated_at = deque()
        self._described_at = deque()
        generation = await self.mass.cache.get(
            CACHE_KEY_GENERATION, provider=self.instance_id, default=0
        )
        self._generation = generation if isinstance(generation, int) else 0

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        # covers are otherwise only made on a playlist's (infrequent) metadata refresh, so
        # this catches up on the playlists in scope that have none yet
        self.mass.tasks.register_scheduled_task(
            task_id=f"{self.instance_id}_refresh",
            name="AI playlist artwork",
            handler=self._refresh_covers,
            schedule=TaskSchedule.hourly(),
            initial_delay=120,
        )

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        :param is_removed: Whether the provider is being removed rather than reloaded.
        """
        self.mass.tasks.unregister_scheduled_task(
            f"{self.instance_id}_refresh", clear_persisted_state=is_removed
        )

    async def resolve_image(self, path: str) -> bytes:
        """
        Return the bytes of a generated cover.

        :param path: The file name of the cover, as stored on the image.
        """
        real_images_dir = os.path.realpath(self._images_dir)
        real_file_path = os.path.realpath(os.path.join(self._images_dir, path))
        # the path comes from the library, so it must never reach outside our directory
        if not real_file_path.startswith(real_images_dir + os.sep):
            msg = f"Invalid image path: {path}"
            raise FileNotFoundError(msg)
        if not await asyncio.to_thread(os.path.isfile, real_file_path):
            msg = f"AI artwork not found: {path}"
            raise FileNotFoundError(msg)
        return await asyncio.to_thread(Path(real_file_path).read_bytes)

    async def get_playlist_metadata(self, playlist: Playlist) -> None:
        """
        Give the playlist its generated cover and description, generating them when needed.

        Both are set on the playlist directly: returning them would merge the cover in behind
        the images the playlist already has (while the first one is the one shown), and leave
        a description the playlist already has in place.

        :param playlist: The (library) playlist to generate the cover and description for.
        """
        if not self._in_scope(playlist):
            self._remove_own_images(playlist)
            return
        await self._apply_cover(playlist)
        await self._apply_description(playlist)

    async def _config_entries(self, preview: str | None = None) -> tuple[ConfigEntry, ...]:
        """
        Return the options config entries, with a preview cover below the preview button.

        :param preview: The preview cover to show, as a data url.
        """
        return (
            *await create_image_engine_config_entries(
                self.mass, CONF_IMAGE_ENGINE, category="generic"
            ),
            ConfigEntry(
                key=CONF_STYLE,
                type=ConfigEntryType.STRING,
                required=True,
                default_value=Style.MUSIC_ASSISTANT.value,
                options=[ConfigValueOption(value=style.value) for style in Style],
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
            ),
            ConfigEntry(
                key=CONF_PREVIEW_PLAYLIST,
                type=ConfigEntryType.STRING,
                required=False,
                default_value=None,
                options=await self._preview_playlist_options(),
                immediate_apply=True,
            ),
            ConfigEntry(
                key=CONF_ACTION_PREVIEW,
                type=ConfigEntryType.ACTION,
                action=CONF_ACTION_PREVIEW,
                required=False,
            ),
            *(
                (
                    ConfigEntry(
                        key=CONF_PREVIEW_IMAGE,
                        type=ConfigEntryType.IMAGE,
                        required=False,
                        value=preview,
                    ),
                )
                if preview
                else ()
            ),
            ConfigEntry(
                key=CONF_PLAYLISTS,
                type=ConfigEntryType.STRING,
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
                multi_value=True,
                required=False,
                default_value=[],
                options=await self._extra_playlist_options(),
                depends_on=CONF_PLAYLISTS,
                depends_on_value=PLAYLISTS_SMART,
            ),
            ConfigEntry(
                key=CONF_KEEP_EXISTING_ARTWORK,
                type=ConfigEntryType.BOOLEAN,
                required=False,
                default_value=True,
            ),
            ConfigEntry(
                key=CONF_DESCRIPTIONS,
                type=ConfigEntryType.BOOLEAN,
                required=False,
                default_value=True,
            ),
            # writes the descriptions and, when asked, the visual concept of the covers
            *await create_ai_engine_config_entries(self.mass, CONF_TEXT_ENGINE, category="generic"),
            ConfigEntry(
                key=CONF_USE_PROMPT_ENGINE,
                type=ConfigEntryType.BOOLEAN,
                required=False,
                default_value=False,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_MAX_PER_HOUR,
                type=ConfigEntryType.INTEGER,
                required=False,
                default_value=DEFAULT_MAX_PER_HOUR,
                range=(1, 100),
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_ACTION_REGENERATE,
                type=ConfigEntryType.ACTION,
                action=CONF_ACTION_REGENERATE,
                required=False,
                advanced=True,
            ),
        )

    async def _apply_cover(self, playlist: Playlist) -> None:
        """Put the playlist's generated cover in front of its images."""
        image_engine = await select_image_engine(self, CONF_IMAGE_ENGINE)
        if image_engine is None or not self._wants_cover(playlist):
            self._remove_own_images(playlist)
            return
        file_name = await self._file_name(playlist, image_engine)
        if not await asyncio.to_thread(os.path.isfile, os.path.join(self._images_dir, file_name)):
            if not await self._generate(playlist, image_engine, file_name):
                return
        cover = MediaItemImage(
            type=ImageType.THUMB,
            path=file_name,
            provider=self.instance_id,
            remotely_accessible=False,
        )
        previous = [img.path for img in self._own_images(playlist) if img.path != file_name]
        self._remove_own_images(playlist)
        playlist.metadata.images = UniqueList([cover, *(playlist.metadata.images or [])])
        for path in previous:
            await self._delete_file(path)

    async def _apply_description(self, playlist: Playlist) -> None:
        """Set the playlist's generated description, generating it when it has none yet."""
        if not self.config.get_value(CONF_DESCRIPTIONS):
            return
        text_engine = await select_ai_engine(self, CONF_TEXT_ENGINE)
        if text_engine is None:
            return
        description = await self._stored_description(playlist, text_engine)
        if description is None:
            description = await self._generate_description(playlist, text_engine)
        if description is not None:
            playlist.metadata.description = description

    def _in_scope(self, playlist: Playlist) -> bool:
        """Return whether the playlist gets a generated cover and description."""
        mappings = playlist.provider_mappings
        if any(m.provider_domain == "builtin" for m in mappings) and not playlist.is_editable:
            # generated by Music Assistant itself, with content that keeps changing
            return False
        return (
            any(m.provider_domain == SMART_PLAYLIST_DOMAIN for m in mappings)
            or self.config.get_value(CONF_PLAYLISTS) == PLAYLISTS_ALL
            or str(playlist.item_id) in self._extra_playlists()
        )

    def _wants_cover(self, playlist: Playlist) -> bool:
        """Return whether the (in scope) playlist has no artwork worth keeping."""
        if self.config.get_value(CONF_KEEP_EXISTING_ARTWORK):
            return not any(
                img.type == ImageType.THUMB and not self._is_replaceable(img)
                for img in playlist.metadata.images or []
            )
        return True

    async def _generate(self, playlist: Playlist, engine: ImageEngine, file_name: str) -> bool:
        """Generate and store the playlist's cover, returning whether that succeeded."""
        async with self._lock:
            # another refresh of this playlist may have generated it while we waited
            if await asyncio.to_thread(os.path.isfile, os.path.join(self._images_dir, file_name)):
                return True
            if (retry_in := self._throttle_delay(self._generated_at)) is not None:
                self.logger.info(
                    "Reached the limit of covers per hour, generating '%s' in %d minutes",
                    playlist.name,
                    retry_in // 60 + 1,
                )
                self.mass.call_later(
                    retry_in,
                    self.mass.metadata.update_metadata,
                    playlist.uri,
                    force_refresh=True,
                    task_id=f"{self.instance_id}_retry_{playlist.item_id}",
                )
                return False
            self._generated_at.append(time.time())
            try:
                cover = await self._render(playlist, engine)
                await asyncio.to_thread(
                    Path(self._images_dir, file_name).write_bytes,
                    cover,
                )
            except Exception as err:
                # an unreachable or misbehaving engine must not break the metadata refresh,
                # the next refresh simply tries again
                self.logger.warning(
                    "Could not generate a cover for '%s': %s",
                    playlist.name,
                    err,
                    exc_info=err if self.logger.isEnabledFor(10) else None,
                )
                return False
            self.logger.info("Generated a cover for '%s'", playlist.name)
            return True

    async def _render(self, playlist: Playlist, engine: ImageEngine) -> bytes:
        """Return a newly generated cover for the playlist, in the configured style."""
        prompt = await self._build_prompt(playlist)
        self.logger.debug("Generating a cover for '%s': %s", playlist.name, prompt)
        async with asyncio.timeout(IMAGE_TIMEOUT_SECONDS):
            image = await engine.provider.ai_generate_image(prompt, engine_id=engine.id)
        return await asyncio.to_thread(_render_cover, image, self._overlay_layout(), playlist.name)

    async def _preview(self) -> str:
        """Return a cover for the playlist picked to preview, as a data url, without storing it."""
        playlist_id = self.config.get_value(CONF_PREVIEW_PLAYLIST)
        if not playlist_id:
            raise ActionUnavailable("Pick a playlist to preview first")
        image_engine = await select_image_engine(self, CONF_IMAGE_ENGINE)
        if image_engine is None:
            raise ActionUnavailable("Pick an image engine first")
        playlist = await self.mass.music.playlists.get_library_item(str(playlist_id))
        self.logger.info("Generating a preview cover for '%s'", playlist.name)
        cover = await self._render(playlist, image_engine)
        return await asyncio.to_thread(_preview_data_url, cover)

    async def _generate_description(self, playlist: Playlist, engine: AIEngine) -> str | None:
        """Write and store the playlist's description, returning None when that failed."""
        async with self._description_lock:
            # another refresh of this playlist may have written it while we waited
            if (stored := await self._stored_description(playlist, engine)) is not None:
                return stored
            if self._throttle_delay(self._described_at) is not None:
                # the hourly refresh catches up on it
                self.logger.debug(
                    "Reached the limit of descriptions per hour, skipping '%s'", playlist.name
                )
                return None
            self._described_at.append(time.time())
            try:
                context = await self._playlist_context(playlist)
                query = DESCRIPTION_PROMPT.format(
                    name=playlist.name,
                    seeds=_description_seeds(context),
                    favorites=await self._favorites(context.artists),
                )
                async with asyncio.timeout(PROMPT_TIMEOUT_SECONDS):
                    answer = await engine.provider.ai_query(query, engine_id=engine.id)
            except Exception as err:
                # the next refresh simply tries again
                self.logger.warning(
                    "Could not write a description for '%s': %s",
                    playlist.name,
                    err,
                    exc_info=err if self.logger.isEnabledFor(10) else None,
                )
                return None
            description = " ".join(answer.split()).strip().strip('"')[:MAX_DESCRIPTION_CHARS]
            if not description:
                return None
            await self.mass.cache.set(
                self._description_key(playlist),
                description,
                expiration=CACHE_KEEP_SECONDS,
                provider=self.instance_id,
                category=CACHE_CATEGORY_DESCRIPTIONS,
                checksum=self._description_checksum(playlist, engine),
                persistent=True,
            )
            self.logger.info("Wrote a description for '%s': %s", playlist.name, description)
            return description

    async def _stored_description(self, playlist: Playlist, engine: AIEngine) -> str | None:
        """Return the description written earlier for the playlist as it is now."""
        description = await self.mass.cache.get(
            self._description_key(playlist),
            provider=self.instance_id,
            category=CACHE_CATEGORY_DESCRIPTIONS,
            checksum=self._description_checksum(playlist, engine),
        )
        return description if isinstance(description, str) and description else None

    def _description_key(self, playlist: Playlist) -> str:
        """Return the cache key of the playlist's description."""
        return f"description_{playlist.item_id}"

    def _description_checksum(self, playlist: Playlist, engine: AIEngine) -> str:
        """Return what the stored description must match to still be the playlist's."""
        return json_dumps([playlist.name, engine.uid, self._generation])

    async def _favorites(self, artists: list[str]) -> str:
        """Return the listener's most-played library tracks by the given artists."""
        if not artists:
            return "n/a"
        query = (
            f"SELECT {DB_TABLE_TRACKS}.name AS track, {DB_TABLE_ARTISTS}.name AS artist, "
            f"{DB_TABLE_TRACKS}.play_count AS plays FROM {DB_TABLE_TRACKS} "
            f"JOIN {DB_TABLE_TRACK_ARTISTS} "
            f"ON {DB_TABLE_TRACK_ARTISTS}.track_id = {DB_TABLE_TRACKS}.item_id "
            f"JOIN {DB_TABLE_ARTISTS} "
            f"ON {DB_TABLE_ARTISTS}.item_id = {DB_TABLE_TRACK_ARTISTS}.artist_id "
            f"WHERE {DB_TABLE_ARTISTS}.name IN :artists AND {DB_TABLE_TRACKS}.play_count > 0 "
            f"GROUP BY {DB_TABLE_TRACKS}.item_id ORDER BY plays DESC"
        )
        try:
            rows = await self.mass.music.database.get_rows_from_query(
                query, {"artists": artists[:MAX_CONTEXT_ITEMS]}, limit=MAX_FAVORITES
            )
        except Exception as err:
            self.logger.debug("Could not read the play counts: %s", err)
            return "n/a"
        return (
            "; ".join(f"{row['artist']} - {row['track']} ({row['plays']} plays)" for row in rows)
            or "n/a"
        )

    async def _build_prompt(self, playlist: Playlist) -> str:
        """Return the image prompt for the playlist."""
        context = await self._playlist_context(playlist)
        concept = await self._concept(playlist, context)
        style = self._style()
        values = {
            "name": playlist.name,
            "title": playlist.name,
            "context": context.describe(),
            "concept": concept,
            "description": concept,
            "artists": ", ".join(context.artists[:MAX_CONTEXT_ITEMS]),
            "genres": ", ".join(context.genres),
            "era": context.era or "",
        }
        if style == Style.CUSTOM:
            template = str(self.config.get_value(CONF_CUSTOM_PROMPT) or DEFAULT_CUSTOM_PROMPT)
            return _format_template(template, values).strip()
        preset = STYLE_PRESETS[style]
        if self.config.get_value(CONF_TITLE_OVERLAY):
            text_rules = NO_TEXT_RULES.format(area=preset.title_area)
        else:
            text_rules = f"{preset.typography.format(title=playlist.name)} {TITLE_RULES}"
        values["style"] = f"{preset.art} {text_rules} {FULL_BLEED}"
        return BASE_PROMPT.format(**values)

    async def _concept(self, playlist: Playlist, context: PlaylistContext) -> str:
        """Return the visual concept sentence, written by the prompt engine when one is used."""
        if not self.config.get_value(CONF_USE_PROMPT_ENGINE):
            return DEFAULT_CONCEPT
        engine = await select_ai_engine(self, CONF_TEXT_ENGINE)
        if engine is None:
            return DEFAULT_CONCEPT
        query = CONCEPT_PROMPT.format(name=playlist.name, context=context.describe())
        try:
            async with asyncio.timeout(PROMPT_TIMEOUT_SECONDS):
                concept = await engine.provider.ai_query(query, engine_id=engine.id)
        except Exception as err:
            self.logger.debug("Prompt engine failed for '%s': %s", playlist.name, err)
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
            async for track in self.mass.music.playlists.tracks(
                playlist.item_id, playlist.provider
            ):
                if not isinstance(track, Track):
                    continue
                if track.artists:
                    artist = track.artists[0].name
                    artist_counts[artist] += 1
                    first_track.setdefault(artist, track)
                if track.metadata and track.metadata.genres:
                    genre_counts.update(track.metadata.genres)
                count += 1
                if count >= MAX_CONTEXT_TRACKS:
                    break
        except Exception as err:
            self.logger.debug("Could not read the tracks of '%s': %s", playlist.name, err)
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
                self.logger.debug("Could not read the rules of '%s': %s", playlist.name, err)
        return None

    async def _file_name(self, playlist: Playlist, image_engine: ImageEngine) -> str:
        """Return the cover's file name, which changes with everything that shapes the cover."""
        style = self._style()
        prompt_engine = (
            str(self.config.get_value(CONF_TEXT_ENGINE) or "")
            if self.config.get_value(CONF_USE_PROMPT_ENGINE)
            else ""
        )
        key = json_dumps(
            [
                str(playlist.item_id),
                playlist.name,
                style.value,
                str(self.config.get_value(CONF_CUSTOM_PROMPT) or "")
                if style == Style.CUSTOM
                else "",
                bool(self.config.get_value(CONF_TITLE_OVERLAY)),
                image_engine.uid,
                prompt_engine,
                self._generation,
            ]
        )
        digest = hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()
        return f"{playlist.item_id}_{digest[:16]}{IMAGE_EXTENSION}"

    def _throttle_delay(self, timestamps: deque[float]) -> int | None:
        """
        Return the seconds until another generation may run, None when it may now.

        :param timestamps: When each generation of this kind ran, oldest first.
        """
        now = time.time()
        while timestamps and now - timestamps[0] >= THROTTLE_WINDOW_SECONDS:
            timestamps.popleft()
        limit = self.config.get_value(CONF_MAX_PER_HOUR)
        limit = limit if isinstance(limit, int) and limit > 0 else DEFAULT_MAX_PER_HOUR
        if len(timestamps) < limit:
            return None
        return int(timestamps[0] + THROTTLE_WINDOW_SECONDS - now) + 5

    def _style(self) -> Style:
        """Return the configured style."""
        try:
            return Style(str(self.config.get_value(CONF_STYLE)))
        except ValueError:
            return Style.MUSIC_ASSISTANT

    def _overlay_layout(self) -> TitleLayout | None:
        """Return how to draw the title over the image, None when the image carries it."""
        if not self.config.get_value(CONF_TITLE_OVERLAY):
            return None
        style = self._style()
        return CUSTOM_LAYOUT if style == Style.CUSTOM else STYLE_PRESETS[style].layout

    def _extra_playlists(self) -> set[str]:
        """Return the library ids of the playlists opted in on top of the smart playlists."""
        value = self.config.get_value(CONF_EXTRA_PLAYLISTS)
        return {str(item) for item in value} if isinstance(value, list) else set()

    async def _extra_playlist_options(self) -> list[ConfigValueOption]:
        """Return the library playlists that can be opted in, keeping the current selection."""
        selected = self._extra_playlists()
        options: list[ConfigValueOption] = []
        with contextlib.suppress(Exception):
            async for playlist in self.mass.music.playlists.iter_library_items():
                if any(
                    m.provider_domain == SMART_PLAYLIST_DOMAIN for m in playlist.provider_mappings
                ):
                    continue
                options.append(ConfigValueOption(str(playlist.item_id), title=playlist.name))
        known = {option.value for option in options}
        options.extend(ConfigValueOption(item_id) for item_id in sorted(selected - known))
        return options

    async def _preview_playlist_options(self) -> list[ConfigValueOption]:
        """Return the library playlists that can be previewed, keeping the current selection."""
        selected = self.config.get_value(CONF_PREVIEW_PLAYLIST)
        options: list[ConfigValueOption] = []
        with contextlib.suppress(Exception):
            async for playlist in self.mass.music.playlists.iter_library_items():
                options.append(ConfigValueOption(str(playlist.item_id), title=playlist.name))
        if selected and str(selected) not in {option.value for option in options}:
            options.append(ConfigValueOption(str(selected)))
        return options

    def _is_replaceable(self, img: MediaItemImage) -> bool:
        """Return whether an image is generated artwork a new cover may go in front of."""
        if img.provider == self.instance_id:
            return True
        provider = self.mass.get_provider(img.provider)
        return provider is not None and provider.domain in REPLACEABLE_IMAGE_DOMAINS

    def _own_images(self, playlist: Playlist) -> list[MediaItemImage]:
        """Return the covers this provider gave the playlist."""
        return [img for img in playlist.metadata.images or [] if img.provider == self.instance_id]

    def _remove_own_images(self, playlist: Playlist) -> None:
        """Take this provider's covers off the playlist, leaving the files to the cleanup."""
        if not self._own_images(playlist):
            return
        playlist.metadata.images = UniqueList(
            [img for img in playlist.metadata.images or [] if img.provider != self.instance_id]
        )

    async def _delete_file(self, file_name: str) -> None:
        """Delete a cover file, ignoring one that is already gone."""
        with contextlib.suppress(OSError):
            await asyncio.to_thread(Path(self._images_dir, Path(file_name).name).unlink, True)

    async def _refresh_covers(self) -> None:
        """
        Refresh the playlists in scope that lack their cover or description.

        This also puts back a description something else replaced, such as the smart playlist
        provider when its rules are edited. Afterwards, orphaned cover files are removed.
        """
        image_engine = await select_image_engine(self, CONF_IMAGE_ENGINE)
        text_engine = (
            await select_ai_engine(self, CONF_TEXT_ENGINE)
            if self.config.get_value(CONF_DESCRIPTIONS)
            else None
        )
        referenced: set[str] = set()
        async for playlist in self.mass.music.playlists.iter_library_items():
            referenced.update(Path(img.path).name for img in self._own_images(playlist))
            if not self._in_scope(playlist):
                continue
            file_name: str | None = None
            if image_engine is not None and self._wants_cover(playlist):
                file_name = await self._file_name(playlist, image_engine)
                current = playlist.image
                if current and current.provider == self.instance_id and current.path == file_name:
                    file_name = None
            needs_description = text_engine is not None and (
                playlist.metadata.description is None
                or playlist.metadata.description
                != await self._stored_description(playlist, text_engine)
            )
            if file_name is None and not needs_description:
                continue
            # the metadata refresh generates them and stores the result in the library
            await self.mass.metadata.update_metadata(playlist, force_refresh=True)
            if file_name is not None:
                referenced.add(file_name)
        await self._remove_orphaned_files(referenced)

    async def _remove_orphaned_files(self, referenced: set[str]) -> None:
        """Delete cover files no playlist refers to any more."""
        now = time.time()
        for file_name in await asyncio.to_thread(os.listdir, self._images_dir):
            if file_name in referenced:
                continue
            file_path = Path(self._images_dir, file_name)
            try:
                modified = (await asyncio.to_thread(file_path.stat)).st_mtime
            except OSError:
                continue
            # a cover written moments ago may not be stored in the library yet
            if now - modified < ORPHAN_GRACE_SECONDS:
                continue
            await self._delete_file(file_name)


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


def _render_cover(image: bytes, layout: TitleLayout | None, title: str) -> bytes:
    """Return the generated image as a square JPEG cover, with the title drawn when asked."""
    with Image.open(BytesIO(image)) as source:
        canvas = ImageOps.fit(source.convert("RGB"), (CANVAS_SIZE, CANVAS_SIZE))
    if layout is not None:
        canvas = _draw_title(canvas, layout, title)
    buffer = BytesIO()
    canvas.save(buffer, "JPEG", optimize=True, quality=90)
    return buffer.getvalue()


def _preview_data_url(cover: bytes) -> str:
    """Return the cover scaled down to a preview, as a JPEG data url."""
    with Image.open(BytesIO(cover)) as image:
        preview = image.resize((PREVIEW_SIZE, PREVIEW_SIZE))
    buffer = BytesIO()
    preview.save(buffer, "JPEG", optimize=True, quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def _draw_title(canvas: Image.Image, layout: TitleLayout, title: str) -> Image.Image:
    """Draw the title over the cover, with a shade behind it for legibility."""
    size = canvas.width
    margin = int(size * 0.07)
    if layout == TitleLayout.BOTTOM_LEFT_SMALL:
        font_size, max_chars = int(size * 0.065), 26
    elif layout == TitleLayout.CENTER:
        font_size, max_chars = int(size * 0.09), 16
    else:
        font_size, max_chars = int(size * 0.12), 12
    font = _get_font(font_size)
    lines = textwrap.wrap(title, width=max_chars, break_long_words=True)[:3] or [title]
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
