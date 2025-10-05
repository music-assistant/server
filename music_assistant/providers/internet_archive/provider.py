"""Internet Archive music provider implementation."""

from __future__ import annotations

import contextlib
import re
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    MediaItemChapter,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    SearchResults,
    Track,
)
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import UNKNOWN_ARTIST
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.models.music_provider import MusicProvider

from .helpers import InternetArchiveClient, clean_text, extract_year, parse_duration
from .parsers import (
    add_item_image,
    artist_exists,
    create_artist,
    create_provider_mapping,
    create_title_from_identifier,
    doc_to_album,
    doc_to_audiobook,
    doc_to_podcast,
    doc_to_track,
    is_audiobook_content,
    is_likely_album,
    is_podcast_content,
)
from .streaming import InternetArchiveStreaming

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant import MusicAssistant


class InternetArchiveProvider(MusicProvider):
    """Implementation of Internet Archive music provider."""

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature],
    ) -> None:
        """Initialize the provider."""
        super().__init__(mass, manifest, config, supported_features)
        self.throttler = ThrottlerManager(
            rate_limit=10, period=60, retry_attempts=5, initial_backoff=5
        )
        self.client = InternetArchiveClient(mass)
        self.streaming = InternetArchiveStreaming(self)

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if provider is a streaming provider."""
        return True

    @throttle_with_retries
    async def _get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Make a GET request and return JSON response with throttling."""
        return await self.client._get_json(url, params)

    @throttle_with_retries
    async def _search(self, **kwargs: Any) -> dict[str, Any]:
        """Throttled search wrapper."""
        return await self.client.search(**kwargs)

    @throttle_with_retries
    async def _get_metadata(self, identifier: str) -> dict[str, Any]:
        """Throttled metadata wrapper."""
        return await self.client.get_metadata(identifier)

    @throttle_with_retries
    @use_cache(expiration=86400 * 30)  # 30 days - file listings are static
    async def _get_audio_files(self, identifier: str) -> list[dict[str, Any]]:
        """Throttled audio files wrapper."""
        return await self.client.get_audio_files(identifier)

    @use_cache(86400 * 7)  # 7 days
    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """
        Perform search on Internet Archive.

        Uses multiple search strategies to maximize result coverage with
        proper result accumulation and broader search patterns.

        Args:
            search_query: The search term to look for
            media_types: List of media types to search for
            limit: Maximum number of results to return per media type

        Returns:
            SearchResults object containing found items
        """
        if not search_query.strip():
            return SearchResults()

        # Adjust search intensity based on what's being requested
        rows_per_strategy = min(limit * 2, 16) if len(media_types) > 1 else min(limit * 2, 100)

        # Collect results in separate lists
        tracks: list[Track] = []
        albums: list[Album] = []
        artists: list[Artist] = []
        audiobooks: list[Audiobook] = []
        podcasts: list[Podcast] = []

        # Track processed identifiers to avoid duplicates across strategies
        processed_ids: set[str] = set()

        # Build search strategies based on requested media types
        search_strategies = []

        # For music searches: focus on title and creator
        if any(mt in media_types for mt in [MediaType.TRACK, MediaType.ALBUM, MediaType.ARTIST]):
            search_strategies.extend(
                [
                    (f"creator:({search_query}) AND mediatype:audio", "downloads desc"),
                    (f"title:({search_query}) AND mediatype:audio", "downloads desc"),
                    (f"subject:({search_query}) AND mediatype:audio", "downloads desc"),
                ]
            )

        # For audiobooks: search within audiobook collections, still limit to audio
        if MediaType.AUDIOBOOK in media_types:
            audiobook_query = f"{search_query} AND collection:(librivoxaudio OR audio_bookspoetry) AND mediatype:audio"  # noqa: E501
            search_strategies.append((audiobook_query, "downloads desc"))

        # For podcasts: search within podcast collections
        if MediaType.PODCAST in media_types:
            podcast_query = f"{search_query} AND collection:podcasts AND mediatype:audio"
            search_strategies.append((podcast_query, "downloads desc"))

        for strategy_idx, (strategy_query, sort_order) in enumerate(search_strategies):
            self.logger.debug("Trying search strategy %d: %s", strategy_idx + 1, strategy_query)

            try:
                search_response = await self._search(
                    query=strategy_query,
                    rows=rows_per_strategy,
                    sort=sort_order,
                )

                response_data = search_response.get("response", {})
                docs = response_data.get("docs", [])
                self.logger.debug(
                    "Strategy %d '%s' found %d raw results",
                    strategy_idx + 1,
                    strategy_query,
                    len(docs),
                )

                # Process results and extract different media types
                strategy_processed = 0
                strategy_skipped = 0

                for doc in docs:
                    try:
                        identifier = doc.get("identifier")
                        if not identifier or identifier in processed_ids:
                            strategy_skipped += 1
                            continue

                        # Track this identifier to avoid duplicates
                        processed_ids.add(identifier)

                        await self._process_search_result(
                            doc, tracks, albums, artists, audiobooks, podcasts, media_types
                        )
                        strategy_processed += 1

                        # Check if we have enough results across all types
                        if self._has_sufficient_results(
                            tracks, albums, artists, audiobooks, podcasts, media_types, limit
                        ):
                            self.logger.debug(
                                "Sufficient results found after strategy %d, stopping search",
                                strategy_idx + 1,
                            )
                            break

                    except (InvalidDataError, KeyError) as err:
                        self.logger.debug("Skipping invalid search result: %s", err)
                        strategy_skipped += 1
                        continue

                self.logger.debug(
                    "Strategy %d '%s': processed %d new items, skipped %d items. "
                    "Running totals - tracks: %d, albums: %d, artists: %d, "
                    "audiobooks: %d, podcasts: %d",
                    strategy_idx + 1,
                    strategy_query,
                    strategy_processed,
                    strategy_skipped,
                    len(tracks),
                    len(albums),
                    len(artists),
                    len(audiobooks),
                    len(podcasts),
                )

                # If we have sufficient results, stop trying more strategies
                if self._has_sufficient_results(
                    tracks, albums, artists, audiobooks, podcasts, media_types, limit
                ):
                    break

            except Exception as err:
                self.logger.warning("Search strategy %d failed: %s", strategy_idx + 1, err)
                continue

        # Log final results for debugging
        self.logger.debug(
            "Search for '%s' completed. Final results - tracks: %d, albums: %d, "
            "artists: %d, audiobooks: %d, podcasts: %d (processed %d unique items)",
            search_query,
            len(tracks),
            len(albums),
            len(artists),
            len(audiobooks),
            len(podcasts),
            len(processed_ids),
        )

        return SearchResults(
            tracks=tracks[:limit] if MediaType.TRACK in media_types else [],
            albums=albums[:limit] if MediaType.ALBUM in media_types else [],
            artists=artists[:limit] if MediaType.ARTIST in media_types else [],
            audiobooks=audiobooks[:limit] if MediaType.AUDIOBOOK in media_types else [],
            podcasts=podcasts[:limit] if MediaType.PODCAST in media_types else [],
        )

    def _has_sufficient_results(
        self,
        tracks: list[Track],
        albums: list[Album],
        artists: list[Artist],
        audiobooks: list[Audiobook],
        podcasts: list[Podcast],
        media_types: list[MediaType],
        limit: int,
    ) -> bool:
        """Check if we have sufficient results for all requested media types."""
        return (
            (MediaType.TRACK not in media_types or len(tracks) >= limit)
            and (MediaType.ALBUM not in media_types or len(albums) >= limit)
            and (MediaType.ARTIST not in media_types or len(artists) >= limit)
            and (MediaType.AUDIOBOOK not in media_types or len(audiobooks) >= limit)
            and (MediaType.PODCAST not in media_types or len(podcasts) >= limit)
        )

    async def _process_search_result(
        self,
        doc: dict[str, Any],
        tracks: list[Track],
        albums: list[Album],
        artists: list[Artist],
        audiobooks: list[Audiobook],
        podcasts: list[Podcast],
        media_types: list[MediaType],
    ) -> None:
        """
        Process a single search result document from Internet Archive.

        Determines the appropriate media type and creates corresponding objects.
        Uses improved heuristics to classify items as tracks, albums, or audiobooks.
        """
        identifier = doc.get("identifier")
        if not identifier:
            raise InvalidDataError("Missing identifier in search result")

        title = clean_text(doc.get("title"))
        creator = clean_text(doc.get("creator"))

        # Be lenient - allow items without title if they have identifier
        if not title and not identifier:
            raise InvalidDataError("Missing both title and identifier in search result")

        # Use identifier as fallback title if needed
        if not title:
            title = create_title_from_identifier(identifier)

        # Determine what type of item this is
        mediatype = doc.get("mediatype", "")
        collection = doc.get("collection", [])
        if isinstance(collection, str):
            collection = [collection]

        # Check if this is audiobook content using improved detection
        if is_audiobook_content(doc) and MediaType.AUDIOBOOK in media_types:
            audiobook = doc_to_audiobook(
                doc, self.domain, self.instance_id, self.client.get_item_url
            )
            if audiobook:
                audiobooks.append(audiobook)
            return  # Don't process as other media types

        # Check if this is podcast content
        if is_podcast_content(doc) and MediaType.PODCAST in media_types:
            podcast = doc_to_podcast(doc, self.domain, self.instance_id, self.client.get_item_url)
            if podcast:
                podcasts.append(podcast)
            return  # Don't process as other media types

        # For etree items, usually each item is an album (concert)
        if mediatype == "etree" or "etree" in collection:
            if MediaType.ALBUM in media_types:
                album = doc_to_album(doc, self.domain, self.instance_id, self.client.get_item_url)
                if album:
                    albums.append(album)

            if MediaType.ARTIST in media_types and creator:
                artist = create_artist(creator, self.domain, self.instance_id)
                if artist and not artist_exists(artist, artists):
                    artists.append(artist)

        elif mediatype == "audio":
            # Use heuristics to determine album vs track without expensive API calls
            if is_likely_album(doc):
                if MediaType.ALBUM in media_types:
                    album = doc_to_album(
                        doc, self.domain, self.instance_id, self.client.get_item_url
                    )
                    if album:
                        albums.append(album)
            elif MediaType.TRACK in media_types:
                track = doc_to_track(doc, self.domain, self.instance_id, self.client.get_item_url)
                if track:
                    tracks.append(track)

            if MediaType.ARTIST in media_types and creator:
                artist = create_artist(creator, self.domain, self.instance_id)
                if artist and not artist_exists(artist, artists):
                    artists.append(artist)

    @use_cache(expiration=86400 * 60)  # Cache for 60 days - artist "tracks" change infrequently
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        metadata = await self._get_metadata(prov_track_id)
        item_metadata = metadata.get("metadata", {})

        title = clean_text(item_metadata.get("title"))
        creator = clean_text(item_metadata.get("creator"))

        if not title:
            raise MediaNotFoundError(f"Track {prov_track_id} not found or invalid")

        track = Track(
            item_id=prov_track_id,
            provider=self.instance_id,
            name=title,
            provider_mappings={
                create_provider_mapping(
                    prov_track_id, self.domain, self.instance_id, self.client.get_item_url
                )
            },
        )

        # Add artist
        if creator:
            track.artists = UniqueList([create_artist(creator, self.domain, self.instance_id)])
        else:
            track.artists = UniqueList(
                [create_artist(UNKNOWN_ARTIST, self.domain, self.instance_id)]
            )

        # Add duration from first audio file
        try:
            audio_files = await self._get_audio_files(prov_track_id)
            if audio_files and audio_files[0].get("length"):
                duration = parse_duration(audio_files[0]["length"])
                if duration:
                    track.duration = duration
        except (TimeoutError, aiohttp.ClientError) as err:
            self.logger.debug("Network error getting duration for track %s: %s", prov_track_id, err)
        except (KeyError, ValueError, TypeError) as err:
            self.logger.debug("Could not parse duration for track %s: %s", prov_track_id, err)

        # Add metadata
        if description := clean_text(item_metadata.get("description")):
            track.metadata.description = description

        # Add thumbnail
        add_item_image(track, prov_track_id, self.instance_id)

        return track

    @use_cache(expiration=86400 * 60)  # Cache for 60 days - album catalogs change infrequently
    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        metadata = await self._get_metadata(prov_album_id)
        item_metadata = metadata.get("metadata", {})

        title = clean_text(item_metadata.get("title"))
        creator = clean_text(item_metadata.get("creator"))

        if not title:
            raise MediaNotFoundError(f"Album {prov_album_id} not found or invalid")

        album = Album(
            item_id=prov_album_id,
            provider=self.instance_id,
            name=title,
            provider_mappings={
                create_provider_mapping(
                    prov_album_id, self.domain, self.instance_id, self.client.get_item_url
                )
            },
        )

        # Add artist
        if creator:
            album.artists = UniqueList([create_artist(creator, self.domain, self.instance_id)])
        else:
            album.artists = UniqueList(
                [create_artist(UNKNOWN_ARTIST, self.domain, self.instance_id)]
            )

        # Add metadata
        if date := extract_year(item_metadata.get("date")):
            album.year = date

        if description := clean_text(item_metadata.get("description")):
            album.metadata.description = description

        # Add thumbnail
        add_item_image(album, prov_album_id, self.instance_id)

        return album

    @use_cache(expiration=86400 * 60)  # Cache for 60 days - artist catalogs change infrequently
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """
        Get full artist details by id.

        Args:
            prov_artist_id: Provider-specific artist identifier (artist name)

        Returns:
            Artist object
        """
        # Artist IDs are just the creator names
        return Artist(
            item_id=prov_artist_id,
            provider=self.instance_id,
            name=prov_artist_id,
            provider_mappings={
                ProviderMapping(
                    item_id=prov_artist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

    @use_cache(expiration=86400 * 30)  # Cache for 30 days - audiobook catalogs change infrequently
    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Get full audiobook details by id."""
        metadata = await self._get_metadata(prov_audiobook_id)
        item_metadata = metadata.get("metadata", {})

        title = clean_text(item_metadata.get("title"))
        creator = clean_text(item_metadata.get("creator"))

        if not title:
            raise MediaNotFoundError(f"Audiobook {prov_audiobook_id} not found or invalid")

        audiobook = Audiobook(
            item_id=prov_audiobook_id,
            provider=self.instance_id,
            name=title,
            provider_mappings={
                create_provider_mapping(
                    prov_audiobook_id, self.domain, self.instance_id, self.client.get_item_url
                )
            },
        )

        # Add author/narrator
        if creator:
            author_list = [creator]
            audiobook.authors = UniqueList(author_list)

        # Add metadata
        if description := clean_text(item_metadata.get("description")):
            audiobook.metadata.description = description

        # Add thumbnail
        add_item_image(audiobook, prov_audiobook_id, self.instance_id)

        # Calculate duration and chapters
        try:
            total_duration, chapters = await self._calculate_audiobook_duration_and_chapters(
                prov_audiobook_id
            )
            audiobook.duration = total_duration
            if len(chapters) > 1:
                audiobook.metadata.chapters = chapters

        except Exception as err:
            self.logger.warning(
                f"Could not process audio files for audiobook {prov_audiobook_id}: {err}"
            )
            audiobook.duration = 0
            audiobook.metadata.chapters = []

        return audiobook

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        metadata = await self._get_metadata(prov_album_id)
        item_metadata = metadata.get("metadata", {})
        audio_files = await self._get_audio_files(prov_album_id)
        tracks = []

        # Pre-create album artist to avoid duplicates
        album_artist = clean_text(item_metadata.get("creator"))
        album_artist_normalized = album_artist.lower() if album_artist else ""
        album_artist_obj = None
        if album_artist:
            album_artist_obj = create_artist(album_artist, self.domain, self.instance_id)
        else:
            album_artist_obj = create_artist(UNKNOWN_ARTIST, self.domain, self.instance_id)

        for i, file_info in enumerate(audio_files, 1):
            filename = file_info.get("name", "")

            # Use file's title if available, otherwise clean up filename
            track_name = file_info.get("title", filename)
            if not track_name or track_name == filename:
                track_name = filename.rsplit(".", 1)[0] if "." in filename else filename

            # Try to extract track number from file metadata first, then filename
            track_number = self._extract_track_number(file_info, track_name, i)

            track = Track(
                item_id=f"{prov_album_id}#{filename}",
                provider=self.instance_id,
                name=track_name,
                track_number=track_number,
                provider_mappings={
                    ProviderMapping(
                        item_id=f"{prov_album_id}#{filename}",
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                        url=self.client.get_download_url(prov_album_id, filename),
                        available=True,
                    )
                },
            )

            # Add file-specific artist if available, otherwise use album artist
            file_artist = file_info.get("artist") or file_info.get("creator")
            if file_artist:
                file_artist_cleaned = clean_text(file_artist)
                file_artist_normalized = file_artist_cleaned.lower()
                # Check if this is the same as album artist to avoid duplicates (case-insensitive)
                if album_artist_normalized and file_artist_normalized == album_artist_normalized:
                    track.artists = UniqueList([album_artist_obj])
                else:
                    track.artists = UniqueList(
                        [create_artist(file_artist_cleaned, self.domain, self.instance_id)]
                    )
            else:
                # Use pre-created album artist object
                track.artists = UniqueList([album_artist_obj])

            # Add duration if available
            if duration_str := file_info.get("length"):
                if duration := parse_duration(duration_str):
                    track.duration = duration

            # Add genre if available
            if genre := file_info.get("genre"):
                track.metadata.genres = {clean_text(genre)}

            tracks.append(track)

        return tracks

    def _extract_track_number(
        self, file_info: dict[str, Any], track_name: str, fallback: int
    ) -> int:
        """Extract track number from file metadata or filename."""
        track_number = None

        if "track" in file_info:
            with contextlib.suppress(ValueError, AttributeError):
                track_number = int(str(file_info["track"]).split("/")[0])

        if track_number is None:
            # Fallback to filename parsing
            track_num_match = re.search(r"^(\d+)[\s\-_.]*(.+)", track_name)
            track_number = int(track_num_match.group(1)) if track_num_match else fallback

        return track_number

    @use_cache(expiration=86400 * 30)  # Cache for 30 days - artist catalogs change infrequently
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """
        Get albums for a specific artist.

        Uses metadata heuristics to determine likely albums without expensive
        API calls for better performance.

        Args:
            prov_artist_id: Provider-specific artist identifier (artist name)

        Returns:
            List of Album objects by the artist
        """
        albums: list[Album] = []
        page = 0
        page_size = 200  # IA's maximum

        while len(albums) < 1000:  # Reasonable upper limit
            search_response = await self._search(
                query=f'creator:"{prov_artist_id}" AND (format:"VBR MP3" OR format:"FLAC" \
        OR format:"Ogg Vorbis")',
                sort="downloads desc",
                rows=page_size,
                page=page,
            )

            docs = search_response.get("response", {}).get("docs", [])
            if not docs:
                break

            for doc in docs:
                try:
                    # Use metadata heuristics instead of expensive API calls
                    # to determine if item is an album
                    if is_likely_album(doc):
                        album = doc_to_album(
                            doc, self.domain, self.instance_id, self.client.get_item_url
                        )
                        if album:
                            albums.append(album)
                except (KeyError, ValueError, TypeError) as err:
                    self.logger.debug(
                        "Skipping invalid album for artist %s: %s", prov_artist_id, err
                    )
                    continue
                except (TimeoutError, aiohttp.ClientError) as err:
                    self.logger.debug(
                        "Network error processing album for artist %s: %s", prov_artist_id, err
                    )
                    continue
                except Exception as err:
                    self.logger.exception(
                        "Unexpected error processing album for artist %s: %s", prov_artist_id, err
                    )
                    continue
            page += 1
        return albums

    @use_cache(expiration=86400 * 7)  # Cache for 1 week
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """
        Get top tracks for a specific artist.

        Uses the same search as get_artist_albums but filters for single tracks.

        Args:
            prov_artist_id: Provider-specific artist identifier (artist name)

        Returns:
            List of Track objects representing the artist's top tracks
        """
        tracks = []
        search_response = await self._search(
            query=(
                f'creator:"{prov_artist_id}" AND '
                f'(format:"VBR MP3" OR format:"FLAC" OR format:"Ogg Vorbis")'
            ),
            rows=25,  # Limit for "top" tracks
            sort="downloads desc",
        )

        response_data = search_response.get("response", {})
        docs = response_data.get("docs", [])

        for doc in docs:
            try:
                # Only include items that are NOT classified as albums
                if not is_likely_album(doc):
                    track = doc_to_track(
                        doc, self.domain, self.instance_id, self.client.get_item_url
                    )
                    if track:
                        tracks.append(track)
            except (KeyError, ValueError, TypeError) as err:
                self.logger.debug("Skipping invalid track for artist %s: %s", prov_artist_id, err)
                continue
            except (TimeoutError, aiohttp.ClientError) as err:
                self.logger.debug(
                    "Network error processing track for artist %s: %s", prov_artist_id, err
                )
                continue
            except Exception as err:
                self.logger.exception(
                    "Unexpected error processing track for artist %s: %s", prov_artist_id, err
                )
                continue

            if len(tracks) >= 25:
                break

        return tracks

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Get streamdetails for a track or audiobook.

        Delegates to the streaming handler for proper multi-file support.

        Args:
            item_id: Provider-specific item identifier
            media_type: The type of media being requested

        Returns:
            StreamDetails object configured for the specific item type

        Raises:
            MediaNotFoundError: If no audio files are found for the item
        """
        return await self.streaming.get_stream_details(item_id, media_type)

    async def _calculate_audiobook_duration_and_chapters(
        self, item_id: str
    ) -> tuple[int, list[MediaItemChapter]]:
        """Calculate duration and chapters for audiobooks."""
        audio_files = await self._get_audio_files(item_id)
        total_duration = 0
        chapters = []
        current_position = 0.0

        for i, file_info in enumerate(audio_files, 1):
            chapter_duration = parse_duration(file_info.get("length", "0")) or 0
            total_duration += chapter_duration

            chapter_name = file_info.get("title") or file_info.get("name", f"Chapter {i}")
            chapter = MediaItemChapter(
                position=i,
                name=clean_text(chapter_name),
                start=current_position,
                end=current_position + chapter_duration if chapter_duration > 0 else None,
            )
            chapters.append(chapter)
            current_position += chapter_duration

        return total_duration, chapters

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Get audio stream from Internet Archive."""
        # Use sock_read=None to allow long audiobook chapters to stream fully
        timeout = aiohttp.ClientTimeout(sock_read=None, total=None)

        if streamdetails.media_type == MediaType.AUDIOBOOK and isinstance(streamdetails.data, dict):
            chapter_urls = streamdetails.data.get("chapters", [])
            chapters_data = streamdetails.data.get("chapters_data", [])

            # Calculate which chapter to start from based on seek_position
            seek_position_ms = seek_position * 1000
            start_chapter = 0

            if seek_position > 0 and chapters_data:
                accumulated_duration_ms = 0

                for i, chapter_data in enumerate(chapters_data):
                    chapter_duration_ms = (
                        parse_duration(chapter_data.get("length", "0")) or 0
                    ) * 1000

                    if accumulated_duration_ms + chapter_duration_ms > seek_position_ms:
                        start_chapter = i
                        break
                    accumulated_duration_ms += chapter_duration_ms

            # Stream chapters starting from calculated position
            chapters_yielded = False
            for i in range(start_chapter, len(chapter_urls)):
                chapter_url = chapter_urls[i]

                try:
                    async with self.mass.http_session.get(chapter_url, timeout=timeout) as response:
                        response.raise_for_status()
                        async for chunk in response.content.iter_chunked(8192):
                            chapters_yielded = True
                            yield chunk
                except Exception as e:
                    self.logger.error(f"Chapter {i + 1} streaming failed: {e}")
                    continue

            # If no chapters succeeded, raise an error instead of silent failure
            if not chapters_yielded:
                raise MediaNotFoundError(
                    f"Failed to stream any chapters for audiobook {streamdetails.item_id}"
                )

        else:
            # Handle single files
            audio_files = await self._get_audio_files(streamdetails.item_id)
            if audio_files:
                download_url = self.client.get_download_url(
                    streamdetails.item_id, audio_files[0]["name"]
                )
                async with self.mass.http_session.get(download_url, timeout=timeout) as response:
                    response.raise_for_status()
                    async for chunk in response.content.iter_chunked(8192):
                        yield chunk

    @use_cache(expiration=86400 * 7)  # Cache for 1 week
    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full podcast details by id."""
        metadata = await self._get_metadata(prov_podcast_id)
        item_metadata = metadata.get("metadata", {})

        title = clean_text(item_metadata.get("title"))
        creator = clean_text(item_metadata.get("creator"))

        if not title:
            raise MediaNotFoundError(f"Podcast {prov_podcast_id} not found or invalid")

        podcast = Podcast(
            item_id=prov_podcast_id,
            provider=self.instance_id,
            name=title,
            provider_mappings={
                create_provider_mapping(
                    prov_podcast_id, self.domain, self.instance_id, self.client.get_item_url
                )
            },
        )

        # Add publisher/creator
        if creator:
            podcast.publisher = creator

        # Add metadata
        if description := clean_text(item_metadata.get("description")):
            podcast.metadata.description = description

        # Add thumbnail
        add_item_image(podcast, prov_podcast_id, self.instance_id)

        # Calculate total episodes
        try:
            audio_files = await self._get_audio_files(prov_podcast_id)
            podcast.total_episodes = len(audio_files)
        except Exception as err:
            self.logger.warning(f"Could not get episode count for podcast {prov_podcast_id}: {err}")
            podcast.total_episodes = None

        return podcast

    async def get_podcast_episodes(
        self, prov_podcast_id: str
    ) -> AsyncGenerator[PodcastEpisode, None]:
        """Get podcast episodes for given podcast id."""
        metadata = await self._get_metadata(prov_podcast_id)
        item_metadata = metadata.get("metadata", {})
        audio_files = await self._get_audio_files(prov_podcast_id)

        # Create podcast reference for episodes
        podcast = Podcast(
            item_id=prov_podcast_id,
            provider=self.instance_id,
            name=clean_text(item_metadata.get("title", prov_podcast_id)),
            provider_mappings={
                create_provider_mapping(
                    prov_podcast_id, self.domain, self.instance_id, self.client.get_item_url
                )
            },
        )

        for i, file_info in enumerate(audio_files, 1):
            filename = file_info.get("name", "")

            # Use file's title if available, otherwise clean up filename
            episode_name = file_info.get("title", filename)
            if not episode_name or episode_name == filename:
                episode_name = filename.rsplit(".", 1)[0] if "." in filename else filename

            # Try to extract episode number from file metadata first, then filename
            episode_number = self._extract_track_number(file_info, episode_name, i)

            episode = PodcastEpisode(
                item_id=f"{prov_podcast_id}#{filename}",
                provider=self.instance_id,
                name=episode_name,
                position=episode_number,
                podcast=podcast,
                provider_mappings={
                    ProviderMapping(
                        item_id=f"{prov_podcast_id}#{filename}",
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                        url=self.client.get_download_url(prov_podcast_id, filename),
                        available=True,
                    )
                },
            )

            # Add duration if available
            if duration_str := file_info.get("length"):
                if duration := parse_duration(duration_str):
                    episode.duration = duration

            # Add episode metadata
            if description := file_info.get("description"):
                episode.metadata.description = clean_text(description)

            yield episode

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get single podcast episode by id."""
        if "#" not in prov_episode_id:
            raise MediaNotFoundError(f"Invalid episode ID format: {prov_episode_id}")

        podcast_id, _ = prov_episode_id.split("#", 1)

        async for episode in self.get_podcast_episodes(podcast_id):
            if episode.item_id == prov_episode_id:
                return episode

        raise MediaNotFoundError(f"Episode {prov_episode_id} not found")
