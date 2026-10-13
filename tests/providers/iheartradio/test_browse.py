"""Tests for the iHeartRadio live radio browse tree."""

from __future__ import annotations

from typing import cast

from music_assistant_models.media_items import BrowseFolder, Radio

from music_assistant.providers.iheartradio.constants import (
    PATH_GENRES,
    PATH_LIVE_STATIONS,
    PATH_MARKETS,
)
from music_assistant.providers.iheartradio.provider import IHeartRadioProvider

from .conftest import INSTANCE_ID, STATION, FakeApi

ROOT = f"{INSTANCE_ID}://"


async def _browse_folders(provider: IHeartRadioProvider, path: str) -> list[BrowseFolder]:
    """Browse a path that is expected to list folders only."""
    items = await provider.browse(path)
    assert all(isinstance(item, BrowseFolder) for item in items)
    return cast("list[BrowseFolder]", items)


async def test_root_and_live_folders(provider: IHeartRadioProvider, api: FakeApi) -> None:
    """The root offers live radio and podcasts, live radio offers cities and genres."""
    root = await _browse_folders(provider, ROOT)
    assert [folder.path for folder in root] == [f"{ROOT}live", f"{ROOT}podcasts"]
    live = await _browse_folders(provider, f"{ROOT}live")
    assert [folder.path for folder in live] == [f"{ROOT}live/markets", f"{ROOT}live/genres"]
    assert api.calls == []


async def test_browse_by_market(provider: IHeartRadioProvider, api: FakeApi) -> None:
    """Markets of the country are listed by name and open into their stations."""
    api.responses[PATH_MARKETS] = {
        "hits": [
            {"marketId": 159, "city": "Sydney", "stateAbbreviation": "NSW"},
            {"marketId": 160, "city": "Brisbane", "stateAbbreviation": "QLD"},
            {"city": "No id"},
        ]
    }
    markets = await _browse_folders(provider, f"{ROOT}live/markets")
    assert [(folder.name, folder.path) for folder in markets] == [
        ("Brisbane, QLD", f"{ROOT}live/markets/160"),
        ("Sydney, NSW", f"{ROOT}live/markets/159"),
    ]
    assert api.calls[-1][1]["countryCode"] == "AU"

    api.responses[PATH_LIVE_STATIONS] = {"hits": [STATION]}
    stations = await provider.browse(f"{ROOT}live/markets/159")
    assert [(type(item), item.name) for item in stations] == [(Radio, "KIIS 1065")]
    _, params = api.calls[-1]
    assert params["marketId"] == "159"
    assert params["genreId"] is None


async def test_browse_by_genre(provider: IHeartRadioProvider, api: FakeApi) -> None:
    """Live station genres are listed with their artwork and open into their stations."""
    api.responses[PATH_GENRES] = {
        "genres": [
            {"id": 16, "genreName": "Pop", "image": "https://i.iheart.com/v3/re/pop.png"},
            {"id": 17, "genreName": ""},
        ]
    }
    genres = await _browse_folders(provider, f"{ROOT}live/genres")
    assert [(folder.name, folder.path) for folder in genres] == [("Pop", f"{ROOT}live/genres/16")]
    assert genres[0].image is not None
    assert api.calls[-1][1]["genreType"] == "liveStation"

    api.responses[PATH_LIVE_STATIONS] = {"hits": [STATION]}
    stations = await provider.browse(f"{ROOT}live/genres/16")
    assert [item.item_id for item in stations] == [str(STATION["id"])]
    _, params = api.calls[-1]
    assert params["genreId"] == "16"
    assert params["marketId"] is None
