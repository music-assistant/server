"""Tests for the MetaDataController public API."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from music_assistant_models.enums import ImageType
from music_assistant_models.media_items import MediaItemImage, MediaItemPalette

from music_assistant.controllers.metadata import MetaDataController

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


@pytest.fixture
async def metadata_controller(mass_minimal: MusicAssistant) -> MetaDataController:
    """Construct a MetaDataController with the minimal MA fixture."""
    await mass_minimal.cache._setup_database()
    controller = MetaDataController(mass_minimal)
    mass_minimal.metadata = controller
    return controller


async def test_get_image_palette_dispatches_by_input_type(
    metadata_controller: MetaDataController, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A MediaItemImage resolves by (path, provider); a plain URL goes via get_palette_for_url."""
    palette = MediaItemPalette(primary=(1, 2, 3))
    calls: dict[str, object] = {}

    async def fake_get_palette(_mass: object, path: str, provider: str) -> MediaItemPalette:
        calls["get_palette"] = (path, provider)
        return palette

    async def fake_get_palette_for_url(_mass: object, url: str) -> MediaItemPalette:
        calls["get_palette_for_url"] = url
        return palette

    monkeypatch.setattr("music_assistant.controllers.metadata.get_palette", fake_get_palette)
    monkeypatch.setattr(
        "music_assistant.controllers.metadata.get_palette_for_url", fake_get_palette_for_url
    )

    image = MediaItemImage(type=ImageType.THUMB, path="cover.jpg", provider="spotify")
    assert await metadata_controller.get_image_palette(image) is palette
    assert calls["get_palette"] == ("cover.jpg", "spotify")

    assert await metadata_controller.get_image_palette("https://example/cover.jpg") is palette
    assert calls["get_palette_for_url"] == "https://example/cover.jpg"


async def test_get_image_palette_returns_none_for_unreadable_image(
    metadata_controller: MetaDataController, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An image whose data can't be read yields None instead of raising."""

    async def boom(_mass: object, path: str, _provider: str) -> MediaItemPalette:
        raise FileNotFoundError(path)

    monkeypatch.setattr("music_assistant.controllers.metadata.get_palette", boom)
    image = MediaItemImage(type=ImageType.THUMB, path="missing.jpg", provider="filesystem")
    assert await metadata_controller.get_image_palette(image) is None
