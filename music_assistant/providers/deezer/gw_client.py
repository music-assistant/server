"""
A minimal client for the unofficial gw-API, which deezer is using on their website and app.

Credits go out to RemixDev (https://gitlab.com/RemixDev) for figuring out, how to get the arl
cookie based on the api_token.
"""

from __future__ import annotations

from collections.abc import Mapping
from http.cookies import BaseCookie, Morsel
from typing import TYPE_CHECKING, Any, ClassVar, cast

from aiohttp import ClientSession, ClientTimeout
from music_assistant_models.errors import MediaNotFoundError
from yarl import URL

from music_assistant.helpers.datetime import future_timestamp, utc_timestamp

if TYPE_CHECKING:
    from music_assistant_models.streamdetails import StreamDetails

USER_AGENT_HEADER = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/79.0.3945.130 Safari/537.36"
)

GW_LIGHT_URL = "https://www.deezer.com/ajax/gw-light.php"


class DeezerGWError(Exception):
    """Exception type for GWClient related exceptions."""


class GWClient:
    """The GWClient class can be used to perform actions not being of the official API."""

    _arl_token: str
    _gw_csrf_token: str | None
    _license: str | None
    _license_expiration_timestamp: int
    _user_id: int
    session: ClientSession
    formats: list[dict[str, str]]
    user_country: str

    def __init__(self, session: ClientSession, arl_token: str) -> None:
        """Provide an aiohttp ClientSession and the deezer ARL token."""
        self._arl_token = arl_token
        self.session = session
        self.formats = [{"cipher": "BF_CBC_STRIPE", "format": "MP3_128"}]

    async def _set_cookie(self) -> None:
        cookie: Morsel[str] = Morsel()

        cookie.set("arl", self._arl_token, self._arl_token)
        cookie.update({"domain": ".deezer.com", "path": "/", "httponly": "True"})

        self.session.cookie_jar.update_cookies(BaseCookie({"arl": cookie}), URL(GW_LIGHT_URL))

    async def _update_user_data(self) -> None:
        user_data = await self._gw_api_call("deezer.getUserData", False)
        if not user_data["results"]["USER"]["USER_ID"]:
            await self._set_cookie()
            user_data = await self._gw_api_call("deezer.getUserData", False)

        if not user_data["results"]["OFFER_ID"]:
            msg = "Free subscriptions cannot be used in MA. Make sure you set a valid ARL."
            raise DeezerGWError(msg)

        self._gw_csrf_token = user_data["results"]["checkForm"]
        self._user_id = int(user_data["results"]["USER"]["USER_ID"])
        self._license = user_data["results"]["USER"]["OPTIONS"]["license_token"]
        self._license_expiration_timestamp = user_data["results"]["USER"]["OPTIONS"][
            "expiration_timestamp"
        ]
        # Rebuilt on every license refresh, so start from the default list
        formats = [{"cipher": "BF_CBC_STRIPE", "format": "MP3_128"}]
        web_qualities = user_data["results"]["USER"]["OPTIONS"]["web_sound_quality"]
        mobile_qualities = user_data["results"]["USER"]["OPTIONS"]["mobile_sound_quality"]
        if web_qualities["high"] or mobile_qualities["high"]:
            formats.insert(0, {"cipher": "BF_CBC_STRIPE", "format": "MP3_320"})
        if web_qualities["lossless"] or mobile_qualities["lossless"]:
            formats.insert(0, {"cipher": "BF_CBC_STRIPE", "format": "FLAC"})
        self.formats = formats

        self.user_country = user_data["results"]["COUNTRY"]

    async def setup(self) -> None:
        """Call this to let the client get its cookies, license and tokens."""
        await self._set_cookie()
        await self._update_user_data()

    async def _get_license(self) -> str | None:
        if self._license_expiration_timestamp < future_timestamp(days=1):
            await self._update_user_data()
        return self._license

    async def _gw_api_call(
        self,
        method: str,
        use_csrf_token: bool = True,
        args: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        http_method: str = "POST",
        retry: bool = True,
    ) -> dict[str, Any]:
        csrf_token = self._gw_csrf_token if use_csrf_token else "null"
        if params is None:
            params = {}
        parameters = {"api_version": "1.0", "api_token": csrf_token, "input": "3", "method": method}
        parameters |= params
        result = await self.session.request(
            http_method,
            GW_LIGHT_URL,
            params=cast("Mapping[str, str]", parameters),
            timeout=ClientTimeout(total=30),
            json=args,
            headers={"User-Agent": USER_AGENT_HEADER},
        )
        result_json = await result.json()

        if result_json["error"]:
            if retry:
                await self._update_user_data()
                return await self._gw_api_call(
                    method, use_csrf_token, args, params, http_method, False
                )
            msg = "Failed to call GW-API"
            raise DeezerGWError(msg, result_json["error"])
        return cast("dict[str, Any]", result_json)

    # Content support descriptor for page.get — tells the API which module types to return
    _PAGE_SUPPORT: ClassVar[dict[str, Any]] = {
        "grid": ["channel", "album", "playlist", "artist"],
        "horizontal-grid": ["channel", "album", "playlist", "artist"],
        "slideshow": ["album", "playlist"],
        "grid-preview-one": ["album", "playlist"],
        "grid-preview-two": ["album", "playlist"],
        "filterable-grid": ["album", "playlist"],
        "large-card": ["album", "playlist"],
    }

    async def get_page(self, page: str, language: str = "en") -> dict[str, Any]:
        """
        Fetch a content page from the Deezer page.get GW API.

        :param page: The page path (e.g., 'channels/audiobooks').
        :param language: Language code for localized content.
        """
        result = await self._gw_api_call(
            "page.get",
            args={
                "PAGE": page,
                "VERSION": "2.5",
                "SUPPORT": self._PAGE_SUPPORT,
                "LANG": language,
                "OPTIONS": [],
            },
        )
        return cast("dict[str, Any]", result["results"])

    async def get_deezer_track_urls(self, track_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """Get the URL for a given track id."""
        dz_license = await self._get_license()

        song_results = await self._gw_api_call("song.getData", args={"SNG_ID": track_id})

        song_data = song_results["results"]
        # If the song has been replaced by a newer version, the old track will
        # not play anymore. The data for the newer song is contained in a
        # "FALLBACK" entry in the song data. So if that is available, use that
        # instead so we get the right track token.
        if "FALLBACK" in song_data:
            song_data = song_data["FALLBACK"]

        track_token = song_data["TRACK_TOKEN"]
        # Personal songs (user uploads) only support MP3_MISC format
        is_personal = int(track_id) < 0
        formats = (
            [{"cipher": "BF_CBC_STRIPE", "format": "MP3_MISC"}] if is_personal else self.formats
        )
        url_data = {
            "license_token": dz_license,
            "media": [
                {
                    "type": "FULL",
                    "formats": formats,
                }
            ],
            "track_tokens": [track_token],
        }
        url_response = await self.session.post(
            "https://media.deezer.com/v1/get_url",
            json=url_data,
            headers={"User-Agent": USER_AGENT_HEADER},
        )
        result_json = await url_response.json()

        if error := result_json["data"][0].get("errors"):
            error_code = error[0].get("code") if isinstance(error, list) and error else None
            if error_code == 2002:
                msg = f"Track {track_id} not available: insufficient streaming rights"
            else:
                msg = "Received an error from API"
            raise DeezerGWError(msg, error)

        media_list = result_json["data"][0].get("media", [])
        if not media_list:
            raise MediaNotFoundError(f"No media available for track {track_id}")

        return media_list[0], song_data

    async def log_listen(
        self, next_track: str | None = None, last_track: StreamDetails | None = None
    ) -> None:
        """Log the next and/or previous track of the current playback queue."""
        if not (next_track or last_track):
            msg = "last or current track information must be provided."
            raise DeezerGWError(msg)

        payload: dict[str, Any] = {}

        if next_track:
            payload["next_media"] = {"media": {"id": next_track, "type": "song"}}

        if last_track:
            elapsed = utc_timestamp() - last_track.data["start_ts"]
            seconds_streamed = (
                min(elapsed, last_track.seconds_streamed)
                if last_track.seconds_streamed is not None
                else elapsed
            )

            payload["params"] = {
                "media": {
                    "id": last_track.item_id,
                    "type": "song",
                    "format": last_track.data["format"],
                },
                "type": 1,
                "stat": {
                    "seek": 1 if seconds_streamed < last_track.duration else 0,
                    "pause": 0,
                    "sync": 0,
                    "next": bool(next_track),
                },
                "lt": int(seconds_streamed),
                "ctxt": {"t": "search_page", "id": last_track.item_id},
                "dev": {"v": "10020230525142740", "t": 0},
                "ls": [],
                "ts_listen": int(last_track.data["start_ts"]),
                "is_shuffle": False,
                "stream_id": str(last_track.data["stream_id"]),
            }

        await self._gw_api_call("log.listen", args=payload)

    async def get_personal_songs(self, start: int = 0, nb: int = 500) -> dict[str, Any]:
        """
        Get user-uploaded personal songs via the GW API.

        :param start: Offset for pagination.
        :param nb: Number of songs to fetch per page.
        """
        result = await self._gw_api_call(
            "personal_song.getList",
            args={"start": start, "nb": nb},
        )
        return cast("dict[str, Any]", result["results"])
