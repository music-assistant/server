"""A minimal client for the unofficial gw-API, which deezer is using on their website and app.

Credits go out to RemixDev (https://gitlab.com/RemixDev) for figuring out, how to get the arl
cookie based on the api_token.
"""
import datetime
from http.cookies import BaseCookie, Morsel

from aiohttp import ClientSession
from yarl import URL

USER_AGENT_HEADER = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/79.0.3945.130 Safari/537.36"
)

GW_LIGHT_URL = "https://www.deezer.com/ajax/gw-light.php"


class DeezerGWError(BaseException):
    """Exception type for GWClient related exceptions."""


class GWClient:
    """The GWClient class can be used to perform actions not being of the official API."""

    _api_token: str
    _gw_csrf_token: str | None
    _license: str | None
    _license_expiration_timestamp: int
    session: ClientSession
    formats: list[dict[str, str]] = [
        {"cipher": "BF_CBC_STRIPE", "format": "MP3_128"},
    ]
    user_country: str

    def __init__(self, session: ClientSession, api_token: str):
        """Provide an aiohttp ClientSession and the deezer api_token."""
        self._api_token = api_token
        self.session = session

    async def _get_cookie(self):
        await self.session.get(
            "https://api.deezer.com/platform/generic/track/3135556",
            headers={"Authorization": f"Bearer {self._api_token}", "User-Agent": USER_AGENT_HEADER},
        )
        json_response = await self._gw_api_call("user.getArl", False, http_method="GET")
        arl = json_response.get("results")

        cookie = Morsel()

        cookie.set("arl", arl, arl)
        cookie.domain = ".deezer.com"
        cookie.path = "/"
        cookie.httponly = {"HttpOnly": True}

        self.session.cookie_jar.update_cookies(BaseCookie({"arl": cookie}), URL(GW_LIGHT_URL))

    async def _update_user_data(self):
        user_data = await self._gw_api_call("deezer.getUserData", False)
        if not user_data["results"]["USER"]["USER_ID"]:
            await self._get_cookie()
            user_data = await self._gw_api_call("deezer.getUserData", False)

        if not user_data["results"]["OFFER_ID"]:
            raise DeezerGWError("Free subscriptions cannot be used in MA.")

        self._gw_csrf_token = user_data["results"]["checkForm"]
        self._license = user_data["results"]["USER"]["OPTIONS"]["license_token"]
        self._license_expiration_timestamp = user_data["results"]["USER"]["OPTIONS"][
            "expiration_timestamp"
        ]
        web_qualities = user_data["results"]["USER"]["OPTIONS"]["web_sound_quality"]
        mobile_qualities = user_data["results"]["USER"]["OPTIONS"]["mobile_sound_quality"]
        if web_qualities["high"] or mobile_qualities["high"]:
            self.formats.insert(0, {"cipher": "BF_CBC_STRIPE", "format": "MP3_320"})
        if web_qualities["lossless"] or mobile_qualities["lossless"]:
            self.formats.insert(0, {"cipher": "BF_CBC_STRIPE", "format": "FLAC"})

        self.user_country = user_data["results"]["COUNTRY"]

    async def setup(self):
        """Call this to let the client get its cookies, license and tokens."""
        await self._get_cookie()
        await self._update_user_data()

    async def _get_license(self):
        if (
            self._license_expiration_timestamp
            < (datetime.datetime.now() + datetime.timedelta(days=1)).timestamp()
        ):
            await self._update_user_data()
        return self._license

    async def _gw_api_call(
        self, method, use_csrf_token=True, args=None, params=None, http_method="POST", retry=True
    ):
        csrf_token = self._gw_csrf_token if use_csrf_token else "null"
        if params is None:
            params = {}
        parameters = {"api_version": "1.0", "api_token": csrf_token, "input": "3", "method": method}
        parameters |= params
        result = await self.session.request(
            http_method,
            GW_LIGHT_URL,
            params=parameters,
            timeout=30,
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
            else:
                raise DeezerGWError("Failed to call GW-API", result_json["error"])
        return result_json

    async def get_song_data(self, track_id):
        """Get data such as the track token for a given track."""
        return await self._gw_api_call("song.getData", args={"SNG_ID": track_id})

    async def get_deezer_track_urls(self, track_id):
        """Get the URL for a given track id."""
        dz_license = await self._get_license()
        song_data = await self.get_song_data(track_id)
        track_token = song_data["results"]["TRACK_TOKEN"]
        url_data = {
            "license_token": dz_license,
            "media": [
                {
                    "type": "FULL",
                    "formats": self.formats,
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
            raise DeezerGWError("Received an error from API", error)

        return result_json["data"][0]["media"][0], song_data["results"]
