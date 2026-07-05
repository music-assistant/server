"""
Radio stream artwork lookup for the Metadata Controller.

Provides the RadioArtworkMixin, mixed into the MetaDataController, which resolves
artist/track artwork for radio streams by matching the station's now-playing
metadata against the local library and MusicBrainz/online metadata providers.
"""

from __future__ import annotations

from time import time
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import ExternalID, ImageType, ProviderFeature
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    MusicAssistantError,
    ProviderUnavailableError,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    Track,
)
from music_assistant_models.streamdetails import StreamMetadata
from music_assistant_models.unique_list import UniqueList

from music_assistant.helpers.compare import compare_strings, create_safe_string
from music_assistant.helpers.tags import split_artists
from music_assistant.helpers.util import parse_title_and_version

from .constants import (
    AD_DETECTION_PHRASES,
    CACHE_CATEGORY_RADIO_ARTWORK,
    CACHE_EXPIRATION_RADIO_ARTWORK,
    CACHE_EXPIRATION_RADIO_ARTWORK_MISS,
    CONF_ENABLE_RADIO_METADATA_LOOKUP,
)

if TYPE_CHECKING:
    import logging

    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant import MusicAssistant
    from music_assistant.models.metadata_provider import MetadataProvider
    from music_assistant.providers.musicbrainz import MusicbrainzProvider
    from music_assistant.providers.musicbrainz.models import MusicBrainzReleaseGroup


