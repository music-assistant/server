from music_assistant.common.models.enums import ProviderFeature
from music_assistant.server.models.music_provider import MusicProvider

from .helpers import *


class DeezerProvider(MusicProvider):
    """Deezer provider support"""

    async def setup(self) -> None:
        """Set up the Deezer provider."""
        try:
            creds = credential(
                self.config.get_value("app_id"),
                self.config.get_value("app_secret"),
                self.config.get_value("access_token"),
            )
        except:
            raise TypeError("whatever")

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        return (
            ProviderFeature.SEARCH,
        )
