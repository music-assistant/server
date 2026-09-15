"""Regression tests for external-id precedence in the similar-artists dedup."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from music_assistant_models.enums import ExternalID, ProviderFeature
from music_assistant_models.media_items import Artist, ProviderMapping

from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

INSTANCE = "tidal1"
DOMAIN = "tidal"


class _StubProvider(MusicProvider):
    """Minimal streaming provider returning a fixed similar-artists listing."""

    def __init__(self) -> None:
        """Initialize the stub provider without going through Provider.__init__."""
        self.config = MagicMock()
        self.config.instance_id = INSTANCE
        self.manifest = MagicMock()
        self.manifest.domain = DOMAIN
        self.logger = MagicMock()
        self.available = True
        self._supported_features = {ProviderFeature.SIMILAR_ARTISTS}
        self.similar_artists: list[Artist] = []

    @property
    def is_streaming_provider(self) -> bool:
        """Behave as a streaming provider, so same-instance id conflicts are enforced."""
        return True

    async def get_similar_artists(self, prov_artist_id: str, limit: int = 25) -> list[Artist]:
        """Return the fixed similar-artists listing prepared for the test."""
        return self.similar_artists


def _mapping(item_id: str) -> ProviderMapping:
    """Create a provider mapping on the shared test instance."""
    return ProviderMapping(
        item_id=item_id, provider_domain=DOMAIN, provider_instance=INSTANCE, in_library=True
    )


def _candidate(
    item_id: str, name: str, external_ids: set[tuple[ExternalID, str]] | None = None
) -> Artist:
    """Build a similar-artist candidate as the stub provider would return it."""
    return Artist(
        item_id=item_id,
        provider=INSTANCE,
        name=name,
        external_ids=external_ids or set(),
        provider_mappings={_mapping(item_id)},
    )


async def _add_reference_artist(mass: MusicAssistant) -> tuple[Artist, _StubProvider]:
    """Add the library artist whose similar-artists listing is under test."""
    provider = _StubProvider()
    mass._providers[INSTANCE] = provider
    library_artist = await mass.music.artists.add_item_to_library(
        Artist(item_id="1", provider=INSTANCE, name="Reference", provider_mappings={_mapping("1")})
    )
    return library_artist, provider


async def test_similar_artists_dedupes_on_shared_external_id(mass: MusicAssistant) -> None:
    """Two same-instance candidates sharing a MusicBrainz id count as one artist."""
    library_artist, provider = await _add_reference_artist(mass)
    mb_id = {(ExternalID.MB_ARTIST, "same-artist")}
    provider.similar_artists = [
        _candidate("10", "Similar", mb_id),
        _candidate("11", "Similar", mb_id),
    ]

    result = await mass.music.artists.similar_artists(library_artist.item_id, "library")

    assert len(result) == 1


async def test_similar_artists_keeps_conflicting_ids_apart(mass: MusicAssistant) -> None:
    """Two same-instance candidates with no shared external id stay distinct."""
    library_artist, provider = await _add_reference_artist(mass)
    provider.similar_artists = [
        _candidate("10", "Similar"),
        _candidate("11", "Similar"),
    ]

    result = await mass.music.artists.similar_artists(library_artist.item_id, "library")

    assert len(result) == 2
