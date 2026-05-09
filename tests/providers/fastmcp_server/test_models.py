"""Tests for response Brief dataclasses + ``_common`` adapters."""

from __future__ import annotations

from types import SimpleNamespace

from music_assistant.providers.fastmcp_server.models import (
    AlbumBrief,
    ArtistBrief,
    PlayerBrief,
    PlaylistBrief,
    QueueBrief,
    RadioBrief,
    TrackBrief,
)
from music_assistant.providers.fastmcp_server.tools._common import (
    page_args,
    to_brief_album,
    to_brief_artist,
    to_brief_player,
    to_brief_playlist,
    to_brief_queue,
    to_brief_radio,
    to_brief_track,
)


def test_track_brief_defaults() -> None:
    """TrackBrief fills sensible defaults."""
    t = TrackBrief(uri="library://track/1", name="X")
    assert t.artists == []
    assert t.album is None
    assert t.duration is None


def test_to_brief_track_extracts_artists_and_album() -> None:
    """``to_brief_track`` reads names from artists/album attributes."""
    track = SimpleNamespace(
        uri="library://track/42",
        name="Sample",
        artists=[SimpleNamespace(name="A1"), SimpleNamespace(name="A2")],
        album=SimpleNamespace(name="Album"),
        duration=180,
    )
    brief = to_brief_track(track)
    assert brief == TrackBrief(
        uri="library://track/42",
        name="Sample",
        artists=["A1", "A2"],
        album="Album",
        duration=180,
    )


def test_to_brief_album_falls_back_to_artists_list() -> None:
    """``to_brief_album`` uses ``artists[0]`` when there's no scalar artist."""
    album = SimpleNamespace(
        uri="library://album/1",
        name="Album",
        artist=None,
        artists=[SimpleNamespace(name="A1")],
        year=2020,
    )
    assert to_brief_album(album) == AlbumBrief(
        uri="library://album/1", name="Album", artist="A1", year=2020
    )


def test_to_brief_artist() -> None:
    """``to_brief_artist`` extracts uri and name."""
    artist = SimpleNamespace(uri="library://artist/x", name="X")
    assert to_brief_artist(artist) == ArtistBrief(uri="library://artist/x", name="X")


def test_to_brief_playlist() -> None:
    """``to_brief_playlist`` includes track_count and owner when available."""
    playlist = SimpleNamespace(
        uri="library://playlist/1",
        name="Mix",
        track_count=12,
        owner=SimpleNamespace(name="me"),
    )
    assert to_brief_playlist(playlist) == PlaylistBrief(
        uri="library://playlist/1", name="Mix", track_count=12, owner="me"
    )


def test_to_brief_radio() -> None:
    """``to_brief_radio`` maps name + description."""
    radio = SimpleNamespace(uri="library://radio/1", name="R", description="d")
    assert to_brief_radio(radio) == RadioBrief(uri="library://radio/1", name="R", description="d")


def test_to_brief_player_state_enum_value() -> None:
    """``to_brief_player`` unwraps StrEnum-like state via ``.value``."""
    player = SimpleNamespace(
        player_id="kitchen",
        display_name="Kitchen",
        state=SimpleNamespace(value="playing"),
        volume_level=42,
        powered=True,
        current_media=None,
    )
    brief = to_brief_player(player)
    assert brief == PlayerBrief(
        player_id="kitchen", name="Kitchen", state="playing", volume_level=42, powered=True
    )


def test_to_brief_queue_with_items() -> None:
    """``to_brief_queue`` builds a ``QueueBrief`` with item summaries."""
    queue = SimpleNamespace(
        queue_id="kitchen",
        current_index=2,
        items=10,
        shuffle_enabled=True,
        repeat_mode=SimpleNamespace(value="off"),
    )
    items = [
        SimpleNamespace(
            queue_item_id="i1",
            name="One",
            duration=120,
            media_item=SimpleNamespace(artists=[SimpleNamespace(name="A1")]),
        ),
        SimpleNamespace(queue_item_id="i2", name="Two", duration=240, media_item=None),
    ]
    brief = to_brief_queue(queue, items=items)
    assert isinstance(brief, QueueBrief)
    assert brief.queue_id == "kitchen"
    assert brief.shuffle is True
    assert brief.repeat == "off"
    assert len(brief.items) == 2
    assert brief.items[0].artists == ["A1"]


def test_to_brief_queue_uses_canonical_items_int_for_count() -> None:
    """``items`` (int) on the canonical PlayerQueue is the **total** length.

    Earlier code mis-fell back to len(brief_items) (the truncated lookahead),
    under-reporting real queue depth. ``items_count`` from the truncated
    lookahead must not win over the explicit total.
    """
    queue = SimpleNamespace(
        queue_id="q",
        current_index=0,
        items=42,  # canonical MA: total length, not a list
        shuffle_enabled=False,
        repeat_mode=None,
    )
    # Pass only 5 items as the truncated lookahead.
    truncated = [
        SimpleNamespace(queue_item_id=str(i), name=f"t{i}", duration=60, media_item=None)
        for i in range(5)
    ]
    brief = to_brief_queue(queue, items=truncated)
    assert brief.item_count == 42  # not 5
    assert len(brief.items) == 5


def test_page_args_clamps() -> None:
    """``page_args`` clamps negatives and oversized limits."""
    assert page_args(-5, 5000) == (0, 200)
    assert page_args(0, 0) == (0, 1)
    assert page_args(10, 25) == (10, 25)
