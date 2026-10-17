"""Tests for the Storytel helper's content parsing and authentication."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.storytel.storytel_helper import StorytelAuth, StorytelHelper


def _episode_data(**overrides: Any) -> dict[str, Any]:
    """Return minimal Storytel podcast episode API data."""
    data: dict[str, Any] = {
        "consumableId": "episode-1",
        "title": "Episode 1",
        "duration": {"hours": 0, "minutes": 30, "seconds": 0},
        "seriesInfo": {"id": "podcast-1", "orderInSeries": 14},
    }
    data.update(overrides)
    return data


@pytest.fixture
def helper() -> StorytelHelper:
    """Return a StorytelHelper whose provider_instance.get_podcast resolves to a stub podcast."""
    provider_instance = MagicMock()
    provider_instance.get_podcast = AsyncMock(return_value=MagicMock())
    return StorytelHelper(
        session=MagicMock(),
        provider_instance=provider_instance,
        provider_id="storytel--test",
        provider_domain="storytel",
    )


async def test_episode_number_is_set_from_order_in_series(helper: StorytelHelper) -> None:
    """OrderInSeries becomes episode_number, while position keeps its own sort-order value."""
    episode = await helper._parse_podcast_episode_item(_episode_data())
    assert episode.episode_number == 14
    assert episode.position == 14


async def test_missing_order_in_series_leaves_episode_number_unset(
    helper: StorytelHelper,
) -> None:
    """Without orderInSeries, episode_number stays None and position falls back to 0."""
    data = _episode_data()
    data["seriesInfo"] = {"id": "podcast-1"}
    episode = await helper._parse_podcast_episode_item(data)
    assert episode.episode_number is None
    assert episode.position == 0


async def test_zero_order_in_series_does_not_show_as_episode_zero(
    helper: StorytelHelper,
) -> None:
    """A seriesInfo.orderInSeries of 0 is not a real episode number, so it is not displayed."""
    data = _episode_data()
    data["seriesInfo"] = {"id": "podcast-1", "orderInSeries": 0}
    episode = await helper._parse_podcast_episode_item(data)
    assert episode.episode_number is None


def test_parse_languages_maps_name_to_iso_value() -> None:
    """AllLanguageObjects entries become a name -> isoValue mapping, split by checkedByDefault."""
    acc = {
        "allLanguageObjects": [
            {
                "id": 6,
                "isoValue": "nn",
                "name": "Norwegian",
                "localizedName": "Norsk",
                "checkedByDefault": True,
            },
            {
                "id": 2,
                "isoValue": "en",
                "name": "English",
                "localizedName": "Engelsk",
                "checkedByDefault": True,
            },
            {
                "id": 1,
                "isoValue": "sv",
                "name": "Swedish",
                "localizedName": "Svensk",
                "checkedByDefault": False,
            },
        ]
    }
    available, default = StorytelHelper._parse_languages(acc)
    assert available == {"Norwegian": "nn", "English": "en", "Swedish": "sv"}
    assert default == {"Norwegian": "nn", "English": "en"}


@pytest.mark.parametrize(
    "acc",
    [
        {},
        {"allLanguageObjects": None},
        {"allLanguageObjects": []},
        {"allLanguageObjects": ["not-a-dict"]},
        {"allLanguageObjects": [{"id": 1, "name": "", "isoValue": "sv"}]},
        {"allLanguageObjects": [{"id": 1, "name": "Swedish", "isoValue": ""}]},
        {"allLanguageObjects": [{"id": 1, "name": "Swedish"}]},
    ],
)
def test_parse_languages_ignores_malformed_entries(acc: dict[str, Any]) -> None:
    """Missing, malformed or empty entries are skipped rather than raising."""
    assert StorytelHelper._parse_languages(acc) == ({}, {})


async def test_login_populates_available_and_default_languages(helper: StorytelHelper) -> None:
    """A successful login records both the account's available and default-checked languages."""
    helper._request_json = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "accountInfo": {
                "jwt": "jwt-token",
                "singleSignToken": "sso-token",
                "allLanguageObjects": [
                    {"isoValue": "nn", "name": "Norwegian", "checkedByDefault": True},
                    {"isoValue": "sv", "name": "Swedish", "checkedByDefault": False},
                ],
            }
        }
    )
    await helper.login("user", "pass")
    assert helper.available_languages == {"Norwegian": "nn", "Swedish": "sv"}
    assert helper.default_languages == {"Norwegian": "nn"}


