"""Tests for the same-instance/different-id artist conflict check in ArtistsController."""

from __future__ import annotations

from unittest.mock import MagicMock, Mock

from music_assistant_models.enums import ExternalID
from music_assistant_models.media_items import Artist, ProviderMapping

from music_assistant.controllers.music.media.artists import ArtistsController
from music_assistant.models.music_provider import MusicProvider


def _artist(
    item_id: str,
    provider_instance: str,
    name: str,
    *,
    provider_domain: str = "tidal",
    external_ids: set[tuple[ExternalID, str]] | None = None,
    in_library: bool = False,
    available: bool = True,
) -> Artist:
    """Build an Artist as a provider returns it, or as the library row it was stored as."""
    return Artist(
        item_id="42" if in_library else item_id,
        provider="library" if in_library else provider_instance,
        name=name,
        external_ids=external_ids or set(),
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=provider_domain,
                provider_instance=provider_instance,
                available=available,
            )
        },
    )


def _controller(providers: dict[str, MusicProvider]) -> ArtistsController:
    """Return an ArtistsController with only mass.get_provider wired up."""
    ctrl = ArtistsController.__new__(ArtistsController)
    ctrl.mass = Mock()
    ctrl.mass.get_provider = Mock(side_effect=lambda instance, **_kwargs: providers.get(instance))
    return ctrl


def _streaming_provider(instance_id: str = "tidal1") -> MusicProvider:
    provider = MagicMock(spec=MusicProvider)
    provider.instance_id = instance_id
    provider.is_streaming_provider = True
    return provider


def _local_provider(instance_id: str = "filesystem1") -> MusicProvider:
    provider = MagicMock(spec=MusicProvider)
    provider.instance_id = instance_id
    provider.is_streaming_provider = False
    return provider


async def test_same_streaming_instance_different_ids_does_not_confirm() -> None:
    """Two artists on the same streaming instance under different ids stay distinct."""
    ctrl = _controller({"tidal1": _streaming_provider()})
    db_item = _artist("1", "tidal1", "loud", in_library=True)
    item = _artist("2", "tidal1", "LOUD")

    assert await ctrl._confirm_library_candidate(db_item, item) is False


async def test_unavailable_mapping_does_not_block_rematch() -> None:
    """A stale mapping on the same instance must not keep a re-added artist out."""
    ctrl = _controller({"tidal1": _streaming_provider()})
    db_item = _artist("1", "tidal1", "loud", in_library=True, available=False)
    item = _artist("2", "tidal1", "loud")

    assert await ctrl._confirm_library_candidate(db_item, item) is True


async def test_same_streaming_instance_different_ids_external_id_wins() -> None:
    """A shared external id is decided before the conflicting-provider-id check."""
    ctrl = _controller({"tidal1": _streaming_provider()})
    mb_id = {(ExternalID.MB_ARTIST, "123")}
    db_item = _artist("1", "tidal1", "loud", external_ids=mb_id)
    item = _artist("2", "tidal1", "LOUD", external_ids=mb_id)

    assert await ctrl._confirm_library_candidate(db_item, item) is True


async def test_same_non_streaming_instance_different_ids_still_confirms() -> None:
    """A non-streaming (e.g. filesystem) instance may legitimately reuse ids for the same artist."""
    ctrl = _controller({"filesystem1": _local_provider()})
    db_item = _artist("1", "filesystem1", "Artist A", provider_domain="filesystem")
    item = _artist("2", "filesystem1", "Artist A", provider_domain="filesystem")

    assert await ctrl._confirm_library_candidate(db_item, item) is True


async def test_different_instances_same_name_still_confirms() -> None:
    """Different provider instances with different ids still merge on a shared name."""
    ctrl = _controller(
        {"tidal1": _streaming_provider("tidal1"), "spotify1": _streaming_provider("spotify1")}
    )
    db_item = _artist("1", "tidal1", "Artist A")
    item = _artist("2", "spotify1", "Artist A", provider_domain="spotify")

    assert await ctrl._confirm_library_candidate(db_item, item) is True
