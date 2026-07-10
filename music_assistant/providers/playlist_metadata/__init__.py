"""
Playlist Metadata Provider for Music Assistant.

Generates metadata for library playlists, including custom artwork using artist images
and album covers, with support for multiple configurable layout templates.

Future enhancements may include automatic genre detection and AI-generated descriptions.

Supported artwork templates:
- artist_mosaic: Dominant artist large in centre, others as smaller tiles around it
- artist_grid: Equal-sized grid of unique artist images (up to 4)
- album_grid:  Classic album-cover grid (same as the built-in collage, kept as fallback)
- artist_radio: Artist-focused layout with main artist centered on a solid background with overlapping circles of secondary artists
- artist_banner: Full-bleed artist image with playlist name text overlay (Tidal / Apple Music Essentials style)
- album_fan:     Up to three album covers as framed photo-print cards in a rotated stack (Apple Music collage style)
- album_grid_tilted: Album-cover grid with white gaps between tiles, the whole composition rotated ~15° (Apple Music tilted collage style)

The metadata controller calls this provider during its regular refresh cycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import math
import os
import random
import tempfile
from collections import Counter
from io import BytesIO
from pathlib import Path
from time import time
from typing import TYPE_CHECKING, Any, Final

from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, ImageType, MediaType, ProviderFeature
from music_assistant_models.errors import ProviderUnavailableError
from music_assistant_models.media_items import (
    Artist,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    Playlist,
    Track,
)
from music_assistant_models.unique_list import UniqueList
from PIL import Image, ImageDraw, ImageFont, ImageOps

from music_assistant.helpers.images import get_image_data
from music_assistant.helpers.uri import parse_uri
from music_assistant.mass import MusicAssistant
from music_assistant.models import ProviderInstanceType
from music_assistant.models.metadata_provider import MetadataProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

LOGGER = logging.getLogger(__name__)

SUPPORTED_FEATURES: Final[set[ProviderFeature]] = {ProviderFeature.PLAYLIST_METADATA}

CONF_TEMPLATE: Final[str] = "template"
CONF_SKIP_PROVIDER_PLAYLISTS: Final[str] = "skip_provider_playlists"
CONF_ENABLE_GENRE_DETECTION: Final[str] = "enable_genre_detection"
CONF_GENRE_MIN_THRESHOLD: Final[str] = "genre_min_threshold"
CONF_GENRE_MAX_COUNT: Final[str] = "genre_max_count"

TEMPLATE_ARTIST_MOSAIC: Final[str] = "artist_mosaic"
TEMPLATE_ARTIST_GRID: Final[str] = "artist_grid"
TEMPLATE_ALBUM_GRID: Final[str] = "album_grid"
TEMPLATE_ARTIST_RADIO: Final[str] = "artist_radio"
TEMPLATE_ARTIST_BANNER: Final[str] = "artist_banner"
TEMPLATE_ALBUM_FAN: Final[str] = "album_fan"
TEMPLATE_ALBUM_GRID_TILTED: Final[str] = "album_grid_tilted"

# Directory name inside the plugin's cache space
_IMAGES_DIR: Final[str] = "playlist_metadata_images"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize the provider instance."""
    return PlaylistMetadataProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return config entries for the provider."""
    return (
        ConfigEntry(
            key=CONF_TEMPLATE,
            type=ConfigEntryType.STRING,
            required=True,
            default_value=TEMPLATE_ALBUM_GRID,
            options=[
                ConfigValueOption(value=TEMPLATE_ALBUM_GRID),
                ConfigValueOption(value=TEMPLATE_ALBUM_FAN),
                ConfigValueOption(value=TEMPLATE_ALBUM_GRID_TILTED),
                ConfigValueOption(value=TEMPLATE_ARTIST_MOSAIC),
                ConfigValueOption(value=TEMPLATE_ARTIST_GRID),
                ConfigValueOption(value=TEMPLATE_ARTIST_RADIO),
                ConfigValueOption(value=TEMPLATE_ARTIST_BANNER),
            ],
            value=values.get(CONF_TEMPLATE, TEMPLATE_ALBUM_GRID) if values else TEMPLATE_ALBUM_GRID,
        ),
        ConfigEntry(
            key=CONF_SKIP_PROVIDER_PLAYLISTS,
            type=ConfigEntryType.BOOLEAN,
            required=False,
            default_value=True,
            value=values.get(CONF_SKIP_PROVIDER_PLAYLISTS, True) if values else True,
        ),
        ConfigEntry(
            key=CONF_ENABLE_GENRE_DETECTION,
            type=ConfigEntryType.BOOLEAN,
            required=False,
            default_value=False,
            value=values.get(CONF_ENABLE_GENRE_DETECTION, False) if values else False,
        ),
        ConfigEntry(
            key=CONF_GENRE_MIN_THRESHOLD,
            type=ConfigEntryType.INTEGER,
            required=False,
            default_value=10,
            range=(5, 50),
            advanced=True,
            value=values.get(CONF_GENRE_MIN_THRESHOLD, 10) if values else 10,
        ),
        ConfigEntry(
            key=CONF_GENRE_MAX_COUNT,
            type=ConfigEntryType.INTEGER,
            required=False,
            default_value=3,
            range=(1, 10),
            advanced=True,
            value=values.get(CONF_GENRE_MAX_COUNT, 3) if values else 3,
        ),
    )


class PlaylistMetadataProvider(MetadataProvider):
    """Metadata provider that generates artwork and metadata for library playlists."""

    @property
    def priority(self) -> int:
        """Priority for this provider (lower = more preferred)."""
        return 90  # Run after theaudiodb/fanart.tv but before builtin collage

    _images_dir: str

    async def loaded_in_mass(self) -> None:
        """Set up the provider after it has been loaded into Music Assistant."""
        self._images_dir = os.path.join(self.mass.storage_path, _IMAGES_DIR)
        await asyncio.to_thread(os.makedirs, self._images_dir, exist_ok=True)
        self.mass.tasks.register_scheduled_task(
            task_id=f"{self.instance_id}_cleanup",
            name="Playlist metadata cleanup",
            handler=self._cleanup_stale_images,
            schedule=TaskSchedule.hourly(every=2),
            initial_delay=120,
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Unload the provider."""
        self.mass.tasks.unregister_scheduled_task(
            f"{self.instance_id}_cleanup", clear_persisted_state=is_removed
        )

    async def resolve_image(self, path: str) -> str | bytes:
        """Resolve a playlist art image path to raw image bytes."""
        file_path = os.path.join(self._images_dir, path)
        # Validate the path stays inside our images directory (path traversal guard)
        real_images_dir = os.path.realpath(self._images_dir)
        real_file_path = os.path.realpath(file_path)
        if not real_file_path.startswith(real_images_dir + os.sep):
            msg = f"Invalid image path: {path}"
            raise FileNotFoundError(msg)
        if not os.path.isfile(real_file_path):
            msg = f"Playlist art image not found: {path}"
            raise FileNotFoundError(msg)
        # Return bytes directly so MA never passes a bare filename to ffmpeg
        return await asyncio.to_thread(_read_bytes, real_file_path)

    async def get_playlist_metadata(self, playlist: Playlist) -> MediaItemMetadata | None:
        """
        Generate playlist metadata (primarily artwork).

        :param playlist: The playlist to generate metadata for.
        """
        skip_provider = self.config.get_value(CONF_SKIP_PROVIDER_PLAYLISTS)
        if skip_provider:
            has_builtin = any(pm.provider_domain == "builtin" for pm in playlist.provider_mappings)
            has_smart_playlist = any(
                pm.provider_domain == "smart_playlist" for pm in playlist.provider_mappings
            )
            if not has_builtin and not has_smart_playlist:
                # Only skip if playlist has a provider-supplied image (not our generated one)
                if playlist.metadata.images:
                    for img in playlist.metadata.images:
                        if img.type == ImageType.THUMB and not self._is_our_image(img):
                            return None

        generated_images: list[MediaItemImage] = []
        detected_genres: set[str] | None = None

        if thumb_image := await self._generate_and_write(playlist, fanart=False):
            generated_images.append(thumb_image)

        if fanart_image := await self._generate_and_write(playlist, fanart=True):
            generated_images.append(fanart_image)

        # Aggregate genres from playlist tracks if enabled
        if self.config.get_value(CONF_ENABLE_GENRE_DETECTION):
            detected_genres = await self._analyze_playlist_genres(playlist)

        if generated_images or detected_genres:
            # Only clean up old images when we have new ones to replace them
            if generated_images:
                await self._cleanup_old_playlist_images(playlist)
            metadata = MediaItemMetadata()
            if generated_images:
                metadata.images = UniqueList(generated_images)
            if detected_genres:
                metadata.genres = detected_genres
            return metadata
        return None

    async def _analyze_playlist_genres(self, playlist: Playlist) -> set[str] | None:
        """
        Analyze playlist tracks and aggregate most common genres.

        :param playlist: The playlist to analyze.
        :return: Set of detected genres or None if not enough data.
        """
        genre_counter: Counter[str] = Counter()
        track_count = 0
        max_tracks = 500  # Analyze up to 500 tracks to avoid performance issues

        try:
            async for track in self.mass.music.playlists.tracks(
                playlist.item_id,
                playlist.provider,
            ):
                if not isinstance(track, Track):
                    continue

                track_count += 1
                if track.metadata and track.metadata.genres:
                    genre_counter.update(track.metadata.genres)

                if track_count >= max_tracks:
                    break

            if track_count == 0:
                return None

            # Calculate threshold and get top genres
            min_threshold_val = self.config.get_value(CONF_GENRE_MIN_THRESHOLD) or 10
            max_genre_val = self.config.get_value(CONF_GENRE_MAX_COUNT) or 3
            min_threshold_pct = (
                int(min_threshold_val) if isinstance(min_threshold_val, int | float) else 10
            )
            max_genre_count = int(max_genre_val) if isinstance(max_genre_val, int | float) else 3
            required_count = math.ceil(track_count * min_threshold_pct / 100)

            # Get most common genres that meet the threshold
            detected = {
                genre
                for genre, count in genre_counter.most_common(max_genre_count)
                if count >= required_count
            }

            return detected if detected else None

        except (KeyError, AttributeError, TypeError, ValueError) as err:
            LOGGER.debug("Failed to analyze genres for playlist %s: %s", playlist.name, err)
            return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_our_image(self, img: MediaItemImage) -> bool:
        """
        Return True if this image was generated by this plugin.

        Checks both the provider field and whether the path lives inside our images directory.
        Bare filenames without directory separators are treated as builtin assets, not
        plugin-generated images.
        """
        # Remote URLs and data URIs are never ours, regardless of provider
        if img.path.startswith(("http://", "https://", "data:")):
            return False
        if img.provider == self.instance_id:
            return True
        # A bare filename without any directory separator (e.g. "logo.png", "fanart.jpg")
        # is a builtin provider asset, not something we generated.
        if "/" not in img.path and "\\" not in img.path:
            return False
        images_dir = Path(self._images_dir).resolve()
        img_path = Path(img.path)
        resolved = (
            img_path.resolve() if img_path.is_absolute() else (images_dir / img_path).resolve()
        )
        with contextlib.suppress(ValueError):
            resolved.relative_to(images_dir)
            return True
        return False

    async def _cleanup_old_playlist_images(self, playlist: Playlist) -> None:
        """
        Remove old images generated by this provider for the given playlist.

        This ensures that when the template changes or metadata is regenerated,
        old images are immediately removed rather than waiting for the periodic cleanup.

        :param playlist: The playlist whose old images should be removed.
        """
        if not playlist.metadata.images:
            return

        images_to_remove = [img for img in playlist.metadata.images if self._is_our_image(img)]
        if not images_to_remove:
            return

        images_dir = Path(self._images_dir).resolve()
        for img in images_to_remove:
            img_path = Path(img.path)
            file_path = img_path if img_path.is_absolute() else images_dir / img_path.name
            with contextlib.suppress(OSError):
                await asyncio.to_thread(file_path.unlink, missing_ok=True)

        playlist.metadata.images = UniqueList(
            [img for img in playlist.metadata.images if img not in images_to_remove]
        )

    async def _cleanup_stale_images(self) -> None:
        """Remove stale DB references and orphaned image files from storage."""
        referenced_paths: set[str] = set()
        images_dir_resolved = Path(self._images_dir).resolve()

        async for playlist in self.mass.music.playlists.iter_library_items():
            if not playlist.metadata.images:
                continue
            stale = []
            for img in playlist.metadata.images:
                if not self._is_our_image(img):
                    continue
                exists = await asyncio.to_thread(
                    os.path.isfile, images_dir_resolved / Path(img.path).name
                )
                if not exists:
                    stale.append(img)
            if stale:
                stale_paths = {img.path for img in stale}
                fresh = await self.mass.music.playlists.get_library_item(playlist.item_id)
                if fresh.metadata.images:
                    fresh.metadata.images = UniqueList(
                        [img for img in fresh.metadata.images if img.path not in stale_paths]
                    )
                    await self.mass.music.playlists.update_item_in_library(
                        fresh.item_id, fresh, overwrite=True
                    )

            # Collect paths still in use after cleanup (use fresh object if we updated it)
            source = fresh.metadata.images if stale else playlist.metadata.images
            for img in source or []:
                if self._is_our_image(img):
                    # Normalise to basename: path may be absolute or relative depending on
                    # which version of the plugin wrote the record.
                    referenced_paths.add(Path(img.path).name)

        # Delete any files in the images directory not referenced by any playlist.
        # Keep files that were written in the last 5 minutes to cover the race between
        # file write and DB update (e.g. artwork just generated but metadata not yet refreshed).
        for fname in await asyncio.to_thread(os.listdir, self._images_dir):
            if fname in referenced_paths:
                continue
            fpath = Path(self._images_dir) / fname
            try:
                mtime = await asyncio.to_thread(fpath.stat)
            except OSError:
                continue
            if time() - mtime.st_mtime < 300:
                continue
            with contextlib.suppress(OSError):
                fpath.unlink()

    async def _get_smart_playlist_rules(self, playlist: Playlist) -> dict[str, Any] | None:
        """
        Get the Smart Playlist rules for a playlist if it's a smart playlist.

        :param playlist: The playlist to check.
        :return: The rules dictionary or None if not a smart playlist.
        """
        try:
            smart_playlist_provider = None
            for provider in self.mass.providers:
                if provider.domain == "smart_playlist":
                    smart_playlist_provider = provider
                    break

            if not smart_playlist_provider:
                return None

            smart_mapping = None
            for pm in playlist.provider_mappings:
                if pm.provider_domain == "smart_playlist":
                    smart_mapping = pm
                    break

            if not smart_mapping:
                return None

            if not hasattr(smart_playlist_provider, "get_smart_playlist_rules"):
                LOGGER.debug("Smart playlist provider not fully initialized yet")
                return None

            return await smart_playlist_provider.get_smart_playlist_rules(  # type: ignore[union-attr, no-any-return]
                smart_mapping.item_id
            )
        except Exception as err:
            LOGGER.debug("Could not get smart playlist rules for %s: %s", playlist.name, err)
            return None

    async def _collect_images_from_smart_playlist_rules(
        self, rules: dict[str, Any]
    ) -> tuple[list[MediaItemImage], list[MediaItemImage]]:
        """
        Collect images based on smart playlist rules.

        Returns (primary_images, secondary_images) where primary are artist/album images
        from the rules, and secondary are additional images that could be used.

        :param rules: The smart playlist rules dictionary.
        :return: Tuple of (primary_images, secondary_images).
        """
        artist_images: dict[str, MediaItemImage] = {}
        album_images: dict[str, MediaItemImage] = {}

        for artist_id in rules.get("artist_ids", []):
            try:
                artist = await self.mass.music.artists.get_library_item(artist_id)
                if artist and artist.image:
                    artist_images[artist.name] = artist.image
            except Exception as err:
                LOGGER.debug("Could not get artist %s: %s", artist_id, err)

        for uri in rules.get("seed_artist_uris", []):
            try:
                media_type, provider_instance_id, item_id = await parse_uri(uri)
                if media_type != MediaType.ARTIST:
                    continue
                if not self.mass.get_provider(provider_instance_id):
                    LOGGER.debug("Provider %s not available for URI %s", provider_instance_id, uri)
                    continue
                artist = await self.mass.music.artists.get(item_id, provider_instance_id)
                if artist and artist.image:
                    artist_images[artist.name] = artist.image
            except ProviderUnavailableError:
                LOGGER.debug("Provider not available for URI %s", uri)
            except Exception as err:
                LOGGER.debug("Could not get artist from URI %s: %s", uri, err)

        for album_id in rules.get("album_ids", []):
            try:
                album = await self.mass.music.albums.get_library_item(album_id)
                if album and album.image:
                    album_images[album.name] = album.image
            except Exception as err:
                LOGGER.debug("Could not get album %s: %s", album_id, err)

        for uri in rules.get("seed_album_uris", []):
            try:
                media_type, provider_instance_id, item_id = await parse_uri(uri)
                if media_type != MediaType.ALBUM:
                    continue
                if not self.mass.get_provider(provider_instance_id):
                    LOGGER.debug("Provider %s not available for URI %s", provider_instance_id, uri)
                    continue
                album = await self.mass.music.albums.get(item_id, provider_instance_id)
                if album and album.image:
                    album_images[album.name] = album.image
            except ProviderUnavailableError:
                LOGGER.debug("Provider not available for URI %s", uri)
            except Exception as err:
                LOGGER.debug("Could not get album from URI %s: %s", uri, err)

        # TODO: Could also collect images from genre_ids by getting artists from those genres
        # For now, we'll use what we have and fall back to track analysis if needed

        return list(artist_images.values()), list(album_images.values())

    async def _generate_and_write(  # noqa: PLR0915
        self,
        playlist: Playlist,
        template: str | None = None,
        fanart: bool = False,
    ) -> MediaItemImage | None:
        """
        Render playlist artwork and write to disk.

        :param playlist: The playlist to generate artwork for.
        :param template: Template override (None uses configured default).
        :param fanart: If True, generate FANART image; if False, generate THUMB image.
        """
        effective_template = template or str(self.config.get_value(CONF_TEMPLATE))

        artist_images: dict[str, MediaItemImage] = {}  # artist_name → image (deduped)
        artist_count: dict[str, int] = {}  # artist_name → track count
        album_image_map: dict[str, MediaItemImage] = {}  # path → image (deduped)
        album_count: dict[str, int] = {}  # path → track count

        smart_rules = await self._get_smart_playlist_rules(playlist)
        if smart_rules:
            LOGGER.debug(
                "Playlist %s is a smart playlist, collecting images from rules", playlist.name
            )
            (
                rules_artist_images,
                rules_album_images,
            ) = await self._collect_images_from_smart_playlist_rules(smart_rules)

            if rules_artist_images or rules_album_images:
                for img in rules_artist_images:
                    key = img.path
                    if key not in artist_images:
                        artist_images[key] = img
                        artist_count[key] = 1

                for img in rules_album_images:
                    _path = img.path
                    if _path not in album_image_map:
                        album_image_map[_path] = img
                        album_count[_path] = 1

                LOGGER.debug(
                    "Smart playlist %s: collected %d artist images, %d album images from rules",
                    playlist.name,
                    len(artist_images),
                    len(album_image_map),
                )
                if len(artist_images) < 4 and len(album_image_map) < 4:
                    LOGGER.debug("Not enough images from rules, falling back to track analysis")
                    smart_rules = None
                    artist_images.clear()
                    artist_count.clear()
                    album_image_map.clear()
                    album_count.clear()
            else:
                LOGGER.debug(
                    "No images found in smart playlist rules, falling back to track analysis"
                )
                smart_rules = None

        if not smart_rules or (len(artist_images) < 4 and len(album_image_map) < 4):
            async for track in self.mass.music.playlists.tracks(
                playlist.item_id, playlist.provider
            ):
                if not isinstance(track, Track):
                    continue

                # Collect album image, counting how often each album appears
                if track.image:
                    _path = track.image.path
                    album_image_map[_path] = track.image
                    album_count[_path] = album_count.get(_path, 0) + 1

                # Collect artist images + count occurrences
                for artist_or_mapping in track.artists:
                    artist_name = artist_or_mapping.name
                    artist_count[artist_name] = artist_count.get(artist_name, 0) + 1
                    if artist_name in artist_images:
                        continue
                    artist_img = await self._get_artist_image(artist_or_mapping)
                    if artist_img:
                        artist_images[artist_name] = artist_img

                await asyncio.sleep(0)  # yield to event loop

        # Sort album images by frequency so the most-seen album comes first.
        # This lets album-grid templates place the dominant album at a prominent position.
        sorted_album_paths = sorted(
            album_image_map, key=lambda p: album_count.get(p, 0), reverse=True
        )
        album_images = [album_image_map[p] for p in sorted_album_paths]

        if effective_template in (
            TEMPLATE_ALBUM_GRID,
            TEMPLATE_ALBUM_FAN,
            TEMPLATE_ALBUM_GRID_TILTED,
        ):
            images = album_images
            secondary_images: list[MediaItemImage] = []
        else:
            # Sort artists by frequency so the most-used artist image comes first
            sorted_artists = sorted(
                artist_images, key=lambda n: artist_count.get(n, 0), reverse=True
            )
            images = [artist_images[n] for n in sorted_artists]
            secondary_images = album_images
            if not images:
                # Fall back to album images if no artist images were found
                images = album_images
                secondary_images = []

        LOGGER.debug(
            "Playlist %s: %d artist image(s), %d album image(s), template=%s",
            playlist.name,
            len(artist_images),
            len(album_images),
            effective_template,
        )

        if not images:
            return None

        try:
            img_data = await self._render(
                effective_template, images, secondary_images, playlist_name=playlist.name
            )
        except Exception as err:
            LOGGER.warning("Failed to generate playlist artwork for %s: %s", playlist.name, err)
            return None

        # Use timestamp in filename to bust browser cache when tracks change.
        # Frontend uses absolute path with provider="builtin" so builtin's resolve_image
        # is bypassed and the file is served directly via os.path.isfile().
        image_type = ImageType.FANART if fanart else ImageType.THUMB
        suffix = "fanart" if fanart else "thumb"
        filename = f"{playlist.item_id}_{int(time())}_{suffix}.jpg"
        file_path = os.path.join(self._images_dir, filename)

        # mkstemp prevents collisions if multiple calls regenerate the same playlist concurrently
        tmp_fd, tmp_path = await asyncio.to_thread(
            tempfile.mkstemp, dir=self._images_dir, suffix=".tmp"
        )
        await asyncio.to_thread(os.close, tmp_fd)
        await asyncio.to_thread(_write_bytes, tmp_path, img_data)
        await asyncio.to_thread(os.replace, tmp_path, file_path)

        return MediaItemImage(
            type=image_type,
            path=file_path,
            provider=self.instance_id,
            remotely_accessible=False,
        )

    async def _get_artist_image(self, artist: Artist | ItemMapping) -> MediaItemImage | None:
        """Return a THUMB image for the given artist, checking the library first."""
        # Both Artist and ItemMapping have an .image attribute; check it first
        if artist.image:
            return artist.image

        # Exact library lookup by provider mapping (faster and more accurate than name search)
        try:
            library_artist = await self.mass.music.artists.get_library_item_by_prov_id(
                artist.item_id, artist.provider
            )
            if library_artist and library_artist.image:
                return library_artist.image
        except Exception as err:
            LOGGER.debug("Artist provider lookup failed for %s: %s", artist.name, err)

        # Fall back to name search in case the provider mapping differs
        try:
            results = await self.mass.music.artists.library_items(
                search=artist.name,
                limit=1,
                summary=False,
            )
            if results and results[0].image:
                return results[0].image
        except Exception as err:
            LOGGER.debug("Artist library lookup failed for %s: %s", artist.name, err)

        return None

    async def _render(
        self,
        template: str,
        images: list[MediaItemImage],
        secondary_images: list[MediaItemImage] | None = None,
        playlist_name: str = "",
    ) -> bytes:
        """Dispatch to the correct renderer."""
        if template == TEMPLATE_ARTIST_MOSAIC:
            return await _render_artist_mosaic(self.mass, images, secondary_images or [])
        if template == TEMPLATE_ARTIST_GRID:
            return await _render_artist_grid(self.mass, images, secondary_images or [])
        if template == TEMPLATE_ARTIST_RADIO:
            return await _render_artist_radio(self.mass, images)
        if template == TEMPLATE_ARTIST_BANNER:
            return await _render_artist_banner(
                self.mass, images, secondary_images or [], playlist_name
            )
        if template == TEMPLATE_ALBUM_FAN:
            return await _render_album_fan(self.mass, images)
        if template == TEMPLATE_ALBUM_GRID_TILTED:
            return await _render_album_grid_tilted(self.mass, images)
        # album_grid / fallback
        return await _render_album_grid(self.mass, images)


