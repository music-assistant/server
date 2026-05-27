"""Tests for response Brief dataclasses + ``_common`` adapters."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

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


def test_to_brief_player_reads_playback_state() -> None:
    """``to_brief_player`` reads the canonical ``Player.playback_state`` enum."""
    player = SimpleNamespace(
        player_id="kitchen",
        name="Kitchen",
        playback_state=SimpleNamespace(value="playing"),
        volume_level=42,
        powered=True,
        current_media=None,
    )
    brief = to_brief_player(player)
    assert brief == PlayerBrief(
        player_id="kitchen", name="Kitchen", state="playing", volume_level=42, powered=True
    )


def test_to_brief_player_falls_back_to_legacy_state_attr() -> None:
    """When only the legacy ``state`` attr exists, ``to_brief_player`` still resolves it.

    Kept for back-compat with older shims / hand-built test stubs.
    """
    player = SimpleNamespace(
        player_id="kitchen",
        name="Kitchen",
        state=SimpleNamespace(value="paused"),
        volume_level=10,
        powered=True,
        current_media=None,
    )
    assert to_brief_player(player).state == "paused"


def test_to_brief_player_current_item_prefers_title() -> None:
    """``current_item`` uses :class:`PlayerMedia.title` when available."""
    player = SimpleNamespace(
        player_id="p1",
        name="P1",
        playback_state=SimpleNamespace(value="playing"),
        volume_level=50,
        powered=True,
        current_media=SimpleNamespace(uri="spotify://track/x", title="Song Name"),
    )
    assert to_brief_player(player).current_item == "Song Name"


def test_to_brief_player_current_item_falls_back_to_uri() -> None:
    """No title → ``current_item`` falls back to URI (always present on PlayerMedia)."""
    player = SimpleNamespace(
        player_id="p1",
        name="P1",
        playback_state=SimpleNamespace(value="playing"),
        volume_level=50,
        powered=True,
        current_media=SimpleNamespace(uri="spotify://track/x", title=None),
    )
    assert to_brief_player(player).current_item == "spotify://track/x"


def test_to_brief_player_no_current_media() -> None:
    """``current_item`` is ``None`` when the player is idle (no current media)."""
    player = SimpleNamespace(
        player_id="p1",
        name="P1",
        playback_state=SimpleNamespace(value="idle"),
        volume_level=0,
        powered=False,
        current_media=None,
    )
    assert to_brief_player(player).current_item is None


@pytest.mark.parametrize(
    ("player_powered", "state_powered", "expected", "case"),
    [
        # 1. ``state`` present, value differs from raw ``powered`` →
        #    canonical state wins.
        (False, True, True, "state.powered=True overrides raw .powered=False"),
        # 2. Contradictory direction — the other way — also wins via state.
        #    Without this case, a test that always read raw .powered would
        #    still pass case #1 (because it happens to match expected=True
        #    when state.powered=True).
        (True, False, False, "state.powered=False overrides raw .powered=True"),
    ],
)
def test_to_brief_player_powered_prefers_state(
    player_powered: bool, state_powered: bool, expected: bool, case: str
) -> None:
    """When ``Player.state.powered`` is present, it wins over raw ``Player.powered``.

    MA core builds ``_state.powered`` from ``__final_power_state`` and
    serialises it in the REST API; the raw ``Player.powered`` property
    returns ``_attr_powered`` which lags behind (and stays ``False`` for
    some virtual player types). The brief must match what
    ``Player.state.to_dict()`` would emit — i.e. the canonical ``state.powered``
    value, not the raw attribute.
    """
    player = SimpleNamespace(
        player_id="p1",
        name="P1",
        playback_state=SimpleNamespace(value="playing"),
        volume_level=100,
        powered=player_powered,
        current_media=None,
        state=SimpleNamespace(powered=state_powered, current_media=None),
    )
    assert to_brief_player(player).powered is expected, case


def test_to_brief_player_powered_falls_back_to_raw_when_no_state() -> None:
    """Without a ``state`` attribute, the raw ``Player.powered`` is the only signal.

    Pairs with the parametrized test above to pin both branches of the
    canonical-vs-raw selection: state present (state wins) and state absent
    (raw wins).
    """
    player = SimpleNamespace(
        player_id="p1",
        name="P1",
        playback_state=SimpleNamespace(value="playing"),
        volume_level=100,
        powered=False,
        current_media=None,
        # NO `state` attribute at all.
    )
    assert to_brief_player(player).powered is False


def test_to_brief_player_current_item_uses_state_current_media() -> None:
    """``current_item`` is cleared when ``Player.state.current_media`` is None.

    After ``stop`` MA core clears ``_state.current_media``, but the raw
    ``_attr_current_media`` may persist until the next playback. The brief
    must reflect the canonical state so the LLM doesn't think a track is
    still playing.
    """
    stale = SimpleNamespace(uri="library://track/48", title="07")
    player = SimpleNamespace(
        player_id="p1",
        name="P1",
        playback_state=SimpleNamespace(value="idle"),
        volume_level=0,
        powered=True,
        current_media=stale,
        state=SimpleNamespace(powered=True, current_media=None),
    )
    assert to_brief_player(player).current_item is None


def test_to_brief_player_exposes_available_and_enabled() -> None:
    """``available`` / ``enabled`` flow through from the upstream player object."""
    player = SimpleNamespace(
        player_id="p1",
        name="P1",
        playback_state=SimpleNamespace(value="idle"),
        volume_level=0,
        powered=True,
        current_media=None,
        available=False,
        enabled=False,
    )
    brief = to_brief_player(player)
    assert brief.available is False
    assert brief.enabled is False


def test_to_brief_player_available_enabled_default_true_when_attrs_missing() -> None:
    """Legacy stubs without ``available`` / ``enabled`` keep working (defaults to True).

    Pins back-compat: tests built before this feature use bare
    ``SimpleNamespace`` players, and they must still produce a usable brief.
    """
    player = SimpleNamespace(
        player_id="p1",
        name="P1",
        playback_state=SimpleNamespace(value="playing"),
        volume_level=50,
        powered=True,
        current_media=None,
    )
    brief = to_brief_player(player)
    assert brief.available is True
    assert brief.enabled is True


def test_to_brief_player_unavailable_overrides_state() -> None:
    """``state`` becomes ``"unavailable"`` when the player is offline.

    Without the override the brief reports the cached ``playback_state``
    (typically ``"idle"``) and an LLM cannot distinguish a quiet speaker
    from one that fell off the network.
    """
    player = SimpleNamespace(
        player_id="p1",
        name="P1",
        playback_state=SimpleNamespace(value="idle"),
        volume_level=None,
        powered=True,
        current_media=None,
        available=False,
    )
    assert to_brief_player(player).state == "unavailable"


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


def test_to_brief_queue_returns_none_count_when_unknown() -> None:
    """When the queue exposes no canonical count, report ``item_count=None``.

    A silent ``0`` (formerly returned via ``len(brief_items)`` when the
    truncated lookahead was empty) would tell the LLM the queue is empty
    when in fact it just doesn't know. ``None`` is the honest answer and
    lets clients prompt the user instead of acting on false data.
    """
    queue = SimpleNamespace(
        queue_id="q",
        current_index=0,
        # No `items` / `items_count` / `items_total` exposed at all.
        shuffle_enabled=False,
        repeat_mode=None,
    )
    brief = to_brief_queue(queue, items=None)
    assert brief.item_count is None
    assert brief.items == []
