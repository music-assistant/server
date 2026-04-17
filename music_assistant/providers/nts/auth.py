"""NTS Radio authentication via Firebase Auth and Firestore live track queries."""

from __future__ import annotations

import logging
from typing import Any, TypedDict, cast

import aiohttp


class FirebaseLoginResponse(TypedDict, total=False):
    """Subset of Firebase signInWithPassword response fields we consume."""

    idToken: str
    refreshToken: str
    email: str


class FirebaseRefreshResponse(TypedDict, total=False):
    """Subset of Firebase securetoken refresh response fields we consume."""

    id_token: str
    refresh_token: str


LOGGER = logging.getLogger(__name__)

# Public Firebase web API key for the NTS `nts-ios-app` project.
#
# Firebase web API keys are client-side identifiers, not secrets — they're
# embedded in NTS's public JS bundle and readable from any browser visiting
# nts.live. Access control is enforced server-side via Firebase Security
# Rules, so distributing this value with the provider is safe.
# See: https://firebase.google.com/docs/projects/api-keys
#
# If NTS rotates the key we'll need to re-extract it from their JS bundle:
#
#   1. Visit https://www.nts.live/
#   2. View page source, find the <script src="/js/app.min.*.js"> URL
#   3. Fetch that bundle (curl / Save As) and search for "AIzaSy"
#   4. The production config block contains apiKey, authDomain, projectId
#      etc. Copy the apiKey value for projectId "nts-ios-app".
NTS_FIREBASE_API_KEY = "AIzaSyA4Qp5AvHC8Rev72-10-_DY614w_bxUCJU"

FIREBASE_AUTH_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
FIREBASE_REFRESH_URL = "https://securetoken.googleapis.com/v1/token"

FIRESTORE_QUERY_URL = (
    "https://firestore.googleapis.com/v1/projects/nts-ios-app"
    "/databases/(default)/documents:runQuery"
)

CHANNEL_STREAM_PATHS = {"1": "/stream", "2": "/stream2"}


class NTSAuth:
    """NTS Supporter authentication via Firebase Auth."""

    def __init__(self) -> None:
        """Initialize an unauthenticated NTSAuth instance."""
        self._id_token: str | None = None
        self._refresh_token: str | None = None
        self._email: str | None = None
        self._authenticated: bool = False

    @property
    def is_authenticated(self) -> bool:
        """Return True when a valid Firebase session is held."""
        return self._authenticated

    @property
    def email(self) -> str | None:
        """Return the authenticated user's email, or None if not signed in."""
        return self._email

    async def login(self, email: str, password: str, http_session: aiohttp.ClientSession) -> bool:
        """Sign in via Firebase Auth."""
        try:
            async with http_session.post(
                f"{FIREBASE_AUTH_URL}?key={NTS_FIREBASE_API_KEY}",
                json={"email": email, "password": password, "returnSecureToken": True},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                payload: dict[str, Any] = await resp.json()
                if resp.status != 200:
                    error_msg = payload.get("error", {}).get("message", resp.status)
                    LOGGER.warning("NTS login failed: %s", error_msg)
                    self._authenticated = False
                    return False

                data = cast("FirebaseLoginResponse", payload)
                self._id_token = data["idToken"]
                self._refresh_token = data["refreshToken"]
                self._email = data.get("email", email)
                self._authenticated = True
                LOGGER.info("NTS authenticated via Firebase as %s", self._email)
                return True
        except (aiohttp.ClientError, TimeoutError, KeyError) as err:
            LOGGER.warning("NTS login error: %s", err)
            self._authenticated = False
            return False

    async def refresh(self, http_session: aiohttp.ClientSession) -> bool:
        """Refresh the Firebase ID token."""
        if not self._refresh_token:
            return False
        try:
            async with http_session.post(
                f"{FIREBASE_REFRESH_URL}?key={NTS_FIREBASE_API_KEY}",
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    LOGGER.info("NTS Firebase token refresh failed (status %s)", resp.status)
                    self._authenticated = False
                    return False

                data = cast("FirebaseRefreshResponse", await resp.json())
                self._id_token = data["id_token"]
                self._refresh_token = data["refresh_token"]
                self._authenticated = True
                LOGGER.info("NTS Firebase token refreshed")
                return True
        except (aiohttp.ClientError, TimeoutError, KeyError) as err:
            LOGGER.warning("NTS token refresh error: %s", err)
            self._authenticated = False
            return False

    async def get_live_tracks(
        self, channel: str, http_session: aiohttp.ClientSession
    ) -> dict[str, str] | None:
        """Query Firestore for the current track on a channel.

        Returns dict with "artist" and "title", or None.
        """
        if not self._authenticated or not self._id_token:
            return None

        stream_path = CHANNEL_STREAM_PATHS.get(channel)
        if not stream_path:
            return None

        query_body = {
            "structuredQuery": {
                "from": [{"collectionId": "live_tracks"}],
                "where": {
                    "fieldFilter": {
                        "field": {"fieldPath": "stream_pathname"},
                        "op": "EQUAL",
                        "value": {"stringValue": stream_path},
                    }
                },
                "orderBy": [{"field": {"fieldPath": "start_time"}, "direction": "DESCENDING"}],
                "limit": 1,
            }
        }

        try:
            async with http_session.post(
                FIRESTORE_QUERY_URL,
                json=query_body,
                headers={"Authorization": f"Bearer {self._id_token}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 401:
                    LOGGER.info("NTS Firestore token expired")
                    self._authenticated = False
                    return None
                if resp.status != 200:
                    LOGGER.warning("NTS Firestore query failed (status %s)", resp.status)
                    return None

                results: list[dict[str, Any]] = await resp.json()
                for result in results:
                    fields = result.get("document", {}).get("fields", {})
                    if not fields:
                        continue

                    artist_values = (
                        fields.get("artist_names", {}).get("arrayValue", {}).get("values", [])
                    )
                    artists = [v.get("stringValue", "") for v in artist_values]
                    title = fields.get("song_title", {}).get("stringValue", "")

                    if artists or title:
                        return {"artist": ", ".join(artists), "title": title}

                return None
        except (aiohttp.ClientError, TimeoutError) as err:
            LOGGER.warning("NTS Firestore query error: %s", err)
            return None

    def logout(self) -> None:
        """Clear the session."""
        self._id_token = None
        self._refresh_token = None
        self._email = None
        self._authenticated = False
