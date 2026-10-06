"""Scrobble tracks played through the NetEase Cloud Music provider back to NetEase."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Final

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, MediaType, ProviderFeature
from music_assistant_models.errors import InvalidDataError, ResourceTemporarilyUnavailable

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
# NetEase credits a play once roughly half a minute of a track has been heard,
# matching what the official clients report for skipped tracks
_MIN_PLAY_SECONDS: Final[int] = 30
# a track's album (the scrobble source id) is stable catalog data
_SOURCEID_CACHE_TTL: Final[int] = 60 * 60 * 24 * 30
_CACHE_CATEGORY_SCROBBLE: Final[int] = 1


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

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()
        if self._ncm_provider is not None:
            self._handler = NeteaseScrobbleHandler(self)

    async def on_media_item_played(self, report: MediaItemPlaybackProgressReport) -> None:
        """Forward a playback progress report to NetEase once a source is available."""
        if self._handler is None:
            # the selected NetEase provider may have (re)loaded after this plugin
            self._ncm_provider = self._ncm_provider or self._resolve_ncm_provider()
            if self._ncm_provider is None:
                return
            self._handler = NeteaseScrobbleHandler(self)
        await self._handler.on_media_item_played(report)

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
        # one check-in per play of a track: uri -> seconds_played at check-in time.
        # ScrobblerHelper's own last_scrobbled dedup cannot be used here because it
        # treats any same-uri progress report as a loop restart, which would re-check
        # in on every periodic progress report of a single continuous play.
        self._scrobbled_plays: dict[str, int] = {}
        # uri -> last seen seconds_played, to detect a track starting over (loop)
        self._last_progress: dict[str, int] = {}

    def should_scrobble(self, report: MediaItemPlaybackProgressReport) -> bool:
        """Determine if a track should be checked in on NetEase."""
        last_progress = self._last_progress.get(report.uri)
        if last_progress is not None and report.seconds_played < last_progress:
            # progress went backwards: the track started over (loop/replay),
            # so its previous play is done and may be checked in again
            self._scrobbled_plays.pop(report.uri, None)
        self._last_progress[report.uri] = report.seconds_played
        if report.uri in self._scrobbled_plays:
            # already checked in for this play
            self.logger.debug("skipped check-in: track %s already checked in", report.uri)
            return False
        if not (report.fully_played or report.seconds_played >= _MIN_PLAY_SECONDS):
            return False
        self._scrobbled_plays[report.uri] = report.seconds_played
        if len(self._last_progress) > 128:
            # opportunistic cleanup of long radio sessions
            for uri in list(self._last_progress)[:64]:
                del self._last_progress[uri]
                self._scrobbled_plays.pop(uri, None)
        return True

    async def _scrobble(self, report: MediaItemPlaybackProgressReport) -> None:
        """Scrobble a track to NetEase."""
        resolved = await self._resolve_ncm_track(report)
        if resolved is None:
            return
        track_id, source_id = resolved
        await self._ncm.api_client.get(
            "/scrobble",
            params={"id": track_id, "sourceid": source_id, "time": report.seconds_played},
            cookie=self._ncm.cookie,
        )
        self.logger.info(
            "Checked in track %s to NetEase (source %s, played %ss)",
            track_id,
            source_id,
            report.seconds_played,
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
        for queue_item in self._plugin.mass.player_queues.items(report.player_id, limit=500):
            if queue_item.uri != report.uri:
                continue
            streamdetails = queue_item.streamdetails
            if streamdetails is None:
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
