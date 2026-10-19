"""Subsonic endpoints for favorites, lyrics, play queue state and scan status."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.auth import Scope
from music_assistant_models.enums import MediaType

from music_assistant.controllers.webserver.helpers.auth_middleware import has_scope
from music_assistant.helpers.datetime import utc
from music_assistant.helpers.lyrics import normalize_lrc_lyrics

from . import mappers
from .constants import ErrorCode
from .ids import parse_subsonic_id
from .response import SubsonicError

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track

    from .browse import BrowseEndpoints
    from .request import EndpointHandler, SubsonicRequest

PLAY_QUEUE_FETCH_CONCURRENCY = 8
LRC_LINE = re.compile(r"^\[(\d{1,3}):(\d{1,2}(?:\.\d{1,3})?)\](.*)$")


@dataclass(frozen=True)
class SavedPlayQueue:
    """A play queue a user saved, kept as Subsonic ids and resolved when it is read."""

    ids: list[str]
    current: str | None
    position: int
    changed: str
    changed_by: str


class ActionEndpoints:
    """Favorites, lyrics, play queue state and scan status."""

    def __init__(self, browse: BrowseEndpoints) -> None:
        """
        Initialize the endpoints.

        :param browse: The browse endpoints, used to resolve Subsonic ids to media items.
        """
        self.browse = browse
        self.mass = browse.mass
        self._play_queues: dict[str, SavedPlayQueue] = {}

    def endpoints(self) -> dict[str, EndpointHandler]:
        """Return the handlers of this group, keyed by endpoint name."""
        return {
            "star": self._star,
            "unstar": self._unstar,
            "scrobble": self._scrobble,
            "getLyrics": self._get_lyrics,
            "getLyricsBySongId": self._get_lyrics_by_song_id,
            "getPlayQueue": self._get_play_queue,
            "savePlayQueue": self._save_play_queue,
            "getScanStatus": self._get_scan_status,
            "startScan": self._start_scan,
        }

    async def _star(self, request: SubsonicRequest) -> dict[str, Any]:
        await self._set_favorite(request, favorite=True)
        return {}

    async def _unstar(self, request: SubsonicRequest) -> dict[str, Any]:
        await self._set_favorite(request, favorite=None)
        return {}

    async def _scrobble(self, request: SubsonicRequest) -> dict[str, Any]:
        # every play of this server happens on the jukebox player, whose queue records it
        return {}

    async def _get_lyrics(self, request: SubsonicRequest) -> dict[str, Any]:
        artist = request.get("artist") or ""
        title = request.get("title") or ""
        lyrics: dict[str, Any] = {"artist": artist, "title": title}
        if title:
            tracks = await self.mass.music.tracks.library_items(search=title, limit=25)
            for track in tracks:
                if track.name.lower() == title.lower() and (
                    not artist or artist.lower() in track.artist_str.lower()
                ):
                    plain, lrc = await self.mass.metadata.get_track_lyrics(track)
                    lyrics["value"] = plain or _strip_timestamps(lrc)
                    break
        return {"lyrics": lyrics}

    async def _get_lyrics_by_song_id(self, request: SubsonicRequest) -> dict[str, Any]:
        ref = parse_subsonic_id(request.require("id"), MediaType.TRACK)
        track = cast("Track", await self.browse.get_item(ref))
        plain, lrc = await self.mass.metadata.get_track_lyrics(track)
        structured: list[dict[str, Any]] = []
        common = {"displayArtist": track.artist_str, "displayTitle": track.name, "lang": "xxx"}
        if lines := _parse_lrc(lrc):
            structured.append({**common, "synced": True, "line": lines})
        elif plain:
            text_lines = [{"value": line} for line in plain.splitlines()]
            structured.append({**common, "synced": False, "line": text_lines})
        return {"lyricsList": {"structuredLyrics": structured}}

    async def _get_play_queue(self, request: SubsonicRequest) -> dict[str, Any]:
        saved = self._play_queues.get(request.user.user_id)
        if saved is None:
            return {"playQueue": {"entry": []}}
        semaphore = asyncio.Semaphore(PLAY_QUEUE_FETCH_CONCURRENCY)

        async def song(subsonic_id: str) -> dict[str, Any] | None:
            async with semaphore:
                try:
                    track = await self.browse.get_item(parse_subsonic_id(subsonic_id))
                except SubsonicError:
                    return None
                return mappers.song(cast("Track", track))

        songs = await asyncio.gather(*(song(subsonic_id) for subsonic_id in saved.ids))
        play_queue: dict[str, Any] = {
            "entry": [entry for entry in songs if entry is not None],
            "changed": saved.changed,
            "changedBy": saved.changed_by,
            "username": request.user.username,
            "position": saved.position,
        }
        if saved.current:
            play_queue["current"] = saved.current
        return {"playQueue": play_queue}

    async def _save_play_queue(self, request: SubsonicRequest) -> dict[str, Any]:
        ids = request.get_all("id")
        for value in ids:
            parse_subsonic_id(value, MediaType.TRACK)
        self._play_queues[request.user.user_id] = SavedPlayQueue(
            ids=ids,
            current=request.get("current"),
            position=request.get_int("position", 0),
            changed=mappers.iso_timestamp(utc()),
            changed_by=request.get("c") or "",
        )
        return {}

    async def _get_scan_status(self, request: SubsonicRequest) -> dict[str, Any]:
        return {"scanStatus": await self._scan_status()}

    async def _start_scan(self, request: SubsonicRequest) -> dict[str, Any]:
        if not has_scope(request.user, Scope.LIBRARY_MANAGE):
            raise SubsonicError(ErrorCode.NOT_AUTHORIZED, "User may not start a library sync")
        await self.mass.music.start_sync()
        return {"scanStatus": await self._scan_status()}

    async def _set_favorite(self, request: SubsonicRequest, favorite: bool | None) -> None:
        values = request.get_all("id") + request.get_all("albumId") + request.get_all("artistId")
        refs = [parse_subsonic_id(value) for value in values]
        if not has_scope(request.user, Scope.LIBRARY_WRITE):
            raise SubsonicError(ErrorCode.NOT_AUTHORIZED, "User may not change favorites")
        for ref in refs:
            await self.mass.music.set_item_favorite(ref.uri, favorite)

    async def _scan_status(self) -> dict[str, Any]:
        return {
            "scanning": bool(self.mass.music.active_sync_tasks),
            "count": await self.mass.music.tracks.library_count(),
        }


def _parse_lrc(lrc: str | None) -> list[dict[str, Any]]:
    lines = []
    for raw_line in (normalize_lrc_lyrics(lrc) or "").splitlines():
        if match := LRC_LINE.match(raw_line.strip()):
            minutes, seconds, text = match.groups()
            start = round((int(minutes) * 60 + float(seconds)) * 1000)
            lines.append({"start": start, "value": text.strip()})
    return lines


def _strip_timestamps(lrc: str | None) -> str | None:
    lines = _parse_lrc(lrc)
    return "\n".join(line["value"] for line in lines) if lines else None
