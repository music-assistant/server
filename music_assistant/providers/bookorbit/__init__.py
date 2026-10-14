"""BookOrbit provider for Music Assistant."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, ClientResponseError, web
from music_assistant_models.enums import (
    ArtistType,
    ContentType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
)
from music_assistant_models.media_items import (
    Audiobook,
    AudioFormat,
    BrowseFolder,
    SearchResults,
)
from music_assistant_models.streamdetails import MultiPartPath, StreamDetails

from music_assistant.models.music_provider import MusicProvider

from .client import BookOrbitAuthError, BookOrbitClient
from .constants import (
    BROWSE_ITEMS_TO_PATH,
    CATALOG_TTL_SEC,
    CONF_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    PART_STREAM_CHUNK_SIZE,
    PART_STREAM_HEADERS,
    PART_STREAM_TIMEOUT,
    STREAMDETAILS_EXPIRATION_S,
    BrowseItemsTranslationKey,
    BrowsePaths,
)
from .helpers import library_may_contain_audio, normalize_base_url, parse_browse_path
from .parsers import (
    AUTHOR_PREFIX,
    NARRATOR_PREFIX,
    _audio_files,
    parse_audiobook,
    parse_author,
    parse_narrator,
)
from .progress import build_progress_update, parse_resume_position

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Iterable, Sequence
    from datetime import datetime

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.media_items import Artist, ItemMapping, MediaItemType
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_AUDIOBOOKS,
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
}


def _people(entries: Any) -> list[dict[str, Any]]:
    """Return the entries that are person objects with an id."""
    return [e for e in entries or [] if isinstance(e, dict) and "id" in e]


def _book_series(book: dict[str, Any]) -> list[tuple[str, str]]:
    """Return (series id, series name) of a book."""
    memberships = book.get("seriesMemberships") or []
    if memberships:
        return [
            (str(m["seriesId"]), m["seriesName"])
            for m in memberships
            if m.get("seriesId") is not None and m.get("seriesName")
        ]
    if book.get("seriesId") is not None and book.get("seriesName"):
        return [(str(book["seriesId"]), book["seriesName"])]
    return []


def _series_index(book: dict[str, Any], series_id: str | None) -> float | None:
    """Return the position of a book in the given series."""
    if series_id is None:
        return None
    for membership in book.get("seriesMemberships") or []:
        if str(membership.get("seriesId")) == series_id:
            index = membership.get("seriesIndex")
            return float(index) if index is not None else None
    index = book.get("seriesIndex")
    return float(index) if index is not None else None


def _book_haystack(book: dict[str, Any]) -> str:
    """Return the lower-cased text of a book that a local search matches against."""
    parts = [book.get("title"), book.get("subtitle")]
    parts.extend(a.get("name") for a in _people(book.get("authors")))
    return " ".join(str(p) for p in parts if p).casefold()


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider instance."""
    return BookOrbitProvider(mass, manifest, config, SUPPORTED_FEATURES)


