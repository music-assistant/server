"""Scrobble tracks played through the NetEase Cloud Music provider back to NetEase."""

from __future__ import annotations

from itertools import count
from typing import TYPE_CHECKING, ClassVar, Final

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, MediaType, ProviderFeature
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    ResourceTemporarilyUnavailable,
)

from music_assistant.helpers.scrobbler import ScrobblerConfig, ScrobblerHelper
from music_assistant.mass import MusicAssistant
from music_assistant.models import ProviderInstanceType
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.neteasecloudmusic import NeteaseCloudMusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport
    from music_assistant_models.provider import ProviderManifest


SUPPORTED_FEATURES: Final[set[ProviderFeature]] = {ProviderFeature.SCROBBLE}
SUPPORTED_SCROBBLE_MEDIA_TYPES: Final[frozenset[MediaType]] = frozenset({MediaType.TRACK})

CONF_NCM_PROVIDER: Final[str] = "ncm_provider"

NETEASE_DOMAIN: Final[str] = "neteasecloudmusic"
# a track's album (the scrobble source id) is stable catalog data
_SOURCEID_CACHE_TTL: Final[int] = 60 * 60 * 24 * 30
_CACHE_CATEGORY_SCROBBLE: Final[int] = 1
# queue items are scanned page by page so oversized (synced) queues are covered too
QUEUE_PAGE_SIZE: Final[int] = 500
# the NCM api backend reports this code (surfaced inside the InvalidDataError message)
# when the login cookie is no longer accepted
_SESSION_EXPIRED_CODE: Final[str] = "error code 301 for"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider instance with given configuration."""
    return NeteaseScrobbleProvider(mass, manifest, config, SUPPORTED_FEATURES)


class NeteaseScrobbleProvider(PluginProvider):
    """Plugin provider to scrobble NetEase Cloud Music plays (listening check-in)."""

    _ncm_provider: NeteaseCloudMusicProvider | None = None
    _handler: NeteaseScrobbleHandler | None = None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return the configuration entries for this plugin.

        The only plugin-specific option is which NetEase Cloud Music provider
        instance to report plays for; the shared scrobbler filters (users/players)
        come from ScrobblerConfig.
        """
        options = [
            ConfigValueOption(prov.instance_id, title=prov.name)
            for prov in self.mass.providers
            if isinstance(prov, NeteaseCloudMusicProvider)
        ]
        ncm_entry = ConfigEntry(
            key=CONF_NCM_PROVIDER,
            type=ConfigEntryType.STRING,
            required=True,
            default_value=options[0].value if len(options) == 1 else None,
            options=options,
        )
        return (ncm_entry, *await ScrobblerConfig.get_shared_config_entries(self.mass, None))

    async def handle_async_init(self) -> None:
        """Handle async setup."""
        self._ncm_provider = self._resolve_ncm_provider()
        if self._ncm_provider is None:
            self.logger.warning(
                "No usable NetEase Cloud Music provider instance available yet; "
                "plays will be scrobbled once one becomes available"
            )

    async def on_media_item_played(self, report: MediaItemPlaybackProgressReport) -> None:
        """Forward a playback progress report to NetEase once a source is available."""
        ncm = self._resolve_ncm_provider()
        if ncm is None:
            return
        if self._handler is None or ncm is not self._ncm_provider:
            # the selected provider instance became available or was (re)loaded since
            # the handler was built (e.g. a fresh login): rebuild it so check-ins use
            # the current instance and cookie instead of a stale one
            self._ncm_provider = ncm
            self._handler = NeteaseScrobbleHandler(self)
        try:
            await self._handler.on_media_item_played(report)
        except LoginFailed as err:
            # stop submitting right away: the unload below only runs after a short delay
            self._handler = None
            self.logger.warning("%s, re-authenticate this plugin to resume scrobbling", err)
            self.unload_with_error(err)

    def _resolve_ncm_provider(self) -> NeteaseCloudMusicProvider | None:
        """Return the configured NetEase Cloud Music provider instance, if loaded."""
        selected = str(self.get_setup_value(CONF_NCM_PROVIDER) or "").strip()
        candidates = [
            prov
            for prov in self.mass.providers
            if isinstance(prov, NeteaseCloudMusicProvider) and prov.available
        ]
        if selected:
            return next((prov for prov in candidates if prov.instance_id == selected), None)
        return candidates[0] if len(candidates) == 1 else None


