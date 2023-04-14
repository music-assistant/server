"""The Musicbrainz Metadata provider for Music Assistant.

At this time only used for retrieval of ID's but to be expanded to fetch metadata too.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from json import JSONDecodeError
from typing import TYPE_CHECKING, Any

import aiohttp.client_exceptions
from asyncio_throttle import Throttler

from music_assistant.common.helpers.util import create_sort_name
from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
from music_assistant.common.models.enums import ProviderFeature
from music_assistant.server.controllers.cache import use_cache
from music_assistant.server.helpers.compare import compare_strings
from music_assistant.server.models.metadata_provider import MetadataProvider

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.media_items import Album, Artist, Track
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType


LUCENE_SPECIAL = r'([+\-&|!(){}\[\]\^"~*?:\\\/])'

SUPPORTED_FEATURES = (ProviderFeature.GET_ARTIST_MBID,)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = MusicbrainzProvider(mass, manifest, config)
    await prov.handle_setup()
    return prov


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    return tuple()  # we do not have any config entries (yet)


class MusicbrainzProvider(MetadataProvider):
    """The Musicbrainz Metadata provider."""

    throttler: Throttler

    async def handle_setup(self) -> None:
        """Handle async initialization of the provider."""
        self.cache = self.mass.cache
        self.throttler = Throttler(rate_limit=1, period=1)

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

    async def get_musicbrainz_artist_id(
        self, artist: Artist, ref_albums: Iterable[Album], ref_tracks: Iterable[Track]
    ) -> str | None:
        """Discover MusicBrainzArtistId for an artist given some reference albums/tracks."""
        for ref_album in ref_albums:
            # try matching on album musicbrainz id
            if ref_album.musicbrainz_id:  # noqa: SIM102
                if musicbrainz_id := await self._search_artist_by_album_mbid(
                    artistname=artist.name, album_mbid=ref_album.musicbrainz_id
                ):
                    return musicbrainz_id
            # try matching on album barcode
            for barcode in ref_album.barcode:
                if musicbrainz_id := await self._search_artist_by_album(
                    artistname=artist.name,
                    album_barcode=barcode,
                ):
                    return musicbrainz_id

        # try again with matching on track isrc
        for ref_track in ref_tracks:
            for isrc in ref_track.isrc:
                if musicbrainz_id := await self._search_artist_by_track(
                    artistname=artist.name,
                    track_isrc=isrc,
                ):
                    return musicbrainz_id

        # last restort: track matching by name
        for ref_track in ref_tracks:
            if musicbrainz_id := await self._search_artist_by_track(
                artistname=artist.name,
                trackname=ref_track.name,
            ):
                return musicbrainz_id

        return None

    async def _search_artist_by_album(
        self,
        artistname: str,
        albumname: str | None = None,
        album_barcode: str | None = None,
    ) -> str | None:
        """Retrieve musicbrainz artist id by providing the artist name and albumname or barcode."""
        assert albumname or album_barcode
        for searchartist in (
            artistname,
            re.sub(LUCENE_SPECIAL, r"\\\1", create_sort_name(artistname)),
        ):
            if album_barcode:
                # search by album barcode (EAN or UPC)
                query = f"barcode:{album_barcode}"
            elif albumname:
                # search by name
                searchalbum = re.sub(LUCENE_SPECIAL, r"\\\1", albumname)
                query = f'artist:"{searchartist}" AND release:"{searchalbum}"'
            result = await self.get_data("release", query=query)
            if result and "releases" in result:
                for strict in (True, False):
                    for item in result["releases"]:
                        if not (
                            album_barcode
                            or (albumname and compare_strings(item["title"], albumname, strict))
                        ):
                            continue
                        for artist in item["artist-credit"]:
                            if compare_strings(artist["artist"]["name"], artistname, strict):
                                return artist["artist"]["id"]  # type: ignore[no-any-return]
                            for alias in artist.get("aliases", []):
                                if compare_strings(alias["name"], artistname, strict):
                                    return artist["id"]  # type: ignore[no-any-return]
        return None

    async def _search_artist_by_track(
        self,
        artistname: str,
        trackname: str | None = None,
        track_isrc: str | None = None,
    ) -> str | None:
        """Retrieve artist id by providing the artist name and trackname or track isrc."""
        assert trackname or track_isrc
        searchartist = re.sub(LUCENE_SPECIAL, r"\\\1", artistname)
        if track_isrc:
            result = await self.get_data(f"isrc/{track_isrc}", inc="artist-credits")
        elif trackname:
            searchtrack = re.sub(LUCENE_SPECIAL, r"\\\1", trackname)
            result = await self.get_data(
                "recording", query=f'"{searchtrack}" AND artist:"{searchartist}"'
            )
        if result and "recordings" in result:
            for strict in (True, False):
                for item in result["recordings"]:
                    if not (
                        track_isrc
                        or (trackname and compare_strings(item["title"], trackname, strict))
                    ):
                        continue
                    for artist in item["artist-credit"]:
                        if compare_strings(artist["artist"]["name"], artistname, strict):
                            return artist["artist"]["id"]  # type: ignore[no-any-return]
                        for alias in artist["artist"].get("aliases", []):
                            if compare_strings(alias["name"], artistname, strict):
                                return artist["artist"]["id"]  # type: ignore[no-any-return]
        return None

    async def _search_artist_by_album_mbid(self, artistname: str, album_mbid: str) -> str | None:
        """Retrieve musicbrainz artist id by providing the artist name or album id."""
        result = await self.get_data(f"release-group/{album_mbid}?inc=artist-credits")
        if result and "artist-credit" in result:
            for item in result["artist-credit"]:
                if (artist := item.get("artist")) and compare_strings(artistname, artist["name"]):
                    return artist["id"]  # type: ignore[no-any-return]
        return None

    @use_cache(86400 * 30)
    async def get_data(self, endpoint: str, **kwargs: dict[str, Any]) -> Any:
        """Get data from api."""
        url = f"http://musicbrainz.org/ws/2/{endpoint}"
        headers = {"User-Agent": "Music Assistant/1.0.0 https://github.com/music-assistant"}
        kwargs["fmt"] = "json"  # type: ignore[assignment]
        async with self.throttler:
            async with self.mass.http_session.get(
                url, headers=headers, params=kwargs, ssl=False
            ) as response:
                try:
                    result = await response.json()
                except (
                    aiohttp.client_exceptions.ContentTypeError,
                    JSONDecodeError,
                ) as exc:
                    msg = await response.text()
                    self.logger.warning("%s - %s", str(exc), msg)
                    result = None
                return result
