"""
Test the page parsers against real YouTube Music responses.

The fixtures are public pages captured without an account and trimmed: tracking fields
dropped, item menus cut to their "Go to artist/album" entries, carousels cut to three
cards and the playlist and podcast lists to a few entries.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from music_assistant.providers.ytmusic_native import parsers

FIXTURES = Path(__file__).parent / "fixtures"
DAFT_PUNK = "UCRr1xG_2WIDs18a6cIiCxeA"


def _fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return data


def test_album_page() -> None:
    """Random Access Memories: header, all 13 numbered tracks, and its other version."""
    page = parsers.parse_album_page(_fixture("album"), "MPREb_K8qWMWVqXGi")

    album = page["album"]
    assert album["title"] == "Random Access Memories"
    assert album["artists"] == [{"id": DAFT_PUNK, "name": "Daft Punk"}]
    assert (album["year"], album["album_type"]) == (2013, "album")
    # saving the album to the library likes this audio playlist
    assert album["playlist_id"] == "OLAK5uy_kNhM2yaBTOVwrcZJepB1C9P3-n5_Sfy5c"
    assert album["description"].startswith("Random Access Memories is")
    assert album["thumbnails"]

    tracks = page["tracks"]
    assert [track["track_number"] for track in tracks] == list(range(1, 14))
    assert (tracks[0]["video_id"], tracks[0]["title"], tracks[0]["duration"]) == (
        # served anonymously from the US, this is a different upload of the same track
        "IluRBvnYMoY",
        "Give Life Back to Music",
        276,
    )
    assert all(t["album"] == {"id": "MPREb_K8qWMWVqXGi", "name": album["title"]} for t in tracks)
    assert all(t["duration"] and t["artists"] and t["set_video_id"] for t in tracks)
    # a featured artist is credited next to the album artist
    assert [artist["name"] for artist in tracks[6]["artists"]] == ["Daft Punk", "Paul Williams"]

    assert [(v["browse_id"], v["title"]) for v in page["versions"]] == [
        ("MPREb_eMBjPmWySjR", "Random Access Memories (10th Anniversary Edition)")
    ]


def test_artist_page() -> None:
    """Daft Punk: the subscribe channel differs from the page id, shelves link their full lists."""
    page = parsers.parse_artist_page(_fixture("artist"), DAFT_PUNK)

    assert page["artist"]["name"] == "Daft Punk"
    assert page["artist"]["description"]
    assert page["subscribe_channel_id"] == "UC_kRDKYrUlrbtrSiyu5Tflg"
    assert page["songs_playlist_id"] == "OLAK5uy_lNVBcjNtiCwq-n95uoxJ-Gd4vsfMkyZNs"
    assert page["top_tracks"]
    assert all(t["kind"] == "track" and t["artists"] for t in page["top_tracks"])

    shelves = {shelf["title"]: shelf for shelf in page["shelves"]}
    assert "Top songs" not in shelves
    albums = shelves["Albums"]
    assert (albums["more_browse_id"], albums["more_params"]) == (
        "MPADUCRr1xG_2WIDs18a6cIiCxeA",
        "ggMIegYIARoCAQI%3D",
    )
    assert all(item["kind"] == "album" and item["year"] for item in albums["items"])
    assert {item["kind"] for item in shelves["Fans might also like"]["items"]} == {"artist"}


def test_playlist_page() -> None:
    """A public editorial playlist: owner, description, and entries with their setVideoIds."""
    page = parsers.parse_playlist_page(
        _fixture("playlist"), "RDCLAK5uy_n20FRYQXNt1p1wS55Nj2r14IouO5weaYU"
    )

    playlist = page["playlist"]
    assert playlist["title"] == "Presenting Daft Punk"
    assert playlist["author"] == "YouTube Music"
    assert playlist["description"] == "The most played hits and essential tracks."
    assert playlist["editable"] is False
    assert page["continuation"] is None
    tracks = page["tracks"]
    assert len(tracks) == 4
    assert (tracks[0]["video_id"], tracks[0]["set_video_id"]) == ("D8K90hX4PrE", "56B44F6D10557CC6")
    assert all(t["artists"] and t["duration"] for t in tracks)


def test_podcast_page() -> None:
    """A podcast: its episodes with release date, length and show, and the next page's token."""
    podcast_id = "PLM8bxV0E1Gse5C3n1u-mkVSTZLL5AJICG"
    page = parsers.parse_podcast_page(_fixture("podcast"), podcast_id)

    podcast = page["podcast"]
    assert (podcast["title"], podcast["author"]) == (
        "Ongoing History of New Music",
        "Curiouscast Podcasts",
    )
    assert podcast["description"].startswith("Ongoing History of New Music looks at")
    assert page["continuation"]
    episodes = page["episodes"]
    assert [(e["video_id"], e["duration"]) for e in episodes] == [
        ("jk6Uidp3QqU", 1740),
        ("lFUdx2p16Jg", 2220),
        ("POLGWEJ2RZw", 1920),
    ]
    assert episodes[0]["title"] == "10 Traumas of My Chemical Romance"
    assert all(e["podcast"] == {"id": podcast_id, "name": podcast["title"]} for e in episodes)
    assert all(e["description"] and e["thumbnails"] for e in episodes)
    # recent episodes show their release date without a year, or relative
    assert [e["date"] for e in episodes] == ["2d ago", "Oct 20", "Oct 13"]
    captured = datetime(2026, 10, 31, 12, tzinfo=UTC)
    assert [parsers.parse_release_date(e["date"], captured) for e in episodes] == [
        datetime(2026, 10, 29, tzinfo=UTC),
        datetime(2026, 10, 20, tzinfo=UTC),
        datetime(2026, 10, 13, tzinfo=UTC),
    ]
