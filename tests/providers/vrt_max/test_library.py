"""Tests for VRT MAX library sync (favourites) and library add/remove."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import LoginFailed

from music_assistant.providers.vrt_max.constants import STATIONS_BY_ID
from music_assistant.providers.vrt_max.models import (
    VrtApiError,
    VrtAuthError,
    VrtNotFoundError,
    VrtProgram,
)
from music_assistant.providers.vrt_max.provider import VrtMaxProvider

from .conftest import _set_credentials, async_gen


async def test_get_library_podcasts_disabled_yields_nothing(provider: VrtMaxProvider) -> None:
    """Without VRT credentials, the favourites sync yields nothing."""
    provider._auth.enabled = False  # type: ignore[misc]

    items = [podcast async for podcast in provider.get_library_podcasts()]

    assert items == []


async def test_get_library_podcasts_skips_unresolvable_favourites(
    provider: VrtMaxProvider,
) -> None:
    """A favourite that no longer resolves is skipped without aborting the sync."""
    provider._auth.enabled = True  # type: ignore[misc]
    provider._auth.get_access_token = AsyncMock(  # type: ignore[method-assign]
        return_value="tok"
    )
    provider._client.iter_favourite_ids = async_gen(  # type: ignore[method-assign]
        ["/fav/ok/", "/fav/missing/", "/fav/ok2/"]
    )

    def _get_program(page_id: str, *_args: object, **_kwargs: object) -> VrtProgram:
        if page_id == "/fav/missing/":
            raise VrtNotFoundError("nope")
        return VrtProgram(page_id, f"Program {page_id}")

    provider._client.get_program = AsyncMock(  # type: ignore[method-assign]
        side_effect=_get_program
    )

    items = [podcast async for podcast in provider.get_library_podcasts()]

    assert {podcast.item_id for podcast in items} == {"/fav/ok/", "/fav/ok2/"}


async def test_get_library_podcasts_aborts_on_transient_error(provider: VrtMaxProvider) -> None:
    """A transient failure aborts the sync instead of silently pruning the favourite."""
    provider._auth.enabled = True  # type: ignore[misc]
    provider._auth.get_access_token = AsyncMock(  # type: ignore[method-assign]
        return_value="tok"
    )
    provider._client.iter_favourite_ids = async_gen(  # type: ignore[method-assign]
        ["/fav/ok/", "/fav/bad/"]
    )

    def _get_program(page_id: str, *_args: object, **_kwargs: object) -> VrtProgram:
        if page_id == "/fav/bad/":
            raise VrtApiError("boom")
        return VrtProgram(page_id, f"Program {page_id}")

    provider._client.get_program = AsyncMock(  # type: ignore[method-assign]
        side_effect=_get_program
    )

    with pytest.raises(VrtApiError):
        [podcast async for podcast in provider.get_library_podcasts()]


async def test_library_add_podcast_calls_set_favourite(provider: VrtMaxProvider) -> None:
    """Adding a Podcast to the library syncs it to VRT's 'Mijn lijst'."""
    provider._auth.enabled = True  # type: ignore[misc]
    podcast = provider._podcast_base("/p/1", "Show")
    provider._set_favourite = AsyncMock(return_value=True)  # type: ignore[method-assign]

    result = await provider.library_add(podcast)

    assert result is True
    provider._set_favourite.assert_awaited_once_with("/p/1", favourite=True)


async def test_library_add_non_podcast_skips_set_favourite(provider: VrtMaxProvider) -> None:
    """Adding a non-podcast item is a local-only MA operation; VRT is not touched."""
    radio = provider._radio_item(STATIONS_BY_ID["radio1"])
    provider._set_favourite = AsyncMock(return_value=True)  # type: ignore[method-assign]

    result = await provider.library_add(radio)

    assert result is True
    provider._set_favourite.assert_not_called()


async def test_library_remove_podcast_calls_set_favourite(provider: VrtMaxProvider) -> None:
    """Removing a podcast from the library syncs the removal to VRT's 'Mijn lijst'."""
    provider._auth.enabled = True  # type: ignore[misc]
    provider._set_favourite = AsyncMock(return_value=True)  # type: ignore[method-assign]

    result = await provider.library_remove("/p/1", MediaType.PODCAST)

    assert result is True
    provider._set_favourite.assert_awaited_once_with("/p/1", favourite=False)


async def test_library_remove_non_podcast_skips_set_favourite(provider: VrtMaxProvider) -> None:
    """Removing a non-podcast item is a local-only MA operation; VRT is not touched."""
    provider._set_favourite = AsyncMock(return_value=True)  # type: ignore[method-assign]

    result = await provider.library_remove("radio1", MediaType.RADIO)

    assert result is True
    provider._set_favourite.assert_not_called()


async def test_set_favourite_adds_when_not_already_favourited(provider: VrtMaxProvider) -> None:
    """_set_favourite performs the mutation when the current state differs."""
    provider._auth.enabled = True  # type: ignore[misc]
    provider._auth.get_access_token = AsyncMock(  # type: ignore[method-assign]
        return_value="tok"
    )
    provider._client.get_favourite_action = AsyncMock(  # type: ignore[method-assign]
        return_value=("action123", False)
    )
    provider._client.set_favourite = AsyncMock()  # type: ignore[method-assign]

    result = await provider._set_favourite("/p/1", favourite=True)

    assert result is True
    provider._client.set_favourite.assert_awaited_once_with("action123", True, "tok")