class RadioArtworkMixin:
    """
    Radio stream artwork functionality for the MetaDataController.

    Expects to be mixed with a class providing ``mass``, ``logger``, ``domain``,
    the ``providers`` property and the ``get_image_url`` method.
    """

    if TYPE_CHECKING:
        mass: MusicAssistant
        logger: logging.Logger
        domain: str

        @property
        def providers(self) -> list[MetadataProvider]: ...  # noqa: D102

        def get_image_url(  # noqa: D102
            self,
            image: MediaItemImage,
            size: int = 0,
            prefer_proxy: bool = False,
            image_format: str | None = None,
            prefer_stream_server: bool = False,
        ) -> str: ...

    async def get_track_metadata_by_name(
        self,
        artist_name: str,
        track_name: str,
        album_name: str | None = None,
    ) -> tuple[MediaItemMetadata | None, str | None, str | None, str | None]:
        """
        Search for track/artist metadata by name.

        Checks library first for immediate results, then falls back to
        MusicBrainz for external metadata lookups.

        :param artist_name: Artist name to search for.
        :param track_name: Track title to search for.
        :param album_name: Album announced by the stream, used to refine which artwork is chosen.
        :returns: Tuple of (metadata, source_description, corrected_artist, corrected_track).
        """
        # Clean track name by stripping version suffixes and featuring credits
        clean_track_name, _ = parse_title_and_version(track_name, strip_for_search=True)

        # Check library track first - fast, no API calls, respects user-curated images
        if metadata := await self._get_library_track_metadata(artist_name, clean_track_name):
            return metadata, "library track", artist_name, clean_track_name

        # Use MusicBrainz to get IDs for accurate external metadata lookups
        musicbrainz_provider = self.mass.get_provider("musicbrainz")
        if not musicbrainz_provider:
            # No MusicBrainz, try library artist as fallback
            if metadata := await self._get_library_artist_metadata(artist_name):
                return metadata, f"library artist '{artist_name}'", artist_name, clean_track_name
            return None, None, None, None
        musicbrainz: MusicbrainzProvider = cast("MusicbrainzProvider", musicbrainz_provider)

        mb_result, swapped = await self._search_musicbrainz_with_variants(
            musicbrainz, artist_name, clean_track_name
        )

        if not mb_result:
            self.logger.debug("No MusicBrainz match for '%s - %s'", artist_name, clean_track_name)
            # No MB match, try library artist as fallback
            if metadata := await self._get_library_artist_metadata(artist_name):
                return metadata, f"library artist '{artist_name}'", artist_name, clean_track_name
            return None, None, None, None

        mb_artist, mb_release_groups = mb_result
        if swapped:
            # Swap the variables so subsequent lookups use the correct order
            artist_name, clean_track_name = clean_track_name, artist_name
            self.logger.debug(
                "MusicBrainz matched with swapped artist/track: '%s - %s'",
                artist_name,
                clean_track_name,
            )

        # Prefer single artwork (exact track art), then fall back to album artwork
        singles = [rg for rg in mb_release_groups if rg.primary_type == "Single"]
        albums = [rg for rg in mb_release_groups if rg.primary_type == "Album"]

        # When the station told us the album, move a matching release group to the front so
        # the cover reflects the broadcast release rather than an arbitrary one. This only
        # reorders within each type; singles still take precedence over albums.
        if album_name:
            singles = self._prioritize_release_groups(singles, album_name)
            albums = self._prioritize_release_groups(albums, album_name)

        for mb_release_group in singles:
            if result := await self._get_release_group_artwork(mb_release_group):
                thumb, provider_name = result
                return (
                    thumb,
                    f"single '{mb_release_group.title}' via {provider_name}",
                    artist_name,
                    clean_track_name,
                )

        if singles:
            self.logger.debug(
                "No artwork found for single release of '%s - %s', trying album artwork",
                artist_name,
                clean_track_name,
            )

        for mb_release_group in albums:
            if result := await self._get_release_group_artwork(mb_release_group):
                thumb, provider_name = result
                return (
                    thumb,
                    f"album '{mb_release_group.title}' via {provider_name}",
                    artist_name,
                    clean_track_name,
                )

        # Log when falling back to artist artwork
        self.logger.debug(
            "No album artwork for '%s - %s', trying artist artwork",
            artist_name,
            clean_track_name,
        )

        # Check library for artist before external lookup
        if metadata := await self._get_library_artist_metadata(mb_artist.name):
            return metadata, f"library artist '{mb_artist.name}'", artist_name, clean_track_name

        # Fall back to external artist artwork
        temp_artist = Artist(
            item_id="temp",
            provider="temp",
            name=mb_artist.name,
            provider_mappings=set(),
        )
        temp_artist.mbid = mb_artist.id
        for provider in self.providers:
            if ProviderFeature.ARTIST_METADATA not in provider.supported_features:
                continue
            try:
                if artist_metadata := await provider.get_artist_metadata(temp_artist):
                    if artist_thumb := self._get_thumb_image(artist_metadata):
                        return (
                            artist_thumb,
                            f"artist '{mb_artist.name}' via {provider.name}",
                            artist_name,
                            clean_track_name,
                        )
            except (
                ProviderUnavailableError,
                ResourceTemporarilyUnavailable,
                InvalidDataError,
            ):
                pass

        return None, None, None, None

    def get_radio_stream_station_image(self, streamdetails: StreamDetails) -> str | None:
        """
        Get station image URL from queue current item.

        :param streamdetails: StreamDetails for the radio stream.
        """
        if streamdetails.queue_id and (
            queue := self.mass.player_queues.get(streamdetails.queue_id)
        ):
            if queue.current_item and queue.current_item.media_item:
                if station_image := queue.current_item.media_item.image:
                    return station_image.path
        return None

    @staticmethod
    def normalize_radio_artist_name(artist_name: str) -> str:
        """
        Normalize artist name from radio stream metadata.

        Handles common formats like "Squier, Billy" -> "Billy Squier" while
        avoiding mangling of names like "Lipps, Inc." or "Portugal. The Man".

        :param artist_name: Raw artist name to normalize.
        """
        # Business/title suffixes that should not be flipped
        no_flip_suffixes = ("inc", "inc.", "ltd", "ltd.", "llc", "corp")
        # Specific known bands that are 2 words total and split by a comma
        valid_artist_names = {
            "hello, goodbye",
            "wait, what",
            "goodnight, sunrise",
            "slaughter beach, dog",
            "mount, eerie",
            "american, native",
        }

        normalized = artist_name.replace("_", " ")

        if "," not in normalized:
            return normalized

        # Check against known artist exceptions first
        if normalized.lower() in valid_artist_names:
            return normalized

        # Don't flip if contains "and" or "&" (e.g., "Crosby, Stills & Nash")
        if " and " in normalized.lower() or " & " in normalized:
            return normalized

        parts = normalized.split(",", 1)
        if len(parts) != 2:
            return normalized

        before_comma = parts[0].strip()
        after_comma = parts[1].strip()
        after_comma_lower = after_comma.lower()

        # Don't flip if suffix is a business/title term
        if after_comma_lower in no_flip_suffixes:
            return normalized

        # Flip if suffix is exactly "The" (e.g., "Beatles, The" -> "The Beatles")
        if after_comma_lower == "the":
            return f"{after_comma} {before_comma}"

        # Don't flip if 2+ words after comma (e.g., "Portugal, The Man")
        if len(after_comma.split()) >= 2:
            return normalized

        # Standard flip (e.g., "Squier, Billy" -> "Billy Squier")
        return f"{after_comma} {before_comma}"

    async def get_image_url_by_name(
        self,
        artist_name: str,
        track_name: str,
        fallback_image_url: str | None = None,
        album_name: str | None = None,
    ) -> tuple[str | None, str | None, str | None]:
        """
        Look up artwork by artist and track name.

        Searches library and external providers for matching artwork.
        Also returns corrected artist/track names if the search detects
        swapped metadata (e.g., "Track - Artist" instead of "Artist - Track").

        :param artist_name: Artist name to search for.
        :param track_name: Track title to search for.
        :param fallback_image_url: Fallback image URL if no artwork found.
        :param album_name: Album announced by the stream, used to refine which artwork is chosen.
        :returns: Tuple of (image_url, corrected_artist, corrected_track).
        """
        if " / " in artist_name:
            artist_name = artist_name.split(" / ", 1)[0].strip()
        else:
            artists_tuple = split_artists(artist_name)
            artist_name = artists_tuple[0] if artists_tuple else artist_name

        if any(phrase in artist_name.lower() for phrase in AD_DETECTION_PHRASES):
            return fallback_image_url, None, None

        # album_name influences which release group's artwork is chosen, so it must be
        # part of the cache key, else two albums for the same track would alias.
        album_key = create_safe_string(album_name) if album_name else ""
        cache_key = f"{artist_name.lower()}|{track_name.lower()}|{album_key}"
        cached_result = await self.mass.cache.get(
            key=cache_key,
            category=CACHE_CATEGORY_RADIO_ARTWORK,
        )
        if cached_result is not None:
            if cached_result != "":
                self.logger.debug(
                    "Radio artwork for '%s - %s': cached",
                    artist_name,
                    track_name,
                )
                return str(cached_result), None, None
            self.logger.debug(
                "Radio artwork for '%s - %s': cached miss",
                artist_name,
                track_name,
            )
            return fallback_image_url, None, None

        image_url = None
        corrected_artist = None
        corrected_track = None
        try:
            (
                metadata,
                source,
                corrected_artist,
                corrected_track,
            ) = await self.get_track_metadata_by_name(
                artist_name=artist_name,
                track_name=track_name,
                album_name=album_name,
            )
            # Use corrected artist/track for logging if available (handles swapped metadata)
            log_artist = corrected_artist or artist_name
            log_track = corrected_track or track_name
            if metadata and metadata.images:
                image_url = metadata.images[0].path
                self.logger.debug(
                    "Radio artwork found for '%s - %s': %s",
                    log_artist,
                    log_track,
                    source,
                )
                if "imageproxy" not in image_url:
                    await self.mass.cache.set(
                        key=cache_key,
                        data=image_url,
                        expiration=CACHE_EXPIRATION_RADIO_ARTWORK,
                        category=CACHE_CATEGORY_RADIO_ARTWORK,
                    )
            else:
                self.logger.debug(
                    "Radio artwork for '%s - %s': not found",
                    log_artist,
                    log_track,
                )
                await self.mass.cache.set(
                    key=cache_key,
                    data="",
                    expiration=CACHE_EXPIRATION_RADIO_ARTWORK_MISS,
                    category=CACHE_CATEGORY_RADIO_ARTWORK,
                )
        except ProviderUnavailableError, ResourceTemporarilyUnavailable, InvalidDataError:
            pass

        return image_url or fallback_image_url, corrected_artist, corrected_track

    async def update_radio_stream_artwork(self, streamdetails: StreamDetails) -> None:
        """
        Fetch and update radio stream artwork.

        :param streamdetails: StreamDetails to update with artwork.
        """
        if not self.mass.config.get_raw_core_config_value(
            self.domain, CONF_ENABLE_RADIO_METADATA_LOOKUP, True
        ):
            return
        if not streamdetails.stream_metadata:
            return
        if not streamdetails.stream_metadata.artist or not streamdetails.stream_metadata.title:
            return

        try:
            fallback_url = streamdetails.stream_metadata.image_url
            original_artist = streamdetails.stream_metadata.artist
            original_title = streamdetails.stream_metadata.title
            album = streamdetails.stream_metadata.album
            image_url, corrected_artist, corrected_track = await self.get_image_url_by_name(
                artist_name=original_artist,
                track_name=original_title,
                fallback_image_url=fallback_url,
                album_name=album,
            )
            # Use corrected artist/track if metadata was swapped
            final_artist = corrected_artist or original_artist
            final_title = corrected_track or original_title
            if (
                image_url != fallback_url
                or final_artist != original_artist
                or final_title != original_title
            ):
                streamdetails.stream_metadata = StreamMetadata(
                    title=final_title,
                    artist=final_artist,
                    album=album,
                    image_url=image_url,
                )
                streamdetails.stream_metadata_last_updated = time()
                if streamdetails.queue_id:
                    self.mass.player_queues.signal_update(streamdetails.queue_id)
        except MusicAssistantError:
            pass

    @staticmethod
    def _prioritize_release_groups(
        release_groups: list[MusicBrainzReleaseGroup], album_name: str
    ) -> list[MusicBrainzReleaseGroup]:
        """
        Return the release groups reordered with album-name matches first.

        :param release_groups: Release groups to reorder.
        :param album_name: Album name announced in the stream metadata.
        """
        announced = create_safe_string(album_name)
        if not announced or len(release_groups) < 2:
            return release_groups

        # loose substring match either way, so an original album still wins when the
        # station announces a compilation whose title embeds the original album name
        def matches(release_group: MusicBrainzReleaseGroup) -> bool:
            title = create_safe_string(release_group.title)
            return bool(title) and (title in announced or announced in title)

        return sorted(release_groups, key=lambda rg: not matches(rg))

    async def _get_release_group_artwork(
        self, mb_release_group: MusicBrainzReleaseGroup
    ) -> tuple[MediaItemMetadata, str] | None:
        """
        Try to get thumb artwork for a release group from metadata providers.

        :param mb_release_group: MusicBrainz release group to look up.
        :returns: Tuple of (metadata, provider_name) or None if not found.
        """
        self.logger.debug(
            "Looking up artwork for release group '%s' (mbid: %s)",
            mb_release_group.title,
            mb_release_group.id,
        )
        # Create a minimal Album object to pass the MusicBrainz release group ID
        # to metadata providers for artwork lookup.
        temp_album = Album(
            item_id="temp",
            provider="temp",
            name=mb_release_group.title,
            provider_mappings=set(),
        )
        temp_album.add_external_id(ExternalID.MB_RELEASEGROUP, mb_release_group.id)
        if mb_release_group.barcode:
            temp_album.add_external_id(ExternalID.BARCODE, mb_release_group.barcode)
        for provider in self.providers:
            if ProviderFeature.ALBUM_METADATA not in provider.supported_features:
                continue
            try:
                if metadata := await provider.get_album_metadata(temp_album):
                    if thumb := self._get_thumb_image(metadata):
                        return thumb, provider.name
            except (
                ProviderUnavailableError,
                ResourceTemporarilyUnavailable,
                InvalidDataError,
            ):
                pass
        return None

    async def _search_musicbrainz_with_variants(
        self,
        musicbrainz: MusicbrainzProvider,
        artist_name: str,
        track_name: str,
    ) -> tuple[Any, bool]:
        """
        Search MusicBrainz with fallback variants (swapped, without 'The').

        :param musicbrainz: MusicBrainz provider instance.
        :param artist_name: Artist name to search for.
        :param track_name: Track name to search for.
        :returns: Tuple of (mb_result, swapped) where swapped indicates artist/track were reversed.
        """
        # Try original order
        mb_result = await musicbrainz.get_release_group_by_track_name(artist_name, track_name)
        if mb_result:
            return mb_result, False

        # Try swapped (some stations send "Track - Artist")
        self.logger.debug(
            "No MusicBrainz match for '%s - %s', trying swapped",
            artist_name,
            track_name,
        )
        mb_result = await musicbrainz.get_release_group_by_track_name(track_name, artist_name)
        if mb_result:
            return mb_result, True

        # Try without "The " prefix
        artist_no_the = artist_name[4:] if artist_name.lower().startswith("the ") else None
        track_no_the = track_name[4:] if track_name.lower().startswith("the ") else None

        if artist_no_the:
            self.logger.debug(
                "No match, trying without 'The': '%s - %s'", artist_no_the, track_name
            )
            mb_result = await musicbrainz.get_release_group_by_track_name(artist_no_the, track_name)
            if mb_result:
                return mb_result, False

        if track_no_the:
            self.logger.debug(
                "No match, trying swapped without 'The': '%s - %s'", track_no_the, artist_name
            )
            mb_result = await musicbrainz.get_release_group_by_track_name(track_no_the, artist_name)
            if mb_result:
                return mb_result, True

        return None, False

    def _get_thumb_image(self, metadata: MediaItemMetadata) -> MediaItemMetadata | None:
        """
        Extract only THUMB type image from metadata.

        Returns new metadata with only the thumb image, or None if no thumb found.
        Used for radio artwork where we specifically need artist/album thumbnails,
        not logos or banners.

        :param metadata: Metadata to extract thumb from.
        """
        if not metadata.images:
            return None
        for img in metadata.images:
            if img.type == ImageType.THUMB:
                return MediaItemMetadata(images=UniqueList([img]))
        return None

    async def _get_library_track_metadata(
        self, artist_name: str, track_name: str
    ) -> MediaItemMetadata | None:
        """
        Search library for matching track and return its metadata.

        :param artist_name: Artist name to match.
        :param track_name: Track title to match.
        """
        try:
            search_query = f"{artist_name} {track_name}"
            library_tracks = await self.mass.music.tracks.search(search_query, "library", limit=5)
            for track in library_tracks:
                if not self._match_artist_name(artist_name, track.artists):
                    continue
                if not compare_strings(track_name, track.name, strict=False):
                    continue
                if image_url := await self._get_library_item_thumb(track):
                    return MediaItemMetadata(
                        images=UniqueList(
                            [
                                MediaItemImage(
                                    type=ImageType.THUMB,
                                    path=image_url,
                                    provider="library",
                                    remotely_accessible=True,
                                )
                            ]
                        )
                    )
        except InvalidDataError:
            pass
        return None

    async def _get_library_artist_metadata(self, artist_name: str) -> MediaItemMetadata | None:
        """
        Search library for matching artist and return its metadata.

        :param artist_name: Artist name to match.
        """
        try:
            library_artists = await self.mass.music.artists.search(artist_name, "library", limit=5)
            for artist in library_artists:
                if not compare_strings(artist_name, artist.name, strict=False):
                    continue
                if artist.metadata and artist.metadata.images:
                    for img in artist.metadata.images:
                        if img.type == ImageType.THUMB:
                            return MediaItemMetadata(
                                images=UniqueList(
                                    [
                                        MediaItemImage(
                                            type=ImageType.THUMB,
                                            path=self.get_image_url(img, prefer_proxy=True),
                                            provider="library",
                                            remotely_accessible=True,
                                        )
                                    ]
                                )
                            )
        except InvalidDataError:
            pass
        return None

    def _match_artist_name(self, search_name: str, artists: list[Artist | ItemMapping]) -> bool:
        """
        Check if any artist matches the search name.

        :param search_name: Artist name to search for.
        :param artists: List of artists to check against.
        """
        for artist in artists:
            if compare_strings(search_name, artist.name, strict=False):
                return True
            # Handle "The" prefix variations
            if compare_strings(f"The {search_name}", artist.name, strict=False):
                return True
            if artist.name.lower().startswith("the "):
                if compare_strings(search_name, artist.name[4:], strict=False):
                    return True
        return False

    async def _get_library_item_thumb(self, track: Track) -> str | None:
        """
        Get image URL for library track with fallback: track -> album -> artist.

        :param track: Track to get image for.
        """
        # Try track image
        if track.metadata and track.metadata.images:
            for img in track.metadata.images:
                if img.type == ImageType.THUMB:
                    return self.get_image_url(img, prefer_proxy=True)

        # Try album image
        if track.album:
            album = track.album
            if isinstance(album, ItemMapping):
                try:
                    full_album = await self.mass.music.albums.get_library_item(album.item_id)
                    if full_album and full_album.metadata and full_album.metadata.images:
                        for img in full_album.metadata.images:
                            if img.type == ImageType.THUMB:
                                return self.get_image_url(img, prefer_proxy=True)
                except MediaNotFoundError:
                    pass
            elif isinstance(album, Album) and album.metadata and album.metadata.images:
                for img in album.metadata.images:
                    if img.type == ImageType.THUMB:
                        return self.get_image_url(img, prefer_proxy=True)

        # Try artist image
        for artist in track.artists:
            if isinstance(artist, ItemMapping):
                try:
                    full_artist = await self.mass.music.artists.get_library_item(artist.item_id)
                    if full_artist and full_artist.metadata and full_artist.metadata.images:
                        for img in full_artist.metadata.images:
                            if img.type == ImageType.THUMB:
                                return self.get_image_url(img, prefer_proxy=True)
                except MediaNotFoundError:
                    pass
            elif isinstance(artist, Artist) and artist.metadata and artist.metadata.images:
                for img in artist.metadata.images:
                    if img.type == ImageType.THUMB:
                        return self.get_image_url(img, prefer_proxy=True)

        return None
