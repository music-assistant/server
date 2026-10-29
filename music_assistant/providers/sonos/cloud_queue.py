"""
Sonos CloudQueue HTTP endpoints for Music Assistant.

The Sonos speaker fetches its queue from these endpoints on its own schedule and plays out of
what it cached, so every answer is built from the live Music Assistant queue.
https://docs.sonos.com/reference/cloud-queue-api
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiohttp import web
from music_assistant_models.errors import InvalidDataError

from music_assistant.constants import MASS_LOGO_ONLINE, VERBOSE_LOG_LEVEL
from music_assistant.helpers.audio import get_mime_type

from .player import SonosPlayer, SonosQueueWindow

if TYPE_CHECKING:
    from music_assistant_models.player import PlayerMedia

    from .provider import SonosPlayerProvider

# stands in for "the speaker named no ceiling", so our own window size decides
_NO_CEILING = 1000


def _requested_max(requested: str | None) -> int:
    """
    Return the ceiling a speaker put on one side of the window.

    The sizes are maxima: we may serve fewer, never more. An absent or unreadable size puts
    no ceiling on it.
    """
    try:
        return max(0, int(requested)) if requested is not None else _NO_CEILING
    except ValueError:
        return _NO_CEILING


class SonosCloudQueue:
    """Serve the Sonos CloudQueue HTTP endpoints on behalf of the Sonos provider."""

    def __init__(self, provider: SonosPlayerProvider) -> None:
        """
        Serve the Sonos CloudQueue endpoints for the given provider.

        :param provider: The Sonos player provider whose speakers this queue serves.
        """
        self.provider = provider
        self.mass = provider.mass
        self.logger = provider.logger

    async def handle_request(self, request: web.Request) -> web.Response:
        """
        Handle the Sonos CloudQueue request.

        https://docs.sonos.com/reference/itemwindow
        """
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Cloud Queue request\n - path: %s\n - query: %s\n",
            request.path,
            request.query,
        )
        path_parts = request.path.strip("/").split("/")
        if len(path_parts) != 4 or path_parts[0] != "sonos_queue":
            return web.Response(status=404)
        player_id = path_parts[1]
        if not (sonos_player := self.mass.players.get_player(player_id)):
            return web.Response(status=501)
        if TYPE_CHECKING:
            assert isinstance(sonos_player, SonosPlayer)
        endpoint = path_parts[3]
        if endpoint == "itemWindow":
            return await self._handle_sonos_queue_itemwindow(sonos_player, request)
        if endpoint == "version":
            return await self._handle_sonos_queue_version(sonos_player, request)
        if endpoint == "context":
            return await self._handle_sonos_queue_context(sonos_player, request)
        if endpoint == "timePlayed":
            return await self._handle_sonos_queue_time_played(sonos_player, request)
        return web.Response(status=404)

    async def _handle_sonos_queue_itemwindow(
        self, player: SonosPlayer, request: web.Request
    ) -> web.Response:
        """
        Handle the Sonos CloudQueue ItemWindow endpoint.

        https://docs.sonos.com/reference/itemwindow
        """
        context_version = request.query.get("contextVersion", "1")
        # read the version and the item generation before building, so the items, their ids
        # and the version we label them with always come from the same queue: a bump landing
        # in between would tell the speaker its cache is current while it holds the older
        # window, and a load landing in between would label old items with new-load ids
        queue_version = player.cloud_queue_version
        wire_generation = player.cloud_queue_item_generation
        wire_center = request.query.get("itemId")
        # built from the queue as it is right now: the speaker fetches on its own schedule and
        # plays out of what it cached, so only a live answer keeps a track added mid-playback
        # from being played over. The beginning/end flags must be honest - signalling
        # end-of-queue is what makes Sonos drop items it cached past our window, so a queue
        # rewrite (replace_next) does not resurrect stale tracks.
        unavailable: InvalidDataError | None = None
        try:
            window = await player.build_cloud_queue_window(
                player.bare_item_id(wire_center) if wire_center else None,
                max_previous=_requested_max(request.query.get("previousWindowSize")),
                max_upcoming=_requested_max(request.query.get("upcomingWindowSize")),
            )
        except InvalidDataError as err:
            # the queue went away under us (a stop that never reached this speaker, so it keeps
            # polling): end-of-queue is the right answer and beats a 500 per poll. Only this
            # one - any other failure must not read to the speaker as "queue over".
            window = SonosQueueWindow(includes_beginning=True, includes_end=True)
            unavailable = err
        # log the answer, not just the request: for "stopped playing early" reports the served
        # begin/end flags and item count are the decisive facts, and a wrongly set end flag is
        # what makes a speaker drop items it cached past our window
        message = (
            "Cloud queue itemWindow for %s: reason=%s itemId=%s previous=%s upcoming=%s "
            "queueVersion=%s -> %s begin=%s end=%s items=%s"
        )
        args: list[object] = [
            player.player_id,
            request.query.get("reason"),
            wire_center,
            request.query.get("previousWindowSize"),
            request.query.get("upcomingWindowSize"),
            request.query.get("queueVersion"),
            queue_version,
            window.includes_beginning,
            window.includes_end,
            len(window.items),
        ]
        if unavailable is not None:
            message += " (queue not describable: %s)"
            args.append(unavailable)
        self.logger.debug(message, *args)
        result = {
            "includesBeginningOfQueue": window.includes_beginning,
            "includesEndOfQueue": window.includes_end,
            "contextVersion": context_version,
            # report the version of the items we actually serve instead of echoing the
            # player's requested version, otherwise a changed queue keeps a stale version
            # label and Sonos never realises it changed.
            "queueVersion": str(queue_version),
            "items": [
                self._parse_sonos_queue_item(player, x, wire_generation) for x in window.items
            ],
        }
        return web.json_response(result)

    async def _handle_sonos_queue_version(
        self, player: SonosPlayer, request: web.Request
    ) -> web.Response:
        """
        Handle the Sonos CloudQueue Version endpoint.

        https://docs.sonos.com/reference/version
        """
        context_version = request.query.get("contextVersion") or "1"
        self.logger.debug(
            "Cloud queue version poll from %s: queueVersion=%s -> %s",
            player.player_id,
            request.query.get("queueVersion"),
            player.cloud_queue_version,
        )
        # keep sub-second resolution: the queue can change several times within the same
        # second and Sonos treats an unchanged queueVersion as "nothing changed" (stale window).
        result = {
            "contextVersion": context_version,
            "queueVersion": str(player.cloud_queue_version),
        }
        return web.json_response(result)

    async def _handle_sonos_queue_context(
        self, player: SonosPlayer, request: web.Request
    ) -> web.Response:
        """
        Handle the Sonos CloudQueue Context endpoint.

        https://docs.sonos.com/reference/context
        """
        result = {
            "contextVersion": "1",
            "queueVersion": str(player.cloud_queue_version),
            "container": {
                "type": "trackList",
                "name": "Music Assistant",
                "imageUrl": MASS_LOGO_ONLINE,
                "service": {"name": "Music Assistant", "id": "mass"},
                "id": {
                    "serviceId": "mass",
                    "objectId": f"mass:{player.cloud_queue_id or 'unknown'}",
                    "accountId": "",
                },
            },
            "reports": {
                "sendUpdateAfterMillis": 1000,
                "periodicIntervalMillis": 30000,
                "sendPlaybackActions": True,
            },
            "playbackPolicies": {
                "canSkip": True,
                "limitedSkips": True,
                "canSkipToItem": True,  # unsure
                "canSkipBack": True,
                # seek needs to be disabled because we dont properly support range requests
                "canSeek": False,
                "canRepeat": False,  # handled by MA queue controller
                "canRepeatOne": False,  # handled by MA queue controller
                "canCrossfade": False,  # handled by MA queue controller
                "canShuffle": False,  # handled by MA queue controller
            },
        }
        return web.json_response(result)

    async def _handle_sonos_queue_time_played(
        self, player: SonosPlayer, request: web.Request
    ) -> web.Response:
        """
        Handle the Sonos CloudQueue TimePlayed endpoint.

        https://docs.sonos.com/reference/timeplayed
        """
        json_body = await request.json()
        for item in json_body["items"]:
            if error := item.get("error"):
                self._handle_reported_playback_error(player, item, error)
                continue
            if item["type"] != "update":
                continue
            if "positionMillis" not in item:
                continue
            # only the current load's wire id (or a legacy bare id) may update the
            # position: a report from before a same-track reload carries the old
            # generation and its position, which the reload just seeked away from
            if player.current_media and item["id"] in (
                player.current_media.queue_item_id,
                player.wire_item_id(player.current_media.queue_item_id),
            ):
                player.update_elapsed_time(item["positionMillis"] / 1000)
            break
        return web.Response(status=204)

    def _parse_sonos_queue_item(
        self, player: SonosPlayer, media: PlayerMedia, wire_generation: int
    ) -> dict[str, Any]:
        """Parse MusicAssistant PlayerMedia to a Sonos Media (queue) object."""
        # the speaker tracks its position within the audio we serve, which is
        # shorter than the media item when playback starts at a seek position
        duration = media.stream_duration or media.duration
        return {
            "id": player.wire_item_id(media.queue_item_id, wire_generation) or media.uri,
            "track": {
                "type": "track",
                "mediaUrl": media.uri,
                "contentType": get_mime_type(media.uri.split(".")[-1]),
                "service": {"name": "Music Assistant", "id": "mass"},
                "name": media.title,
                "imageUrl": media.image_url,
                "durationMillis": int(duration * 1000) if duration else 0,
                "artist": {
                    "name": media.artist,
                }
                if media.artist
                else None,
                "album": {
                    "name": media.album,
                }
                if media.album
                else None,
            },
        }

    def _handle_reported_playback_error(
        self, player: SonosPlayer, item: dict[str, Any], error: dict[str, Any]
    ) -> None:
        """
        Log a playback failure the speaker reported for one of its queue items and release it.

        :param player: The speaker that sent the report.
        :param item: The reported queue item the failure belongs to.
        :param error: The error object the speaker attached to it.
        """
        if error.get("type") == "http" and str(error.get("status")) == "404":
            # our own stream server refused the item: a track the queue moved past or no
            # longer holds, or one it failed to stream and logged there. The speaker tries
            # each track it cached before reading the queue again, so these come in bursts
            self.logger.debug(
                "Speaker %s was refused %s by the stream server",
                player.display_name,
                item.get("id"),
            )
            return
        report_id = item.get("reportId")
        if report_id:
            if report_id in player.reported_playback_errors:
                return
            player.reported_playback_errors.append(report_id)
        wire_id = item.get("id", "")
        title = (
            player.current_media.title
            if player.current_media
            and wire_id
            in (
                player.current_media.queue_item_id,
                player.wire_item_id(player.current_media.queue_item_id),
            )
            else wire_id
        )
        # nothing else tells us. Playback stops while Music Assistant still believes
        # the track is playing
        self.logger.warning(
            "Speaker %s could not play %s and reported %s (%s)",
            player.display_name,
            title,
            error.get("status", "an unknown error"),
            error.get("type", "unknown"),
        )
        if wire_id:
            player.release_failed_item(wire_id)
