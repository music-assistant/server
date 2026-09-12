"""Browse support for the iHeartRadio provider."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from music_assistant_models.media_items import BrowseFolder

from .constants import BROWSE_GENRES, BROWSE_LIVE, BROWSE_MARKETS, BROWSE_PODCASTS
from .parsers import parse_live_station, parse_podcast, remote_image

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from typing import Any

    from music_assistant_models.media_items import ItemMapping, MediaItemImage, MediaItemType

    from .provider import IHeartRadioProvider


class IHeartRadioBrowseManager:
    """Build the iHeartRadio browse tree."""

    def __init__(self, provider: IHeartRadioProvider) -> None:
        """Initialize the browse manager."""
        self.provider = provider
        self.instance_id = provider.instance_id
        self.domain = provider.domain

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse iHeartRadio content.

        :param path: The path to browse, e.g. ``iheartradio--xx://live/genres``.
        """
        segments = _path_segments(path)
        if not segments:
            return self._root_folders(path)
        if segments[0] == BROWSE_LIVE:
            return await self._browse_live(path, segments[1:])
        if segments[0] == BROWSE_PODCASTS:
            if len(segments) > 1:
                return await self._browse_category(segments[1])
            return await self._browse_categories(path)
        return []

    def _root_folders(self, path: str) -> list[BrowseFolder]:
        """Return the top level: live radio and podcasts."""
        base = _base_path(path)
        return [
            BrowseFolder(
                item_id=BROWSE_LIVE,
                provider=self.instance_id,
                path=f"{base}{BROWSE_LIVE}",
                name="Live Radio",
                translation_key="live_radio",
            ),
            BrowseFolder(
                item_id=BROWSE_PODCASTS,
                provider=self.instance_id,
                path=f"{base}{BROWSE_PODCASTS}",
                name="Podcasts",
                translation_key="podcasts",
            ),
        ]

    async def _browse_live(
        self, path: str, segments: Sequence[str]
    ) -> Sequence[MediaItemType | BrowseFolder]:
        """
        Browse the live radio branch.

        :param path: The path being browsed, used to build child paths.
        :param segments: The path segments below the live radio folder.
        """
        if not segments:
            return self._live_folders(path)
        if segments[0] == BROWSE_MARKETS:
            if len(segments) > 1:
                return await self._stations(market_id=segments[1])
            return await self._browse_markets(path)
        if segments[0] == BROWSE_GENRES:
            if len(segments) > 1:
                return await self._stations(genre_id=segments[1])
            return await self._browse_genres(path)
        return []

    def _live_folders(self, path: str) -> list[BrowseFolder]:
        """Return the two ways into the station catalogue."""
        base = _base_path(path)
        return [
            BrowseFolder(
                item_id=BROWSE_MARKETS,
                provider=self.instance_id,
                path=f"{base}{BROWSE_MARKETS}",
                name="By City",
                translation_key="by_market",
            ),
            BrowseFolder(
                item_id=BROWSE_GENRES,
                provider=self.instance_id,
                path=f"{base}{BROWSE_GENRES}",
                name="By Genre",
                translation_key="by_genre",
            ),
        ]

    async def _browse_markets(self, path: str) -> list[BrowseFolder]:
        """Return a folder per market (city) of the configured country."""
        base = _base_path(path)
        folders: list[BrowseFolder] = []
        for market in await self.provider.get_markets():
            if not (market_id := market.get("marketId")):
                continue
            folders.append(
                BrowseFolder(
                    item_id=f"market_{market_id}",
                    provider=self.instance_id,
                    path=f"{base}{market_id}",
                    name=_market_name(market),
                )
            )
        return sorted(folders, key=lambda folder: folder.name.lower())

    async def _browse_genres(self, path: str) -> list[BrowseFolder]:
        """Return a folder per live station genre."""
        base = _base_path(path)
        folders: list[BrowseFolder] = []
        for genre in await self.provider.get_genres():
            if not (genre_id := genre.get("id")) or not (name := genre.get("genreName")):
                continue
            folders.append(
                BrowseFolder(
                    item_id=f"genre_{genre_id}",
                    provider=self.instance_id,
                    path=f"{base}{genre_id}",
                    name=str(name),
                    image=self._folder_image(genre),
                )
            )
        return folders

    async def _browse_categories(self, path: str) -> list[BrowseFolder]:
        """Return a folder per podcast category."""
        base = _base_path(path)
        folders: list[BrowseFolder] = []
        for category in await self.provider.get_podcast_categories():
            if not (category_id := category.get("id")) or not (name := category.get("name")):
                continue
            folders.append(
                BrowseFolder(
                    item_id=f"category_{category_id}",
                    provider=self.instance_id,
                    path=f"{base}{category_id}",
                    name=str(name),
                    image=self._folder_image(category),
                )
            )
        return folders

    async def _browse_category(self, category_id: str) -> list[MediaItemType]:
        """
        Return the podcasts of one category.

        :param category_id: The iHeartRadio podcast category id.
        """
        return [
            podcast
            for item in await self.provider.get_podcast_category(category_id)
            if (podcast := parse_podcast(item, self.instance_id, self.domain))
        ]

    async def _stations(
        self, market_id: str | None = None, genre_id: str | None = None
    ) -> list[MediaItemType]:
        """
        Return the stations of one market or genre.

        :param market_id: The iHeartRadio market id to list.
        :param genre_id: The iHeartRadio genre id to list.
        """
        return [
            station
            for item in await self.provider.get_stations(market_id=market_id, genre_id=genre_id)
            if (station := parse_live_station(item, self.instance_id, self.domain))
        ]

    def _folder_image(self, item: Mapping[str, Any]) -> MediaItemImage | None:
        """Return the artwork of a genre or category, if it has any."""
        if (image := item.get("image")) and _is_image_url(str(image)):
            return remote_image(str(image), self.instance_id)
        return None


def _is_image_url(url: str) -> bool:
    """
    Return whether an artwork url points at an actual file.

    Most podcast categories carry a placeholder that encodes a bare host, which the CDN
    rejects with a 400.
    """
    if "/v3/url/" not in url:
        return True
    encoded = url.split("/v3/url/", 1)[1].split("?", 1)[0]
    try:
        origin = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    except ValueError, UnicodeDecodeError:
        return False
    return bool(urlparse(origin).path.strip("/"))


def _base_path(path: str) -> str:
    """Return the browse path with a trailing separator, ready for a child segment."""
    return path if path.endswith("/") else f"{path}/"


def _path_segments(path: str) -> list[str]:
    """Return the path segments below the provider root."""
    if "://" not in path:
        return []
    return [segment for segment in path.split("://", 1)[1].split("/") if segment]


def _market_name(market: Mapping[str, Any]) -> str:
    """Return a market's display name, e.g. 'Sydney, NSW'."""
    city = str(market.get("city") or market.get("name") or market.get("marketId"))
    state = market.get("stateAbbreviation")
    return f"{city}, {state}" if state else city
