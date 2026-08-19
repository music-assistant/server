"""
Report completed plays back to Tidal.

Tidal's public API has no play-reporting endpoint, and the unofficial API MA streams
through never establishes a session Tidal's play-tracking recognizes. This mirrors the
undocumented batch of correlated analytics events the desktop/web client posts instead,
close enough that reported plays show up in Tidal's own "recently played".

Anchored to on_played rather than on_streamed: the latter fires once MA's fetch/decode
pipeline finishes reading the (aggressively prebuffered) stream, often well before the
track has actually finished playing. on_played's fully_played=True reflects real elapsed
playback time instead.

Only genuine, (near-)complete plays are reported - the skipped/interrupted shape was
seen once in a capture but never verified to register anything, so it's not sent.
"""

from __future__ import annotations

import json
import random
import string
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from music_assistant.helpers.datetime import now as local_now

if TYPE_CHECKING:
    from .provider import TidalProvider

EVENT_BATCH_URL = "https://desktop.tidal.com/api/event-batch"
APP_VERSION = "2026.08.18"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "TIDAL/2.39.5 Chrome/140.0.7339.249 Electron/38.5.0 Safari/537.36"
)
# Drop a registered stream that never got a terminal on_played callback after this long,
# so abandoned entries (e.g. a playback error) don't accumulate. Generous on purpose: a
# paused-then-resumed track shouldn't lose its pending entry before it finishes.
PENDING_TTL_SECONDS = 24 * 60 * 60