# ---------------------------------------------------------------------------
# Image renderers (run in thread pool via asyncio.to_thread)
# ---------------------------------------------------------------------------

_CANVAS_SIZE: Final[int] = 1500
_TILE_SIZE: Final[int] = 375  # 4x4 grid
_MOSAIC_MAIN_SIZE: Final[int] = 1000
_MOSAIC_SMALL_SIZE: Final[int] = 500


async def _render_artist_mosaic(
    mass: MusicAssistant,
    images: list[MediaItemImage],
    secondary_images: list[MediaItemImage],
) -> bytes:
    """
    Render the 'Artist Mosaic' template.

    Dominant artist fills most of the canvas; up to five secondary artists
    are arranged as smaller squares on the right/bottom edge.
    If fewer than two artist images are available, album covers fill the secondary slots.
    """
    if not images:
        msg = "No images provided"
        raise ValueError(msg)

    main_data = await _fetch(mass, images[0])

    # Use remaining artist images for secondary slots; pad with album covers if needed
    secondary_candidates = images[1:] + secondary_images
    secondary = []
    seen: set[str] = {images[0].path}
    for img in secondary_candidates:
        if img.path in seen:
            continue
        seen.add(img.path)
        data = await _fetch(mass, img)
        if data:
            secondary.append(data)
        if len(secondary) >= 5:
            break

    def _compose() -> bytes:
        canvas = Image.new("RGB", (_CANVAS_SIZE, _CANVAS_SIZE), (30, 30, 30))

        if main_data:
            main_img = Image.open(BytesIO(main_data)).convert("RGB")
            # Fill most of the canvas
            fill_w = _CANVAS_SIZE if not secondary else _MOSAIC_MAIN_SIZE
            fill_h = _CANVAS_SIZE if not secondary else _MOSAIC_MAIN_SIZE
            main_img = main_img.resize((fill_w, fill_h))
            canvas.paste(main_img, (0, 0))

        # Up to 5 secondary images in 500x500 tiles in the remaining corners
        positions = [
            (_MOSAIC_MAIN_SIZE, 0),
            (_MOSAIC_MAIN_SIZE, _MOSAIC_SMALL_SIZE),
            (0, _MOSAIC_MAIN_SIZE),
            (_MOSAIC_SMALL_SIZE, _MOSAIC_MAIN_SIZE),
            (_MOSAIC_MAIN_SIZE, _MOSAIC_MAIN_SIZE),
        ]
        for i, sec_data in enumerate(secondary[:5]):
            sec_img = Image.open(BytesIO(sec_data)).convert("RGB")
            sec_img = sec_img.resize((_MOSAIC_SMALL_SIZE, _MOSAIC_SMALL_SIZE))
            canvas.paste(sec_img, positions[i])

        buf = BytesIO()
        canvas.convert("RGB").save(buf, "JPEG", optimize=True, quality=85)
        return buf.getvalue()

    return await asyncio.to_thread(_compose)


