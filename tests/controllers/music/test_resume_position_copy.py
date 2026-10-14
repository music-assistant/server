"""
Tests for the provider copy an audiobook's resume point is read from.

A library audiobook can be mapped on several accounts that each keep their own progress. The
copy it plays from is asked first, the others follow in a fixed order.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.media_items import Audiobook, ProviderMapping

from music_assistant.controllers.music import MusicController
from music_assistant.models.music_provider import MusicProvider

ITEM_ID = "book"


def _account(instance: str, position_ms: int | None) -> MagicMock:
    """
    Build a streaming account that keeps its own progress of the book.

    :param instance: The instance id of the account.
    :param position_ms: The position the account reports, None for an account without progress.
    """
    provider = MagicMock(spec=MusicProvider)
    provider.instance_id = instance
    provider.available = True
    if position_ms is None:
        provider.get_resume_position = AsyncMock(side_effect=NotImplementedError)
    else:
        provider.get_resume_position = AsyncMock(
            return_value=(False, position_ms, datetime(2026, 10, 1, tzinfo=UTC))
        )
    return provider


def _music(*accounts: MagicMock) -> MusicController:
    """Build a bare music controller over the given accounts and an empty play log."""
    music = MusicController.__new__(MusicController)
    providers = {account.instance_id: account for account in accounts}
    music.mass = MagicMock()
    music.mass.get_provider = lambda instance, **_kwargs: providers.get(instance)
    music._database = MagicMock(get_row=AsyncMock(return_value=None))
    music._get_user_for_provider = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return music


def _book(*instances: str) -> Audiobook:
    """Build the library audiobook, mapped on the given accounts."""
    return Audiobook(
        item_id="7",
        provider="library",
        name="Dune",
        provider_mappings={
            ProviderMapping(item_id=ITEM_ID, provider_domain="spotify", provider_instance=instance)
            for instance in instances
        },
    )


async def test_the_pinned_copy_gives_the_resume_point() -> None:
    """Of two accounts with their own progress, the one the book plays from is asked."""
    music = _music(_account("spotify--a", 1_000), _account("spotify--b", 2_000))

    resume = await music.get_resume_position(
        _book("spotify--a", "spotify--b"), pinned=("spotify--b", ITEM_ID)
    )

    assert resume == (False, 2_000)


async def test_the_other_copies_are_asked_in_a_fixed_order() -> None:
    """When the pinned copy keeps no progress, the others are asked by instance id."""
    pinned = _account("spotify--c", None)
    music = _music(_account("spotify--b", 2_000), _account("spotify--a", 1_000), pinned)

    resume = await music.get_resume_position(
        _book("spotify--a", "spotify--b", "spotify--c"), pinned=("spotify--c", ITEM_ID)
    )

    pinned.get_resume_position.assert_awaited_once()
    assert resume == (False, 1_000)
