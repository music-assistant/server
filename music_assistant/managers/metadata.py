"""All logic for metadata retrieval."""

import logging
from typing import Dict, List

from music_assistant.helpers.cache import cached
from music_assistant.helpers.typing import MusicAssistant
from music_assistant.helpers.util import merge_dict
from music_assistant.models.provider import MetadataProvider, ProviderType

LOGGER = logging.getLogger("metadata_manager")


class MetaDataManager:
    """Several helpers to search and store metadata for mediaitems using metadata providers."""

    # TODO: create periodic task to search for missing metadata
    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        self.mass = mass
        self.cache = mass.cache

    @property
    def providers(self) -> List[MetadataProvider]:
        """Return all providers of type MetadataProvider."""
        return self.mass.get_providers(ProviderType.METADATA_PROVIDER)

    async def get_artist_metadata(self, mb_artist_id: str, cur_metadata: Dict) -> Dict:
        """Get/update rich metadata for an artist by providing the musicbrainz artist id."""
        metadata = cur_metadata
        for provider in self.providers:
            if "fanart" in metadata:
                # no need to query (other) metadata providers if we already have a result
                break
            cache_key = f"{provider.id}.artist_metadata.{mb_artist_id}"
            res = await cached(
                self.cache, cache_key, provider.get_artist_images, mb_artist_id
            )
            if res:
                metadata = merge_dict(metadata, res)
        return metadata
