"""Library (follows) support for the iHeartRadio provider."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError, MusicAssistantError

from .constants import (
    FOLLOWS_PAGE_LIMIT,
    PATH_FOLLOWS_ARTIST,
    PATH_FOLLOWS_ARTIST_ITEM,
    PATH_FOLLOWS_LIVE,
    PATH_FOLLOWS_LIVE_ITEM,
    PATH_PODCAST_FOLLOW_ITEM,
    PATH_PODCAST_FOLLOWS,
    PODCAST_FOLLOWS_PAGE_LIMIT,
)
from .parsers import (
    parse_artist_radio,
    parse_live_station,
    parse_podcast,
    split_artist_radio_item_id,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.media_items import MediaItemType, Podcast, Radio

    from .provider import IHeartRadioProvider


class IHeartRadioLibraryManager:
    """Sync the followed stations, artists and podcasts of the signed-in account."""

    def __init__(self, provider: IHeartRadioProvider) -> None:
        """Initialize the library manager."""
        self.provider = provider
        self.instance_id = provider.instance_id
        self.domain = provider.domain

    async def get_library_radios(self) -> AsyncGenerator[Radio]:
        """Yield the followed live stations, then the followed artists as artist radios."""
        for entry in await self._follow_entries(PATH_FOLLOWS_LIVE):
            if not (station_id := entry.get("liveStationId")):
                continue
            try:
                station = await self.provider.get_station(str(station_id))
            except MusicAssistantError as err:
                self.provider.report_skipped_sync_item(MediaType.RADIO, str(station_id), err)
                continue
            if station and (radio := parse_live_station(station, self.instance_id, self.domain)):
                yield radio
        for entry in await self._follow_entries(PATH_FOLLOWS_ARTIST):
            if radio := parse_artist_radio(entry, self.instance_id, self.domain):
                yield radio

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast]:
        """Yield the followed podcasts."""
        page_key: str | None = None
        while True:
            payload = await self.provider.request(
                "GET",
                PATH_PODCAST_FOLLOWS,
                params={"limit": PODCAST_FOLLOWS_PAGE_LIMIT, "pageKey": page_key},
            )
            if not isinstance(payload, dict):
                return
            for entry in payload.get("data") or []:
                if isinstance(entry, dict) and (
                    podcast := parse_podcast(entry, self.instance_id, self.domain)
                ):
                    yield podcast
            links = payload.get("links") or {}
            if not (page_key := links.get("next") if isinstance(links, dict) else None):
                return

    async def library_add(self, item: MediaItemType) -> bool:
        """
        Follow a live station, artist radio or podcast.

        :param item: The item to follow.
        """
        if item.media_type == MediaType.PODCAST:
            await self.provider.request(
                "PUT", PATH_PODCAST_FOLLOW_ITEM.format(podcast_id=item.item_id)
            )
        elif item.media_type == MediaType.RADIO:
            if artist_id := split_artist_radio_item_id(item.item_id):
                await self.provider.request(
                    "PUT", PATH_FOLLOWS_ARTIST, json={"artistId": _as_int(artist_id)}
                )
            else:
                await self.provider.request(
                    "PUT", PATH_FOLLOWS_LIVE, json={"liveStationId": _as_int(item.item_id)}
                )
        else:
            return False
        return True

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """
        Unfollow a live station, artist radio or podcast.

        :param prov_item_id: The provider item id.
        :param media_type: The media type of the item.
        """
        if media_type == MediaType.PODCAST:
            path = PATH_PODCAST_FOLLOW_ITEM.format(podcast_id=prov_item_id)
        elif media_type == MediaType.RADIO:
            if artist_id := split_artist_radio_item_id(prov_item_id):
                path = PATH_FOLLOWS_ARTIST_ITEM.format(artist_id=artist_id)
            else:
                path = PATH_FOLLOWS_LIVE_ITEM.format(station_id=prov_item_id)
        else:
            return False
        await self.provider.request("DELETE", path)
        return True

    async def _follow_entries(self, path: str) -> list[dict[str, Any]]:
        """
        Return every entry of a follow list.

        :param path: The follow list to page through.
        """
        entries: list[dict[str, Any]] = []
        while True:
            try:
                payload = await self.provider.request(
                    "GET", path, params={"limit": FOLLOWS_PAGE_LIMIT, "offset": len(entries)}
                )
            except MediaNotFoundError:
                # the API answers an empty follow list with a 404
                return entries
            page = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(page, list):
                return entries
            entries.extend(entry for entry in page if isinstance(entry, dict))
            if len(page) < FOLLOWS_PAGE_LIMIT:
                return entries


def _as_int(value: str) -> int | str:
    """Return an id as the number the follow endpoints expect, or unchanged if it is none."""
    return int(value) if value.isdigit() else value
