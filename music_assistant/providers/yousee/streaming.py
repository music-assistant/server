"""Streaming operations for YouSee Musik."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError, ResourceTemporarilyUnavailable
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.helpers.datetime import iso_from_utc_timestamp, utc_timestamp
from music_assistant.providers.yousee.constants import PLAYBACK_QUALITY

if TYPE_CHECKING:
    from music_assistant.providers.yousee.provider import YouSeeMusikProvider


class YouSeeStreamingManager:
    """Manages YouSee Musik streaming operations."""

    def __init__(self, provider: YouSeeMusikProvider):
        """Initialize streaming manager."""
        self.provider = provider
        self.api = provider.api
        self.mass = provider.mass
        self.logger = provider.logger

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a track."""
        query = """
            query playbackFull($id: ID!, $quality: StreamQuality!) {
                playback(trackId: $id) {
                    full(quality: $quality)
                }
            }
        """

        if media_type != MediaType.TRACK:
            raise MediaNotFoundError(f"Streaming of media type {media_type} is not supported")

        variables = {"id": item_id, "quality": PLAYBACK_QUALITY}

        result = await self.api.post_graphql(query, variables)

        playback_url = result.get("data", {}).get("playback", {}).get("full")
        if not playback_url:
            raise ResourceTemporarilyUnavailable(f"Track {item_id} is not available for streaming")

        return StreamDetails(
            provider=self.provider.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.MP4,
                bit_rate=320000,
            ),
            media_type=MediaType.TRACK,
            stream_type=StreamType.HLS,
            allow_seek=True,
            can_seek=True,
            path=playback_url,
            data={"start_ts": utc_timestamp()},
        )

    async def report_playback(
        self,
        streamdetails: StreamDetails,
    ) -> None:
        """
        Handle callback when given streamdetails completed streaming.

        To get the number of seconds streamed, see streamdetails.seconds_streamed.
        To get the number of seconds seeked/skipped, see streamdetails.seek_position.
        Note that seconds_streamed is the total streamed seconds, so without seeked time.

        NOTE: Due to internal and player buffering,
        this may be called in advance of the actual completion.
        """
        mutation = """
            mutation reportPlayback($report: ReportPlaybackInput!) {
                reportPlayback(report: $report) {
                    ok
                }
            }
        """

        seconds_streamed = min(
            utc_timestamp() - streamdetails.data["start_ts"],
            streamdetails.seconds_streamed,
        )

        variables = {
            "playbackUrl": streamdetails.path,
            "playbackContext": next(
                iter((await self.provider.get_track(streamdetails.item_id)).provider_mappings)
            ).details,  # TODO Is there a better way to obtain the playbackContext?
            # ^ This does not seem intended.
            "playedSeconds": int(seconds_streamed),
            "playedAt": iso_from_utc_timestamp(utc_timestamp()),
        }

        result = await self.api.post_graphql(mutation, {"report": variables})

        if not result.get("data", {}).get("reportPlayback", {}).get("ok"):
            self.logger.warning(
                "Reporting playback for track %s failed with result %s",
                streamdetails.item_id,
                result,
            )
