"""BookOrbit provider for Music Assistant."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, ClientResponseError, DummyCookieJar, web
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

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME, CONF_VERIFY_SSL
from music_assistant.controllers.cache import BYPASS_CACHE
from music_assistant.helpers.aiohttp_client import create_clientsession
from music_assistant.helpers.datetime import utc
from music_assistant.models.music_provider import SYNC_RUN_STATE, MusicProvider

from .client import BookOrbitAuthError, BookOrbitClient, BookOrbitIncompleteError
from .constants import (
    AIOHTTP_TIMEOUT,
    BROWSE_ITEMS_TO_PATH,
    BROWSE_LIBRARY_BATCH_SIZE,
    CACHE_CATEGORY_BOOK,
    CACHE_CATEGORY_LIBRARY_FINGERPRINTS,
    CACHE_EXPIRATION_BOOK,
    CACHE_EXPIRATION_LIBRARY_FINGERPRINTS,
    CACHE_KEY_LIBRARY_FINGERPRINTS,
    CATALOG_DETAIL_CONCURRENCY,
    CATALOG_TTL_SEC,
    CONF_URL,
    PART_STREAM_CHUNK_SIZE,
    PART_STREAM_HEADERS,
    PART_STREAM_TIMEOUT,
    STREAMDETAILS_EXPIRATION_S,
    BrowseItemsTranslationKey,
    BrowsePaths,
)
from .helpers import (
    book_with_manifest_playback_order,
    card_may_contain_audio,
    library_may_contain_audio,
    normalize_base_url,
    parse_browse_path,
    parse_series_index,
    series_index_sort_key,
)
from .parsers import (
    AUTHOR_PREFIX,
    NARRATOR_PREFIX,
    _audio_files,
    parse_audiobook,
    parse_author,
    parse_narrator,
)
from .progress import (
    build_playback_state_update,
    build_progress_update,
    parse_resume_position,
    parse_resume_position_from_playback_state,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Iterable, Sequence

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.media_items import Artist, ItemMapping, MediaItemType
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.controllers.music.media.base import LibraryItemSyncDetails
    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_AUDIOBOOKS,
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
}


@dataclass(slots=True, frozen=True)
class _StreamGrant:
    """Proxy access for one get_stream_details resolution until playback ends."""

    book_id: str
    asset_ids: frozenset[str]
    created_at: float


def _people(entries: Any) -> list[dict[str, Any]]:
    """Return the entries that are person objects with an id."""
    return [e for e in entries or [] if isinstance(e, dict) and "id" in e]


def _session_id_from_streamdetails(streamdetails: StreamDetails) -> str | None:
    """Return the BookOrbit stream session id stored on streamdetails, if any."""
    data = streamdetails.data
    if isinstance(data, dict):
        session_id = data.get("session_id")
        if isinstance(session_id, str) and session_id:
            return session_id
    return None


def _playback_captured_at_ts(value: Any) -> float | None:
    """Parse a BookOrbit ``capturedAt`` ISO timestamp to seconds, or ``None``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _remote_playback_supersedes(state: dict[str, Any] | None, report_captured_at: str) -> bool:
    """Return whether remote playback state is at least as new as this report."""
    if not state:
        return False
    remote_ts = _playback_captured_at_ts(state.get("capturedAt"))
    report_ts = _playback_captured_at_ts(report_captured_at)
    if remote_ts is None or report_ts is None:
        return False
    return remote_ts >= report_ts


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


def _series_index(book: dict[str, Any], series_id: str | None) -> str | None:
    """Return the BookOrbit series index string for the given series, if any."""
    if series_id is None:
        return None
    for membership in book.get("seriesMemberships") or []:
        if str(membership.get("seriesId")) == series_id:
            return parse_series_index(membership.get("seriesIndex"))
    return parse_series_index(book.get("seriesIndex"))


