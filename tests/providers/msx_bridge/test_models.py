"""Tests for MSX Pydantic models."""

from music_assistant.providers.msx_bridge.models import MsxContent, MsxItem, MsxTemplate


def test_msx_template_serialization() -> None:
    """Test MsxTemplate serialization with aliases."""
    template = MsxTemplate(type="separate", layout="0,0,2,4", image_filler="default")
    data = template.model_dump(by_alias=True, exclude_none=True)
    assert data["type"] == "separate"
    assert data["layout"] == "0,0,2,4"
    assert data["imageFiller"] == "default"
    assert "image_filler" not in data


def test_msx_template_new_fields() -> None:
    """Test MsxTemplate serialization with imageWidth and color."""
    template = MsxTemplate(
        type="default", layout="0,0,6,1", image_width=0.83, color="msx-glass"
    )
    data = template.model_dump(by_alias=True, exclude_none=True)
    assert data["type"] == "default"
    assert data["layout"] == "0,0,6,1"
    assert data["imageWidth"] == 0.83
    assert data["color"] == "msx-glass"
    assert "image_width" not in data


def test_msx_item_serialization() -> None:
    """Test MsxItem serialization with aliases and duration."""
    item = MsxItem(
        title="Test Title",
        title_header="Test Header",
        player_label="Test Player Label",
        title_footer="Test Footer",
        duration=180,
    )
    data = item.model_dump(by_alias=True, exclude_none=True)
    assert data["title"] == "Test Title"
    assert data["titleHeader"] == "Test Header"
    assert data["playerLabel"] == "Test Player Label"
    assert data["titleFooter"] == "Test Footer"
    assert data["duration"] == 180
    assert "title_header" not in data


def test_msx_content_serialization() -> None:
    """Test MsxContent serialization with nested models."""
    content = MsxContent(
        headline="Test Headline",
        template=MsxTemplate(type="list"),
        items=[MsxItem(title="Item 1"), MsxItem(title="Item 2")],
    )
    data = content.model_dump(by_alias=True, exclude_none=True)
    assert data["headline"] == "Test Headline"
    assert data["template"]["type"] == "list"
    assert len(data["items"]) == 2
    assert data["items"][0]["title"] == "Item 1"
