"""Test BBC Sounds provider menu handling."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from music_assistant.helpers.datetime import utc_timestamp
from music_assistant.providers.bbc_sounds.constants import _Constants

if TYPE_CHECKING:
    from sounds.models import Menu

    from music_assistant.providers.bbc_sounds import BBCSoundsProvider


class TestMenuLoading:
    """Tests for initial/refresh menu loading."""

    async def test_blank_menu_is_refreshed(
        self, provider: BBCSoundsProvider, blank_menu: Menu, uk_menu: Menu
    ) -> None:
        """Test that when a blank menu, it is refreshed from the API."""
        provider.menu = blank_menu
        provider.menu_last_fetched = utc_timestamp()

        async def fake_refresh() -> None:
            provider.menu = uk_menu

        provider._refresh_menu_from_api = AsyncMock(side_effect=fake_refresh)  # type: ignore[method-assign]

        await provider._get_menu()

        provider._refresh_menu_from_api.assert_awaited_once()
        assert provider.menu is uk_menu

    async def test_expired_menu_is_refreshed(
        self, provider: BBCSoundsProvider, uk_menu: Menu
    ) -> None:
        """Test that when a menu has expired, it is refreshed from the API."""
        provider.menu = uk_menu
        provider.menu_last_fetched = utc_timestamp() - _Constants.SHORT_EXPIRATION - 1
        provider._refresh_menu_from_api = AsyncMock()  # type: ignore[method-assign]

        await provider._get_menu()

        provider._refresh_menu_from_api.assert_awaited_once()

    @pytest.mark.parametrize(
        "last_fetched", [utc_timestamp(), utc_timestamp() - _Constants.SHORT_EXPIRATION + 1]
    )
    async def test_valid_menu_is_not_refreshed(
        self, provider: BBCSoundsProvider, uk_menu: Menu, last_fetched: float
    ) -> None:
        """Test that when we have a valid menu, it isn't refreshed."""
        provider.menu = uk_menu
        provider.menu_last_fetched = last_fetched
        provider._refresh_menu_from_api = AsyncMock()  # type: ignore[method-assign]

        await provider._get_menu()

        provider._refresh_menu_from_api.assert_not_awaited()
