"""Browse and search tests for the Yoto provider."""
# ruff: noqa: D103

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Album, Audiobook, BrowseFolder

from music_assistant.providers.yoto.catalogue import Catalogue, CatalogueGroup
from music_assistant.providers.yoto.provider import YotoProvider

FIXTURES = Path(__file__).parent / "fixtures"


def _provider() -> YotoProvider:
    library = json.loads((FIXTURES / "library.json").read_text())
    detail = json.loads((FIXTURES / "card_detail.json").read_text())
    provider = object.__new__(YotoProvider)
    cast("Any", provider).config = SimpleNamespace(instance_id="yoto-instance")
    provider.catalogue = Catalogue.from_responses(library, {"card-alpha": detail})
    provider.catalogue.groups["bed/time"] = CatalogueGroup(
        item_id="bed/time",
        name="Bedtime",
        card_ids=("card-alpha", "missing-card"),
    )
    return provider


@pytest.mark.asyncio
async def test_search_honours_types_case_and_per_type_limit() -> None:
    provider = _provider()

    result = await provider.search(
        "DREAM READER", [MediaType.ALBUM, MediaType.AUDIOBOOK, MediaType.TRACK], limit=1
    )

    assert result.albums == []
    assert [item.name for item in result.audiobooks] == ["Moshi Moon"]
    assert result.tracks == []


@pytest.mark.asyncio
async def test_browse_exposes_cards_and_url_encoded_groups() -> None:
    provider = _provider()

    root = await provider.browse("yoto-instance://")
    cards = await provider.browse("yoto-instance://cards")
    groups = await provider.browse("yoto-instance://groups")
    grouped = await provider.browse("yoto-instance://group/bed%2Ftime")

    assert [item.name for item in root if isinstance(item, BrowseFolder)] == [
        "All Yoto cards",
        "Yoto library groups",
    ]
    assert [item.translation_key for item in root if isinstance(item, BrowseFolder)] == [
        "all_cards",
        "library_groups",
    ]
    assert isinstance(cards[0], Audiobook)
    assert isinstance(cards[1], Album)
    assert isinstance(groups[0], BrowseFolder)
    assert groups[0].path == "yoto-instance://group/bed%2Ftime"
    assert [item.name for item in grouped] == ["Moshi Moon"]


@pytest.mark.asyncio
async def test_browse_rejects_unknown_paths_and_groups() -> None:
    provider = _provider()

    with pytest.raises(MediaNotFoundError):
        await provider.browse("yoto-instance://unknown")
    with pytest.raises(MediaNotFoundError):
        await provider.browse("yoto-instance://group/missing")