async def _render_artist_grid(
    mass: MusicAssistant,
    images: list[MediaItemImage],
    secondary_images: list[MediaItemImage],
) -> bytes:
    """
    Render the 'Artist Grid' template.

    Up to four unique artist images in equal-sized tiles (2x2).
    If fewer than four artist images are available, album covers fill the remaining slots.
    """
    random.shuffle(images)
    # Deduplicate and merge: artist images first, then album covers to fill up to 4 slots
    seen: set[str] = set()
    candidates: list[MediaItemImage] = []
    for img in images + secondary_images:
        if img.path not in seen:
            seen.add(img.path)
            candidates.append(img)
        if len(candidates) >= 4:
            break
    slots = candidates
    fetched: list[bytes | None] = [await _fetch(mass, img) for img in slots]

    def _compose() -> bytes:
        canvas = Image.new("RGB", (_CANVAS_SIZE, _CANVAS_SIZE), (30, 30, 30))
        coords = [
            (0, 0),
            (_CANVAS_SIZE // 2, 0),
            (0, _CANVAS_SIZE // 2),
            (_CANVAS_SIZE // 2, _CANVAS_SIZE // 2),
        ]
        half = _CANVAS_SIZE // 2
        for data, (x, y) in zip(fetched, coords, strict=False):
            if not data:
                continue
            img = Image.open(BytesIO(data)).convert("RGB").resize((half, half))
            canvas.paste(img, (x, y))

        buf = BytesIO()
        canvas.convert("RGB").save(buf, "JPEG", optimize=True, quality=85)
        return buf.getvalue()

    return await asyncio.to_thread(_compose)


async def _render_album_grid(mass: MusicAssistant, images: list[MediaItemImage]) -> bytes:
    """
    Render the classic album-grid template (identical to the built-in collage).

    250x250 tiles tiled across the canvas, with repetition if needed.
    """
    tile_size = 250
    canvas_size = _CANVAS_SIZE

    random.shuffle(images)

    # Pre-fetch exactly as many unique images as tiles needed (with repetition via cycle)
    tiles_x = math.ceil(canvas_size / tile_size)
    tiles_y = math.ceil(canvas_size / tile_size)
    tile_count = tiles_x * tiles_y

    seen: set[str] = set()
    selected: list[MediaItemImage] = []
    for img in images:
        if img.path not in seen:
            seen.add(img.path)
            selected.append(img)
        if len(selected) >= tile_count:
            break

    fetched: dict[str, bytes | None] = {}
    for img in selected:
        fetched[img.path] = await _fetch(mass, img)

    available = [img for img in selected if fetched.get(img.path)]
    iter_images = itertools.cycle(available) if available else None

    def _compose() -> bytes:
        canvas = Image.new("RGB", (canvas_size, canvas_size), (30, 30, 30))
        if iter_images is None:
            buf = BytesIO()
            canvas.convert("RGB").save(buf, "JPEG", optimize=True, quality=85)
            return buf.getvalue()
        for x in range(0, canvas_size, tile_size):
            for y in range(0, canvas_size, tile_size):
                img_obj = next(iter_images)
                data = fetched[img_obj.path]
                tile = Image.open(BytesIO(data)).convert("RGB").resize((tile_size, tile_size))  # type: ignore[arg-type]
                canvas.paste(tile, (x, y))

        buf = BytesIO()
        canvas.convert("RGB").save(buf, "JPEG", optimize=True, quality=85)
        return buf.getvalue()

    return await asyncio.to_thread(_compose)


async def _render_artist_radio(
    mass: MusicAssistant,
    images: list[MediaItemImage],
) -> bytes:
    """
    Render the 'Artist Radio' template.

    Replicates the Apple Music artist radio station style: the primary artist fills
    a large circle centered on a solid background filled with the most prominent
    color of that artist image. Additional artists appear as smaller circles
    arranged around the edge.
    """
    if not images:
        msg = "No images provided"
        raise ValueError(msg)

    fetched: list[bytes | None] = [await _fetch(mass, img) for img in images[:5]]
    main_data = fetched[0]

    def _dominant_color(data: bytes) -> tuple[int, int, int]:
        """Return the background color of an image by sampling its corners and edges."""
        img = Image.open(BytesIO(data)).convert("RGB").resize((100, 100))
        # Sample the four corners + midpoints of each edge (16 samples total)
        sample_coords = [
            (0, 0),
            (1, 0),
            (0, 1),
            (1, 1),  # top-left corner
            (98, 0),
            (99, 0),
            (98, 1),
            (99, 1),  # top-right corner
            (0, 98),
            (1, 98),
            (0, 99),
            (1, 99),  # bottom-left corner
            (98, 98),
            (99, 98),
            (98, 99),
            (99, 99),  # bottom-right corner
        ]
        r_sum = g_sum = b_sum = 0
        for x, y in sample_coords:
            pixel = img.getpixel((x, y))
            r_sum += pixel[0]  # type: ignore[index]
            g_sum += pixel[1]  # type: ignore[index]
            b_sum += pixel[2]  # type: ignore[index]
        n = len(sample_coords)
        # Darken slightly so the circles pop
        factor = 0.75
        return (int(r_sum / n * factor), int(g_sum / n * factor), int(b_sum / n * factor))

    def _compose() -> bytes:
        bg_color = _dominant_color(main_data) if main_data else (15, 15, 15)
        canvas = Image.new("RGB", (_CANVAS_SIZE, _CANVAS_SIZE), bg_color)

        def _paste_circle(data: bytes, cx: int, cy: int, size: int) -> None:
            """Paste a circular-cropped image centred at (cx, cy)."""
            img = Image.open(BytesIO(data)).convert("RGBA").resize((size, size))
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
            layer = Image.new("RGBA", (_CANVAS_SIZE, _CANVAS_SIZE), (0, 0, 0, 0))
            layer.paste(img, (cx - size // 2, cy - size // 2), mask)
            canvas.paste(layer.convert("RGB"), (0, 0), layer.split()[3])

        centre = _CANVAS_SIZE // 2

        # Main artist — large circle
        if main_data:
            main_circle = int(_CANVAS_SIZE * 0.65)
            _paste_circle(main_data, centre, centre, main_circle)

        # Up to 4 secondary artists as small circles around the edge
        secondary = [d for d in fetched[1:] if d]
        if secondary:
            small_size = int(_CANVAS_SIZE * 0.22)
            radius = int(_CANVAS_SIZE * 0.42)
            for i, sec_data in enumerate(secondary[:4]):
                angle = math.pi / 4 + i * (math.pi / 2)  # 45°, 135°, 225°, 315°
                sx = centre + int(radius * math.cos(angle))
                sy = centre + int(radius * math.sin(angle))
                _paste_circle(sec_data, sx, sy, small_size)

        buf = BytesIO()
        canvas.convert("RGB").save(buf, "JPEG", optimize=True, quality=85)
        return buf.getvalue()

    return await asyncio.to_thread(_compose)


def _get_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Return a TrueType font at the given size, falling back to Pillow's built-in."""
    font_search_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial Bold.ttf",
        "/Windows/Fonts/arialbd.ttf",
    ]
    for path in font_search_paths:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


async def _render_artist_banner(
    mass: MusicAssistant,
    images: list[MediaItemImage],
    secondary_images: list[MediaItemImage],
    playlist_name: str,
) -> bytes:
    """
    Render the 'Artist Banner' template.

    The dominant artist image fills the canvas. A dark gradient is applied at the
    top so that the playlist name drawn in white remains legible — similar to the
    style used by Tidal artist mixes and Apple Music Essentials playlists.
    Falls back to album images when no artist image is available.
    """
    candidates = images or secondary_images
    if not candidates:
        msg = "No images provided"
        raise ValueError(msg)

    main_data = await _fetch(mass, candidates[0])

    def _compose() -> bytes:
        canvas = Image.new("RGB", (_CANVAS_SIZE, _CANVAS_SIZE), (30, 30, 30))

        if main_data:
            bg = Image.open(BytesIO(main_data)).convert("RGB")
            bg = ImageOps.fit(bg, (_CANVAS_SIZE, _CANVAS_SIZE))
            canvas.paste(bg, (0, 0))

        # Dark-to-transparent gradient across the top half for text legibility
        overlay = Image.new("RGBA", (_CANVAS_SIZE, _CANVAS_SIZE), (0, 0, 0, 0))
        gradient_height = _CANVAS_SIZE // 2
        draw_ov = ImageDraw.Draw(overlay)
        for y in range(gradient_height):
            alpha = int(200 * (1 - y / gradient_height) ** 0.7)
            draw_ov.line([(0, y), (_CANVAS_SIZE, y)], fill=(0, 0, 0, alpha))
        canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")

        # Draw playlist name at the top left
        draw = ImageDraw.Draw(canvas)
        margin = int(_CANVAS_SIZE * 0.06)
        font = _get_font(int(_CANVAS_SIZE * 0.075))
        draw.text((margin, margin), playlist_name, fill=(255, 255, 255), font=font)

        buf = BytesIO()
        canvas.convert("RGB").save(buf, "JPEG", optimize=True, quality=85)
        return buf.getvalue()

    return await asyncio.to_thread(_compose)


async def _render_album_fan(
    mass: MusicAssistant,
    images: list[MediaItemImage],
) -> bytes:
    """
    Render the 'Album Fan' template.

    Up to three album covers are framed as photo-print cards (white border) and
    composed in a slightly rotated, overlapping stack on a dark background —
    similar to the Apple Music playlist collage style.
    """
    if not images:
        msg = "No images provided"
        raise ValueError(msg)

    fetched: list[bytes | None] = [await _fetch(mass, img) for img in images[:3]]
    available = [d for d in fetched if d]

    def _compose() -> bytes:
        canvas = Image.new("RGB", (_CANVAS_SIZE, _CANVAS_SIZE), (18, 18, 18))

        card_content = int(_CANVAS_SIZE * 0.52)
        border = int(card_content * 0.06)
        card_total = card_content + 2 * border
        centre = _CANVAS_SIZE // 2

        # Rotation (degrees clockwise) and centre offsets (pixels) for up to 3 cards, back to front
        if len(available) == 1:
            card_configs: list[tuple[int, int, int]] = [(4, 0, 0)]
        elif len(available) == 2:
            card_configs = [
                (-10, -int(_CANVAS_SIZE * 0.12), int(_CANVAS_SIZE * 0.03)),
                (4, int(_CANVAS_SIZE * 0.08), -int(_CANVAS_SIZE * 0.03)),
            ]
        else:
            card_configs = [
                (-14, -int(_CANVAS_SIZE * 0.14), int(_CANVAS_SIZE * 0.04)),
                (-3, -int(_CANVAS_SIZE * 0.03), int(_CANVAS_SIZE * 0.01)),
                (8, int(_CANVAS_SIZE * 0.10), -int(_CANVAS_SIZE * 0.03)),
            ]

        for data, (angle, x_off, y_off) in zip(available, card_configs, strict=False):
            # Build photo-print card: white frame + album image inset
            card = Image.new("RGB", (card_total, card_total), (245, 245, 245))
            album = Image.open(BytesIO(data)).convert("RGB").resize((card_content, card_content))
            card.paste(album, (border, border))

            # Rotate with transparent padding so canvas background shows in the corners
            rotated = card.convert("RGBA").rotate(
                -angle, expand=True, resample=Image.Resampling.BICUBIC
            )
            rx, ry = rotated.size
            px = centre + x_off - rx // 2
            py = centre + y_off - ry // 2
            canvas.paste(rotated.convert("RGB"), (px, py), rotated.split()[3])

        buf = BytesIO()
        canvas.convert("RGB").save(buf, "JPEG", optimize=True, quality=85)
        return buf.getvalue()

    return await asyncio.to_thread(_compose)


async def _render_album_grid_tilted(  # noqa: PLR0915
    mass: MusicAssistant,
    images: list[MediaItemImage],
) -> bytes:
    """
    Render the 'Album Grid Tilted' template.

    Builds a regular grid of album covers separated by white gutters on a dark background,
    then rotates the entire composition ~15° so the grid bleeds off all four edges —
    the Apple Music tilted-collage look.
    """
    if not images:
        msg = "No images provided"
        raise ValueError(msg)

    # 5x5 grid: tile=360, gutter=18, grid=1872 px, which is large enough
    # (1500*(cos15+sin15) ~= 1837 px) so no black corners appear after the
    # -15 rotation and centre-crop to 1500 px.
    # images[0] is the most-frequently-occurring album and goes in the centre cell.
    tile = 360
    gutter = 18
    cols = 5
    rows = 5
    grid_w = cols * tile + (cols - 1) * gutter
    grid_h = rows * tile + (rows - 1) * gutter
    center_row = rows // 2
    center_col = cols // 2
    tile_count = cols * rows

    # images is already sorted by frequency (most common first)
    seen: set[str] = set()
    deduped: list[MediaItemImage] = []
    for img in images:
        if img.path not in seen:
            seen.add(img.path)
            deduped.append(img)

    fetched: dict[str, bytes | None] = {}
    for img in deduped[:tile_count]:
        fetched[img.path] = await _fetch(mass, img)

    available = [img for img in deduped if fetched.get(img.path)]
    center_img = available[0] if available else None
    other_imgs = available[1:] if len(available) > 1 else available
    random.shuffle(other_imgs)
    iter_others = itertools.cycle(other_imgs) if other_imgs else None

    def _compose() -> bytes:
        # Build the grid on a white surface so gutters are white
        grid = Image.new("RGB", (grid_w, grid_h), (255, 255, 255))
        for row in range(rows):
            for col in range(cols):
                if row == center_row and col == center_col:
                    img_obj = center_img
                elif iter_others is not None:
                    img_obj = next(iter_others)
                else:
                    img_obj = None
                if img_obj is None or not fetched.get(img_obj.path):
                    continue
                data = fetched[img_obj.path]
                if not data:
                    continue
                cell = Image.open(BytesIO(data)).convert("RGB").resize((tile, tile))
                x = col * (tile + gutter)
                y = row * (tile + gutter)
                grid.paste(cell, (x, y))

        # Rotate the grid ~15° with expand so no corner is clipped, then centre-crop
        rotated = grid.convert("RGBA").rotate(-15, expand=True, resample=Image.Resampling.BICUBIC)
        # Centre-crop to final canvas
        rx, ry = rotated.size
        cx = (rx - _CANVAS_SIZE) // 2
        cy = (ry - _CANVAS_SIZE) // 2
        cropped = rotated.crop((cx, cy, cx + _CANVAS_SIZE, cy + _CANVAS_SIZE))

        # Composite over dark background so any transparent corners look intentional
        canvas = Image.new("RGB", (_CANVAS_SIZE, _CANVAS_SIZE), (20, 20, 20))
        canvas.paste(cropped.convert("RGB"), (0, 0), cropped.split()[3])

        buf = BytesIO()
        canvas.save(buf, "JPEG", optimize=True, quality=85)
        return buf.getvalue()

    return await asyncio.to_thread(_compose)


async def _fetch(mass: MusicAssistant, image: MediaItemImage) -> bytes | None:
    """Fetch raw image bytes, returning None on failure."""
    try:
        return await get_image_data(mass, image.path, image.provider)
    except Exception as err:
        LOGGER.debug(
            "Failed to fetch image path=%s provider=%s: %s", image.path, image.provider, err
        )
        return None


def _write_bytes(path: str, data: bytes) -> None:
    """Write bytes to a file (used in thread pool)."""
    with open(path, "wb") as fh:
        fh.write(data)


def _read_bytes(path: str) -> bytes:
    """Read bytes from a file (used in thread pool)."""
    with open(path, "rb") as fh:
        return fh.read()