class NeteaseScrobbleHandler(ScrobblerHelper):
    """Submit listening check-ins for NetEase Cloud Music plays."""

    # the NCM api client converts network/timeout errors to these
    scrobble_exceptions: ClassVar[tuple[type[Exception], ...]] = (
        InvalidDataError,
        ResourceTemporarilyUnavailable,
    )

    def __init__(self, plugin: NeteaseScrobbleProvider) -> None:
        """Initialize."""
        super().__init__(
            plugin.logger,
            ScrobblerConfig.create_from_config(plugin.config),
            SUPPORTED_SCROBBLE_MEDIA_TYPES,
        )
        self._plugin = plugin
        # the handler is only built once a provider instance was resolved
        ncm = plugin._ncm_provider
        assert ncm is not None
        self._ncm: NeteaseCloudMusicProvider = ncm
        # one check-in per play of a track.
        # ScrobblerHelper's own last_scrobbled dedup cannot be used here because it
        # treats any same-uri progress report as a loop restart, which would re-check
        # in on every periodic progress report of a single continuous play.
        self._scrobbled_plays: set[str] = set()
        # uri -> last seen seconds_played, to detect a track starting over (loop)
        self._last_progress: dict[str, int] = {}

    def should_scrobble(self, report: MediaItemPlaybackProgressReport) -> bool:
        """
        Determine if a track should be checked in on NetEase.

        A track is only checked in once it has been listened to the end, so the
        reported listen time is the real elapsed time rather than a bare minimum.
        """
        if len(self._last_progress) > 128:
            # opportunistic cleanup of long radio sessions
            for uri in list(self._last_progress)[:64]:
                del self._last_progress[uri]
                self._scrobbled_plays.discard(uri)
        last_progress = self._last_progress.get(report.uri)
        if last_progress is not None and report.seconds_played < last_progress:
            # progress went backwards: the track started over (loop/replay),
            # so its previous play is done and may be checked in again
            self._scrobbled_plays.discard(report.uri)
        self._last_progress[report.uri] = report.seconds_played
        if report.uri in self._scrobbled_plays:
            # already checked in for this play
            self.logger.debug("skipped check-in: track %s already checked in", report.uri)
            return False
        if not report.fully_played:
            # not listened to the end yet: report nothing until it is
            return False
        self._scrobbled_plays.add(report.uri)
        return True

    async def _scrobble(self, report: MediaItemPlaybackProgressReport) -> None:
        """Scrobble a track to NetEase."""
        try:
            resolved = await self._resolve_ncm_track(report)
            if resolved is None:
                return
            track_id, source_id = resolved
            await self._ncm.api_client.get(
                "/scrobble",
                # the login cookie must also travel as a query param: some NCM api
                # backends only attribute the play to the account when it is sent that
                # way, and silently drop it (still answering success) when it is only in
                # the Cookie header. Mirrors the same workaround the NCM music provider
                # applies to its personalized endpoints.
                params={
                    "id": track_id,
                    "sourceid": source_id,
                    "time": report.seconds_played,
                    "cookie": self._ncm.cookie,
                },
                cookie=self._ncm.cookie,
            )
        except InvalidDataError as err:
            if _SESSION_EXPIRED_CODE in str(err):
                raise self._session_expired_error() from err
            raise
        self.logger.info(
            "Checked in track %s to NetEase (source %s, played %ss)",
            track_id,
            source_id,
            report.seconds_played,
        )

    def _session_expired_error(self) -> LoginFailed:
        """Build the error that unloads this plugin until the user logs in again."""
        return LoginFailed(
            "NetEase Cloud Music session is no longer valid",
            translation_key="session_invalid",
            translation_owner=f"provider.{self._plugin.domain}",
        )

    async def _resolve_ncm_track(
        self, report: MediaItemPlaybackProgressReport
    ) -> tuple[str, str] | None:
        """
        Resolve a progress report to the NetEase track and source (album) id.

        Returns None when the play did not stream from the selected NetEase
        provider instance, or the album needed as source id could not be resolved.

        :param report: The playback progress report of the played item.
        """
        track_id: str | None = None

        scheme, _, rest = report.uri.partition("://")
        if scheme in (NETEASE_DOMAIN, self._ncm.instance_id) and rest.startswith("track/"):
            track_id = rest.removeprefix("track/")
        else:
            # library items may have been linked to any provider: only check in
            # when the queue shows the track actually streamed from NetEase
            track_id = await self._lookup_queue_stream_track(report)
        if not track_id:
            return None

        source_id = await self._get_source_id(track_id)
        if not source_id:
            self.logger.debug("Skipping scrobble: no album id found for track %s", track_id)
            return None
        return track_id, source_id

    async def _lookup_queue_stream_track(
        self, report: MediaItemPlaybackProgressReport
    ) -> str | None:
        """Return the NetEase track id when the reported queue item streamed from NetEase."""
        if not report.player_id:
            return None
        # page through the whole queue: a large synced playlist holds more items
        # than a single page, and any of them may be the reported track
        for offset in count(0, QUEUE_PAGE_SIZE):
            queue_items = self._plugin.mass.player_queues.items(
                report.player_id, limit=QUEUE_PAGE_SIZE, offset=offset
            )
            if not queue_items:
                break
            for queue_item in queue_items:
                if queue_item.uri != report.uri:
                    continue
                streamdetails = queue_item.streamdetails
                if streamdetails is None:
                    self.logger.debug(
                        "Skipping scrobble: queue item %s has no streamdetails yet", report.uri
                    )
                    return None
                if streamdetails.provider != self._ncm.instance_id:
                    return None
                return str(streamdetails.item_id)
        self.logger.debug("Skipping scrobble: queue item %s no longer present", report.uri)
        return None

    async def _get_source_id(self, track_id: str) -> str | None:
        """Return the album id of a NetEase track (cached), used as the scrobble source id."""
        cache_key = f"sourceid:{track_id}"
        cached = await self._plugin.mass.cache.get(
            key=cache_key,
            provider=self._plugin.instance_id,
            category=_CACHE_CATEGORY_SCROBBLE,
            default=None,
        )
        if isinstance(cached, str) and cached:
            return cached

        payload = await self._ncm.api_client.get(
            "/song/detail", params={"ids": track_id}, cookie=self._ncm.cookie
        )
        songs = payload.get("songs")
        if not isinstance(songs, list) and isinstance(payload.get("data"), dict):
            songs = payload["data"].get("songs")
        source_id: str | None = None
        if isinstance(songs, list) and songs and isinstance(songs[0], dict):
            album = songs[0].get("al")
            if isinstance(album, dict) and album.get("id"):
                source_id = str(album["id"])
        if source_id is not None:
            await self._plugin.mass.cache.set(
                key=cache_key,
                provider=self._plugin.instance_id,
                category=_CACHE_CATEGORY_SCROBBLE,
                data=source_id,
                expiration=_SOURCEID_CACHE_TTL,
            )
        return source_id
