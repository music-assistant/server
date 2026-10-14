"""Tests for the versions listings: searched for once, then sorted and paged on the server."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.enums import SortField
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import (
    Album,
    Audiobook,
    Podcast,
    ProviderMapping,
    Radio,
    Track,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

# the controllers with a versions listing, and the type of their items
CONTROLLERS = [
    ("albums", Album),
    ("tracks", Track),
    ("podcasts", Podcast),
    ("audiobooks", Audiobook),
    ("radio", Radio),
]


@pytest.fixture(name="mass")
def mass_fixture(music_mass_with_cache: MusicAssistant) -> MusicAssistant:
    """Run on a library-only instance with a cache store: versions listings are cached."""
    return music_mass_with_cache


def _versions(item_cls: type[Any]) -> list[Any]:
    """Return three versions as the providers find them, each on another provider."""
    return [
        item_cls(
            item_id=item_id,
            provider=provider,
            name=name,
            provider_mappings={
                ProviderMapping(
                    item_id=item_id,
                    provider_domain=provider.split("_", maxsplit=1)[0],
                    provider_instance=provider,
                )
            },
        )
        for item_id, name, provider in (
            ("v1", "Zebra", "deezer_1"),
            ("v2", "Apple", "tidal_1"),
            ("v3", "Mango", "qobuz_1"),
        )
    ]


def _names(items: list[Any]) -> list[str]:
    return [item.name for item in items]


@pytest.mark.parametrize(("controller", "item_cls"), CONTROLLERS)
async def test_versions_are_searched_for_once(
    mass: MusicAssistant, controller: str, item_cls: type[Any]
) -> None:
    """The providers are searched for the versions once; sorting, searching and paging reuse them."""
    ctrl = getattr(mass.music, controller)
    assemble = AsyncMock(return_value=_versions(item_cls))

    async def _listing(provider: str = "qobuz_1", **kwargs: Any) -> list[str]:
        return _names(await ctrl.versions("x", provider, **kwargs))

    with (
        patch.object(mass, "get_provider", return_value=SimpleNamespace(instance_id="qobuz_1")),
        patch.object(ctrl, "_assemble_versions", assemble),
    ):
        # the order the providers list them in
        assert await _listing() == ["Zebra", "Apple", "Mango"]
        # the domain and the instance it resolves to share one listing
        assert await _listing("qobuz") == ["Zebra", "Apple", "Mango"]
        assert await _listing(sort_field=SortField.NAME) == ["Apple", "Mango", "Zebra"]
        assert await _listing(sort_field=SortField.PROVIDER) == ["Zebra", "Mango", "Apple"]
        assert await _listing(search="ango") == ["Mango"]
        assert await _listing(limit=1, offset=1) == ["Apple"]
        assert await _listing(limit=0) == ["Zebra", "Apple", "Mango"]
    assemble.assert_awaited_once_with("x", "qobuz_1")


@pytest.mark.parametrize(("controller", "item_cls"), CONTROLLERS)
async def test_versions_of_a_library_item_are_searched_for_once(
    mass: MusicAssistant, controller: str, item_cls: type[Any]
) -> None:
    """The versions of a library item are kept apart from those of a provider item."""
    ctrl = getattr(mass.music, controller)
    assemble = AsyncMock(return_value=_versions(item_cls))
    with patch.object(ctrl, "_assemble_versions", assemble):
        for _ in range(2):
            assert _names(await ctrl.versions("1", "library")) == ["Zebra", "Apple", "Mango"]
        with patch.object(
            mass, "get_provider", return_value=SimpleNamespace(instance_id="qobuz_1")
        ):
            await ctrl.versions("1", "qobuz_1")
    assert [call.args for call in assemble.await_args_list] == [
        ("1", "library"),
        ("1", "qobuz_1"),
    ]


@pytest.mark.parametrize(("controller", "item_cls"), CONTROLLERS)
async def test_versions_reject_an_unsupported_sort_before_searching(
    mass: MusicAssistant, controller: str, item_cls: type[Any]
) -> None:
    """A sort the versions listing does not offer fails before any provider is searched."""
    ctrl = getattr(mass.music, controller)
    assemble = AsyncMock(return_value=_versions(item_cls))
    with (
        patch.object(ctrl, "_assemble_versions", assemble),
        pytest.raises(InvalidDataError),
    ):
        await ctrl.versions("x", "qobuz_1", sort_field=SortField.DURATION)
    assemble.assert_not_awaited()
