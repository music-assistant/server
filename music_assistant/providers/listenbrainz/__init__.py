"""Minimal ListenBrainz playlist provider."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ExternalID, MediaType, ProviderFeature
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Artist,
    ItemMapping,
    Playlist,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.listenbrainz.parsers import parse_artists

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {ProviderFeature.BROWSE}
SAMPLE_RESPONSE: dict[str, Any] = json.loads(
    Path(__file__).with_name("sample_playlist.json").read_text(encoding="utf-8")
)
SAMPLE_PLAYLIST: dict[str, Any] = SAMPLE_RESPONSE["playlist"]
SAMPLE_PLAYLIST_ID = SAMPLE_PLAYLIST["identifier"].rsplit("/", 1)[-1]


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize the ListenBrainz provider."""
    return ListenBrainzProvider(mass, manifest, config, SUPPORTED_FEATURES)


class ListenBrainzProvider(PluginProvider):
    """Expose a static ListenBrainz playlist for initial development."""

    async def browse(self, path: str) -> list[Playlist]:
        """
        Return the static sample playlist.

        :param path: Provider browse path.
        """
        return [await self.get_playlist(SAMPLE_PLAYLIST_ID)]

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Return the static sample playlist."""
        if prov_playlist_id != SAMPLE_PLAYLIST_ID:
            raise MediaNotFoundError(f"Unknown ListenBrainz playlist: {prov_playlist_id}")
        return Playlist(
            item_id=SAMPLE_PLAYLIST_ID,
            provider=self.instance_id,
            name=SAMPLE_PLAYLIST["title"],
            owner="listenbrainz",
            is_editable=False,
            provider_mappings={
                ProviderMapping(
                    item_id=SAMPLE_PLAYLIST_ID,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    is_unique=True,
                )
            },
        )

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Return metadata-only tracks from the static sample playlist."""
        if prov_playlist_id != SAMPLE_PLAYLIST_ID:
            raise MediaNotFoundError(f"Unknown ListenBrainz playlist: {prov_playlist_id}")
        if page > 0:
            return []
        tracks: list[Track] = []
        for position, item in enumerate(SAMPLE_PLAYLIST["track"], 1):
            tracks.append(await self._parse_track(item, position))
        return tracks

    async def _parse_track(self, item: dict[str, Any], position: int) -> Track:
        """Return a matching library track or parse a metadata-only playlist entry."""
        recording_mbid = item["identifier"][0].rsplit("/", 1)[-1]
        if library_track := await self.mass.music.tracks.get_library_item_by_external_id(
            recording_mbid, ExternalID.MB_RECORDING
        ):
            library_track.position = position
            return library_track
        parsed_artists = parse_artists(item, self.instance_id, self.domain)
        track = Track(
            item_id=recording_mbid,
            provider=self.instance_id,
            name=item["title"],
            duration=item["duration"] // 1000,
            artists=UniqueList([await self._resolve_artist(artist) for artist in parsed_artists]),
            album=ItemMapping(
                item_id=item["album"],
                provider=self.instance_id,
                name=item["album"],
                media_type=MediaType.ALBUM,
            ),
            provider_mappings={
                ProviderMapping(
                    item_id=recording_mbid,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=False,
                )
            },
            external_ids={(ExternalID.MB_RECORDING, recording_mbid)},
        )
        track.position = position
        return track

    async def _resolve_artist(self, artist: Artist) -> Artist:
        """Return an existing library artist or the parsed unavailable artist."""
        mbid = artist.get_external_id(ExternalID.MB_ARTIST)
        assert mbid is not None
        if library_artist := await self.mass.music.artists.get_library_item_by_external_id(
            mbid, ExternalID.MB_ARTIST
        ):
            return library_artist
        return artist