class TidalPlayReportingManager:
    """Builds and sends the desktop-client-shaped play report Tidal's backend recognizes."""

    def __init__(self, provider: TidalProvider) -> None:
        """Initialize the play reporting manager."""
        self.provider = provider
        self.mass = provider.mass
        self.logger = provider.logger
        # Opaque per-instance device token, reused across reports (mirrors a real
        # desktop install's persistent client id).
        self._device_token = "".join(random.choices(string.ascii_letters + string.digits, k=16))
        # Format info captured at stream start, keyed by track id, consumed by on_played.
        self._pending: dict[str, dict[str, Any]] = {}

    def register_stream(
        self, item_id: str, quality: str, asset_presentation: str, audio_mode: str
    ) -> None:
        """
        Record format info for a track about to stream, for on_played to report later.

        :param item_id: The provider track id about to stream.
        :param quality: Tidal's own audioQuality string for this stream (e.g. "LOSSLESS").
        :param asset_presentation: Tidal's assetPresentation string for this stream.
        :param audio_mode: Tidal's audioMode string for this stream.
        """
        self._prune_stale_pending()
        self._pending[item_id] = {
            "tidal_session_id": str(uuid4()),
            "tidal_quality": quality,
            "tidal_asset_presentation": asset_presentation,
            "tidal_audio_mode": audio_mode,
            "registered_at": time.time(),
        }

    async def report_played(
        self, item_id: str, duration: int | None, position: int, fully_played: bool
    ) -> None:
        """
        Report a completed play to Tidal, if it looks like a genuine full play.

        :param item_id: The provider track id on_played was called for.
        :param duration: The track's total duration in seconds, if known.
        :param position: The player's last known position in seconds.
        :param fully_played: Whether MA considers the track to have been played to the end.
        """
        session = self._pending.pop(item_id, None)
        if not session or not fully_played or not duration:
            # Skipped/interrupted plays use a different, never-live-verified event
            # shape - silently drop rather than guess.
            return

        try:
            await self._send_completed_play(item_id, session, duration, position)
        except Exception:
            self.logger.debug(
                "Failed to report completed play to Tidal for track %s", item_id, exc_info=True
            )

    async def _send_completed_play(
        self,
        item_id: str,
        session: dict[str, Any],
        duration: int,
        position: int,
    ) -> None:
        """Build and send the correlated event batch for one completed play."""
        token = self.provider.auth.access_token
        user_id = self.provider.auth.user_id
        if not token or not user_id:
            return

        session_id = session["tidal_session_id"]
        track_id = item_id
        end_position = min(float(position), float(duration))
        end_ms = int(time.time() * 1000)
        # position is real elapsed listening time, so the start can be derived from it.
        start_ms = end_ms - int(end_position * 1000)

        client, user = self._build_client_and_user(token, int(user_id))

        events: list[tuple[str, str, dict[str, Any]]] = [
            (
                "streaming_session_start",
                "streaming_metrics",
                {
                    "abTestGroup": None,
                    "abTestName": None,
                    "browser": "Electron",
                    "browserVersion": "38.5.0",
                    "hardwarePlatform": "DESKTOP",
                    "isOfflineModeStart": False,
                    "mobileNetworkType": None,
                    "networkType": "WIFI",
                    "operatingSystem": "macOS",
                    "operatingSystemVersion": "10.15.7",
                    "screenHeight": 1050,
                    "screenWidth": 1680,
                    "sessionProductId": track_id,
                    "sessionProductType": "TRACK",
                    "sessionTags": [],
                    "sessionType": "PLAYBACK",
                    "startReason": "EXPLICIT",
                    "streamingSessionId": session_id,
                    "timestamp": start_ms,
                },
            ),
            (
                "playback_info_fetch",
                "streaming_metrics",
                {
                    "endReason": "COMPLETE",
                    "endTimestamp": start_ms + 350,
                    "errorCode": None,
                    "errorMessage": None,
                    "startTimestamp": start_ms + 50,
                    "streamingSessionId": session_id,
                },
            ),
            (
                "playback_session",
                "play_log",
                {
                    "actions": [],
                    "actualAssetPresentation": session["tidal_asset_presentation"],
                    "actualAudioMode": session["tidal_audio_mode"],
                    "actualProductId": track_id,
                    "actualQuality": session["tidal_quality"],
                    "endAssetPosition": end_position,
                    "endTimestamp": end_ms,
                    "isPostPaywall": True,
                    "playbackSessionId": session_id,
                    "productType": "TRACK",
                    "requestedProductId": track_id,
                    "sourceId": track_id,
                    "sourceType": "ITEM",
                    "startAssetPosition": 0,
                    "startTimestamp": start_ms,
                    "streamingSessionId": session_id,
                },
            ),
            (
                "playback_statistics",
                "streaming_metrics",
                {
                    "actualAssetPresentation": session["tidal_asset_presentation"],
                    "actualAudioMode": session["tidal_audio_mode"],
                    "actualProductId": track_id,
                    "actualQuality": session["tidal_quality"],
                    "actualStartTimestamp": start_ms + 100,
                    "adaptations": [],
                    "cdm": "WIDEVINE",
                    "cdmVersion": None,
                    "endReason": "COMPLETE",
                    "endTimestamp": end_ms,
                    "errorCode": None,
                    "errorMessage": None,
                    "hasAds": False,
                    "idealStartTimestamp": start_ms - 50,
                    "outputDevice": "SYSTEM_DEFAULT",
                    "productType": "TRACK",
                    "stalls": [],
                    "streamingSessionId": session_id,
                },
            ),
            (
                "streaming_session_end",
                "streaming_metrics",
                {"streamingSessionId": session_id, "timestamp": end_ms},
            ),
        ]

        form: dict[str, str] = {}
        for idx, (name, group, payload) in enumerate(events, start=1):
            event_body = {
                "group": group,
                "name": name,
                "payload": payload,
                "version": 2,
                "client": client,
                "extras": {
                    "navigation": {
                        "trace": {"chainSize": 1, "origin": "HOME", "uuid": str(uuid4())}
                    }
                },
                "user": user,
                "ts": end_ms,
                "uuid": str(uuid4()),
            }
            self._add_batch_entry(form, idx, name, event_body, token, end_ms)

        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
            "Origin": "https://desktop.tidal.com",
            "Referer": "https://desktop.tidal.com/",
        }
        async with self.mass.http_session.post(
            EVENT_BATCH_URL, data=form, headers=headers
        ) as response:
            if response.status >= 400:
                self.logger.debug(
                    "Tidal play report for track %s got HTTP %s", track_id, response.status
                )

    def _build_client_and_user(
        self, token: str, user_id: int
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Build the (client, user) envelope objects shared by every event in the batch."""
        # isoformat()'s colon-separated UTC offset ("+02:00") matches the real client;
        # strftime's "%z" would give "+0200".
        now = local_now()
        client = {
            "localDate": now.strftime("%Y-%m-%d"),
            "localTime": now.strftime("%H:%M:%S"),
            "platform": "desktop",
            "system": {"browserName": "Electron", "browserVersion": "38.5.0", "osName": "OS X"},
            "timeOffset": now.isoformat()[-6:],
            "token": self._device_token,
            "version": f"desktop@{APP_VERSION}",
            "experimentationIdentifier": str(uuid4()),
        }
        user = {
            "accessToken": token,
            "clientId": self._device_token,
            "id": user_id,
            "trackingUuid": str(uuid4()),
        }
        return client, user

    def _add_batch_entry(
        self,
        form: dict[str, str],
        idx: int,
        name: str,
        event_body: dict[str, Any],
        token: str,
        ts_ms: int,
    ) -> None:
        """Add one SQS-SendMessageBatch-shaped form entry for a single event."""
        prefix = f"SendMessageBatchRequestEntry.{idx}"
        headers_blob = {
            "app-name": "Tidal Client",
            "app-version": APP_VERSION,
            "browser-name": "Electron",
            "browser-version": "38.5.0",
            "client-id": self._device_token,
            "consent-category": "NECESSARY",
            "os-name": "OS X",
            "requested-sent-timestamp": ts_ms,
            "authorization": token,
        }
        form[f"{prefix}.Id"] = str(uuid4())
        form[f"{prefix}.MessageBody"] = json.dumps(event_body)
        form[f"{prefix}.MessageAttribute.1.Name"] = "Name"
        form[f"{prefix}.MessageAttribute.1.Value.StringValue"] = name
        form[f"{prefix}.MessageAttribute.1.Value.DataType"] = "String"
        form[f"{prefix}.MessageAttribute.2.Name"] = "Headers"
        form[f"{prefix}.MessageAttribute.2.Value.DataType"] = "String"
        form[f"{prefix}.MessageAttribute.2.Value.StringValue"] = json.dumps(headers_blob)

    def _prune_stale_pending(self) -> None:
        """Drop registered streams that never got a terminal on_played callback."""
        cutoff = time.time() - PENDING_TTL_SECONDS
        stale = [
            item_id for item_id, entry in self._pending.items() if entry["registered_at"] < cutoff
        ]
        for item_id in stale:
            del self._pending[item_id]