async def test_set_favourite_without_action_id_returns_false(provider: VrtMaxProvider) -> None:
    """No favourite action available means it can't be synced; nothing is mutated."""
    provider._auth.enabled = True  # type: ignore[misc]
    provider._auth.get_access_token = AsyncMock(  # type: ignore[method-assign]
        return_value="tok"
    )
    provider._client.get_favourite_action = AsyncMock(  # type: ignore[method-assign]
        return_value=(None, False)
    )
    provider._client.set_favourite = AsyncMock()  # type: ignore[method-assign]

    result = await provider._set_favourite("/p/1", favourite=True)

    assert result is False
    provider._client.set_favourite.assert_not_called()


async def test_set_favourite_already_matching_state_skips_mutation(
    provider: VrtMaxProvider,
) -> None:
    """When the remote state already matches, no mutation call is made."""
    provider._auth.enabled = True  # type: ignore[misc]
    provider._auth.get_access_token = AsyncMock(  # type: ignore[method-assign]
        return_value="tok"
    )
    provider._client.get_favourite_action = AsyncMock(  # type: ignore[method-assign]
        return_value=("action123", True)
    )
    provider._client.set_favourite = AsyncMock()  # type: ignore[method-assign]

    result = await provider._set_favourite("/p/1", favourite=True)

    assert result is True
    provider._client.set_favourite.assert_not_called()


async def test_set_favourite_error_returns_false(provider: VrtMaxProvider) -> None:
    """An API error while syncing the favourite is reported as a failure, not raised."""
    provider._auth.enabled = True  # type: ignore[misc]
    provider._auth.get_access_token = AsyncMock(  # type: ignore[method-assign]
        return_value="tok"
    )
    provider._client.get_favourite_action = AsyncMock(  # type: ignore[method-assign]
        side_effect=VrtApiError("boom")
    )

    result = await provider._set_favourite("/p/1", favourite=True)

    assert result is False


async def test_set_favourite_disabled_returns_true_without_calling_client(
    provider: VrtMaxProvider,
) -> None:
    """Without credentials, the change stays local to MA and no client call is made."""
    provider._auth.enabled = False  # type: ignore[misc]

    result = await provider._set_favourite("/p/1", favourite=True)

    assert result is True
    provider._client.get_favourite_action.assert_not_called()  # type: ignore[attr-defined]


async def test_library_add_without_account_stays_local(provider: VrtMaxProvider) -> None:
    """Without credentials there is no 'Mijn lijst' to sync to, so the add stays local."""
    provider._auth.enabled = False  # type: ignore[misc]
    podcast = provider._podcast_base("/p/1", "Show")
    provider._set_favourite = AsyncMock(return_value=True)  # type: ignore[method-assign]

    result = await provider.library_add(podcast)

    assert result is True
    provider._set_favourite.assert_not_called()


def test_supported_features_without_account_excludes_library(provider: VrtMaxProvider) -> None:
    """Without credentials the library features are not declared at all."""
    features = provider.supported_features

    # Declaring these without an account would add sync switches that cannot work, and
    # "sync library deletions" would then prune previously synced podcasts.
    assert ProviderFeature.LIBRARY_PODCASTS not in features
    assert ProviderFeature.LIBRARY_PODCASTS_EDIT not in features
    assert ProviderFeature.BROWSE in features
    assert ProviderFeature.SEARCH in features


def test_supported_features_with_account_includes_library(provider: VrtMaxProvider) -> None:
    """With credentials the favourites sync features are declared."""
    _set_credentials(provider)

    features = provider.supported_features

    assert ProviderFeature.LIBRARY_PODCASTS in features
    assert ProviderFeature.LIBRARY_PODCASTS_EDIT in features


def test_supported_media_types_includes_radio(provider: VrtMaxProvider) -> None:
    """Radio must be declared so the provider joins radio matching and per-type search."""
    assert MediaType.RADIO in provider.supported_media_types
    assert MediaType.PODCAST in provider.supported_media_types


def test_supported_features_before_async_init(provider: VrtMaxProvider) -> None:
    """
    The features must be readable before handle_async_init has run.

    MA resolves a provider's config entries, and with them its features, while setting the
    provider up, which is before the auth manager exists. Reading runtime state here made
    adding the provider fail with an AttributeError.
    """
    del provider._auth

    assert ProviderFeature.BROWSE in provider.supported_features


async def test_get_library_podcasts_aborts_on_token_failure(provider: VrtMaxProvider) -> None:
    """A failed token refresh must abort the sync, not report an empty favourites list."""
    provider._auth.enabled = True  # type: ignore[misc]
    provider._auth.get_access_token = AsyncMock(  # type: ignore[method-assign]
        side_effect=VrtAuthError("token refresh failed")
    )

    # Yielding nothing here would tell MA every synced favourite had been removed, and
    # the deletion pass would prune them all.
    with pytest.raises(LoginFailed):
        [podcast async for podcast in provider.get_library_podcasts()]
