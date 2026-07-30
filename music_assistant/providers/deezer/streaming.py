"""
Streaming, decryption, and playback callbacks for the Deezer provider.

Handles stream URL resolution, Blowfish decryption for track audio,
radio/podcast stream details, and listen logging callbacks.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime
from math import ceil
from typing import TYPE_CHECKING, NoReturn

from aiohttp import ClientTimeout
from Crypto.Cipher import Blowfish
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import AudioError, MediaNotFoundError
from music_assistant_models.media_items import AudioFormat, MediaItemType
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.helpers.app_vars import app_var
from music_assistant.helpers.datetime import utc_timestamp

from .gw_client import DeezerGWError
from .helpers import fetch_all_audiobook_chapter_edges, fetch_all_bookmarks

if TYPE_CHECKING:
    from .provider import DeezerProvider


class DeezerStreamingManager:
    """Handles streaming, decryption, and playback lifecycle for Deezer."""

    def __init__(self, provider: DeezerProvider) -> None:
        """Initialize streaming manager."""
        self.provider = provider
        self.mass = provider.mass
        self.instance_id = provider.instance_id
        self.domain = provider.domain
        self.logger = provider.logger

    # -- Resume position --

    async def get_resume_position(
        self, item_id: str, media_type: MediaType
    ) -> tuple[bool, int, datetime | None]:
        """
        Get the resume position for a podcast episode.

        :param item_id: The provider-specific episode ID.
        :param media_type: The media type (only PODCAST_EPISODE is supported).
        :returns: Tuple of (fully_played, resume_position_ms, timestamp).
        """
        if media_type != MediaType.PODCAST_EPISODE:
            return (False, 0, None)
        bookmarks = await fetch_all_bookmarks(self.provider.gql_client)
        if item_id in bookmarks:
            is_played, position_ms = bookmarks[item_id]
            return (is_played, position_ms, None)
        return (False, 0, None)

    # -- Playback callbacks --

    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """
        Handle callback when a podcast episode has been played or is playing.

        Syncs playback progress back to Deezer's bookmark/play-state system.
        Only handles podcast episodes — Deezer's Pipe API has no track listen logging.
        """
        if media_type != MediaType.PODCAST_EPISODE:
            return
        if fully_played:
            await self.provider.gql_client.mark_as_played_podcast_episode(episode_id=prov_item_id)
        elif position == 0 and not is_playing:
            await self.provider.gql_client.mark_as_not_played_podcast_episode(
                episode_id=prov_item_id
            )
        elif is_playing or position > 0:
            await self.provider.gql_client.bookmark_podcast_episode(
                episode_id=prov_item_id, offset=position
            )

    # -- Stream details --

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        if media_type == MediaType.RADIO:
            return await self._get_radio_stream_details(item_id)
        if media_type == MediaType.PODCAST_EPISODE:
            return await self._get_podcast_episode_stream_details(item_id)
        if media_type == MediaType.AUDIOBOOK:
            return await self._get_audiobook_stream_details(item_id)
        return await self._get_track_stream_details(item_id)

    async def _get_track_stream_details(self, item_id: str) -> StreamDetails:
        """Return stream details for a regular Deezer track."""
        try:
            url_details, song_data = await self.provider.gw_client.get_deezer_track_urls(item_id)
        except DeezerGWError as err:
            _raise_stream_error(err, item_id, "Track")
        url = url_details["sources"][0]["url"]
        size_key = f"FILESIZE_{url_details['format']}"
        size = int(song_data.get(size_key) or song_data.get("FILESIZE_MP3_MISC") or 0)
        return StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            audio_format=AudioFormat(
                content_type=ContentType.try_parse(url_details["format"].split("_")[0])
            ),
            stream_type=StreamType.CUSTOM,
            duration=int(song_data["DURATION"]),
            data={
                "url": url,
                "format": url_details["format"],
                "track_id": str(song_data["SNG_ID"]),
            },
            size=size,
            can_seek=True,
            allow_seek=True,
        )

    async def _get_audiobook_stream_details(self, item_id: str) -> StreamDetails:
        """
        Return stream details for a Deezer audiobook.

        Resolves all chapter IDs and durations. Each chapter is streamed
        as a regular encrypted track via get_audio_stream.
        """
        all_edges = await fetch_all_audiobook_chapter_edges(self.provider.gql_client, item_id)

        chapter_ids: list[str] = []
        chapter_durations_ms: list[int] = []
        for edge in all_edges:
            if edge.node is None:
                continue
            chapter_ids.append(edge.node.id)
            chapter_durations_ms.append(edge.node.duration * 1000)

        if not chapter_ids:
            raise MediaNotFoundError(f"No chapters found for audiobook {item_id}")

        # Probe the first chapter to determine audio format
        try:
            first_url_details, _ = await self.provider.gw_client.get_deezer_track_urls(
                chapter_ids[0]
            )
        except DeezerGWError as err:
            _raise_stream_error(err, item_id, "Audiobook")
        total_duration = sum(chapter_durations_ms) // 1000

        return StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            media_type=MediaType.AUDIOBOOK,
            audio_format=AudioFormat(
                content_type=ContentType.try_parse(first_url_details["format"].split("_")[0])
            ),
            stream_type=StreamType.CUSTOM,
            duration=total_duration,
            data={
                "chapter_ids": chapter_ids,
                "chapter_durations_ms": chapter_durations_ms,
            },
            can_seek=True,
            allow_seek=True,
        )

    async def _get_radio_stream_details(self, item_id: str) -> StreamDetails:
        """Return stream details for a Deezer livestream (radio station)."""
        result = await self.provider.gql_client.get_livestream(livestream_id=item_id)
        if result is None or not result.media:
            raise MediaNotFoundError(f"Radio {item_id} has no stream URL")
        # Prefer HLS, then AAC, then MP3
        best_media = result.media[0]
        for media in result.media:
            if media.codec and media.codec.type_ == "hls":
                best_media = media
                break
        content_type = ContentType.UNKNOWN
        if best_media.codec:
            content_type = ContentType.try_parse(best_media.codec.type_)
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=content_type,
                bit_rate=best_media.codec.bitrate if best_media.codec else None,
            ),
            media_type=MediaType.RADIO,
            stream_type=StreamType.HTTP,
            path=best_media.url,
            can_seek=False,
            allow_seek=False,
        )

    async def _get_podcast_episode_stream_details(self, item_id: str) -> StreamDetails:
        """Return stream details for a Deezer podcast episode."""
        result = await self.provider.gql_client.get_podcast_episode(podcast_episode_id=item_id)
        if result is None or not result.media:
            raise MediaNotFoundError(f"Podcast episode {item_id} has no stream URL")
        content_type = ContentType.UNKNOWN
        if result.media.codec:
            content_type = ContentType.try_parse(result.media.codec.type_)
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=content_type,
                bit_rate=result.media.codec.bitrate if result.media.codec else None,
            ),
            media_type=MediaType.PODCAST_EPISODE,
            stream_type=StreamType.HTTP,
            path=result.media.url,
            duration=result.duration,
            can_seek=True,
            allow_seek=True,
        )

    # -- Audio stream (Blowfish decryption) --

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """Return the audio stream for the provider item."""
        if streamdetails.media_type == MediaType.AUDIOBOOK and isinstance(streamdetails.data, dict):
            async for chunk in self._stream_audiobook_chapters(streamdetails, seek_position):
                yield chunk
            return
        async for chunk in self._stream_encrypted_track(streamdetails, seek_position):
            yield chunk

    async def _stream_audiobook_chapters(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """Stream audiobook by iterating through encrypted chapter tracks."""
        chapter_ids: list[str] = streamdetails.data["chapter_ids"]
        chapter_durations_ms: list[int] = streamdetails.data["chapter_durations_ms"]

        # Resolve which chapter to start from based on seek_position
        start_chapter = 0
        chapter_seek = 0
        if seek_position > 0:
            seek_ms = seek_position * 1000
            accumulated_ms = 0
            for i, dur_ms in enumerate(chapter_durations_ms):
                if accumulated_ms + dur_ms > seek_ms:
                    start_chapter = i
                    chapter_seek = (seek_ms - accumulated_ms) // 1000
                    break
                accumulated_ms += dur_ms
            else:
                start_chapter = max(len(chapter_ids) - 1, 0)
                chapter_seek = 0

        prev_chapter: StreamDetails | None = None
        current_chapter: StreamDetails | None = None
        try:
            for i in range(start_chapter, len(chapter_ids)):
                chapter_id = chapter_ids[i]
                try:
                    url_details, song_data = await self.provider.gw_client.get_deezer_track_urls(
                        chapter_id
                    )
                except DeezerGWError, MediaNotFoundError, KeyError:
                    self.logger.warning("Failed to get URL for audiobook chapter %s", chapter_id)
                    continue
                url = url_details["sources"][0]["url"]
                size_key = f"FILESIZE_{url_details['format']}"
                size = int(song_data.get(size_key) or song_data.get("FILESIZE_MP3_MISC") or 0)
                duration = int(song_data["DURATION"])
                chapter_details = StreamDetails(
                    item_id=chapter_id,
                    provider=self.instance_id,
                    audio_format=streamdetails.audio_format,
                    stream_type=StreamType.CUSTOM,
                    duration=duration,
                    data={
                        "url": url,
                        "format": url_details["format"],
                        "track_id": str(song_data["SNG_ID"]),
                    },
                    size=size,
                )
                # Log the previous chapter as fully played before starting the next
                if prev_chapter and "start_ts" in prev_chapter.data:
                    self.mass.create_task(
                        self.provider.gw_client.log_listen(last_track=prev_chapter)
                    )
                current_chapter = chapter_details
                seek = chapter_seek if i == start_chapter else 0
                async for chunk in self._stream_encrypted_track(chapter_details, seek):
                    yield chunk
                prev_chapter = chapter_details
                current_chapter = None
        finally:
            # Log the last chapter that was playing (completed or cancelled)
            last = current_chapter or prev_chapter
            if last and "start_ts" in last.data:
                self.mass.create_task(self.provider.gw_client.log_listen(last_track=last))

    async def _stream_encrypted_track(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """Stream and decrypt a single encrypted Deezer track."""
        blowfish_key = self._get_blowfish_key(streamdetails.data["track_id"])
        chunk_index = 0
        timeout = ClientTimeout(total=None, connect=30, sock_read=600)
        headers: dict[str, str] = {}

        # Seek by skipping chunks (Range header causes malformed audio)
        if seek_position and streamdetails.size and streamdetails.duration:
            chunk_count = ceil(streamdetails.size / 2048)
            skip_chunks = int(chunk_count / streamdetails.duration) * seek_position
        else:
            skip_chunks = 0

        buffer = bytearray()
        streamdetails.data["start_ts"] = utc_timestamp()
        streamdetails.data["stream_id"] = uuid.uuid1()
        self.mass.create_task(self.provider.gw_client.log_listen(next_track=streamdetails.item_id))
        async with self.mass.http_session.get(
            streamdetails.data["url"], headers=headers, timeout=timeout
        ) as resp:
            if resp.status != 200:
                raise MediaNotFoundError(
                    f"Failed to stream track {streamdetails.item_id}: HTTP {resp.status}"
                )
            async for chunk in resp.content.iter_chunked(2048):
                buffer += chunk
                if len(buffer) >= 2048:
                    if chunk_index >= skip_chunks or chunk_index == 0:
                        if chunk_index % 3 > 0:
                            yield bytes(buffer[:2048])
                        else:
                            yield self._decrypt_chunk(bytes(buffer[:2048]), blowfish_key)

                    chunk_index += 1
                    del buffer[:2048]
        yield bytes(buffer)

    async def on_streamed(self, streamdetails: StreamDetails) -> None:
        """Handle callback when an item completed streaming."""
        if not isinstance(streamdetails.data, dict) or "start_ts" not in streamdetails.data:
            return
        await self.provider.gw_client.log_listen(last_track=streamdetails)

    # -- Decryption helpers --

    @staticmethod
    def _md5(data: str, data_type: str = "ascii") -> str:
        md5sum = hashlib.md5()
        md5sum.update(data.encode(data_type))
        return md5sum.hexdigest()

    def _get_blowfish_key(self, track_id: str) -> str:
        """Get blowfish key to decrypt a chunk of a track."""
        secret = app_var("deezer_decrypt_key")
        id_md5 = self._md5(track_id)
        return "".join(
            chr(ord(id_md5[i]) ^ ord(id_md5[i + 16]) ^ ord(secret[i])) for i in range(16)
        )

    @staticmethod
    def _decrypt_chunk(chunk: bytes, blowfish_key: str) -> bytes:
        """Decrypt a given chunk using the blow fish key."""
        cipher = Blowfish.new(
            blowfish_key.encode("ascii"),
            Blowfish.MODE_CBC,
            b"\x00\x01\x02\x03\x04\x05\x06\x07",
        )
        return cipher.decrypt(chunk)  # type: ignore[no-any-return,unused-ignore]


def _raise_stream_error(err: DeezerGWError, item_id: str, label: str) -> NoReturn:
    """
    Translate a DeezerGWError into the appropriate streaming error.

    :param err: The GW error raised while resolving the stream URL.
    :param item_id: The provider-specific item ID, used in the error message.
    :param label: Human-readable item kind (e.g. "Track", "Audiobook").
    """
    api_errors = err.args[1] if len(err.args) > 1 else []
    # Code 2002 means the item is genuinely unavailable (region/plan rights), so it
    # maps to MediaNotFoundError. Any other GW failure is treated as a transient
    # AudioError. Both are caught by the queue's skip-on-error path.
    if isinstance(api_errors, list) and api_errors and api_errors[0].get("code") == 2002:
        raise MediaNotFoundError(f"{label} {item_id} is not available on Deezer") from err
    raise AudioError(str(err)) from err
