"""Test Tidal Page Parser."""

import json
import pathlib
from unittest.mock import Mock

import pytest
from music_assistant_models.enums import MediaType

from music_assistant.providers.tidal.tidal_page_parser import TidalPageParser

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
PAGE_FIXTURES = list(FIXTURES_DIR.glob("pages/*.json"))


@pytest.fixture
def provider_mock() -> Mock:
    """Return a mock provider."""
    provider = Mock()
    provider.domain = "tidal"
    provider.instance_id = "tidal_instance"
    provider.auth.user_id = "12345"
    provider.logger = Mock()
    return provider


@pytest.mark.parametrize("example", PAGE_FIXTURES, ids=lambda val: str(val.stem))
def test_page_parser(example: pathlib.Path, provider_mock: Mock) -> None:
    """Test page parser with fixtures."""
    with open(example) as f:
        data = json.load(f)

    parser = TidalPageParser(provider_mock)
    parser.parse_page_structure(data, "pages/home")

    assert len(parser._module_map) == 3

    # Test first module (Playlists)
    module_info = parser._module_map[0]
    items, content_type = parser.get_module_items(module_info)
    assert content_type == MediaType.PLAYLIST
    assert len(items) == 1
    assert items[0].name == "Test Playlist"

    # Test second module (Albums)
    module_info = parser._module_map[1]
    items, content_type = parser.get_module_items(module_info)
    assert content_type == MediaType.ALBUM
    assert len(items) == 1
    assert items[0].name == "Test Album"

    # Test third module (Mixes)
    module_info = parser._module_map[2]
    items, content_type = parser.get_module_items(module_info)
    assert content_type == MediaType.PLAYLIST
    assert len(items) == 1
    assert items[0].name == "My Mix"