async def test_revalidate_account_refreshes_default_languages(
    helper: StorytelHelper,
) -> None:
    """
    Revalidation refreshes default-checked languages too, not just available ones.

    A stale default (from login time) would make ``_reconcile_languages`` fall back to an
    outdated choice if the account's region/entitlements changed since login.
    """
    helper._request_json = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "accountInfo": {
                "jwt": "jwt-token",
                "singleSignToken": "sso-token",
                "allLanguageObjects": [
                    {"isoValue": "nn", "name": "Norwegian", "checkedByDefault": True},
                ],
            }
        }
    )
    await helper.login("user", "pass")
    assert helper.default_languages == {"Norwegian": "nn"}

    helper._request_json = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "accountInfo": {
                "jwt": "jwt-token-2",
                "singleSignToken": "sso-token",
                "allLanguageObjects": [
                    {"isoValue": "nn", "name": "Norwegian", "checkedByDefault": False},
                    {"isoValue": "en", "name": "English", "checkedByDefault": True},
                ],
            }
        }
    )
    await helper.revalidate_account()
    assert helper.available_languages == {"Norwegian": "nn", "English": "en"}
    assert helper.default_languages == {"English": "en"}


async def test_revalidate_account_drops_selected_language_no_longer_available(
    helper: StorytelHelper,
) -> None:
    """A previously selected language that lost entitlement is removed from the active query."""
    helper._auth = StorytelAuth(jwt="jwt-token", single_sign_token="sso-token")
    helper.languages = {"Swedish": "sv", "Norwegian": "nn"}
    helper._request_json = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "accountInfo": {
                "jwt": "jwt-token",
                "singleSignToken": "sso-token",
                "allLanguageObjects": [
                    {"isoValue": "nn", "name": "Norwegian", "checkedByDefault": True},
                    {"isoValue": "en", "name": "English", "checkedByDefault": False},
                ],
            }
        }
    )
    await helper.revalidate_account()
    assert helper.languages == {"Norwegian": "nn"}


async def test_revalidate_account_falls_back_to_default_language_when_selection_lost(
    helper: StorytelHelper,
) -> None:
    """When the whole selection loses entitlement, the account's own default is used instead."""
    helper._auth = StorytelAuth(jwt="jwt-token", single_sign_token="sso-token")
    helper.languages = {"Swedish": "sv"}
    helper._default_languages = {"Norwegian": "nn"}
    helper._request_json = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "accountInfo": {
                "jwt": "jwt-token",
                "singleSignToken": "sso-token",
                "allLanguageObjects": [
                    {"isoValue": "nn", "name": "Norwegian", "checkedByDefault": True},
                    {"isoValue": "en", "name": "English", "checkedByDefault": False},
                ],
            }
        }
    )
    await helper.revalidate_account()
    assert helper.languages == {"Norwegian": "nn"}


async def test_revalidate_account_falls_back_to_any_available_language_as_last_resort(
    helper: StorytelHelper,
) -> None:
    """With no default-checked match either, any language the account still has is kept."""
    helper._auth = StorytelAuth(jwt="jwt-token", single_sign_token="sso-token")
    helper.languages = {"Swedish": "sv"}
    helper._request_json = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "accountInfo": {
                "jwt": "jwt-token",
                "singleSignToken": "sso-token",
                "allLanguageObjects": [
                    {"isoValue": "en", "name": "English", "checkedByDefault": False},
                ],
            }
        }
    )
    await helper.revalidate_account()
    assert helper.languages == {"English": "en"}


async def test_revalidate_account_keeps_selection_when_still_fully_available(
    helper: StorytelHelper,
) -> None:
    """A selection that is still entirely available to the account is left unchanged."""
    helper._auth = StorytelAuth(jwt="jwt-token", single_sign_token="sso-token")
    helper.languages = {"Swedish": "sv"}
    helper._request_json = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "accountInfo": {
                "jwt": "jwt-token",
                "singleSignToken": "sso-token",
                "allLanguageObjects": [
                    {"isoValue": "sv", "name": "Swedish", "checkedByDefault": False},
                    {"isoValue": "en", "name": "English", "checkedByDefault": True},
                ],
            }
        }
    )
    await helper.revalidate_account()
    assert helper.languages == {"Swedish": "sv"}