def _book_haystack(book: dict[str, Any]) -> str:
    """Return the lower-cased text of a book that a local search matches against."""
    parts = [book.get("title"), book.get("subtitle")]
    parts.extend(a.get("name") for a in _people(book.get("authors")))
    return " ".join(str(p) for p in parts if p).casefold()


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Create and return a BookOrbit provider instance."""
    return BookOrbitProvider(mass, manifest, config, SUPPORTED_FEATURES)


class BookOrbitProvider(MusicProvider):
    """Music provider for BookOrbit audiobooks."""

    _client: BookOrbitClient
    _unregister_route: Callable[[], None] | None = None

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature] | None = None,
    ) -> None:
        """Initialize provider caches used for catalog, browse, and streaming."""
        super().__init__(mass, manifest, config, supported_features)
        self._catalog: dict[str, dict[str, Any]] = {}
        self._catalog_refreshed_at: float = 0.0
        self._catalog_lock = asyncio.Lock()
        self._force_catalog_refresh = False
        # Book ids whose detail fetch failed on the last catalog refresh. Artist sync
        # must treat its listing as incomplete while this is non-empty (TTL reuse).
        self._catalog_detail_failed_ids: set[str] = set()
        # session_id -> grant; active playback sessions are not TTL-pruned.
        self._stream_sessions: dict[str, _StreamGrant] = {}
        self._active_stream_sessions: set[str] = set()
        self._progress_locks: dict[int, asyncio.Lock] = {}
        self._progress_generation: dict[int, int] = {}
        self._library_names: dict[int, str] = {}
        self._library_books: dict[int, list[str]] = {}
        self._author_meta: dict[int, dict[str, Any]] = {}
        self._catalog_detail_tasks: set[asyncio.Task[Any]] = set()
        # book id -> last ProviderMapping.details applied to the MA library (updatedAt)
        self._applied_mapping_details: dict[str, str] = {}
        # Fingerprints from the catalog snapshot used by the active audiobook sync listing.
        # Commit must not read live `_catalog` (browse/search may replace it mid-sync).
        self._sync_catalog_fingerprints: dict[str, str] | None = None
        # Audiobook provider ids that failed during the active sync_library run.
        self._failed_audiobook_ids: set[str] = set()

    async def handle_async_init(self) -> None:
        """Create the BookOrbit client from config, log in, and register the stream proxy."""
        self.base_url = normalize_base_url(str(self.get_setup_value(CONF_URL)))
        verify_ssl = bool(self.get_setup_value(CONF_VERIFY_SSL, True))
        http_session = create_clientsession(
            self.mass,
            verify_ssl=verify_ssl,
            cookie_jar=DummyCookieJar(),
            timeout=AIOHTTP_TIMEOUT,
        )
        self._client = BookOrbitClient(
            base_url=self.base_url,
            username=str(self.get_setup_value(CONF_USERNAME)),
            password=str(self.get_setup_value(CONF_PASSWORD)),
            verify_ssl=verify_ssl,
            session=http_session,
            owns_session=True,
        )
        try:
            await self._client.login()
        except BookOrbitAuthError as err:
            await self._client.close()
            raise LoginFailed(f"Login to BookOrbit at {self.base_url} failed.") from err
        except (ClientError, TimeoutError) as err:
            await self._client.close()
            raise ProviderUnavailableError(f"BookOrbit at {self.base_url} is unreachable.") from err
        except asyncio.CancelledError:
            # MA may cancel init before the client's longer HTTP timeout; close the
            # owned session because the provider is not registered for unload yet.
            await self._client.close()
            raise
        # Proxy audio through MA so stream URLs never carry auth tokens.
        self._unregister_route = self.mass.streams.register_dynamic_route(
            f"/{self.instance_id}_part_stream", self._handle_part_request
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Unload the provider and close its BookOrbit client."""
        await super().unload(is_removed)
        pending = list(self._catalog_detail_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._catalog_detail_tasks.clear()
        self._stream_sessions.clear()
        self._active_stream_sessions.clear()
        if unregister := self._unregister_route:
            self._unregister_route = None
            unregister()
        if client := getattr(self, "_client", None):
            await client.close()

    async def resolve_image(self, path: str) -> str | bytes | None:
        """Fetch a BookOrbit image using the provider credentials when needed."""
        if not path.startswith(f"{self.base_url}/"):
            return path
        try:
            return await self._client.get_cover(path)
        except (ClientError, BookOrbitAuthError) as err:
            self.logger.debug("Fetching BookOrbit cover %s failed: %s", path, err)
            return None

    @property
    def is_streaming_provider(self) -> bool:
        """Return False: each BookOrbit instance has its own library catalog."""
        return False

    @property
    def supported_artist_types(self) -> set[ArtistType]:
        """Return the artist types this provider supports."""
        return {ArtistType.AUTHOR, ArtistType.NARRATOR}

    async def sync_library(self, media_type: MediaType) -> None:
        """Sync library items, refreshing the catalog under the active sync state."""
        # Refresh inside get_library_* so skipped-item protection lands in SyncRunState.
        self._force_catalog_refresh = media_type == MediaType.AUDIOBOOK
        self._failed_audiobook_ids = set()
        if media_type == MediaType.AUDIOBOOK:
            self._sync_catalog_fingerprints = None
            await self._load_applied_mapping_details()
        try:
            await super().sync_library(media_type)
        finally:
            self._force_catalog_refresh = False
        if media_type == MediaType.AUDIOBOOK:
            self._commit_applied_mapping_details(self._failed_audiobook_ids)
            await self._save_applied_mapping_details()
            self._sync_catalog_fingerprints = None

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook]:
        """Yield audiobooks from the current catalog."""
        await self._refresh_catalog(force=self._force_catalog_refresh)
        # Browse/search may refresh the catalog with no sync running. Re-report those
        # failures when a later sync reuses the catalog so deletions do not remove them.
        if self._catalog_detail_failed_ids and SYNC_RUN_STATE.get() is not None:
            for book_id in self._catalog_detail_failed_ids:
                self.report_skipped_sync_item(
                    MediaType.AUDIOBOOK,
                    book_id,
                    ValueError(f"BookOrbit book detail {book_id} failed on last catalog refresh"),
                )
        # Snapshot before yielding: `_catalog` can change while the parent sync awaits writes.
        books = list(self._catalog.items())
        if SYNC_RUN_STATE.get() is not None:
            self._sync_catalog_fingerprints = {
                book_id: str(book.get("updatedAt") or "") for book_id, book in books
            }
        for _book_id, book in books:
            yield parse_audiobook(
                book, provider_instance_id=self.instance_id, base_url=self.base_url
            )

    async def get_library_artists(self) -> AsyncGenerator[Artist]:
        """Yield authors and narrators from the current catalog."""
        await self._refresh_catalog(force=self._force_catalog_refresh)
        # Authors/narrators are derived only from books that loaded. A partial catalog
        # (including one reused within the TTL) must not drive artist deletions.
        if self._catalog_detail_failed_ids and SYNC_RUN_STATE.get() is not None:
            self.report_skipped_sync_item(
                MediaType.ARTIST,
                None,
                ValueError(
                    "BookOrbit catalog incomplete after "
                    f"{len(self._catalog_detail_failed_ids)} book detail failure(s)"
                ),
            )
        for artist in self._catalog_artists().values():
            yield artist

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """
        Return an author or narrator by provider id.

        :param prov_artist_id: ``author:<id>`` or ``narrator:<id>``.
        """
        if not prov_artist_id.startswith((AUTHOR_PREFIX, NARRATOR_PREFIX)):
            raise MediaNotFoundError(f"Invalid BookOrbit artist id {prov_artist_id}")
        # Only force when core asks (music/refresh_item → get_provider_item(force_refresh=True)).
        await self._refresh_catalog(force=BYPASS_CACHE.get())
        if artist := self._catalog_artists().get(prov_artist_id):
            return artist
        raise MediaNotFoundError(f"BookOrbit artist {prov_artist_id} not found")

    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Return a single audiobook by BookOrbit book id."""
        try:
            book_id = int(prov_audiobook_id)
            book = await self._client.get_book(book_id)
        except ValueError as err:
            raise MediaNotFoundError(f"Invalid BookOrbit book id {prov_audiobook_id}") from err
        except ClientResponseError as err:
            if err.status == 404:
                raise MediaNotFoundError(f"BookOrbit book {prov_audiobook_id} not found") from err
            raise
        if not _audio_files(book):
            raise MediaNotFoundError(f"BookOrbit book {prov_audiobook_id} has no audio files")
        try:
            book = await self._book_with_playback_order(book)
        except (TypeError, ValueError) as err:
            raise MediaNotFoundError(
                f"BookOrbit book {prov_audiobook_id} has an unusable audio file list"
            ) from err
        return parse_audiobook(book, provider_instance_id=self.instance_id, base_url=self.base_url)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Return stream details for an audiobook.

        :param item_id: BookOrbit book id.
        :param media_type: Must be ``MediaType.AUDIOBOOK``.
        """
        if media_type != MediaType.AUDIOBOOK:
            raise MediaNotFoundError("Stream unknown")
        try:
            book_id = int(item_id)
        except ValueError as err:
            raise MediaNotFoundError(f"Invalid BookOrbit book id {item_id}") from err
        try:
            manifest = await self._client.get_audiobook_manifest(book_id)
        except ClientResponseError as err:
            if err.status == 404:
                raise MediaNotFoundError(f"BookOrbit audiobook {item_id} not found") from err
            # Non-404 HTTP errors must be MA errors so core can try another mapping.
            raise ProviderUnavailableError(
                f"BookOrbit manifest for {item_id} failed: HTTP {err.status}"
            ) from err
        except BookOrbitAuthError as err:
            raise LoginFailed(f"Login to BookOrbit at {self.base_url} failed.") from err
        except (ClientError, TimeoutError) as err:
            raise ProviderUnavailableError(f"BookOrbit at {self.base_url} is unreachable.") from err
        assets = sorted(
            (a for a in (manifest.get("assets") or []) if a.get("assetId")),
            key=lambda asset: int(asset.get("sequence") or 0),
        )
        if not assets:
            raise MediaNotFoundError(f"BookOrbit book {item_id} has no audio assets")
        session_id = self._register_stream_session(
            str(book_id), {str(asset["assetId"]) for asset in assets}
        )
        parts = [
            MultiPartPath(
                path=(
                    f"{self.mass.streams.base_url}/{self.instance_id}_part_stream?"
                    f"session_id={session_id}&book_id={book_id}&asset_id={asset['assetId']}"
                ),
                duration=(
                    float(asset["durationMs"]) / 1000.0
                    if asset.get("durationMs") is not None
                    else None
                ),
            )
            for asset in assets
        ]
        duration = await self._stream_duration(manifest, assets, item_id)
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=ContentType.try_parse(assets[0]["format"])),
            media_type=media_type,
            stream_type=StreamType.HTTP,
            duration=duration,
            path=parts[0].path if len(parts) == 1 else parts,
            can_seek=True,
            allow_seek=True,
            expiration=STREAMDETAILS_EXPIRATION_S,
            data={"session_id": session_id},
        )

    async def on_stream_started(self, streamdetails: StreamDetails) -> None:
        """Mark the proxied stream session active so part grants outlive streamdetails TTL."""
        if (session_id := _session_id_from_streamdetails(streamdetails)) is None:
            return
        if session_id in self._stream_sessions:
            self._active_stream_sessions.add(session_id)

    async def on_streamed(self, streamdetails: StreamDetails) -> None:
        """
        Revoke proxy access when a stream consumer ends.

        Also expires the streamdetails so seek/reconnect cannot reuse part URLs after
        the grant is gone (MA may keep unexpired streamdetails on the queue item).
        """
        if (session_id := _session_id_from_streamdetails(streamdetails)) is None:
            return
        self._active_stream_sessions.discard(session_id)
        self._stream_sessions.pop(session_id, None)
        # Force re-resolution: cached details would otherwise keep serving dead part URLs.
        streamdetails.expiration = 0

    async def get_resume_position(
        self, item_id: str, media_type: MediaType
    ) -> tuple[bool, int, datetime | None]:
        """Return resume position from BookOrbit playback state when available."""
        if media_type != MediaType.AUDIOBOOK:
            raise NotImplementedError
        try:
            book_id = int(item_id)
            manifest = await self._client.get_audiobook_manifest(book_id)
            state = await self._client.get_playback_state(book_id)
            if state:
                return parse_resume_position_from_playback_state(manifest, state)
            # Fall back to legacy /audio-progress when playback-state is empty.
            book = await self._get_book(item_id, fresh=True)
            book = book_with_manifest_playback_order(book, manifest)
            progress = await self._client.get_audio_progress(book_id)
            if not progress:
                raise NotImplementedError  # fall back to MA's own position
            # Keep conversion inside this block: ValueError must become NotImplementedError
            # so MusicController can use its saved position.
            return parse_resume_position(_audio_files(book), progress)
        except (
            BookOrbitAuthError,
            ClientError,
            TimeoutError,
            MediaNotFoundError,
            TypeError,
            ValueError,
        ) as err:
            # Auth failures must not abort playback prep; core only suppresses NotImplementedError.
            raise NotImplementedError from err

    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """Report audiobook progress back to BookOrbit."""
        if media_type != MediaType.AUDIOBOOK:
            return
        try:
            book_id = int(prov_item_id)
        except ValueError:
            return
        generation = self._progress_generation.get(book_id, 0) + 1
        self._progress_generation[book_id] = generation
        # Stamp before waiting on the lock or any BookOrbit I/O so 409 retries can
        # discard this write if another client saved a newer capturedAt meanwhile.
        captured_at = utc().isoformat().replace("+00:00", "Z")
        try:
            async with self._progress_lock_for(book_id):
                if self._progress_generation.get(book_id) != generation:
                    return  # a newer report superseded this one while waiting
                await self._sync_playback_state(
                    book_id,
                    position=position,
                    fully_played=fully_played,
                    captured_at=captured_at,
                )
        except (ClientError, TimeoutError, BookOrbitAuthError, MediaNotFoundError) as err:
            self.logger.warning("Updating BookOrbit progress of %s failed: %s", prov_item_id, err)

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse BookOrbit libraries, authors, narrators, series, and audiobooks.

        :param path: Browse path for this provider instance.
        """
        try:
            library_id, sub_path = parse_browse_path(path)
        except ValueError:
            return []
        await self._ensure_catalog()
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
        """
        Search audiobooks, authors, and narrators.

        :param search_query: Free-text query.
        :param media_types: Media types to include in the result.
        :param limit: Maximum number of results per media type.
        """
        results = SearchResults()
        query = search_query.strip()
        if not query:
            return results
        await self._ensure_catalog()
        if MediaType.AUDIOBOOK in media_types:
            results.audiobooks = await self._search_audiobooks(query, limit)
        if MediaType.ARTIST in media_types:
            results.artists = await self._search_artists(query, limit)
        return results

    def _library_item_needs_update(
        self, library_item: MediaItemType | LibraryItemSyncDetails, prov_item: MediaItemType
    ) -> bool:
        """Return whether a library item should be refreshed from the provider item."""
        if super()._library_item_needs_update(library_item, prov_item):
            return True
        # Core audiobook/artist sync passes LibraryItemSyncDetails without mapping.details.
        # Fall back to last-applied fingerprints from mass.cache (missing key => refresh).
        lib_details = self._provider_mapping_details(library_item)
        prov_details = self._provider_mapping_details(prov_item)
        return bool(
            lib_details is None
            and isinstance(prov_item, Audiobook)
            and prov_details is not None
            and self._applied_mapping_details.get(prov_item.item_id) != prov_details
        )

    async def _load_applied_mapping_details(self) -> None:
        """Load last-applied audiobook mapping fingerprints from mass.cache."""
        cached = await self.mass.cache.get(
            key=CACHE_KEY_LIBRARY_FINGERPRINTS,
            provider=self.instance_id,
            category=CACHE_CATEGORY_LIBRARY_FINGERPRINTS,
            default=None,
        )
        if isinstance(cached, dict):
            self._applied_mapping_details = {
                str(item_id): str(details) for item_id, details in cached.items()
            }
        else:
            self._applied_mapping_details = {}

    def _commit_applied_mapping_details(self, skipped_ids: set[str]) -> None:
        """Record fingerprints for books that synced; keep prior values for failures."""
        # Prefer the sync listing snapshot so a concurrent browse/search refresh cannot
        # mark newer versions applied. Core catches per-item write errors, so skipped
        # ids keep their previous fingerprint (or stay absent) for the next sync.
        source = self._sync_catalog_fingerprints
        if source is None:
            source = {
                book_id: str(book.get("updatedAt") or "") for book_id, book in self._catalog.items()
            }
        next_details: dict[str, str] = {}
        for book_id, fingerprint in source.items():
            if book_id in skipped_ids:
                if book_id in self._applied_mapping_details:
                    next_details[book_id] = self._applied_mapping_details[book_id]
                continue
            next_details[book_id] = fingerprint
        self._applied_mapping_details = next_details

    async def _save_applied_mapping_details(self) -> None:
        """Persist last-applied audiobook mapping fingerprints to mass.cache."""
        await self.mass.cache.set(
            key=CACHE_KEY_LIBRARY_FINGERPRINTS,
            provider=self.instance_id,
            category=CACHE_CATEGORY_LIBRARY_FINGERPRINTS,
            data=self._applied_mapping_details,
            expiration=CACHE_EXPIRATION_LIBRARY_FINGERPRINTS,
            persistent=True,
        )

    def _handle_sync_item_failure(
        self, media_type: MediaType, item_ref: str | None, err: Exception
    ) -> None:
        """Track failed audiobook ids so fingerprints are not marked applied."""
        if media_type == MediaType.AUDIOBOOK and item_ref:
            # item_ref is a provider id or a full MA uri (.../audiobook/<id>).
            self._failed_audiobook_ids.add(item_ref.rsplit("/", 1)[-1])
        super()._handle_sync_item_failure(media_type, item_ref, err)

    def _provider_mapping_details(self, item: MediaItemType | LibraryItemSyncDetails) -> str | None:
        """Return this instance's provider-mapping ``details`` fingerprint, if any."""
        for mapping in item.provider_mappings:
            if mapping.provider_instance == self.instance_id:
                return mapping.details
        return None

    async def _sync_playback_state(
        self,
        book_id: int,
        *,
        position: int,
        fully_played: bool,
        captured_at: str,
    ) -> None:
        """Write an absolute Music Assistant position to BookOrbit playback state."""
        manifest = await self._client.get_audiobook_manifest(book_id)
        update = build_playback_state_update(manifest, position=position, fully_played=fully_played)
        if update is None:
            await self._sync_audio_progress_legacy(
                book_id, position=position, fully_played=fully_played
            )
            return
        operation_id = str(uuid.uuid4())
        last_err: ClientResponseError | None = None
        for _attempt in range(3):
            state = await self._client.get_playback_state(book_id)
            # Match BookOrbit client: drop this write if remote progress is newer.
            if _remote_playback_supersedes(state, captured_at):
                self.logger.debug("Discarding superseded BookOrbit progress write for %s", book_id)
                return
            base_revision = (
                int(state["revision"]) if state and state.get("revision") is not None else 0
            )
            try:
                await self._client.put_playback_state(
                    book_id,
                    asset_id=update.asset_id,
                    position_ms=update.position_ms,
                    captured_at=captured_at,
                    operation_id=operation_id,
                    base_revision=base_revision,
                    manifest_revision=update.manifest_revision,
                )
            except ClientResponseError as err:
                last_err = err
                if err.status == 409:
                    continue  # concurrent writer; reload state and maybe discard
                if err.status == 412:
                    # Manifest changed; rebuild asset/offset from a fresh manifest.
                    manifest = await self._client.get_audiobook_manifest(book_id)
                    update = build_playback_state_update(
                        manifest, position=position, fully_played=fully_played
                    )
                    if update is None:
                        await self._sync_audio_progress_legacy(
                            book_id, position=position, fully_played=fully_played
                        )
                        return
                    continue
                raise
            self.logger.debug(
                "Synced BookOrbit playback state for %s at %ss (asset %s)",
                book_id,
                position,
                update.asset_id,
            )
            return
        if last_err is not None:
            raise last_err

    async def _sync_audio_progress_legacy(
        self, book_id: int, *, position: int, fully_played: bool
    ) -> None:
        """Write progress using the legacy BookOrbit audio-progress endpoint."""
        book = await self._get_book(str(book_id), fresh=True)
        try:
            manifest = await self._client.get_audiobook_manifest(book_id)
            book = book_with_manifest_playback_order(book, manifest)
        except (ClientResponseError, TypeError, ValueError) as err:
            self.logger.debug(
                "BookOrbit manifest unavailable for legacy progress of %s: %s", book_id, err
            )
            # Multipart BookDetail omits sortOrder; refuse unordered absolute→file mapping.
            if len(_audio_files(book)) > 1:
                return
        files = _audio_files(book)
        try:
            legacy = build_progress_update(files, position=position, fully_played=fully_played)
        except ValueError:
            self.logger.debug(
                "Skipping BookOrbit progress for %s: missing durationSeconds on a file",
                book_id,
            )
            return
        if legacy is None:
            return
        await self._client.upsert_audio_progress(
            book_id,
            percentage=legacy.percentage,
            current_file_id=legacy.current_file_id,
            position_seconds=legacy.position_seconds,
        )

    async def _get_book(self, item_id: str, *, fresh: bool = False) -> dict[str, Any]:
        """Return book detail from the catalog, or fetch it when ``fresh`` is set."""
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
        """Proxy one audiobook asset to the player with auth and Range support."""
        try:
            session_id = str(request.query["session_id"])
            book_id = str(int(request.query["book_id"]))
            asset_id = str(request.query["asset_id"])
        except KeyError, ValueError:
            return web.Response(status=400, text="Missing or invalid session_id/book_id/asset_id")
        if not asset_id or not self._stream_session_allows(session_id, book_id, asset_id):
            return web.Response(status=404, text="Unknown asset")
        try:
            upstream = await self._client.open_file(
                int(book_id),
                asset_id,
                range_header=request.headers.get("Range"),
                timeout=PART_STREAM_TIMEOUT,
            )
        except (ClientError, BookOrbitAuthError, TimeoutError) as err:
            self.logger.warning(
                "Requesting asset %s of book %s from BookOrbit failed: %s",
                asset_id,
                book_id,
                err,
            )
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
                self.logger.debug("Client disconnected from asset %s", asset_id)
            except TimeoutError as err:
                self.logger.warning("BookOrbit interrupted asset %s: %s", asset_id, err)
            except ClientError as err:
                self.logger.warning("BookOrbit interrupted asset %s: %s", asset_id, err)
        return response

    def _progress_lock_for(self, book_id: int) -> asyncio.Lock:
        """Return the per-book lock used to serialize progress reports."""
        if (lock := self._progress_locks.get(book_id)) is None:
            lock = asyncio.Lock()
            self._progress_locks[book_id] = lock
        return lock

    def _register_stream_session(self, book_id: str, asset_ids: set[str]) -> str:
        """Create a proxy session for assets from a fresh get_stream_details call."""
        self._prune_stream_sessions()
        session_id = uuid.uuid4().hex
        self._stream_sessions[session_id] = _StreamGrant(
            book_id=book_id,
            asset_ids=frozenset(asset_ids),
            created_at=time.monotonic(),
        )
        return session_id

    def _stream_session_allows(self, session_id: str, book_id: str, asset_id: str) -> bool:
        """Return whether the proxy may serve this asset for the given session."""
        self._prune_stream_sessions()
        grant = self._stream_sessions.get(session_id)
        return grant is not None and grant.book_id == book_id and asset_id in grant.asset_ids

    def _prune_stream_sessions(self) -> None:
        """Drop never-started sessions whose streamdetails would already have expired."""
        now = time.monotonic()
        stale = [
            session_id
            for session_id, grant in self._stream_sessions.items()
            if session_id not in self._active_stream_sessions
            and now - grant.created_at >= STREAMDETAILS_EXPIRATION_S
        ]
        for session_id in stale:
            del self._stream_sessions[session_id]

    async def _stream_duration(
        self,
        manifest: dict[str, Any],
        assets: list[dict[str, Any]],
        item_id: str,
    ) -> int | None:
        """Return audiobook duration only when every part (or a book total) is known."""
        if all(asset.get("durationMs") is not None for asset in assets):
            total_ms = manifest.get("totalDurationMs")
            if total_ms is not None:
                return round(float(total_ms) / 1000.0)
            return round(sum(int(asset["durationMs"]) for asset in assets) / 1000.0)
        # Partial measured totals understate length; prefer an authoritative book duration.
        # Translate fetch failures like get_stream_details so core can try another mapping.
        try:
            book = await self._get_book(item_id, fresh=True)
        except ClientResponseError as err:
            if err.status == 404:
                raise MediaNotFoundError(f"BookOrbit book {item_id} not found") from err
            raise ProviderUnavailableError(
                f"BookOrbit book {item_id} failed: HTTP {err.status}"
            ) from err
        except BookOrbitAuthError as err:
            raise LoginFailed(f"Login to BookOrbit at {self.base_url} failed.") from err
        except (ClientError, TimeoutError) as err:
            raise ProviderUnavailableError(f"BookOrbit at {self.base_url} is unreachable.") from err
        parsed = parse_audiobook(
            book, provider_instance_id=self.instance_id, base_url=self.base_url
        ).duration
        return parsed if parsed else None

    async def _ensure_catalog(self) -> None:
        """Ensure a catalog is loaded (and TTL-fresh) before browse or search."""
        await self._refresh_catalog()

    async def _refresh_catalog(self, *, force: bool = False) -> None:
        """Refresh the in-memory catalog, coalescing concurrent callers."""
        requested_at = time.monotonic()
        async with self._catalog_lock:
            if self._catalog_refreshed_at and (
                self._catalog_refreshed_at >= requested_at
                or (not force and (time.monotonic() - self._catalog_refreshed_at) < CATALOG_TTL_SEC)
            ):
                return  # refreshed while waiting, or still fresh
            await self._fetch_catalog()

    async def _fetch_catalog(self) -> None:
        """Fetch audiobook details and author metadata from BookOrbit."""
        try:
            libraries = await self._client.list_libraries()
            library_names: dict[int, str] = {}
            library_books: dict[int, list[str]] = {}
            # Collect unique cards first, then fetch missing details concurrently.
            cards_by_id: dict[str, dict[str, Any]] = {}
            for library in libraries:
                if not library_may_contain_audio(library):
                    continue
                members: list[str] = []
                for card in await self._client.query_audiobooks(library_id=library["id"]):
                    book_id = str(card["id"])
                    cards_by_id.setdefault(book_id, card)
                    members.append(book_id)
                if members:
                    library_names[library["id"]] = str(library.get("name") or library["id"])
                    library_books[library["id"]] = members
            catalog, failed_ids = await self._load_catalog_books(cards_by_id)
            # Drop library memberships that failed detail fetch or have no audio.
            library_books = {
                library_id: [book_id for book_id in members if book_id in catalog]
                for library_id, members in library_books.items()
            }
            library_books = {lid: members for lid, members in library_books.items() if members}
            library_names = {
                lid: name for lid, name in library_names.items() if lid in library_books
            }
            # Book detail authors often omit imageUrl; the authors list carries photos.
            # Fail the refresh if this list is unavailable so we do not treat a transient
            # error as confirmed photo removal (which would rewrite artist mappings).
            author_meta: dict[int, dict[str, Any]] = {}
            for author in await self._client.list_authors():
                if isinstance(author, dict) and "id" in author:
                    author_meta[int(author["id"])] = author
        except BookOrbitAuthError as err:
            raise LoginFailed(f"Login to BookOrbit at {self.base_url} failed.") from err
        except BookOrbitIncompleteError as err:
            # Partial catalog/author lists must not drive sync deletions or bio clears.
            raise ProviderUnavailableError(str(err)) from err
        except ClientResponseError:
            raise
        except ClientError as err:
            raise ProviderUnavailableError(f"BookOrbit at {self.base_url} is unreachable.") from err
        self._catalog = catalog
        self._catalog_detail_failed_ids = failed_ids
        self._library_names = library_names
        self._library_books = library_books
        self._author_meta = author_meta
        self._catalog_refreshed_at = time.monotonic()

    async def _load_catalog_books(
        self, cards_by_id: dict[str, dict[str, Any]]
    ) -> tuple[dict[str, dict[str, Any]], set[str]]:
        """Fetch book details concurrently; return successes and failed book ids."""
        semaphore = asyncio.Semaphore(CATALOG_DETAIL_CONCURRENCY)

        async def _load_book(
            book_id: str, card: dict[str, Any]
        ) -> tuple[str, dict[str, Any] | None, Exception | None]:
            try:
                book = await self._get_book_detail(book_id, card, semaphore)
            except (ClientError, TimeoutError, TypeError, ValueError) as err:
                # Per-book only: ClientError covers HTTP (incl. ClientResponseError);
                # TimeoutError for stalled fetches; TypeError/ValueError for a bad id,
                # missing/invalid manifest, or unusable multipart file list.
                self.logger.warning("BookOrbit book detail %s failed: %s", book_id, err)
                # Only record skips inside an active library sync (deletion protection).
                if SYNC_RUN_STATE.get() is not None:
                    self.report_skipped_sync_item(MediaType.AUDIOBOOK, book_id, err)
                return book_id, None, err
            if book is None or not _audio_files(book):
                # List claimed audio but detail has none — skip this book; sync skips
                # protect library membership until a later refresh succeeds.
                no_audio = ValueError("BookOrbit book detail has no audio files")
                self.logger.warning("BookOrbit book detail %s has no audio files", book_id)
                if SYNC_RUN_STATE.get() is not None:
                    self.report_skipped_sync_item(MediaType.AUDIOBOOK, book_id, no_audio)
                return book_id, None, no_audio
            return book_id, book, None

        detail_tasks = [
            self.mass.create_task(
                _load_book(book_id, card),
                task_name=f"bookorbit-detail-{book_id}",
                log_exceptions=False,
            )
            for book_id, card in cards_by_id.items()
        ]
        self._catalog_detail_tasks.update(detail_tasks)
        try:
            results = await asyncio.gather(*detail_tasks)
        except BaseException:
            # Unexpected escape (e.g. cancel during unload); stop siblings before unlock.
            for task in detail_tasks:
                task.cancel()
            await asyncio.gather(*detail_tasks, return_exceptions=True)
            raise
        finally:
            self._catalog_detail_tasks.difference_update(detail_tasks)
        failed_ids = {book_id for book_id, book, _ in results if book is None}
        if failed_ids:
            self.logger.warning(
                "BookOrbit catalog refresh skipped %d book(s); continuing with %d",
                len(failed_ids),
                len(results) - len(failed_ids),
            )
        catalog = {book_id: book for book_id, book, _ in results if book is not None}
        return catalog, failed_ids

    async def _get_book_detail(
        self,
        book_id: str,
        card: dict[str, Any],
        semaphore: asyncio.Semaphore,
    ) -> dict[str, Any] | None:
        """Return a BookDetail from cache or a bounded BookOrbit fetch."""
        checksum = card.get("updatedAt")
        cached = await self.mass.cache.get(
            key=book_id,
            provider=self.instance_id,
            category=CACHE_CATEGORY_BOOK,
            checksum=str(checksum) if checksum is not None else None,
            default=None,
        )
        if isinstance(cached, dict):
            return cached
        async with semaphore:
            book = await self._client.get_book(int(card["id"]))
            book = await self._book_with_playback_order(book)
        await self.mass.cache.set(
            key=book_id,
            provider=self.instance_id,
            category=CACHE_CATEGORY_BOOK,
            data=book,
            expiration=CACHE_EXPIRATION_BOOK,
            checksum=str(book.get("updatedAt") or checksum or ""),
        )
        return book

    async def _book_with_playback_order(self, book: dict[str, Any]) -> dict[str, Any]:
        """Attach manifest ``sortOrder`` to multipart books."""
        if len(_audio_files(book)) <= 1:
            return book
        # Do not cache/publish unordered BookDetail when the manifest is unavailable
        # or has no mappable assets: BookDetail omits sortOrder, so file-id order can
        # invent wrong chapter offsets. TypeError/ValueError: callers skip or map to
        # MediaNotFoundError. HTTP errors propagate as ClientError/TimeoutError.
        manifest = await self._client.get_audiobook_manifest(int(book["id"]))
        return book_with_manifest_playback_order(book, manifest)

    def _enrich_author(self, author: dict[str, Any]) -> dict[str, Any]:
        """Overlay list-level author fields onto a (possibly cached) book author."""
        meta = self._author_meta.get(int(author["id"]))
        if not meta:
            return author
        # Prefer /authors list for name/sortName so renames apply even when the
        # book detail is still served from a stale cache entry.
        return {
            **author,
            "name": meta.get("name") or author.get("name"),
            "sortName": meta["sortName"] if "sortName" in meta else author.get("sortName"),
            "imageUrl": author.get("imageUrl") or meta.get("imageUrl"),
            "description": author.get("description") or meta.get("description"),
        }

    def _catalog_artists(self, books: Iterable[dict[str, Any]] | None = None) -> dict[str, Artist]:
        """Return authors and narrators for the catalog or the given books."""
        artists: dict[str, Artist] = {}
        for book in self._catalog.values() if books is None else books:
            for author in _people(book.get("authors")):
                parsed = parse_author(
                    self._enrich_author(author),
                    provider_instance_id=self.instance_id,
                    base_url=self.base_url,
                )
                artists.setdefault(parsed.item_id, parsed)
            for narrator in _people((book.get("audioMetadata") or {}).get("narrators")):
                parsed = parse_narrator(
                    narrator,
                    provider_instance_id=self.instance_id,
                    base_url=self.base_url,
                )
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
        """Build a browse folder for the given path."""
        return BrowseFolder(
            item_id=item_id,
            name=name,
            translation_key=translation_key,
            translation_params=translation_params,
            provider=self.instance_id,
            path=path,
        )

    def _browse_root(self) -> Sequence[BrowseFolder]:
        """List libraries that contain audiobooks."""
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
        """List the sections inside a library."""
        return [
            self._folder(
                f"{current_path}/{BROWSE_ITEMS_TO_PATH[key]}", key.lower(), "", translation_key=key
            )
            for key in BrowseItemsTranslationKey
            if key in BROWSE_ITEMS_TO_PATH
        ]

    def _library_book_details(self, library_id: int) -> list[dict[str, Any]]:
        """Return catalog book details for one library."""
        return [
            self._catalog[book_id]
            for book_id in self._library_books.get(library_id, [])
            if book_id in self._catalog
        ]

    def _browse_people(
        self, current_path: str, library_id: int, prefix: str
    ) -> Sequence[BrowseFolder]:
        """List authors or narrators in a library as folders."""
        people: dict[str, str] = {}
        for artist in self._catalog_artists(self._library_book_details(library_id)).values():
            if artist.item_id.startswith(prefix):
                people[artist.item_id] = artist.name
        return [
            self._folder(f"{current_path}/{item_id}", item_id, name)
            for item_id, name in sorted(people.items(), key=lambda x: x[1].casefold())
        ]

    def _browse_series(self, current_path: str, library_id: int) -> Sequence[BrowseFolder]:
        """List series in a library as folders."""
        series: dict[str, str] = {}
        for book in self._library_book_details(library_id):
            for series_id, name in _book_series(book):
                series.setdefault(series_id, name)
        return [
            self._folder(f"{current_path}/{series_id}", series_id, name)
            for series_id, name in sorted(series.items(), key=lambda x: x[1].casefold())
        ]

    def _browse_book_ids(self, library_id: int, section: str, item_id: str) -> list[str]:
        """Return book ids in a library for one author, narrator, or series."""
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
        """Resolve book ids to library or freshly parsed audiobook items."""
        # Snapshot before awaits: a concurrent catalog refresh can replace `_catalog`.
        known_books = {
            book_id: book
            for book_id in book_ids
            if (book := self._catalog.get(book_id)) is not None
        }
        known_ids = list(known_books)
        library_by_id: dict[str, Audiobook] = {}
        for offset in range(0, len(known_ids), BROWSE_LIBRARY_BATCH_SIZE):
            batch = known_ids[offset : offset + BROWSE_LIBRARY_BATCH_SIZE]
            library_items = await self.mass.music.audiobooks.get_library_items_by_prov_id(
                provider_instance=self.instance_id,
                provider_item_ids=batch,
                limit=0,
            )
            for library_item in library_items:
                for mapping in library_item.provider_mappings:
                    if (
                        mapping.provider_instance == self.instance_id
                        and mapping.item_id in batch
                        and mapping.item_id not in library_by_id
                    ):
                        library_by_id[mapping.item_id] = library_item
        items: list[tuple[Audiobook, str | None]] = []
        for book_id, book in known_books.items():
            item = library_by_id.get(book_id) or parse_audiobook(
                book, provider_instance_id=self.instance_id, base_url=self.base_url
            )
            items.append((item, _series_index(book, series_id)))
        if series_id is not None:
            items.sort(
                key=lambda x: (series_index_sort_key(x[1]), x[0].name.casefold()),
            )
        else:
            items.sort(key=lambda x: x[0].name.casefold())
        return [item for item, _ in items]

    async def _search_audiobooks(self, query: str, limit: int) -> list[Audiobook]:
        """Search audiobooks by query, merging catalog matches so ebooks cannot hide them."""
        needle = query.casefold()
        catalog_ids = [b for b, book in self._catalog.items() if needle in _book_haystack(book)]
        try:
            cards = await self._client.search_books(query)
        except (ClientError, TimeoutError) as err:
            self.logger.debug("BookOrbit book search failed: %s", err)
            ids = catalog_ids
        else:
            # Prefer cards that already list audio files; skip ebook-only detail fetches.
            api_ids = [
                str(card["id"])
                for card in cards
                if isinstance(card, dict)
                and "id" in card
                and (str(card["id"]) in self._catalog or card_may_contain_audio(card))
            ]
            ids = list(dict.fromkeys([*api_ids, *catalog_ids]))
        audiobooks: list[Audiobook] = []
        for book_id in ids:
            if len(audiobooks) >= limit:
                break
            # Resolve from the synced catalog only — search cards are not BookDetail.
            book = self._catalog.get(book_id)
            if book is None:
                continue
            audiobooks.append(
                parse_audiobook(book, provider_instance_id=self.instance_id, base_url=self.base_url)
            )
        return audiobooks

    async def _search_artists(self, query: str, limit: int) -> list[Artist]:
        """Search authors and narrators known from the current catalog."""
        known = self._catalog_artists()
        needle = query.casefold()
        catalog_matches = [a for a in known.values() if needle in a.name.casefold()]
        try:
            authors = await self._client.search_authors(query)
            narrators = await self._client.search_narrators(query)
        except (ClientError, TimeoutError) as err:
            self.logger.debug("BookOrbit author/narrator search failed: %s", err)
            return catalog_matches[:limit]
        found: list[Artist] = []
        for prefix, people in ((AUTHOR_PREFIX, authors), (NARRATOR_PREFIX, narrators)):
            for person in people:
                # Only artists already present in the audiobook catalog can be resolved later.
                if "id" in person:
                    artist = known.get(f"{prefix}{person['id']}")
                elif name := person.get("name"):
                    # /metadata/authors|narrators search returns name-only rows.
                    name_needle = str(name).casefold()
                    artist = next(
                        (
                            a
                            for item_id, a in known.items()
                            if item_id.startswith(prefix) and a.name.casefold() == name_needle
                        ),
                        None,
                    )
                else:
                    continue
                if artist is not None and artist not in found:
                    found.append(artist)
        for artist in catalog_matches:
            if artist not in found:
                found.append(artist)
        return found[:limit]
