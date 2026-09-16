"""Allows scrobbling of tracks with the help of liblistenbrainz."""

# icon.svg from https://github.com/metabrainz/design-system/tree/master/brand/logos
# released under the Creative Commons Attribution-ShareAlike(BY-SA) 4.0 license.
# https://creativecommons.org/licenses/by-sa/4.0/

import logging
import time
from typing import TYPE_CHECKING, ClassVar, Final

import aiohttp
from liblistenbrainz import LISTEN_TYPE_PLAYING_NOW, LISTEN_TYPE_SINGLE, Listen
from music_assistant_models.constants import SECURE_STRING_SUBSTITUTE
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import (
    InvalidToken,
    RateLimited,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
    SetupFailedError,
)

from music_assistant.constants import UNKNOWN_ARTIST
from music_assistant.helpers.scrobbler import ScrobblerConfig, ScrobblerHelper
from music_assistant.helpers.throttle_retry import (
    ThrottlerManager,
    parse_retry_after,
    throttle_with_retries,
)
from music_assistant.mass import MusicAssistant
from music_assistant.models import ProviderInstanceType
from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport
    from music_assistant_models.provider import ProviderManifest

CONF_USER_TOKEN = "_user_token"
CONF_API_BASE_URL = "api_base_url"
LISTENBRAINZ_API_URL = "https://api.listenbrainz.org"
SUPPORTED_FEATURES: set[ProviderFeature] = {ProviderFeature.SCROBBLE}
SUPPORTED_SCROBBLE_MEDIA_TYPES: Final[frozenset[MediaType]] = frozenset({MediaType.TRACK})
# ListenBrainz can accept a connection and then never respond, so every request to it is
# bounded by a finite timeout to keep a hung call from stalling the caller.
_REQUEST_TIMEOUT: Final = aiohttp.ClientTimeout(total=30)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return ListenBrainzScrobbleProvider(mass, manifest, config, SUPPORTED_FEATURES)


class ListenBrainzScrobbleProvider(PluginProvider):
    """Plugin provider to support scrobbling of tracks."""

    _handler: ListenBrainzEventHandler | None = None
    _api_base_url: str
    _token: str

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return tuple(await ScrobblerConfig.get_shared_config_entries(self.mass, None))

    async def handle_async_init(self) -> None:
        """Validate the configured user token so the provider only loads when it can scrobble."""
        token = self.get_setup_value(CONF_USER_TOKEN)
        api_base_url = str(self.get_setup_value(CONF_API_BASE_URL) or LISTENBRAINZ_API_URL).rstrip(
            "/"
        )
        if not token:
            raise SetupFailedError("User token needs to be set")
        assert token != SECURE_STRING_SUBSTITUTE
        await self._validate_token(api_base_url, str(token))
        self._api_base_url = api_base_url
        self._token = str(token)

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()

        self._handler = ListenBrainzEventHandler(
            self.mass, self._api_base_url, self._token, self.logger, self.config
        )

    async def on_media_item_played(self, report: MediaItemPlaybackProgressReport) -> None:
        """Forward a playback progress report to ListenBrainz."""
        if self._handler is not None:
            await self._handler.on_media_item_played(report)

    async def _validate_token(self, api_base_url: str, token: str) -> None:
        """
        Check the configured user token against ListenBrainz.

        :param api_base_url: Base URL of the ListenBrainz API.
        :param token: The user token to validate.
        """
        url = f"{api_base_url.rstrip('/')}/1/validate-token"
        try:
            async with self.mass.http_session.get(
                url,
                headers={"Authorization": f"Token {token}"},
                timeout=_REQUEST_TIMEOUT,
            ) as response:
                response.raise_for_status()
                result = await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            # ValueError covers a malformed JSON body from response.json()
            raise SetupFailedError(f"Unable to connect to ListenBrainz: {err}") from err
        # only an explicit boolean valid=false proves the token is bad; any other
        # shape is a response we can't trust, so keep setup retryable
        valid = result.get("valid") if isinstance(result, dict) else None
        if not isinstance(valid, bool):
            raise SetupFailedError("Unexpected response from ListenBrainz")
        if not valid:
            raise InvalidToken("Invalid ListenBrainz user token")


