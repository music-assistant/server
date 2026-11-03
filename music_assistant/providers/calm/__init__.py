"""
Calm music provider for Music Assistant.

Exposes the Calm catalog (sleep stories, meditations, soundscapes, music) through the
unofficial calm-client library:

- a Calm *program* (album, course, series, playlist) maps to an Album,
- a Calm *guide* (single session/story/track) maps to a Track,
- a narrator or composer maps to an Artist,
- Calm feeds and packs are exposed as browse folders.

Playback requires a Calm login; the provider only streams items Calm reports as
unlocked for the logged-in account.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any

from calm_client import CalmRateLimitError
from calm_client import models as calm
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    AlbumType,
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import LoginFailed, MediaNotFoundError, RateLimited
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    ProviderMapping,
    RecommendationFolder,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import (
    CONF_ENTRY_UNOFFICIAL_PROVIDER,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from calm_client import CalmClient
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.RECOMMENDATIONS,
}

# setup_data key holding the (encrypted) token set returned by Calm's login
CONF_CREDENTIALS = "credentials"
CONF_AUDIO_QUALITY = "audio_quality"

# top-level Calm feeds shown when browsing the provider: (feed slug, display name)
ROOT_FEEDS: tuple[tuple[str, str], ...] = (
    ("sleep", "Sleep"),
    ("meditate", "Meditate"),
    ("music", "Music"),
    ("work", "For Work"),
    ("wisdom", "Wisdom"),
    ("kids", "Kids"),
    ("movement", "Movement"),
)

CACHE_TTL_CATALOG = 3600 * 24
CACHE_TTL_FEED = 3600 * 3


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return CalmProvider(mass, manifest, config, SUPPORTED_FEATURES)


class CalmProvider(MusicProvider):
    """Music provider for the Calm catalog."""

    _client: CalmClient
    throttler: ThrottlerManager = ThrottlerManager(
        rate_limit=2,  # requests per period seconds
        period=1,
        initial_backoff=5,
        retry_attempts=3,
    )

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return config entries for this provider (credentials come from the setup flow)."""
        return (
            CONF_ENTRY_UNOFFICIAL_PROVIDER,
            ConfigEntry(
                key=CONF_AUDIO_QUALITY,
                type=ConfigEntryType.STRING,
                options=[ConfigValueOption(quality.value) for quality in calm.AudioQuality],
                default_value=calm.AudioQuality.COMPACT.value,
            ),
        )

    async def handle_async_init(self) -> None:
        """Log in to Calm, reusing stored tokens where possible."""
        from calm_client import CalmAuthError, CalmClient, Credentials  # noqa: PLC0415

        stored = self.get_setup_value(CONF_CREDENTIALS)
        if isinstance(stored, str) and stored:
            try:
                self._client = CalmClient(Credentials.model_validate_json(stored))
                # cheap authenticated call; also refreshes the tokens when they expired
                await self._client.get_favorites()
                self._store_credentials()
                return
            except CalmAuthError:
                self.logger.info("Stored Calm session is no longer valid, logging in again")
                await self._client.close()
            except Exception as err:
                # never block startup on the probe itself; fall back to a fresh login
                self.logger.debug("Could not verify stored Calm session: %s", err)
                await self._client.close()
        await self._login()

    async def unload(self, is_removed: bool = False) -> None:
        """Close the HTTP client on unload."""
        if client := getattr(self, "_client", None):
            await client.close()
        await super().unload(is_removed)

    async def get_album(self, prov_album_id: str) -> Album:
        """Get a Calm program as an album."""
        program = calm.Program.model_validate(await self._program_data(prov_album_id))
        return self._parse_program(program)

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get the guides of a Calm program as album tracks."""
        program = calm.Program.model_validate(await self._program_data(prov_album_id))
        album = self._parse_program(program)
        return [self._parse_guide(guide, program, album) for guide in program.sorted_guides]

    async def get_track(self, prov_track_id: str) -> Track:
        """Get a single Calm guide as a track."""
        guide, program = await self._resolve_guide(prov_track_id)
        return self._parse_guide(guide, program, self._parse_program(program))

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get a narrator/composer as an artist (Calm has no standalone artist lookup)."""
        return self._parse_artist(prov_artist_id, prov_artist_id)

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """Yield the programs in the user's Calm favorites."""
        for item in await self._call(self._client.get_favorites):
            if item.media_type is calm.MediaType.ALBUM:
                yield self._parse_feed_program(item)

    async def get_library_tracks(self) -> AsyncGenerator[Track]:
        """Yield the guides in the user's Calm favorites."""
        for item in await self._call(self._client.get_favorites):
            if item.media_type is calm.MediaType.TRACK:
                yield self._parse_feed_guide(item)

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Search the Calm catalog."""
        result = SearchResults()
        response = await self._call(lambda: self._client.search(search_query))
        albums: list[Album] = []
        tracks: list[Track] = []
        for item in response.items:
            if item.media_type is calm.MediaType.ALBUM:
                albums.append(self._parse_feed_program(item))
            elif item.media_type is calm.MediaType.TRACK:
                tracks.append(self._parse_feed_guide(item))
        if MediaType.ALBUM in media_types:
            result.albums = albums[:limit]
        if MediaType.TRACK in media_types:
            result.tracks = tracks[:limit]
        return result

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse Calm feeds and packs.

        Paths: ``<instance>://`` (root feeds), ``<instance>://feed/<feed_id>`` (packs of a
        feed) and ``<instance>://pack/<pack_class>`` (items of a pack).

        :param path: The path to browse.
        """
        root = path.split("://", 1)[0] + "://"
        subpath = path.split("://", 1)[1] if "://" in path else ""
        kind, _, ident = subpath.partition("/")
        if not kind:
            return [self._folder(root, f"feed/{feed_id}", name) for feed_id, name in ROOT_FEEDS]
        if kind == "feed" and ident:
            feed = calm.FeedResponse.model_validate(await self._feed_data(ident))
            return self._browse_feed(root, feed)
        if kind == "pack" and ident:
            pack = calm.Pack.model_validate(await self._pack_data(ident))
            return self._parse_pack_items(pack.items, root)
        return await super().browse(path)

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Expose the packs of Calm's home feed as recommendation rows."""
        feed = calm.FeedResponse.model_validate(await self._feed_data("home"))
        return [
            RecommendationFolder(
                item_id=f"pack/{pack.pack_class}",
                provider=self.instance_id,
                name=pack.title,
            )
            for pack in feed.packs
            if pack.pack_class and pack.title and pack.has_playable_items
        ]

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """Return the items of a recommendation row (a Calm pack)."""
        pack_class = item_id.removeprefix("pack/")
        pack = calm.Pack.model_validate(await self._pack_data(pack_class))
        return UniqueList(self._parse_pack_items(pack.items, f"{self.instance_id}://"))

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the stream details for a guide."""
        guide, program = await self._resolve_guide(item_id)
        if not calm.is_available(guide, program):
            raise MediaNotFoundError(f"Calm guide {item_id} is locked for this account")
        asset = guide.pick_audio(self._audio_quality)
        if not asset:
            raise MediaNotFoundError(f"Calm guide {item_id} has no audio")
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=self._content_type(asset.content_type, asset.url)
            ),
            media_type=MediaType.TRACK,
            stream_type=StreamType.HTTP,
            path=asset.url,
            duration=int(asset.duration) if asset.duration else None,
            allow_seek=True,
            can_seek=True,
        )

    @property
    def _audio_quality(self) -> calm.AudioQuality:
        """Return the audio quality the user configured, compact when unset or unknown."""
        try:
            return calm.AudioQuality(str(self.config.get_value(CONF_AUDIO_QUALITY)))
        except ValueError:
            return calm.AudioQuality.COMPACT

    async def _login(self) -> None:
        """Log in with the configured email and password."""
        from calm_client import CalmAuthError, CalmClient  # noqa: PLC0415

        username = self.get_setup_value(CONF_USERNAME)
        password = self.get_setup_value(CONF_PASSWORD)
        if not username or not password:
            raise LoginFailed("Calm email and password are required")
        self._client = CalmClient()
        try:
            await self._client.login(str(username), str(password))
        except CalmAuthError as err:
            await self._client.close()
            raise LoginFailed(f"Calm login failed: {err}") from err
        self._store_credentials()

    def _store_credentials(self) -> None:
        """Persist the current token set (Calm rotates refresh tokens)."""
        creds = self._client.credentials.model_dump_json(by_alias=True)
        if creds != self.get_setup_value(CONF_CREDENTIALS):
            self._update_setup_data(CONF_CREDENTIALS, creds)

    async def _call[T](self, factory: Callable[[], Awaitable[T]]) -> T:
        """
        Run a calm-client call, logging in again once if the session expired.

        :param factory: Builds a fresh coroutine, so the call can be retried after a login.
        """
        from calm_client import CalmAuthError  # noqa: PLC0415

        try:
            result = await self._send(factory)
        except CalmAuthError:
            await self._client.close()
            await self._login()
            result = await self._send(factory)
        self._store_credentials()
        return result

    @throttle_with_retries
    async def _send[T](self, factory: Callable[[], Awaitable[T]]) -> T:
        """Run a calm-client call through the throttler, backing off when Calm rate-limits."""
        try:
            return await factory()
        except CalmRateLimitError as err:
            raise RateLimited(str(err), backoff_time=int(err.retry_after or 0)) from err

    # The cached fetchers return plain dicts because the cache serializes to JSON; callers
    # rebuild the calm-client models with model_validate. exclude_unset keeps the lock flags
    # faithful: a program detail carries none, and a default is_unlocked=False would read
    # as "locked".

    @use_cache(CACHE_TTL_CATALOG)
    async def _program_data(self, program_id: str) -> dict[str, Any]:
        """Return a program (with its guides and assets) as a plain dict."""
        program = await self._call(lambda: self._client.get_program(program_id))
        return program.model_dump(mode="json", exclude_unset=True)

    @use_cache(CACHE_TTL_FEED)
    async def _guide_summary_data(self, guide_id: str) -> dict[str, Any]:
        """Return a guide's summary (program id and lock flags) as a plain dict."""
        summary = await self._call(lambda: self._client.get_guide_summary(guide_id))
        return summary.model_dump(mode="json", exclude_unset=True)

    @use_cache(CACHE_TTL_FEED)
    async def _feed_data(self, category: str) -> dict[str, Any]:
        """Return a category feed (its packs and tags) as a plain dict."""
        feed = await self._call(lambda: self._client.get_category_feed(category))
        return feed.model_dump(mode="json", exclude_unset=True)

    @use_cache(CACHE_TTL_FEED)
    async def _pack_data(self, pack_class: str) -> dict[str, Any]:
        """Return a pack with all of its items as a plain dict."""
        pack = await self._call(lambda: self._client.get_pack(pack_class))
        return pack.model_dump(mode="json", exclude_unset=True)

    async def _resolve_guide(self, guide_id: str) -> tuple[calm.Guide, calm.Program]:
        """
        Return the guide and the program it belongs to.

        The program detail carries the audio assets but no lock flags, so the flags from the
        guide summary are copied onto the program, as CalmClient.get_guide does.
        """
        summary = calm.GuideSummary.model_validate(await self._guide_summary_data(guide_id))
        program = calm.Program.model_validate(
            await self._program_data(summary.program_id or summary.id)
        )
        for flag in ("is_unlocked", "is_free"):
            if flag in summary.model_fields_set:
                setattr(program, flag, getattr(summary, flag))
        for guide in program.guides:
            if guide.id == guide_id:
                return guide, program
        raise MediaNotFoundError(f"Calm guide {guide_id} not found in its program")

    def _browse_feed(
        self, root: str, feed: calm.FeedResponse
    ) -> list[MediaItemType | ItemMapping | BrowseFolder]:
        """List a feed's packs that hold playable content, then its tag sub-feeds."""
        result: list[MediaItemType | ItemMapping | BrowseFolder] = []
        for pack in feed.packs:
            if pack.pack_class and pack.title and pack.has_playable_items:
                result.append(self._folder(root, f"pack/{pack.pack_class}", pack.title))
        for tag in feed.tags:
            if tag.title and tag.path:
                result.append(self._folder(root, tag.path, tag.title))
        return result

    def _parse_pack_items(
        self, items: list[calm.PackItem], root: str
    ) -> list[MediaItemType | ItemMapping | BrowseFolder]:
        """Map the items of a pack to tracks, albums and sub-folders."""
        result: list[MediaItemType | ItemMapping | BrowseFolder] = []
        for item in items:
            if item.media_type is calm.MediaType.TRACK:
                result.append(self._parse_feed_guide(item))
            elif item.media_type is calm.MediaType.ALBUM:
                result.append(self._parse_feed_program(item))
            elif item.media_type is calm.MediaType.FOLDER and item.path:
                result.append(self._folder(root, item.path, item.title))
        return result

    def _folder(self, root: str, subpath: str, name: str | None) -> BrowseFolder:
        """Build a browse folder below this provider."""
        return BrowseFolder(
            item_id=subpath,
            provider=self.instance_id,
            path=f"{root}{subpath}",
            name=name or subpath,
        )

    def _mapping(self, item_id: str, available: bool = True, **kwargs: Any) -> ProviderMapping:
        """Build the provider mapping of an item."""
        return ProviderMapping(
            item_id=item_id,
            provider_domain=self.domain,
            provider_instance=self.instance_id,
            available=available,
            **kwargs,
        )

    def _image(self, url: str | None) -> MediaItemImage | None:
        """Build a thumbnail image for a URL, if there is one."""
        if not url:
            return None
        return MediaItemImage(
            type=ImageType.THUMB,
            path=url,
            provider=self.instance_id,
            remotely_accessible=True,
        )

    def _parse_artist(self, artist_id: str, name: str, image_url: str | None = None) -> Artist:
        """Build an artist for a narrator or composer."""
        artist = Artist(
            item_id=artist_id,
            provider=self.instance_id,
            name=name,
            provider_mappings={self._mapping(artist_id)},
        )
        if image := self._image(image_url):
            artist.metadata.add_image(image)
        return artist

    def _narrator_artist(self, narrator: calm.Narrator | None) -> Artist | None:
        """Build an artist for the narrator or composer credited on an item, if any."""
        if narrator is None:
            return None
        return self._parse_artist(narrator.id or narrator.name, narrator.name, narrator.image_url)

    @staticmethod
    def _content_type(mime: str, url: str = "") -> ContentType:
        """Map an asset's mime type (or, failing that, its URL) to a content type."""
        content_type = ContentType.try_parse(mime) if "/" in mime else ContentType.UNKNOWN
        if content_type == ContentType.UNKNOWN and url:
            content_type = ContentType.try_parse(url)
        return content_type

    def _audio_format(self, asset: calm.Asset | calm.PackItemAsset | None) -> AudioFormat:
        """Return the audio format of an asset, unknown when the item has no audio."""
        if asset is None:
            return AudioFormat(content_type=ContentType.UNKNOWN)
        return AudioFormat(content_type=self._content_type(asset.content_type, asset.url))

    def _parse_program(self, program: calm.Program) -> Album:
        """Parse a full program to an Album."""
        album = Album(
            item_id=program.id,
            provider=self.instance_id,
            name=program.title or program.id,
            album_type=AlbumType.SINGLE if len(program.guides) == 1 else AlbumType.ALBUM,
            provider_mappings={self._mapping(program.id, program.is_available)},
        )
        if artist := self._narrator_artist(program.artist):
            album.artists.append(artist)
        if image := self._image(program.image_url):
            album.metadata.add_image(image)
        if program.description:
            album.metadata.description = program.description
        return album

    def _parse_guide(self, guide: calm.Guide, program: calm.Program, album: Album) -> Track:
        """Parse a guide of a full program to a Track."""
        asset = guide.pick_audio(self._audio_quality)
        track = Track(
            item_id=guide.id,
            provider=self.instance_id,
            name=guide.title or album.name,
            duration=int(guide.duration),
            track_number=guide.position or 0,
            album=album,
            provider_mappings={
                self._mapping(
                    guide.id,
                    asset is not None and calm.is_available(guide, program),
                    audio_format=self._audio_format(asset),
                )
            },
        )
        if artist := self._narrator_artist(guide.artist):
            track.artists.append(artist)
        elif album.artists:
            track.artists = album.artists
        if image := self._image(program.image_url):
            track.metadata.add_image(image)
        return track

    def _parse_feed_program(self, item: calm.PackItem) -> Album:
        """Parse a program entry of a feed, pack or search result to an Album."""
        program_id = str(item.program_id)
        album = Album(
            item_id=program_id,
            provider=self.instance_id,
            name=item.title or program_id,
            provider_mappings={self._mapping(program_id, item.is_available)},
        )
        if artist := self._narrator_artist(item.artist):
            album.artists.append(artist)
        if image := self._image(item.image_url):
            album.metadata.add_image(image)
        if item.subtitle:
            album.metadata.description = item.subtitle
        return album

    def _parse_feed_guide(self, item: calm.PackItem) -> Track:
        """Parse a guide entry of a feed, pack or search result to a Track."""
        guide_id = str(item.guide_id)
        track = Track(
            item_id=guide_id,
            provider=self.instance_id,
            name=item.title or guide_id,
            duration=int(item.duration),
            provider_mappings={
                self._mapping(
                    guide_id,
                    item.is_available,
                    audio_format=self._audio_format(item.pick_audio(self._audio_quality)),
                )
            },
        )
        if item.program_id:
            track.album = ItemMapping(
                item_id=str(item.program_id),
                provider=self.instance_id,
                name=item.subtitle or item.title or "",
                media_type=MediaType.ALBUM,
            )
        if artist := self._narrator_artist(item.artist):
            track.artists.append(artist)
        if image := self._image(item.image_url):
            track.metadata.add_image(image)
        return track
