"""Minimal ListenBrainz playlist provider."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ExternalID, MediaType, ProviderFeature
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    ItemMapping,
    Playlist,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {ProviderFeature.BROWSE}
SAMPLE_PLAYLIST: dict[str, Any] = json.loads(
    Path(__file__).with_name("sample_playlist.json").read_text(encoding="utf-8")
)
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
        return [
            self._parse_track(item, position)
            for position, item in enumerate(SAMPLE_PLAYLIST["track"], 1)
        ]

    def _parse_track(self, item: dict[str, Any], position: int) -> Track:
        """Parse one metadata-only ListenBrainz playlist entry."""
        track = Track(
            item_id=item["id"],
            provider=self.instance_id,
            name=item["title"],
            duration=item["duration"] // 1000,
            artists=UniqueList(
                [
                    ItemMapping(
                        item_id=item["creator"],
                        provider=self.instance_id,
                        name=item["creator"],
                        media_type=MediaType.ARTIST,
                    )
                ]
            ),
            album=ItemMapping(
                item_id=item["album"],
                provider=self.instance_id,
                name=item["album"],
                media_type=MediaType.ALBUM,
            ),
            provider_mappings={
                ProviderMapping(
                    item_id=item["id"],
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=False,
                )
            },
            external_ids={(ExternalID.MB_RECORDING, item["id"])},
        )
        track.position = position
        return track