class ListenBrainzEventHandler(ScrobblerHelper):
    """Submit now-playing updates and listens to ListenBrainz."""

    # A non-2xx reply becomes aiohttp.ClientResponseError via raise_for_status, a request that
    # outlives its timeout raises TimeoutError, a now-playing update dropped on a rate-limit or
    # 5xx reply raises ResourceTemporarilyUnavailable, and a scrobble whose retries are spent
    # raises RetriesExhausted; all are logged and swallowed so a failed submission never takes
    # down the playback report handling.
    scrobble_exceptions: ClassVar[tuple[type[Exception], ...]] = (
        aiohttp.ClientError,
        TimeoutError,
        ResourceTemporarilyUnavailable,
        RetriesExhausted,
    )

    def __init__(
        self,
        mass: MusicAssistant,
        api_base_url: str,
        token: str,
        logger: logging.Logger,
        config: ProviderConfig,
    ) -> None:
        """Initialize."""
        super().__init__(
            logger,
            ScrobblerConfig.create_from_config(config),
            SUPPORTED_SCROBBLE_MEDIA_TYPES,
        )
        self.mass = mass
        self._api_base_url = api_base_url
        self._token = token
        # low submission volume (one per played track), so a modest throttle never bites in
        # normal use; the retries are what matter, backing off on rate-limit and 5xx replies
        self.throttler = ThrottlerManager(rate_limit=1, period=1, retry_attempts=3)

    def _get_artist_name(self, report: MediaItemPlaybackProgressReport) -> str:
        """Return the best available artist name for the ListenBrainz payload."""
        if report.artists:
            return ", ".join(artist for artist in report.artists)
        return report.artist or UNKNOWN_ARTIST

    def _make_listen(self, report: MediaItemPlaybackProgressReport) -> Listen:
        # album artist and track number are not available without an extra API call
        # so they won't be scrobbled

        additional_info = {}

        if report.duration:
            additional_info["duration"] = report.duration

        if report.seconds_played:
            additional_info["duration_played"] = report.seconds_played

        # https://pylistenbrainz.readthedocs.io/en/latest/api_ref.html#class-listen
        return Listen(
            track_name=self.get_name(report),
            artist_name=self._get_artist_name(report),
            artist_mbids=report.artist_mbids,
            release_name=report.album,
            release_mbid=report.album_mbid,
            recording_mbid=report.mbid,
            listening_from="music-assistant",
            additional_info=additional_info or None,
        )

    async def _update_now_playing(self, report: MediaItemPlaybackProgressReport) -> None:
        # a now-playing update is real-time and short-lived, so it is sent once without retries:
        # a later retry would only push a track the listener has already moved past
        await self._post_listen(self._make_listen(report), LISTEN_TYPE_PLAYING_NOW)

    async def _scrobble(self, report: MediaItemPlaybackProgressReport) -> None:
        listen = self._make_listen(report)
        listen.listened_at = int(time.time())
        await self._submit_listen(listen, LISTEN_TYPE_SINGLE)

    @throttle_with_retries
    async def _submit_listen(self, listen: Listen, listen_type: str) -> None:
        """
        Submit a listen to ListenBrainz, retrying rate-limit and server errors with backoff.

        :param listen: The listen to submit, built by :meth:`_make_listen`.
        :param listen_type: The ListenBrainz listen type, e.g. ``single``.
        """
        await self._post_listen(listen, listen_type)

    async def _post_listen(self, listen: Listen, listen_type: str) -> None:
        """
        Post a single listen to ListenBrainz over a bounded, cancellable request.

        :param listen: The listen to submit, built by :meth:`_make_listen`.
        :param listen_type: The ListenBrainz listen type, e.g. ``single`` or ``playing_now``.
        """
        # reuse liblistenbrainz's own payload builder so the wire format stays in sync with the
        # pinned client, but send it over the shared async http session (finite timeout) instead
        # of the library's blocking, timeout-free transport
        body = {"listen_type": listen_type, "payload": [listen._to_submit_payload()]}
        async with self.mass.http_session.post(
            f"{self._api_base_url}/1/submit-listens",
            headers={"Authorization": f"Token {self._token}"},
            json=body,
            timeout=_REQUEST_TIMEOUT,
        ) as response:
            # a rate-limit or 5xx reply is transient — the retrying caller backs off on these;
            # a connection error or timeout is left to propagate and be dropped rather than
            # hammer a service that isn't answering at all
            if response.status == 429:
                raise RateLimited(
                    "ListenBrainz rate limit reached",
                    backoff_time=parse_retry_after(response.headers.get("Retry-After")),
                )
            if response.status >= 500:
                raise ResourceTemporarilyUnavailable(
                    "ListenBrainz is temporarily unavailable",
                    backoff_time=parse_retry_after(response.headers.get("Retry-After")),
                )
            response.raise_for_status()
