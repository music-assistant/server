"""Tests for the Yoto MusicProvider runtime."""
# ruff: noqa: D103

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Album, Artist, Audiobook

from music_assistant.providers.yoto import setup
from music_assistant.providers.yoto.catalogue import Catalogue
from music_assistant.providers.yoto.provider import SUPPORTED_FEATURES, YotoProvider

FIXTURES = Path(__file__).parent / "fixtures"


def _catalogue() -> Catalogue:
    library = json.loads((FIXTURES / "library.json").read_text())
    detail = json.loads((FIXTURES / "card_detail.json").read_text())
    return Catalogue.from_responses(library, {"card-alpha": detail})


def _provider() -> YotoProvider:
    provider = object.__new__(YotoProvider)
    cast("Any", provider).config = SimpleNamespace(instance_id="yoto-instance")
    provider.catalogue = _catalogue()
    return provider


@pytest.mark.asyncio
async def test_setup_returns_runtime_with_complete_read_only_features() -> None:
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "yoto"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"

    provider = await setup(mass, manifest, config)

    assert isinstance(provider, YotoProvider)
    assert provider.supported_features == SUPPORTED_FEATURES
    assert provider.reload_on_streams_network_change


@pytest.mark.asyncio
async def test_library_generators_separate_artists_albums_tracks_and_audiobooks() -> None:
    provider = _provider()

    artists = [item async for item in provider.get_library_artists()]
    albums = [item async for item in provider.get_library_albums()]
    tracks = [item async for item in provider.get_library_tracks()]
    audiobooks = [item async for item in provider.get_library_audiobooks()]

    assert all(isinstance(item, Artist) for item in artists)
    assert [item.name for item in artists] == ["Dream Reader", "Yoto"]
    assert all(isinstance(item, Album) for item in albums)
    assert [item.name for item in albums] == ["Rain Songs"]
    assert tracks == []
    assert all(isinstance(item, Audiobook) for item in audiobooks)
    assert [item.name for item in audiobooks] == ["Moshi Moon"]


@pytest.mark.asyncio
async def test_get_artist_returns_stable_author_and_rejects_unknown_id() -> None:
    provider = _provider()

    artist = await provider.get_artist("author:dream reader")

    assert artist.name == "Dream Reader"
    with pytest.raises(Exception, match="unavailable"):
        await provider.get_artist("author:missing")


@pytest.mark.asyncio
async def test_direct_getters_and_album_tracks_preserve_media_semantics() -> None:
    provider = _provider()

    audiobook = await provider.get_audiobook("card-alpha")
    album = await provider.get_album("card-beta")
    album_tracks = await provider.get_album_tracks("card-beta")

    assert audiobook.media_type is MediaType.AUDIOBOOK
    assert album.media_type is MediaType.ALBUM
    assert album_tracks == []