class BookOrbitProvider(MusicProvider):
    """BookOrbit MusicProvider (audiobooks)."""

    _client: BookOrbitClient
    _unregister_route: Callable[[], None] | None = None

    async def handle_async_init(self) -> None:
        """Create the BookOrbit client from the config and log in."""
        self.base_url = normalize_base_url(str(self.get_setup_value(CONF_URL)))
        self._client = BookOrbitClient(
            session=self.mass.http_session,
            base_url=self.base_url,
            username=str(self.get_setup_value(CONF_USERNAME)),
            password=str(self.get_setup_value(CONF_PASSWORD)),
            verify_ssl=bool(self.get_setup_value(CONF_VERIFY_SSL, True)),
        )
        # BookOrbit book details (id -> BookDetail) of the audiobooks found by the last sync
        self._catalog: dict[str, dict[str, Any]] = {}
        self._catalog_refreshed_at: float = 0.0
        # library id -> name / ids of the catalog's audiobooks in that library (for browse)
        self._library_names: dict[int, str] = {}
        self._library_books: dict[int, list[str]] = {}
        try:
            await self._client.login()
        except BookOrbitAuthError as err:
            raise LoginFailed(f"Login to BookOrbit at {self.base_url} failed.") from err
        except ClientError as err:
            raise ProviderUnavailableError(f"BookOrbit at {self.base_url} is unreachable.") from err
        # audio files are proxied by MA, so no token ends up in a stream url
        self._unregister_route = self.mass.streams.register_dynamic_route(
            f"/{self.instance_id}_part_stream", self._handle_part_request
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        await super().unload(is_removed)
        if unregister := self._unregister_route:
            self._unregister_route = None
            unregister()

    @property
    def supported_artist_types(self) -> set[ArtistType]:
        """Supported artist types."""
        return {ArtistType.AUTHOR, ArtistType.NARRATOR}

    async def sync_library(self, media_type: MediaType) -> None:
        """Refresh the catalog of audiobooks, then sync audiobooks / artists from it."""
        if media_type in (MediaType.AUDIOBOOK, MediaType.ARTIST):
            await self._refresh_catalog()
        await super().sync_library(media_type)

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook]:
        """Retrieve the audiobooks found by the catalog refresh."""
        for book in list(self._catalog.values()):
            yield parse_audiobook(
                book, provider_instance_id=self.instance_id, base_url=self.base_url
            )

    async def get_library_artists(self) -> AsyncGenerator[Artist]:
        """Retrieve the authors and narrators of the catalog's audiobooks."""
        for artist in self._catalog_artists().values():
            yield artist

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get an author (``author:<id>``) or narrator (``narrator:<id>``)."""
        if not prov_artist_id.startswith((AUTHOR_PREFIX, NARRATOR_PREFIX)):
            raise MediaNotFoundError(f"Invalid BookOrbit artist id {prov_artist_id}")
        if artist := self._catalog_artists().get(prov_artist_id):
            return artist
        if not self._catalog:  # not synced yet
            await self._refresh_catalog(force=True)
            if artist := self._catalog_artists().get(prov_artist_id):
                return artist
        raise MediaNotFoundError(f"BookOrbit artist {prov_artist_id} not found")

    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Get a single audiobook."""
        try:
            book = await self._client.get_book(int(prov_audiobook_id))
        except ValueError as err:
            raise MediaNotFoundError(f"Invalid BookOrbit book id {prov_audiobook_id}") from err
        except ClientResponseError as err:
            if err.status == 404:
                raise MediaNotFoundError(f"BookOrbit book {prov_audiobook_id} not found") from err
            raise
        if not _audio_files(book):
            raise MediaNotFoundError(f"BookOrbit book {prov_audiobook_id} has no audio files")
        return parse_audiobook(book, provider_instance_id=self.instance_id, base_url=self.base_url)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get the stream of an audiobook: one HTTP part per audio file, proxied by MA."""
        if media_type != MediaType.AUDIOBOOK:
            raise MediaNotFoundError("Stream unknown")
        book = await self._get_book(item_id, fresh=True)
        files = _audio_files(book)
        if not files:
            raise MediaNotFoundError(f"BookOrbit book {item_id} has no audio files")
        parts = [
            MultiPartPath(
                path=(
                    f"{self.mass.streams.base_url}/{self.instance_id}_part_stream?"
                    f"book_id={book['id']}&file_id={file['id']}"
                ),
                duration=file.get("durationSeconds"),
            )
            for file in files
        ]
        duration = parse_audiobook(
            book, provider_instance_id=self.instance_id, base_url=self.base_url
        ).duration
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=ContentType.try_parse(files[0]["format"])),
            media_type=media_type,
            stream_type=StreamType.HTTP,
            duration=duration,
            path=parts[0].path if len(parts) == 1 else parts,
            can_seek=True,
            allow_seek=True,
            expiration=STREAMDETAILS_EXPIRATION_S,
        )

    async def get_resume_position(
        self, item_id: str, media_type: MediaType
    ) -> tuple[bool, int, datetime | None]:
        """Return (fully played, position in ms, timestamp) from BookOrbit's audio progress."""
        if media_type != MediaType.AUDIOBOOK:
            raise NotImplementedError
        book = await self._get_book(item_id)
        progress = await self._client.get_audio_progress(book["id"])
        if not progress:
            raise NotImplementedError  # fall back to MA's own position
        return parse_resume_position(_audio_files(book), progress)

    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """Report progress of an audiobook to BookOrbit."""
        if media_type != MediaType.AUDIOBOOK:
            return
        try:
            book = await self._get_book(prov_item_id)
            update = build_progress_update(
                _audio_files(book), position=position, fully_played=fully_played
            )
            if update is None:
                return
            await self._client.upsert_audio_progress(
                book["id"],
                percentage=update.percentage,
                current_file_id=update.current_file_id,
                position_seconds=update.position_seconds,
            )
        except (ClientError, BookOrbitAuthError, MediaNotFoundError) as err:
            self.logger.warning("Updating BookOrbit progress of %s failed: %s", prov_item_id, err)

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse BookOrbit (audiobooks only).

        Generates this view:
        Library_Name_A (Audiobooks library)
            Authors
                Author_1 (folder)
                    Audiobook_1
            Narrators
                Narrator_1 (folder)
                    Audiobook_1
            Series
                Series_1 (folder)
                    Audiobook_1
            Audiobooks
                Audiobook_1
                Audiobook_2
        """
        try:
            library_id, sub_path = parse_browse_path(path)
        except ValueError:
            return []
        await self._refresh_catalog()
        if library_id is None:
            return self._browse_root()
        if library_id not in self._library_books:
            return []
        if not sub_path:
            return self._browse_library(path)
        section = sub_path[0]
        if len(sub_path) == 1:
            match section:
                case BrowsePaths.AUTHORS:
                    return self._browse_people(path, library_id, AUTHOR_PREFIX)
                case BrowsePaths.NARRATORS:
                    return self._browse_people(path, library_id, NARRATOR_PREFIX)
                case BrowsePaths.SERIES:
                    return self._browse_series(path, library_id)
                case BrowsePaths.AUDIOBOOKS:
                    return await self._audiobook_items(self._library_books[library_id])
        elif len(sub_path) == 2:
            book_ids = self._browse_book_ids(library_id, section, sub_path[1])
            return await self._audiobook_items(
                book_ids, series_id=sub_path[1] if section == BrowsePaths.SERIES else None
            )
        return []

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Search audiobooks, authors and narrators."""
        results = SearchResults()
        query = search_query.strip()
        if not query:
            return results
        await self._refresh_catalog()
        if MediaType.AUDIOBOOK in media_types:
            results.audiobooks = await self._search_audiobooks(query, limit)
        if MediaType.ARTIST in media_types:
            results.artists = await self._search_artists(query, limit)
        return results

    async def _get_book(self, item_id: str, *, fresh: bool = False) -> dict[str, Any]:
        """Return the BookDetail of a book (from the catalog unless fresh is requested)."""
        if not fresh and (book := self._catalog.get(item_id)):
            return book
        try:
            return await self._client.get_book(int(item_id))
        except ValueError as err:
            raise MediaNotFoundError(f"Invalid BookOrbit book id {item_id}") from err
        except ClientResponseError as err:
            if err.status == 404:
                raise MediaNotFoundError(f"BookOrbit book {item_id} not found") from err
            raise

    async def _handle_part_request(self, request: web.Request) -> web.StreamResponse:
        """Proxy one audio file of a book, with a fresh token and range support."""
        try:
            file_id = int(request.query["file_id"])
        except KeyError, ValueError:
            return web.Response(status=400, text="Missing or invalid file_id")
        try:
            upstream = await self._client.open_file(
                file_id,
                range_header=request.headers.get("Range"),
                timeout=PART_STREAM_TIMEOUT,
            )
        except (ClientError, BookOrbitAuthError) as err:
            self.logger.warning("Requesting file %s from BookOrbit failed: %s", file_id, err)
            raise web.HTTPBadGateway(text="BookOrbit unreachable") from err
        async with upstream:
            response = web.StreamResponse(
                status=upstream.status,
                headers={
                    name: upstream.headers[name]
                    for name in PART_STREAM_HEADERS
                    if name in upstream.headers
                },
            )
            try:
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(PART_STREAM_CHUNK_SIZE):
                    await response.write(chunk)
                await response.write_eof()
            except ConnectionError:
                # ffmpeg hangs up e.g. when it seeks or has probed enough
                self.logger.debug("Client disconnected from file %s", file_id)
        return response

    async def _refresh_catalog(self, *, force: bool = False) -> None:
        """Fetch all audiobooks (with details) from accessible libraries."""
        if (
            not force
            and self._catalog
            and (time.monotonic() - self._catalog_refreshed_at) < CATALOG_TTL_SEC
        ):
            return
        try:
            libraries = await self._client.list_libraries()
            catalog: dict[str, dict[str, Any]] = {}
            library_names: dict[int, str] = {}
            library_books: dict[int, list[str]] = {}
            for library in libraries:
                if not library_may_contain_audio(library):
                    continue
                members: list[str] = []
                for card in await self._client.query_audiobooks(library_id=library["id"]):
                    book_id = str(card["id"])
                    if book_id not in catalog:
                        try:
                            book = await self._client.get_book(int(card["id"]))
                        except ClientResponseError as err:
                            self.logger.warning("Skipping BookOrbit book %s: %s", book_id, err)
                            self.report_skipped_sync_item(MediaType.AUDIOBOOK, book_id, err)
                            continue
                        # books without audio files (ebook only) are no audiobooks
                        if not _audio_files(book):
                            continue
                        catalog[book_id] = book
                    members.append(book_id)
                if members:
                    library_names[library["id"]] = str(library.get("name") or library["id"])
                    library_books[library["id"]] = members
        except BookOrbitAuthError as err:
            raise LoginFailed(f"Login to BookOrbit at {self.base_url} failed.") from err
        except ClientResponseError:
            raise
        except ClientError as err:
            raise ProviderUnavailableError(f"BookOrbit at {self.base_url} is unreachable.") from err
        self._catalog = catalog
        self._library_names = library_names
        self._library_books = library_books
        self._catalog_refreshed_at = time.monotonic()

    def _catalog_artists(self, books: Iterable[dict[str, Any]] | None = None) -> dict[str, Artist]:
        """Return the authors and narrators of the catalog (or given books) keyed by item id."""
        artists: dict[str, Artist] = {}
        for book in self._catalog.values() if books is None else books:
            for author in book.get("authors") or []:
                if isinstance(author, dict) and "id" in author:
                    parsed = parse_author(author, provider_instance_id=self.instance_id)
                    artists.setdefault(parsed.item_id, parsed)
            for narrator in (book.get("audioMetadata") or {}).get("narrators") or []:
                if isinstance(narrator, dict) and "id" in narrator:
                    parsed = parse_narrator(narrator, provider_instance_id=self.instance_id)
                    artists.setdefault(parsed.item_id, parsed)
        return artists

    def _folder(
        self,
        path: str,
        item_id: str,
        name: str,
        translation_key: str | None = None,
        translation_params: list[str] | None = None,
    ) -> BrowseFolder:
        return BrowseFolder(
            item_id=item_id,
            name=name,
            translation_key=translation_key,
            translation_params=translation_params,
            provider=self.instance_id,
            path=path,
        )

    def _browse_root(self) -> Sequence[BrowseFolder]:
        """List the libraries that contain audiobooks."""
        if not self._library_books:
            self.logger.info("No BookOrbit libraries with audiobooks found.")
            return []
        return [
            self._folder(
                f"{self.instance_id}://{BrowsePaths.LIBRARIES} {library_id}",
                str(library_id),
                name,
                translation_key=BrowseItemsTranslationKey.AUDIOBOOKS_LIBRARY,
                translation_params=[name],
            )
            for library_id, name in self._library_names.items()
            if library_id in self._library_books
        ]

    def _browse_library(self, current_path: str) -> Sequence[BrowseFolder]:
        """List the sections of a library."""
        return [
            self._folder(
                f"{current_path}/{BROWSE_ITEMS_TO_PATH[key]}", key.lower(), "", translation_key=key
            )
            for key in BrowseItemsTranslationKey
            if key in BROWSE_ITEMS_TO_PATH
        ]

    def _library_book_details(self, library_id: int) -> list[dict[str, Any]]:
        return [
            self._catalog[book_id]
            for book_id in self._library_books.get(library_id, [])
            if book_id in self._catalog
        ]

    def _browse_people(
        self, current_path: str, library_id: int, prefix: str
    ) -> Sequence[BrowseFolder]:
        """List the authors or narrators of a library as folders."""
        people: dict[str, str] = {}
        for artist in self._catalog_artists(self._library_book_details(library_id)).values():
            if artist.item_id.startswith(prefix):
                people[artist.item_id] = artist.name
        return [
            self._folder(f"{current_path}/{item_id}", item_id, name)
            for item_id, name in sorted(people.items(), key=lambda x: x[1].casefold())
        ]

    def _browse_series(self, current_path: str, library_id: int) -> Sequence[BrowseFolder]:
        """List the series of a library as folders."""
        series: dict[str, str] = {}
        for book in self._library_book_details(library_id):
            for series_id, name in _book_series(book):
                series.setdefault(series_id, name)
        return [
            self._folder(f"{current_path}/{series_id}", series_id, name)
            for series_id, name in sorted(series.items(), key=lambda x: x[1].casefold())
        ]

    def _browse_book_ids(self, library_id: int, section: str, item_id: str) -> list[str]:
        """Return the ids of the library's audiobooks of one author/narrator/series."""
        matching: list[str] = []
        for book in self._library_book_details(library_id):
            match section:
                case BrowsePaths.AUTHORS:
                    ids = {f"{AUTHOR_PREFIX}{a['id']}" for a in _people(book.get("authors"))}
                case BrowsePaths.NARRATORS:
                    narrators = (book.get("audioMetadata") or {}).get("narrators")
                    ids = {f"{NARRATOR_PREFIX}{n['id']}" for n in _people(narrators)}
                case BrowsePaths.SERIES:
                    ids = {series_id for series_id, _ in _book_series(book)}
                case _:
                    return []
            if item_id in ids:
                matching.append(str(book["id"]))
        return matching

    async def _audiobook_items(
        self, book_ids: list[str], series_id: str | None = None
    ) -> Sequence[MediaItemType]:
        """Resolve catalog audiobooks to library items (parsed from the catalog if not synced)."""
        items: list[tuple[Audiobook, float | None]] = []
        for book_id in book_ids:
            if (book := self._catalog.get(book_id)) is None:
                continue
            item = await self.mass.music.get_library_item_by_prov_id(
                media_type=MediaType.AUDIOBOOK,
                item_id=book_id,
                provider_instance_id_or_domain=self.instance_id,
            )
            if not isinstance(item, Audiobook):
                item = parse_audiobook(
                    book, provider_instance_id=self.instance_id, base_url=self.base_url
                )
            items.append((item, _series_index(book, series_id)))
        if series_id is not None:
            items.sort(key=lambda x: (x[1] is None, x[1] or 0, x[0].name.casefold()))
        else:
            items.sort(key=lambda x: x[0].name.casefold())
        return [item for item, _ in items]

    async def _search_audiobooks(self, query: str, limit: int) -> list[Audiobook]:
        try:
            cards = await self._client.search_books(query)
        except ClientResponseError as err:
            self.logger.debug("BookOrbit book search failed: %s", err)
            needle = query.casefold()
            ids = [b for b, book in self._catalog.items() if needle in _book_haystack(book)]
        else:
            ids = [str(card["id"]) for card in cards if isinstance(card, dict) and "id" in card]
        audiobooks: list[Audiobook] = []
        for book_id in dict.fromkeys(ids):
            if len(audiobooks) >= limit:
                break
            book = self._catalog.get(book_id)
            if book is None:
                try:
                    book = await self._client.get_book(int(book_id))
                except ClientResponseError, ValueError:
                    continue
                if not _audio_files(book):
                    continue
            audiobooks.append(
                parse_audiobook(book, provider_instance_id=self.instance_id, base_url=self.base_url)
            )
        return audiobooks

    async def _search_artists(self, query: str, limit: int) -> list[Artist]:
        known = self._catalog_artists()
        try:
            authors = await self._client.search_authors(query)
            narrators = await self._client.search_narrators(query)
        except ClientResponseError as err:
            self.logger.debug("BookOrbit author/narrator search failed: %s", err)
            needle = query.casefold()
            return [a for a in known.values() if needle in a.name.casefold()][:limit]
        found: list[Artist] = []
        for prefix, people in ((AUTHOR_PREFIX, authors), (NARRATOR_PREFIX, narrators)):
            for person in _people(people):
                # only artists of the audiobook catalog can be resolved later on
                if (artist := known.get(f"{prefix}{person['id']}")) and artist not in found:
                    found.append(artist)
        return found[:limit]
