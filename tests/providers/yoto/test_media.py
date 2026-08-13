"""Tests for mapping stable Yoto records to Music Assistant models."""

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from music_assistant_models.enums import ImageType, MediaType
from music_assistant_models.media_items import Album, Audiobook, Track

from music_assistant.providers.yoto.catalogue import Catalogue
from music_assistant.providers.yoto.media import map_album, map_audiobook, map_track

FIXTURES = Path(__file__).parent / "fixtures"


def catalogue() -> Catalogue:
    """Build the shared catalogue fixture."""

    def load(name: str) -> Mapping[str, Any]:
        return cast("Mapping[str, Any]", json.loads((FIXTURES / name).read_text()))

    return Catalogue.from_responses(load("library.json"), {"card-alpha": load("card_detail.json")})


def test_music_card_mapping_preserves_metadata_order_and_url_free_mappings() -> None:
    """Map albums and ordered tracks with stable URL-free mappings."""
    snapshot = catalogue()
    album = map_album(snapshot.cards["card-alpha"], "yoto-instance")
    assert isinstance(album, Album)
    assert (album.item_id, album.name, album.artists[0].name) == (
        "card-alpha",
        "Moshi Moon",
        "Dream Reader",
    )
    assert album.metadata.description == "Calm bedtime stories"
    assert album.metadata.images is not None
    assert album.metadata.images[0].type is ImageType.THUMB
    assert album.metadata.images[0].path == "fixture-artwork-card-alpha"
    mapping = next(iter(album.provider_mappings))
    assert (mapping.provider_domain, mapping.provider_instance, mapping.url) == (
        "yoto",
        "yoto-instance",
        None,
    )
    assert map_album(snapshot.cards["card-beta"], "yoto-instance").artists[0].name == "Yoto"

    source = snapshot.cards["card-alpha"]
    tracks = [map_track(source, track, "yoto-instance") for track in source.tracks]
    assert all(isinstance(track, Track) for track in tracks)
    assert [track.name for track in tracks] == ["Second", "Third", "First"]
    assert [track.track_number for track in tracks] == [1, 2, 3]
    assert [track.duration for track in tracks] == [8, 7, 4]
    assert tracks[0].album is not None
    assert tracks[0].album.item_id == source.item_id
    assert tracks[0].media_type is MediaType.TRACK
    assert next(iter(tracks[0].provider_mappings)).url is None


def test_story_card_maps_to_resumable_audiobook_with_ordered_chapters() -> None:
    """Map a story card to one audiobook with string authors and chapters."""
    audiobook = map_audiobook(catalogue().cards["card-alpha"], "yoto-instance")
    assert isinstance(audiobook, Audiobook)
    assert audiobook.authors == ["Dream Reader"]
    assert (audiobook.duration, audiobook.position, audiobook.metadata.grouping) == (19, 2, "Moshi")
    assert audiobook.is_playable is False
    assert audiobook.metadata.chapters is not None
    assert [
        (chapter.position, chapter.name, chapter.start, chapter.end)
        for chapter in audiobook.metadata.chapters
    ] == [(1, "Chapter Two", 0, 15), (2, "Chapter One", 15, 19)]
    mapping = next(iter(audiobook.provider_mappings))
    assert (mapping.url, mapping.available) == (None, False)


def test_audiobook_playability_requires_known_matching_stream_properties() -> None:
    """Only expose concatenated playback for normalized compatible stream parts."""
    card = catalogue().cards["card-alpha"]
    compatible = replace(
        card, tracks=tuple(replace(t, format=" AAC ", channels="Stereo") for t in card.tracks)
    )
    missing = replace(
        compatible, tracks=(replace(compatible.tracks[0], format=None), *compatible.tracks[1:])
    )
    mixed = replace(
        compatible, tracks=(replace(compatible.tracks[0], channels="mono"), *compatible.tracks[1:])
    )
    unknown = replace(
        compatible, tracks=tuple(replace(t, format="banana") for t in compatible.tracks)
    )
    assert map_audiobook(compatible, "instance").is_playable is True
    assert map_audiobook(missing, "instance").is_playable is False
    assert map_audiobook(mixed, "instance").is_playable is False
    assert map_audiobook(unknown, "instance").is_playable is False
