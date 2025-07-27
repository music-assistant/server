"""Client module for interacting with the NicoNico API."""

import asyncio
from collections.abc import Callable
from io import StringIO
from typing import Any, TypeVar

import yt_dlp
from music_assistant_models.errors import UnplayableMediaError
from music_assistant_models.media_items import Artist, Playlist, SearchResults, Track
from niconico import NicoNico
from niconico.exceptions import LoginFailureError
from niconico.objects.video import EssentialVideo
from niconico.objects.video.search import EssentialMylist, EssentialSeries

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.niconico.constants import (
    CONF_MFA,
    CONF_USER_SESSION,
    NICONICO_COOKIE_DOMAIN,
)
from music_assistant.providers.niconico.helpers import PlaylistWithTracks, convert_to_netscape
from music_assistant.providers.niconico.parsers import (
    parse_artist,
    parse_playlist_by_mylist,
    parse_playlist_with_tracks_by_mylist,
    parse_track_by_essential_video,
)

T = TypeVar("T")


class NiconicoBaseAdapter:
    """Base adapter for MusicAssistant bridge classes."""

    def __init__(self, adapter: "NicoNicoMusicAssistantAdapter") -> None:
        """Initialize the NiconicoAuthAdapter with a reference to the parent adapter."""
        self.adapter = adapter


class NiconicoAuthAdapter(NiconicoBaseAdapter):
    """Handles authentication and session management for NicoNico."""

    def __init__(self, adapter: "NicoNicoMusicAssistantAdapter") -> None:
        """Initialize the NiconicoAuthAdapter with a reference to the parent adapter."""
        super().__init__(adapter)

    def is_logged_in(self) -> bool:
        """Check if the user is logged in to NicoNico."""
        return self.adapter.niconico_py_client.logined

    async def try_login(self) -> bool:
        """Attempt to login to NicoNico with the configured credentials."""
        if self.is_logged_in():
            return True
        provider = self.adapter.provider
        username = provider.config.get_value(CONF_USERNAME)
        password = provider.config.get_value(CONF_PASSWORD)
        mfa = provider.config.get_value(CONF_MFA)
        user_session = provider.config.get_value(CONF_USER_SESSION)
        max_retries = 3
        retry_delay_seconds = 1
        async with self.adapter.niconico_api_throttler.bypass():
            for attempt in range(max_retries):
                try:
                    self.adapter.logger.info(
                        f"Trying to log in... (Number of attempts: {attempt + 1}/{max_retries})"
                    )
                    if user_session:
                        self.adapter.logger.info("Using user_session for login.")
                        copied_user_session = str(user_session)
                        user_session = None
                        await asyncio.to_thread(
                            self.adapter.niconico_py_client.login_with_session, copied_user_session
                        )
                    else:
                        self.adapter.logger.info("Using mail and password for login.")
                        if not username or not password:
                            self.adapter.logger.info(
                                "Username and password are not set in the configuration."
                            )
                            return False
                        await asyncio.to_thread(
                            self.adapter.niconico_py_client.login_with_mail,
                            str(username),
                            str(password),
                            str(mfa) if mfa else None,
                        )
                    self.adapter.logger.info("Successful login!")
                    self.adapter.mass.config.set_raw_provider_config_value(
                        provider.instance_id,
                        CONF_USER_SESSION,
                        self.adapter.niconico_py_client.get_user_session(),
                        True,
                    )
                    return True
                except LoginFailureError as err:
                    self.adapter.logger.error("Login Failure: %s", err)
                    return False
                except Exception as e:
                    if (
                        "Name or service not known" in str(e)
                        or "Max retries exceeded" in str(e)
                        or "ConnectionError" in str(e)
                    ):
                        self.adapter.logger.warning(
                            f"Network or DNS error occurred: {e}. "
                            f"Retrying in {retry_delay_seconds} seconds..."
                        )
                        await asyncio.sleep(retry_delay_seconds)
                    else:
                        self.adapter.logger.error("An unexpected error has occurred.: %s", e)
                        return False
        self.adapter.logger.error(
            f"Could not login after exceeding the maximum number of retries ({max_retries})."
        )
        return False

    async def try_logout(self) -> None:
        """Log out from the NicoNico service."""
        if self.adapter.niconico_py_client:
            await self.adapter.call_with_throttler(
                self.adapter.niconico_py_client.get, "https://account.nicovideo.jp/logout"
            )
            self.adapter.niconico_py_client = NicoNico()

    def start_periodic_relogin_task(self) -> None:
        """Start the periodic re-login task."""
        self.adapter.mass.create_task(self._schedule_periodic_relogin())

    async def _schedule_periodic_relogin(self) -> None:
        """Periodic re-login every 30 days."""
        while True:
            await asyncio.sleep(30 * 24 * 60 * 60)
            self.adapter.logger.info("Performing periodic re-login to refresh the session.")
            await self.try_logout()
            await self.try_login()


