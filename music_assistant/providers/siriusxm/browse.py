"""Browse support for the SiriusXM provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.media_items import BrowseFolder

from .constants import (
    BROWSE_CHANNELS,
    BROWSE_GENRES,
    BROWSE_LIBRARY,
    BROWSE_XTRA,
    TRACK_QUEUE_TYPES,
)
from .parsers import parse_radio, parse_station, parse_xtra_playlist

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from aiosxm import Channel
    from music_assistant_models.media_items import ItemMapping, MediaItemType

    from .provider import SiriusXMProvider


class SiriusXMBrowseManager:
    """Build the SiriusXM browse tree."""

    def __init__(self, provider: SiriusXMProvider) -> None:
        """Initialize the browse manager."""
        self.provider = provider
        self.instance_id = provider.instance_id
        self.domain = provider.domain
        self.logger = provider.logger
        # Genre names go into the browse path, so map a URL-safe form back to
        # the name SiriusXM knows.
        self._genre_slugs: dict[str, str] = {}

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse SiriusXM content.

        :param path: The path to browse, e.g. ``siriusxm--xx://genres/Rock``.
        """
        subpath, sub_subpath = _split_path(path)

        if not subpath:
            return self._root_folders(path)
        if subpath == BROWSE_LIBRARY:
            return await self._browse_library()
        if subpath == BROWSE_CHANNELS:
            return await self._browse_channels(linear=True)
        if subpath == BROWSE_XTRA:
            return await self._browse_channels(linear=False)
        if subpath == BROWSE_GENRES:
            if sub_subpath:
                return await self._browse_genre(sub_subpath)
            return await self._browse_genres(path)
        return []

    def _root_folders(self, path: str) -> list[BrowseFolder]:
        """Return the top level: the account's own items, then the catalogue."""
        base = path if path.endswith("/") else path + "/"
        return [
            BrowseFolder(
                item_id=BROWSE_LIBRARY,
                provider=self.instance_id,
                path=f"{base}{BROWSE_LIBRARY}",
                name="My Library",
                translation_key="library",
            ),
            BrowseFolder(
                item_id=BROWSE_CHANNELS,
                provider=self.instance_id,
                path=f"{base}{BROWSE_CHANNELS}",
                name="Channels",
                translation_key="channels",
            ),
            BrowseFolder(
                item_id=BROWSE_XTRA,
                provider=self.instance_id,
                path=f"{base}{BROWSE_XTRA}",
                name="Xtra Channels",
                translation_key="xtra_channels",
            ),
            BrowseFolder(
                item_id=BROWSE_GENRES,
                provider=self.instance_id,
                path=f"{base}{BROWSE_GENRES}",
                name="Genres",
                translation_key="genres",
            ),
        ]

    async def _browse_library(self) -> list[MediaItemType]:
        """Return everything saved to the account, in one listing."""
        # Saved channels are radio and saved stations are dynamic playlists, but
        # a listener thinks of them as one list of favourites, so they are not
        # split by type here.
        # Both listings come out of the same library call, so it is made once
        # here rather than once per generator.
        channels = await self.provider.get_library_channels()
        items: list[MediaItemType] = [
            parse_xtra_playlist(channel, self.instance_id, self.domain)
            if channel.type in TRACK_QUEUE_TYPES
            else parse_radio(channel, self.instance_id, self.domain)
            for channel in channels
        ]
        items.extend(
            parse_station(station, self.instance_id, self.domain)
            for station in await self.provider.client.get_library_artist_stations()
        )
        return sorted(items, key=lambda item: item.name.lower())

    async def _browse_channels(self, linear: bool) -> list[MediaItemType]:
        """
        Return the channel list.

        :param linear: True for live linear channels, False for Xtra channels.
        """
        channels = await self.provider.get_channels()
        ordered = _by_channel_number(c for c in channels if c.is_linear is linear)
        if linear:
            return [
                parse_radio(channel, self.instance_id, self.domain, number_prefix=True)
                for channel in ordered
            ]
        # Xtra channels are runs of discrete tracks, so they browse as playlists.
        return [
            parse_xtra_playlist(channel, self.instance_id, self.domain, number_prefix=True)
            for channel in ordered
        ]

    async def _browse_genres(self, path: str) -> list[BrowseFolder]:
        """Return a folder per genre, ordered by how many channels it holds."""
        base = path if path.endswith("/") else path + "/"
        genres = await self.provider.get_genres()
        folders: list[BrowseFolder] = []
        # Busiest genres first: the long tail is mostly one-off channels.
        for genre, _count in sorted(genres.items(), key=lambda kv: (-kv[1], kv[0])):
            slug = genre.replace("/", "-")
            self._genre_slugs[slug] = genre
            folders.append(
                BrowseFolder(
                    item_id=f"genre_{slug}",
                    provider=self.instance_id,
                    path=f"{base}{slug}",
                    name=genre,
                )
            )
        return folders

    async def _browse_genre(self, slug: str) -> list[MediaItemType]:
        """
        Return the channels within one genre.

        :param slug: The URL-safe genre name taken from the browse path.
        """
        genre = self._genre_slugs.get(slug, slug)
        channels = await self.provider.get_channels()
        return [
            parse_xtra_playlist(channel, self.instance_id, self.domain, number_prefix=True)
            if channel.type in TRACK_QUEUE_TYPES
            else parse_radio(channel, self.instance_id, self.domain, number_prefix=True)
            for channel in _by_channel_number(c for c in channels if c.genre == genre)
        ]


def _by_channel_number(channels: Iterable[Channel]) -> list[Channel]:
    """Sort channels by their dial number, which is how listeners find them."""

    def sort_key(channel: Channel) -> tuple[int, str]:
        number = channel.channel_number
        if number and number.isdigit():
            return (int(number), channel.title)
        # Channels without a usable number sort last, alphabetically.
        return (10**6, channel.title)

    return sorted(channels, key=sort_key)


def _split_path(path: str) -> tuple[str | None, str | None]:
    """Split a browse path into its first two segments below the provider root."""
    if "://" not in path:
        return None, None
    parts = [part for part in path.split("://", 1)[1].split("/") if part]
    subpath = parts[0] if parts else None
    sub_subpath = parts[1] if len(parts) > 1 else None
    return subpath, sub_subpath
