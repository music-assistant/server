"""Builder functions for Music Assistant model instances used in tests."""

from music_assistant_models.media_items import Album, Artist, Playlist, ProviderMapping, Track


def make_provider_mapping(
    provider_domain: str = "mock_provider",
    item_id: str = "test-item-1",
) -> ProviderMapping:
    """Create a minimal ProviderMapping for test items."""
    return ProviderMapping(
        item_id=item_id,
        provider_domain=provider_domain,
        provider_instance=provider_domain,
    )


def make_track(
    item_id: str = "test-track-1",
    name: str = "Test Track",
    provider_domain: str = "mock_provider",
    duration: int = 180,
) -> Track:
    """Create a Track with sensible test defaults."""
    return Track(
        item_id=item_id,
        provider=provider_domain,
        name=name,
        duration=duration,
        provider_mappings={make_provider_mapping(provider_domain, item_id)},
    )


def make_album(
    item_id: str = "test-album-1",
    name: str = "Test Album",
    provider_domain: str = "mock_provider",
) -> Album:
    """Create an Album with sensible test defaults."""
    return Album(
        item_id=item_id,
        provider=provider_domain,
        name=name,
        provider_mappings={make_provider_mapping(provider_domain, item_id)},
    )


def make_artist(
    item_id: str = "test-artist-1",
    name: str = "Test Artist",
    provider_domain: str = "mock_provider",
) -> Artist:
    """Create an Artist with sensible test defaults."""
    return Artist(
        item_id=item_id,
        provider=provider_domain,
        name=name,
        provider_mappings={make_provider_mapping(provider_domain, item_id)},
    )


def make_playlist(
    item_id: str = "test-playlist-1",
    name: str = "Test Playlist",
    provider_domain: str = "mock_provider",
) -> Playlist:
    """Create a Playlist with sensible test defaults."""
    return Playlist(
        item_id=item_id,
        provider=provider_domain,
        name=name,
        provider_mappings={make_provider_mapping(provider_domain, item_id)},
        is_editable=True,
    )
