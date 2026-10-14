"""BookOrbit provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, ClientResponseError
from music_assistant_models.enums import ArtistType, MediaType, ProviderFeature
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
)

from music_assistant.models.music_provider import MusicProvider

from .client import BookOrbitAuthError, BookOrbitClient
from .constants import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from .helpers import library_may_contain_audio, normalize_base_url
from .parsers import (
    AUTHOR_PREFIX,
    NARRATOR_PREFIX,
    _audio_files,
    parse_audiobook,
    parse_author,
    parse_narrator,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.media_items import Artist, Audiobook
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_AUDIOBOOKS,
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.BROWSE,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider instance."""
    return BookOrbitProvider(mass, manifest, config, SUPPORTED_FEATURES)


class BookOrbitProvider(MusicProvider):
    """BookOrbit MusicProvider (audiobooks)."""

    _client: BookOrbitClient

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
        try:
            await self._client.login()
        except BookOrbitAuthError as err:
            raise LoginFailed(f"Login to BookOrbit at {self.base_url} failed.") from err
        except ClientError as err:
            raise ProviderUnavailableError(f"BookOrbit at {self.base_url} is unreachable.") from err

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
            await self._refresh_catalog()
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

    async def _refresh_catalog(self) -> None:
        """Fetch all audiobooks (with details) from the audio-capable libraries."""
        try:
            libraries = await self._client.list_libraries()
            catalog: dict[str, dict[str, Any]] = {}
            for library in libraries:
                if not library_may_contain_audio(library):
                    continue
                for card in await self._client.query_audiobooks(library_id=library["id"]):
                    book_id = str(card["id"])
                    if book_id in catalog:
                        continue
                    try:
                        book = await self._client.get_book(int(card["id"]))
                    except ClientResponseError as err:
                        self.logger.warning("Skipping BookOrbit book %s: %s", book_id, err)
                        self.report_skipped_sync_item(MediaType.AUDIOBOOK, book_id, err)
                        continue
                    # books without audio files (ebook only) are no audiobooks
                    if _audio_files(book):
                        catalog[book_id] = book
        except BookOrbitAuthError as err:
            raise LoginFailed(f"Login to BookOrbit at {self.base_url} failed.") from err
        except ClientResponseError:
            raise
        except ClientError as err:
            raise ProviderUnavailableError(f"BookOrbit at {self.base_url} is unreachable.") from err
        self._catalog = catalog

    def _catalog_artists(self) -> dict[str, Artist]:
        """Return all authors and narrators of the catalog keyed by artist item id."""
        artists: dict[str, Artist] = {}
        for book in self._catalog.values():
            for author in book.get("authors") or []:
                if isinstance(author, dict) and "id" in author:
                    parsed = parse_author(author, provider_instance_id=self.instance_id)
                    artists.setdefault(parsed.item_id, parsed)
            for narrator in (book.get("audioMetadata") or {}).get("narrators") or []:
                if isinstance(narrator, dict) and "id" in narrator:
                    parsed = parse_narrator(narrator, provider_instance_id=self.instance_id)
                    artists.setdefault(parsed.item_id, parsed)
        return artists