class NiconicoVideoAdapter(NiconicoBaseAdapter):
    """Handles video and stream related operations for NicoNico."""

    def __init__(self, adapter: "NicoNicoMusicAssistantAdapter") -> None:
        """Initialize NiconicoVideoAdapter with reference to parent adapter."""
        super().__init__(adapter)

    async def get_user_videos(
        self, user_id: str, page: int = 1, page_size: int = 50
    ) -> list[Track]:
        """Get user videos and parse as Track list."""
        user_video_data = await self.adapter.call_with_throttler(
            self.adapter.niconico_py_client.user.get_user_videos,
            user_id,
            page=page,
            page_size=page_size,
        )
        if not user_video_data or not user_video_data.items:
            return []
        return [
            parse_track_by_essential_video(self.adapter.provider, item.essential)
            for item in user_video_data.items
        ]

    async def get_video(self, video_id: str) -> Track | None:
        """Get video details and parse as Track."""
        video = await self.adapter.call_with_throttler(
            self.adapter.niconico_py_client.video.get_video, video_id
        )
        return parse_track_by_essential_video(self.adapter.provider, video) if video else None

    async def get_stream_format(self, item_id: str) -> dict[str, Any]:
        """Use yt-dlp to extract the best stream URL from Niconico."""
        netscape_cookie_str = convert_to_netscape(
            self.adapter.niconico_py_client.session.cookies, NICONICO_COOKIE_DOMAIN
        )

        def _extract() -> dict[str, Any]:
            url = f"https://www.nicovideo.jp/watch/{item_id}"
            ydl_opts = {
                "quiet": True,
                "format": "bestaudio/best",
                "nocheckcertificate": True,
                "noplaylist": True,
                "cookiefile": StringIO(netscape_cookie_str),
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    info = ydl.extract_info(url, download=False)
                    best_format = next(
                        (f for f in info["formats"] if f.get("acodec") != "none"), None
                    )
                    if not best_format:
                        raise UnplayableMediaError("No suitable audio stream found")
                    return {
                        "url": best_format["url"],
                        "audio_ext": best_format["ext"],
                        "audio_channels": best_format.get("channels"),
                        "asr": best_format.get("asr"),
                        "cookies": best_format["cookies"],
                        "user_agent": best_format["http_headers"].get("User-Agent", "Mozilla/5.0"),
                    }
                except Exception as err:
                    raise UnplayableMediaError(f"Niconico extract error: {err}") from err

        return await self.adapter.call_with_throttler(_extract)


class NiconicoMylistAdapter(NiconicoBaseAdapter):
    """Handles mylist related operations for NicoNico."""

    def __init__(self, adapter: "NicoNicoMusicAssistantAdapter") -> None:
        """Initialize NiconicoMylistAdapter with reference to parent adapter."""
        super().__init__(adapter)

    async def get_own_mylists(self) -> list[Playlist]:
        """Get own mylists and parse them."""
        results = await self.adapter.call_with_throttler(
            self.adapter.niconico_py_client.user.get_own_mylists
        )
        return [parse_playlist_by_mylist(self.adapter.provider, entry) for entry in results]

    async def get_mylist(
        self, mylist_id: str, page_size: int = 500, page: int = 1
    ) -> PlaylistWithTracks | None:
        """Get mylist details and parse as Playlist."""
        mylist = await self.adapter.call_with_throttler(
            self.adapter.niconico_py_client.video.get_mylist,
            mylist_id,
            page_size=page_size,
            page=page,
        )
        if not mylist:
            return None
        return parse_playlist_with_tracks_by_mylist(self.adapter.provider, mylist)

    async def get_own_mylist(
        self, mylist_id: str, page_size: int = 500, page: int = 1
    ) -> PlaylistWithTracks | None:
        """Get own mylist details and parse as Playlist."""
        mylist = await self.adapter.call_with_throttler(
            self.adapter.niconico_py_client.user.get_own_mylist,
            mylist_id,
            page_size=page_size,
            page=page,
        )
        if not mylist:
            return None
        return parse_playlist_with_tracks_by_mylist(self.adapter.provider, mylist)


class NiconicoSearchAdapter(NiconicoBaseAdapter):
    """Handles search related operations for NicoNico."""

    def __init__(self, adapter: "NicoNicoMusicAssistantAdapter") -> None:
        """Initialize NiconicoSearchAdapter with reference to parent adapter."""
        super().__init__(adapter)

    async def search_playlists_by_keyword(
        self, search_query: str, limit: int, search_result: SearchResults
    ) -> None:
        """Search for playlists by keyword."""
        mylist_search_data = await self.adapter.call_with_throttler(
            self.adapter.niconico_py_client.video.search.search_lists,
            search_query,
            page_size=limit,
            types=["mylist"],
        )
        if mylist_search_data:
            search_result.playlists = []
            item: EssentialMylist | EssentialSeries
            for item in mylist_search_data.items:
                if isinstance(item, EssentialMylist):
                    search_result.playlists.append(
                        parse_playlist_by_mylist(self.adapter.provider, item)
                    )

    async def search_videos_by_keyword(
        self, search_query: str, limit: int, search_result: SearchResults
    ) -> None:
        """Search for videos by keyword."""
        video_search_data = await self.adapter.call_with_throttler(
            self.adapter.niconico_py_client.video.search.search_videos_by_keyword,
            search_query,
            page_size=limit,
        )
        if video_search_data:
            search_result.tracks = []
            for item in video_search_data.items:
                if isinstance(item, EssentialVideo):
                    track = parse_track_by_essential_video(self.adapter.provider, item)
                    if track:
                        search_result.tracks.append(track)


class NicoNicoUserAdapter(NiconicoBaseAdapter):
    """Get user details from NicoNico."""

    def __init__(self, adapter: "NicoNicoMusicAssistantAdapter") -> None:
        """Initialize NicoNicoUserAdapter with reference to parent adapter."""
        super().__init__(adapter)

    async def get_user(self, user_id: str) -> Artist | None:
        """Get user details as Artist."""
        user = await self.adapter.call_with_throttler(
            self.adapter.niconico_py_client.user.get_user, user_id
        )
        return parse_artist(self.adapter.provider, user) if user else None


class NicoNicoMusicAssistantAdapter:
    """Bridge NicoNico API and MusicAssistant."""

    def __init__(self, provider: MusicProvider) -> None:
        """Initialize adapter with provider."""
        self.provider = provider
        self.mass = provider.mass
        self.niconico_py_client = NicoNico()
        self.niconico_api_throttler = ThrottlerManager(rate_limit=1, period=2)
        self.logger = provider.logger.getChild("NicoNicoMusicAssistantAdapter")
        self.auth = NiconicoAuthAdapter(self)
        self.video = NiconicoVideoAdapter(self)
        self.mylist = NiconicoMylistAdapter(self)
        self.search = NiconicoSearchAdapter(self)
        self.user = NicoNicoUserAdapter(self)

    async def call_with_throttler(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Call function with API throttling."""
        async with self.niconico_api_throttler.bypass():
            return await asyncio.to_thread(func, *args, **kwargs)
