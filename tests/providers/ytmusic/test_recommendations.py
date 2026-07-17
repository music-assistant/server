"""Test YouTube Music recommendations() row filtering via the `wanted` parameter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping, RecommendationFolder

from music_assistant.providers.ytmusic import YoutubeMusicProvider

# two home sections with empty contents: enough to assert the server-derived
# folder item_ids without exercising the item parsers
HOME_DATA = [
    {"title": "Listen again", "contents": []},
    {"title": "Quick picks", "contents": []},
]

GET_HOME_PATH = "music_assistant.providers.ytmusic.get_home"


@pytest.fixture
def provider() -> YoutubeMusicProvider:
    """Return a YoutubeMusicProvider instance with mocked dependencies."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "ytmusic"
    config = MagicMock()
    config.instance_id = "ytmusic--test"
    config.get_value.return_value = "GLOBAL"
    prov = YoutubeMusicProvider(mass, manifest, config)
    prov._headers = {}
    prov._yt_user = None
    prov.language = "en"
    # make the @use_cache decorator treat every call as a cache miss
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.cache.set = AsyncMock()
    return prov


def _stub_mixed_for_you(provider: YoutubeMusicProvider) -> AsyncMock:
    """Attach a _get_mixed_for_you_folder stub returning a folder with one item."""
    folder = RecommendationFolder(
        name="Mixed for you",
        item_id=f"{provider.instance_id}_mixed_for_you",
        provider=provider.instance_id,
        icon="mdi:shuffle-variant",
    )
    folder.items.append(
        ItemMapping(
            media_type=MediaType.PLAYLIST,
            item_id="RDTMAK5uy_mix1",
            provider=provider.instance_id,
            name="My Mix 1",
        )
    )
    mock = AsyncMock(return_value=folder)
    provider._get_mixed_for_you_folder = mock  # type: ignore[method-assign]
    return mock


async def test_recommendations_wanted_none_fetches_all_rows(
    provider: YoutubeMusicProvider,
) -> None:
    """wanted=None (default) fetches home sections AND the mixed_for_you row."""
    mixed_mock = _stub_mixed_for_you(provider)
    with patch(GET_HOME_PATH, new_callable=AsyncMock, return_value=HOME_DATA) as get_home_mock:
        result = await provider.recommendations()

    get_home_mock.assert_awaited_once()
    mixed_mock.assert_awaited_once()
    assert {f.item_id for f in result} == {
        f"{provider.instance_id}_Listen again",
        f"{provider.instance_id}_Quick picks",
        f"{provider.instance_id}_mixed_for_you",
    }


async def test_recommendations_wanted_mixed_only_skips_get_home(
    provider: YoutubeMusicProvider,
) -> None:
    """wanted={<instance>_mixed_for_you} skips get_home and returns only the mixed row."""
    mixed_mock = _stub_mixed_for_you(provider)
    with patch(GET_HOME_PATH, new_callable=AsyncMock, return_value=HOME_DATA) as get_home_mock:
        result = await provider.recommendations(wanted={f"{provider.instance_id}_mixed_for_you"})

    get_home_mock.assert_not_awaited()
    mixed_mock.assert_awaited_once()
    assert [f.item_id for f in result] == [f"{provider.instance_id}_mixed_for_you"]


async def test_recommendations_wanted_section_only_skips_mixed(
    provider: YoutubeMusicProvider,
) -> None:
    """wanted={one section id} fetches home but skips the mixed_for_you fetch chain."""
    mixed_mock = _stub_mixed_for_you(provider)
    with patch(GET_HOME_PATH, new_callable=AsyncMock, return_value=HOME_DATA) as get_home_mock:
        result = await provider.recommendations(wanted={f"{provider.instance_id}_Listen again"})

    get_home_mock.assert_awaited_once()
    mixed_mock.assert_not_awaited()
    # all section rows come from the single get_home fetch, so sibling sections are
    # still built (cheap, no extra fetch); the controller post-filters the superset
    assert {f.item_id for f in result} == {
        f"{provider.instance_id}_Listen again",
        f"{provider.instance_id}_Quick picks",
    }
