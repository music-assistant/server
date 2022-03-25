"""Models for Metadata providers/plugins."""

import logging
from abc import abstractmethod
from typing import Dict, List

from ..config.models import ConfigEntry
from ..helpers.typing import MusicAssistant


class MetadataProvider:
    """Model for a MetadataProvider."""

    def __init__(self, mass: MusicAssistant, id: str, name: str) -> None:
        """Initialize the provider."""
        # pylint: disable=redefined-builtin
        self.mass = mass
        self.logger = logging.getLogger(id)
        self._attr_id = None
        self._attr_name = None
        self._attr_config_entries: List[ConfigEntry] | None = None

    @property
    def id(self) -> str:
        """Return provider ID for this provider."""
        return self._attr_id

    @property
    def name(self) -> str:
        """Return provider Name for this provider."""
        return self._attr_name

    @property
    def config_entries(self) -> List[ConfigEntry] | None:
        """Return Config Entries for this provider (if any)."""
        return self._attr_config_entries

    @abstractmethod
    async def get_artist_images(self, mb_artist_id: str) -> Dict:
        """Retrieve artist metadata as dict by musicbrainz artist id."""
        raise NotImplementedError

    @abstractmethod
    async def get_album_images(self, mb_album_id: str) -> Dict:
        """Retrieve album metadata as dict by musicbrainz album id."""
        raise NotImplementedError
