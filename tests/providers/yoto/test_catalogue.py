"""Tests for stable Yoto catalogue parsing."""

import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from music_assistant.providers.yoto.catalogue import Catalogue, decode_track_id, encode_track_id

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> Mapping[str, Any]:
    """Load a Yoto response fixture."""
    return cast("Mapping[str, Any]", json.loads((FIXTURES / name).read_text()))


def test_catalogue_is_ordered_classified_and_url_free() -> None:
    """Parse cards and tracks without retaining signed stream URLs."""
    catalogue = Catalogue.from_responses(
        load("library.json"), {"card-alpha": load("card_detail.json")}
    )

    assert list(catalogue.cards) == ["card-alpha", "card-beta"]
    card = catalogue.cards["card-alpha"]
    assert (card.title, card.author, card.is_audiobook) == ("Moshi Moon", "Dream Reader", True)
    assert not catalogue.cards["card-beta"].is_audiobook
    assert [track.title for track in card.tracks] == ["Second", "Third", "First"]
    assert [(track.chapter_number, track.track_number) for track in card.tracks] == [
        (1, 1),
        (1, 2),
        (2, 3),
    ]
    track_id = encode_track_id("card-alpha", "chapter-b", "track-z")
    assert decode_track_id(track_id) == ("card-alpha", "chapter-b", "track-z")
    assert card.tracks[0].item_id == track_id
    assert "fixture-signed-stream-never-store" not in repr(catalogue)


@pytest.mark.parametrize("item_id", ["not-base64!", "W10", encode_track_id("a", "b", "c")[:-1]])
def test_decode_track_id_rejects_malformed_ids(item_id: str) -> None:
    """Reject malformed or tampered provider track IDs."""
    with pytest.raises(ValueError, match="Invalid Yoto track ID"):
        decode_track_id(item_id)


def test_catalogue_from_models_preserves_groups_and_drops_stream_urls() -> None:
    """Build a stable snapshot from yoto-api-shaped model objects."""
    track = SimpleNamespace(
        key="track-a",
        title="Moshi Track",
        duration=12,
        format="aac",
        channels="stereo",
        type="audio",
        icon=None,
        track_url="https://secure.example/?signature=fixture",
    )
    chapter = SimpleNamespace(key="chapter-a", title="Sleep", icon=None, tracks={"track-a": track})
    card = SimpleNamespace(
        title="Moshi",
        description=None,
        author="Dream Reader",
        category="stories",
        cover_image_large="fixture-art",
        series_title="Moshi Series",
        series_order=1,
        chapters={"chapter-a": chapter},
    )
    group = SimpleNamespace(name="Sleep", card_ids=["card-a"], image_url=None)

    catalogue = Catalogue.from_yoto_models({"card-a": card}, {"sleep": group})

    assert catalogue.cards["card-a"].is_audiobook
    assert catalogue.cards["card-a"].tracks[0].chapter_title == "Sleep"
    assert catalogue.groups["sleep"].card_ids == ("card-a",)
    assert "signature=" not in repr(catalogue)


def test_catalogue_rejects_response_without_cards() -> None:
    """Reject a library response that has no cards list."""
    with pytest.raises(ValueError, match="cards list"):
        Catalogue.from_responses({}, {})
