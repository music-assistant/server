"""Scrobble tracks played through the NetEase Cloud Music provider back to NetEase."""

from __future__ import annotations

from itertools import count
from typing import TYPE_CHECKING, ClassVar, Final

from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import (
    InvalidDataError,
    ResourceTemporarilyUnavailable,
    SetupFailedError,
)

from music_assistant.helpers.provider_access import exact_provider
from music_assistant.helpers.scrobbler import ScrobblerConfig, ScrobblerHelper
from music_assistant.mass import MusicAssistant
from music_assistant.models import ProviderInstanceType
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.neteasecloudmusic import NeteaseCloudMusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport
    from music_assistant_models.provider import ProviderManifest


SUPPORTED_FEATURES: Final[set[ProviderFeature]] = {ProviderFeature.SCROBBLE}
SUPPORTED_SCROBBLE_MEDIA_TYPES: Final[frozenset[MediaType]] = frozenset({MediaType.TRACK})

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
    ncm = mass.get_provider(NETEASE_DOMAIN)
    if not isinstance(ncm, NeteaseCloudMusicProvider):
        raise SetupFailedError("A NetEase Cloud Music source must be configured first.")
    return NeteaseScrobbleProvider(mass, manifest, config, SUPPORTED_FEATURES)


class NeteaseScrobbleProvider(PluginProvider):
    """Plugin provider to scrobble NetEase Cloud Music plays (listening check-in)."""

    _handler: NeteaseScrobbleHandler | None = None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return the configuration entries for this plugin.

        There is no NetEase source to pick: a play is checked in to whichever NetEase
        Cloud Music instance it actually streamed from, so only the shared scrobbler
        filters (users/players) are configurable.
        """
        return tuple(await ScrobblerConfig.get_shared_config_entries(self.mass, None))

    async def handle_async_init(self) -> None:
        """Handle async setup."""
        # this plugin reuses the configured NetEase Cloud Music source(s) rather than a
        # second login: per play it reports through the instance the track actually
        # streamed from, using the read-only api_client/cookie properties those
        # providers expose for companion plugins. The scrobbler counterpart of how
        # subsonic_scrobble rides whichever OpenSubsonic source served the play.
        self._handler = NeteaseScrobbleHandler(self)

    async def on_media_item_played(self, report: MediaItemPlaybackProgressReport) -> None:
        """Forward a playback progress report to NetEase."""
        if self._handler is not None:
            await self._handler.on_media_item_played(report)


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
        # one check-in per play of a track.
        # ScrobblerHelper's own last_scrobbled dedup cannot be used here because it
        # treats any same-uri progress report as a loop restart, which would re-check
        # in on every periodic progress report of a single continuous play.
        # keyed by (player_id, uri): the same track may play on two players at once.
        self._scrobbled_plays: set[tuple[str, str]] = set()
        # (player_id, uri) -> last seen seconds_played, to detect a track starting over
        self._last_progress: dict[tuple[str, str], int] = {}

    def should_scrobble(self, report: MediaItemPlaybackProgressReport) -> bool:
        """
        Determine if a track should be checked in on NetEase.

        A track is only checked in once it has been listened to the end, so the
        reported listen time is the real elapsed time rather than a bare minimum.
        """
        key = (report.player_id or "", report.uri)
        if len(self._last_progress) > 128:
            # opportunistic cleanup of long radio sessions; never drop the key being
            # evaluated here or its replay detection would be lost
            for stale in list(self._last_progress)[:64]:
                if stale == key:
                    continue
                del self._last_progress[stale]
                self._scrobbled_plays.discard(stale)
        last_progress = self._last_progress.get(key)
        if last_progress is not None and report.seconds_played < last_progress:
            # progress went backwards: the track started over (loop/replay),
            # so its previous play is done and may be checked in again
            self._scrobbled_plays.discard(key)
        self._last_progress[key] = report.seconds_played
        if self._in_flight_key(report) in self._scrobbles_in_flight:
            # a check-in for this track is already running on this player
            return False
        if key in self._scrobbled_plays:
            # already checked in for this play
            self.logger.debug("skipped check-in: track %s already checked in", report.uri)
            return False
        # not marked as done here on purpose: only a successful /scrobble marks the
        # play, so a transient api failure is retried on the next completion report
        return bool(report.fully_played)

    def _in_flight_key(self, report: MediaItemPlaybackProgressReport) -> str:
        """Track in-flight check-ins per player, so two players on one track do not clash."""
        return f"{report.player_id or ''}::{report.uri}"

    async def _scrobble(self, report: MediaItemPlaybackProgressReport) -> None:
        """Scrobble a track to the NetEase instance it streamed from."""
        key = (report.player_id or "", report.uri)
        resolved = await self._resolve_ncm_track(report)
        if resolved is None:
            return
        ncm, track_id = resolved
        try:
            source_id = await self._get_source_id(ncm, track_id)
            if not source_id:
                self.logger.debug("Skipping scrobble: no album id found for track %s", track_id)
                return
            await ncm.api_client.get(
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
                    "cookie": ncm.cookie,
                },
                cookie=ncm.cookie,
            )
        except InvalidDataError as err:
            if _SESSION_EXPIRED_CODE in str(err):
                # that one source's login lapsed; other instances may still be fine, so
                # only skip this check-in (the user re-authenticates the source itself)
                self.logger.warning(
                    "NetEase session for instance %s is no longer valid; skipping check-in",
                    ncm.instance_id,
                )
                return
            raise
        self._scrobbled_plays.add(key)
        self.logger.info(
            "Checked in track %s to NetEase (source %s, played %ss)",
            track_id,
            source_id,
            report.seconds_played,
        )

    async def _resolve_ncm_track(
        self, report: MediaItemPlaybackProgressReport
    ) -> tuple[NeteaseCloudMusicProvider, str] | None:
        """
        Resolve a progress report to the NetEase instance that served it and its track id.

        Returns None when the play did not stream from a loaded NetEase instance, or the
        instance that served it can not be resolved.

        :param report: The playback progress report of the played item.
        """
        # prefer the instance the play actually streamed from (the queue item's
        # streamdetails): under failover between accounts of the same service the
        # streaming instance differs from the one the item uri's scheme names
        streamed = await self._lookup_queue_stream_track(report)
        if streamed is not None:
            instance_id, track_id = streamed
        else:
            # no queue item/streamdetails (yet): fall back to the uri's scheme, which is
            # the instance the item sits on
            scheme, _, rest = report.uri.partition("://")
            if not rest.startswith("track/"):
                return None
            instance_id, track_id = scheme, rest.removeprefix("track/")
        ncm = self._ncm_for_instance(instance_id)
        if ncm is None or not track_id:
            return None
        return ncm, track_id

    async def _lookup_queue_stream_track(
        self, report: MediaItemPlaybackProgressReport
    ) -> tuple[str, str] | None:
        """Return (instance_id, track id) when the reported queue item streamed from NetEase."""
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
                if self._ncm_for_instance(str(streamdetails.provider)) is None:
                    return None
                return str(streamdetails.provider), str(streamdetails.item_id)
        self.logger.debug("Skipping scrobble: queue item %s no longer present", report.uri)
        return None

    def _ncm_for_instance(self, instance_id: str) -> NeteaseCloudMusicProvider | None:
        """Return the loaded, available NetEase instance with exactly this instance id."""
        provider = exact_provider(self._plugin.mass, instance_id)
        if isinstance(provider, NeteaseCloudMusicProvider):
            return provider
        return None

    async def _get_source_id(self, ncm: NeteaseCloudMusicProvider, track_id: str) -> str | None:
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

        payload = await ncm.api_client.get(
            "/song/detail", params={"ids": track_id}, cookie=ncm.cookie
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
