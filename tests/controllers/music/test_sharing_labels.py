"""Metadata sanitization must not reconstruct credential-bearing addresses."""

import json

import pytest
from music_assistant_models.media_items import Album, Artist, Radio, Track, UniqueList

from music_assistant.controllers.music.sharing import share_text, share_title


@pytest.mark.parametrize("control", ["\n", "\r", "\t", "\x00", "\u200b", "\u202e"])
@pytest.mark.parametrize("position", ["scheme", "credentials", "hostname", "www"])
def test_split_addresses_in_metadata(control: str, position: str) -> None:
    """Sanitize every metadata label after joining text split by control characters."""
    addresses = {
        "scheme": f"https:{control}//user:secret@private.example/audio?token=secret",
        "credentials": f"https://user:sec{control}ret@private.example/audio?token=secret",
        "hostname": f"https://user:secret@pri{control}vate.example/audio?token=secret",
        "www": f"WW{control}W.private.example/audio?token=secret",
    }
    address = addresses[position]
    item = Track(
        item_id="1",
        provider="library",
        name=f"Song {address}",
        provider_mappings=set(),
        artists=UniqueList(
            [
                Artist(
                    item_id="2",
                    provider="library",
                    name=f"Artist {address}",
                    provider_mappings=set(),
                )
            ]
        ),
        album=Album(
            item_id="3", provider="library", name=f"Album {address}", provider_mappings=set()
        ),
    )
    response = {"title": share_title(item), "text": share_text(item), "links": []}
    assert response == {"title": "Song", "text": "Artist - Song (Album)", "links": []}
    assert "secret" not in json.dumps(response)
    assert "private.example" not in json.dumps(response)


def test_station_label_and_empty_fallback() -> None:
    """Station identity and the empty-label fallback use the same sanitizer."""
    item = Radio(
        item_id="1",
        provider="library",
        name="Station https:\n//user:secret@private.example/audio",
        provider_mappings=set(),
    )
    assert share_title(item) == share_text(item) == "Station"
    item.name = "https:\n//user:secret@private.example/audio"
    assert share_title(item) == share_text(item) == "Music item"
