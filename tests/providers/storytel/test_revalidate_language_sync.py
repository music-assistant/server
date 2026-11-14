"""Tests that a helper-level language reconciliation during revalidation reaches the provider."""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock, Mock

from music_assistant_models.errors import LoginFailed

from music_assistant.providers.storytel import Storytel
from music_assistant.providers.storytel.constants import CONF_LANGUAGES
from music_assistant.providers.storytel.storytel_helper import StorytelHelper


def _make_authenticated_provider(provider: Storytel) -> None:
    """Attach a helper whose selection has already been reconciled down after revalidation."""
    provider._api = StorytelHelper(
        session=Mock(),
        provider_instance=provider,
        provider_id=provider.instance_id,
        provider_domain=provider.manifest.domain,
        languages={"Norwegian": "nn", "English": "en"},
    )
    # Simulate what revalidate_account()'s _reconcile_languages() would have done: the
    # account lost access to Norwegian, so the helper fell back to its remaining language.
    provider._api._languages = {"English": "en"}
    provider._languages = {"Norwegian": "nn", "English": "en"}


async def test_sync_reconciled_languages_persists_the_helper_selection(
    provider: Storytel,
) -> None:
    """After a reconciliation, the provider's own state and persisted config are updated."""
    _make_authenticated_provider(provider)

    provider._sync_reconciled_languages()

    assert provider._languages == {"English": "en"}
    cast("MagicMock", provider.mass.config.set_raw_provider_config_value).assert_called_once_with(
        "storytel--test", CONF_LANGUAGES, ["English"], encrypted=False, immediate=True
    )


async def test_sync_reconciled_languages_is_a_no_op_when_selection_is_unchanged(
    provider: Storytel,
) -> None:
    """No persistence happens when the helper's selection still matches the provider's."""
    provider._api = StorytelHelper(
        session=Mock(),
        provider_instance=provider,
        provider_id=provider.instance_id,
        provider_domain=provider.manifest.domain,
        languages={"English": "en"},
    )
    provider._languages = {"English": "en"}

    provider._sync_reconciled_languages()

    cast("MagicMock", provider.mass.config.set_raw_provider_config_value).assert_not_called()


async def test_handle_login_failed_syncs_reconciled_languages_after_revalidation(
    provider: Storytel,
) -> None:
    """The retry decorator picks up the helper's reconciled selection after revalidation."""
    _make_authenticated_provider(provider)

    async def _revalidate_account() -> None:
        # Reconciliation already happened on the helper; nothing further required here.
        return None

    helper = cast("StorytelHelper", provider._api)
    helper.revalidate_account = _revalidate_account  # type: ignore[method-assign,assignment]

    calls = {"count": 0}

    @Storytel.handle_login_failed
    async def flaky(_self: Storytel) -> str:
        calls["count"] += 1
        if calls["count"] == 1:
            raise LoginFailed("expired")
        return "ok"

    result = await flaky(provider)

    assert result == "ok"
    assert provider._languages == {"English": "en"}
    cast("MagicMock", provider.mass.config.set_raw_provider_config_value).assert_called_once_with(
        "storytel--test", CONF_LANGUAGES, ["English"], encrypted=False, immediate=True
    )
