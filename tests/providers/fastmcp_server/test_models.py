"""Tests for response Brief dataclasses + ``_common`` adapters."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any, cast

import pytest

from music_assistant.providers.fastmcp_server import models
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
    _external_now_playing,
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
    """
    ``to_brief_player`` reads the canonical ``Player.playback_state`` enum.

    The expected ``PlayerBrief`` pins every defaulted field explicitly —
    leaving them implicit means the test passes only as long as the
    dataclass defaults match what ``to_brief_player`` falls back to for
    legacy stubs. Pinning them here keeps a future default flip from
    silently breaking the playback-state-read contract this test is
    actually about.
    """
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
        player_id="kitchen",
        name="Kitchen",
        state="playing",
        volume_level=42,
        powered=True,
        current_item=None,
        available=True,
        enabled=True,
        needs_setup=False,
        active_group=None,
        synced_to=None,
    )


def test_to_brief_player_falls_back_to_legacy_state_attr() -> None:
    """
    When only the legacy ``state`` attr exists, ``to_brief_player`` still resolves it.

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
    """
    When ``Player.state.powered`` is present, it wins over raw ``Player.powered``.

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
    """
    Without a ``state`` attribute, the raw ``Player.powered`` is the only signal.

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
    """
    ``current_item`` is cleared when ``Player.state.current_media`` is None.

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
    """
    ``available`` / ``enabled`` flow through, and the state ladder fires.

    Combined assert: a regression that breaks the state override only
    when both blocker fields are set would otherwise slip through —
    the dedicated state tests above use single-axis stubs.
    """
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
    # ``unavailable`` wins because it's higher in the priority ladder
    # than ``disabled``; pinning both confirms the ladder ordering on
    # the same stub that exercises the field exposure.
    assert brief.state == "unavailable"


def test_to_brief_player_available_enabled_default_true_when_attrs_missing() -> None:
    """
    Legacy stubs without ``available`` / ``enabled`` keep working (defaults to True).

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
    """
    ``state`` becomes ``"unavailable"`` when the player is offline.

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


def _blocker_stub(**overrides: Any) -> SimpleNamespace:
    """
    Build a minimal player stub for state-ladder tests.

    Every blocker field defaults to its "not blocked" value; tests pass
    ``overrides`` for the axis they're exercising.
    """
    base: dict[str, Any] = {
        "player_id": "p1",
        "name": "P1",
        "playback_state": SimpleNamespace(value="playing"),
        "volume_level": None,
        "powered": True,
        "current_media": None,
        "available": True,
        "enabled": True,
        "needs_setup": False,
        "active_group": None,
        "synced_to": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.parametrize(
    ("blocker", "expected_state"),
    [
        ({"available": False}, "unavailable"),
        ({"enabled": False}, "disabled"),
        ({"needs_setup": True}, "needs_setup"),
        ({"synced_to": "leader-id"}, "synced"),
        ({"active_group": "group-id"}, "synced"),
    ],
)
def test_to_brief_player_state_override_per_blocker(
    blocker: dict[str, object], expected_state: str
) -> None:
    """
    Each blocker in isolation produces its dedicated ``state`` value.

    Pins the per-rung behaviour of the state ladder. Without these
    overrides the LLM would see ``state="playing"`` (the cached
    playback_state on the stub) for every unusable device and pick the
    wrong target.
    """
    assert to_brief_player(_blocker_stub(**blocker)).state == expected_state


@pytest.mark.parametrize(
    ("blockers", "expected_state", "case"),
    [
        (
            {"available": False, "synced_to": "leader"},
            "unavailable",
            "unavailable beats synced",
        ),
        (
            {"available": False, "enabled": False},
            "unavailable",
            "unavailable beats disabled",
        ),
        (
            {"enabled": False, "needs_setup": True},
            "disabled",
            "disabled beats needs_setup",
        ),
        (
            {"needs_setup": True, "active_group": "g"},
            "needs_setup",
            "needs_setup beats synced",
        ),
    ],
)
def test_to_brief_player_state_priority_chain(
    blockers: dict[str, object], expected_state: str, case: str
) -> None:
    """
    When multiple blockers are set, the most-blocking value wins.

    The single ``state`` field has to summarise usability; an LLM that
    only reads ``state`` (skipping the explicit booleans) must make the
    safe call. Priority: unavailable > disabled > needs_setup > synced.
    """
    assert to_brief_player(_blocker_stub(**blockers)).state == expected_state, case


def test_to_brief_player_exposes_new_blocker_fields() -> None:
    """``needs_setup`` / ``active_group`` / ``synced_to`` flow through from MA."""
    player = _blocker_stub(
        needs_setup=True,
        active_group="group-x",
        synced_to="leader-y",
    )
    brief = to_brief_player(player)
    assert brief.needs_setup is True
    assert brief.active_group == "group-x"
    assert brief.synced_to == "leader-y"


def test_to_brief_player_new_fields_default_safely_when_attrs_missing() -> None:
    """
    Legacy stubs without the new attributes still produce a usable brief.

    Mirrors the back-compat pattern already pinned for
    ``available`` / ``enabled`` — tests built before this feature use
    bare ``SimpleNamespace`` players, and they must keep working.
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
    assert brief.needs_setup is False
    assert brief.active_group is None
    assert brief.synced_to is None


def test_to_brief_player_prefers_state_active_group_over_raw_attr() -> None:
    """
    ``Player.state.active_group`` is the canonical sync-membership signal.

    MA's ``__final_active_group`` walks every GROUP player and resolves
    membership / protocol translation; the raw ``Player.active_group``
    dataclass attr lags and stays ``None`` for SyncGroupPlayer
    followers. The brief must read the canonical value so a follower
    captured by an active group surfaces as ``state="synced"``.
    """
    player = SimpleNamespace(
        player_id="follower",
        name="Lenco",
        playback_state=SimpleNamespace(value="idle"),
        volume_level=None,
        powered=True,
        current_media=None,
        # Raw attribute stays None — the case that broke live verification.
        active_group=None,
        synced_to=None,
        state=SimpleNamespace(
            powered=True,
            current_media=None,
            active_group="syncgroup_x",
            synced_to=None,
        ),
    )
    brief = to_brief_player(player)
    assert brief.active_group == "syncgroup_x"
    assert brief.state == "synced"


def test_to_brief_player_prefers_state_synced_to_over_raw_attr() -> None:
    """``Player.state.synced_to`` translates protocol-player ids; the brief must use it."""
    player = SimpleNamespace(
        player_id="follower",
        name="Speaker",
        playback_state=SimpleNamespace(value="idle"),
        volume_level=None,
        powered=True,
        current_media=None,
        synced_to=None,
        active_group=None,
        state=SimpleNamespace(
            powered=True,
            current_media=None,
            active_group=None,
            synced_to="visible-leader-id",
        ),
    )
    brief = to_brief_player(player)
    assert brief.synced_to == "visible-leader-id"
    assert brief.state == "synced"


def test_to_brief_player_falls_back_to_raw_when_state_lacks_group_fields() -> None:
    """
    Back-compat: legacy stubs whose ``state`` lacks the new group fields fall through.

    The existing ``test_to_brief_player_powered_prefers_state`` already
    exercises a stub whose ``state`` has ``powered`` + ``current_media``
    but no ``active_group`` / ``synced_to``. After this change those
    older stubs must keep producing valid briefs — the canonical-read
    branch must guard with ``hasattr(state, "active_group")``.
    """
    player = SimpleNamespace(
        player_id="p1",
        name="P1",
        playback_state=SimpleNamespace(value="playing"),
        volume_level=None,
        powered=True,
        current_media=None,
        active_group="raw-group",
        synced_to=None,
        # state lacks the new fields entirely — only the older attrs.
        state=SimpleNamespace(powered=True, current_media=None),
    )
    brief = to_brief_player(player)
    assert brief.active_group == "raw-group"
    assert brief.state == "synced"


def test_to_brief_player_prefers_state_volume_muted_over_raw() -> None:
    """``volume_muted`` flows through from the canonical state object."""
    player = SimpleNamespace(
        player_id="p1",
        name="P1",
        playback_state=SimpleNamespace(value="playing"),
        volume_level=50,
        volume_muted=False,
        powered=True,
        current_media=None,
        state=SimpleNamespace(
            powered=True,
            current_media=None,
            volume_muted=True,
        ),
    )
    assert to_brief_player(player).volume_muted is True


def test_to_brief_player_prefers_state_group_volume_over_raw() -> None:
    """
    ``group_volume`` is read from state — SyncGroupPlayer holds it there.

    The raw ``Player.group_volume`` dataclass attr can lag; the canonical
    property is exposed on ``Player.state`` (line 1497 of MA's
    ``models/player.py``). A SyncGroupPlayer's brief must surface the
    real group volume, not the cached ``None`` on the raw attribute.
    """
    player = SimpleNamespace(
        player_id="syncgroup_x",
        name="Group",
        playback_state=SimpleNamespace(value="playing"),
        volume_level=None,
        group_volume=None,
        powered=True,
        current_media=None,
        state=SimpleNamespace(
            powered=True,
            current_media=None,
            group_volume=75,
            group_volume_muted=False,
        ),
    )
    brief = to_brief_player(player)
    assert brief.group_volume == 75
    assert brief.group_volume_muted is False


def test_to_brief_player_new_volume_fields_default_to_none_when_attrs_missing() -> None:
    """
    Legacy stubs without volume_muted / group_volume / group_volume_muted attrs work.

    Mirrors the back-compat pattern already pinned for the
    ``active_group`` / ``synced_to`` additions.
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
    assert brief.volume_muted is None
    assert brief.group_volume is None
    assert brief.group_volume_muted is None


def test_to_brief_player_volume_fields_fall_back_to_raw_when_state_lacks_them() -> None:
    """
    Stubs whose ``state`` lacks the volume fields fall through to raw attrs.

    Back-compat with stubs that carry ``state`` for ``powered`` /
    ``current_media`` but predate the new volume fields.
    """
    player = SimpleNamespace(
        player_id="p1",
        name="P1",
        playback_state=SimpleNamespace(value="playing"),
        volume_level=50,
        volume_muted=True,
        group_volume=80,
        group_volume_muted=True,
        powered=True,
        current_media=None,
        state=SimpleNamespace(powered=True, current_media=None),
    )
    brief = to_brief_player(player)
    assert brief.volume_muted is True
    assert brief.group_volume == 80
    assert brief.group_volume_muted is True


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
    assert brief.items[0].index == 0
    assert brief.items[1].index == 1
    assert brief.items[0].artists == ["A1"]


def test_to_brief_queue_exposes_insert_index_fields() -> None:
    """``to_brief_queue`` sets index metadata for agent insert planning."""
    queue = SimpleNamespace(
        queue_id="kitchen",
        current_index=2,
        index_in_buffer=4,
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
    ]
    brief = to_brief_queue(queue, items=items, items_offset=5)
    assert brief.index_in_buffer == 4
    assert brief.next_insertable_index == 5
    assert brief.items_start_index == 5
    assert brief.items[0].index == 5


def test_to_brief_queue_uses_canonical_items_int_for_count() -> None:
    """
    ``items`` (int) on the canonical PlayerQueue is the **total** length.

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
    """
    When the queue exposes no canonical count, report ``item_count=None``.

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


def test_to_brief_queue_exposes_available() -> None:
    """
    ``available`` flows through from ``PlayerQueue`` so callers see the offline case.

    Mirrors the parallel fix on the player side: a queue belonging to
    an offline player is still returned by ``get_active_queue`` but
    now carries an explicit ``available=False`` signal.
    """
    queue = SimpleNamespace(
        queue_id="q",
        current_index=0,
        items=0,
        shuffle_enabled=False,
        repeat_mode=None,
        available=False,
    )
    assert to_brief_queue(queue).available is False


def test_to_brief_queue_available_defaults_true_when_attr_missing() -> None:
    """Legacy queue stubs without ``available`` keep working (defaults to True)."""
    queue = SimpleNamespace(
        queue_id="q",
        current_index=0,
        items=0,
        shuffle_enabled=False,
        repeat_mode=None,
    )
    assert to_brief_queue(queue).available is True


def test_player_brief_external_source_defaults_none() -> None:
    """A self-driven player exposes ``external_source = None`` by default."""
    player = SimpleNamespace(
        player_id="p1",
        name="Speaker",
        playback_state=SimpleNamespace(value="idle"),
        volume_level=None,
        current_media=None,
    )
    assert to_brief_player(player).external_source is None


def _audio_source_item(
    *, provider: str, title: str | None, name: str = "Wrapper"
) -> SimpleNamespace:
    """Build a queue item stub whose stream is a plugin AUDIO_SOURCE."""
    return SimpleNamespace(
        name=name,
        streamdetails=SimpleNamespace(
            media_type=SimpleNamespace(value="audio_source"),
            provider=provider,
            stream_metadata=SimpleNamespace(title=title),
        ),
    )


def test_external_now_playing_returns_provider_and_title() -> None:
    """``_external_now_playing`` returns (provider, title) for an AUDIO_SOURCE item."""
    item = _audio_source_item(provider="yandex_ynison--PL8BnL7a", title="Behind Your Walls")
    assert _external_now_playing(item) == ("yandex_ynison--PL8BnL7a", "Behind Your Walls")


def test_external_now_playing_none_for_normal_track() -> None:
    """``_external_now_playing`` returns ``None`` for a normal track item."""
    item = SimpleNamespace(
        name="Real Track",
        streamdetails=SimpleNamespace(
            media_type=SimpleNamespace(value="track"),
            provider="yandex_music--abc",
            stream_metadata=None,
        ),
    )
    assert _external_now_playing(item) is None


def test_external_now_playing_none_when_no_streamdetails() -> None:
    """``_external_now_playing`` returns ``None`` when there are no stream details."""
    assert _external_now_playing(SimpleNamespace(name="x", streamdetails=None)) is None
    assert _external_now_playing(None) is None


def test_external_now_playing_title_may_be_none() -> None:
    """``_external_now_playing`` returns ``(provider, None)`` when the title is absent."""
    item = _audio_source_item(provider="airplay--1", title=None)
    assert _external_now_playing(item) == ("airplay--1", None)


def test_external_now_playing_accepts_legacy_plugin_source() -> None:
    """The deprecated ``plugin_source`` media type is still treated as external."""
    item = SimpleNamespace(
        name="Wrapper",
        streamdetails=SimpleNamespace(
            media_type=SimpleNamespace(value="plugin_source"),
            provider="spotify--1",
            stream_metadata=SimpleNamespace(title="Some Song"),
        ),
    )
    assert _external_now_playing(item) == ("spotify--1", "Some Song")


def _queue(*, state: str, current_item: SimpleNamespace | None) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(value=state), current_item=current_item)


def test_to_brief_player_external_source_playing() -> None:
    """Idle player + active queue playing an AUDIO_SOURCE reports the real state."""
    player = SimpleNamespace(
        player_id="lenco",
        name="Lenco LS-500",
        playback_state=SimpleNamespace(value="idle"),
        volume_level=None,
        current_media=None,
    )
    queue = _queue(
        state="playing",
        current_item=_audio_source_item(
            provider="yandex_ynison--PL8BnL7a", title="Behind Your Walls"
        ),
    )
    brief = to_brief_player(player, active_queue=queue)
    assert brief.state == "playing"
    assert brief.external_source == "yandex_ynison--PL8BnL7a"
    assert brief.current_item == "Behind Your Walls"


def test_to_brief_player_normal_active_queue_unchanged() -> None:
    """A normal track in the active queue leaves external_source None."""
    player = SimpleNamespace(
        player_id="p",
        name="Speaker",
        playback_state=SimpleNamespace(value="playing"),
        volume_level=None,
        current_media=SimpleNamespace(uri="ym://track/1", title="Song"),
    )
    normal_item = SimpleNamespace(
        name="Song",
        streamdetails=SimpleNamespace(
            media_type=SimpleNamespace(value="track"),
            provider="yandex_music--x",
            stream_metadata=None,
        ),
    )
    brief = to_brief_player(player, active_queue=_queue(state="playing", current_item=normal_item))
    assert brief.external_source is None
    assert brief.state == "playing"
    assert brief.current_item == "Song"


def test_to_brief_player_blocking_ladder_wins_over_queue() -> None:
    """An unavailable player keeps state=unavailable even with a playing queue."""
    player = SimpleNamespace(
        player_id="p",
        name="Offline",
        playback_state=SimpleNamespace(value="idle"),
        volume_level=None,
        current_media=None,
        available=False,
    )
    queue = _queue(
        state="playing",
        current_item=_audio_source_item(provider="airplay--1", title="X"),
    )
    assert to_brief_player(player, active_queue=queue).state == "unavailable"


def test_to_brief_player_synced_wins_over_queue() -> None:
    """A sync follower keeps state=synced even though its leader's queue plays."""
    player = SimpleNamespace(
        player_id="p",
        name="Follower",
        playback_state=SimpleNamespace(value="idle"),
        volume_level=None,
        current_media=None,
        synced_to="leader",
    )
    queue = _queue(
        state="playing",
        current_item=_audio_source_item(provider="airplay--1", title="X"),
    )
    assert to_brief_player(player, active_queue=queue).state == "synced"


def test_to_brief_player_disabled_wins_over_queue() -> None:
    """An admin-disabled player keeps state=disabled even with a playing queue."""
    player = SimpleNamespace(
        player_id="p",
        name="Disabled",
        playback_state=SimpleNamespace(value="idle"),
        volume_level=None,
        current_media=None,
        enabled=False,
    )
    queue = _queue(
        state="playing",
        current_item=_audio_source_item(provider="airplay--1", title="X"),
    )
    assert to_brief_player(player, active_queue=queue).state == "disabled"


def test_to_brief_player_needs_setup_wins_over_queue() -> None:
    """A not-yet-configured player keeps state=needs_setup even with a playing queue."""
    player = SimpleNamespace(
        player_id="p",
        name="Unconfigured",
        playback_state=SimpleNamespace(value="idle"),
        volume_level=None,
        current_media=None,
        needs_setup=True,
    )
    queue = _queue(
        state="playing",
        current_item=_audio_source_item(provider="airplay--1", title="X"),
    )
    assert to_brief_player(player, active_queue=queue).state == "needs_setup"


def test_to_brief_player_external_source_without_title_keeps_current_media() -> None:
    """A titleless external source still sets external_source but does not blank current_item."""
    player = SimpleNamespace(
        player_id="p",
        name="Speaker",
        playback_state=SimpleNamespace(value="idle"),
        volume_level=None,
        current_media=SimpleNamespace(uri="airplay://x", title="Fallback"),
    )
    queue = _queue(
        state="playing",
        current_item=_audio_source_item(provider="airplay--1", title=None),
    )
    brief = to_brief_player(player, active_queue=queue)
    assert brief.state == "playing"
    assert brief.external_source == "airplay--1"
    assert brief.current_item == "Fallback"


def test_to_brief_player_no_active_queue_legacy_behaviour() -> None:
    """With active_queue omitted, state comes from player.playback_state."""
    player = SimpleNamespace(
        player_id="p",
        name="Speaker",
        playback_state=SimpleNamespace(value="idle"),
        volume_level=None,
        current_media=None,
    )
    brief = to_brief_player(player)
    assert brief.state == "idle"
    assert brief.external_source is None


def test_to_brief_queue_relabels_external_item() -> None:
    """An AUDIO_SOURCE item shows the real track title; normal items keep theirs."""
    external = _audio_source_item(
        provider="yandex_ynison--PL8BnL7a",
        title="Behind Your Walls",
        name="Yandex Music Connect (Ynison)",
    )
    external.queue_item_id = "ext"
    external.duration = None
    external.media_item = None
    normal = SimpleNamespace(
        queue_item_id="n1",
        name="Ordinary Song",
        duration=120,
        media_item=SimpleNamespace(artists=[SimpleNamespace(name="A")]),
        streamdetails=None,
    )
    queue = SimpleNamespace(
        queue_id="q",
        current_index=0,
        items=2,
        shuffle_enabled=False,
        repeat_mode=SimpleNamespace(value="off"),
        available=True,
    )
    brief = to_brief_queue(queue, items=[external, normal])
    names = [it.name for it in brief.items]
    assert names == ["Behind Your Walls", "Ordinary Song"]


def test_to_brief_queue_external_item_without_title_keeps_wrapper_name() -> None:
    """A titleless AUDIO_SOURCE item falls back to its wrapper name, not an empty string."""
    external = _audio_source_item(
        provider="airplay--1",
        title=None,
        name="AirPlay",
    )
    external.queue_item_id = "ext"
    external.duration = None
    external.media_item = None
    queue = SimpleNamespace(
        queue_id="q",
        current_index=0,
        items=1,
        shuffle_enabled=False,
        repeat_mode=SimpleNamespace(value="off"),
        available=True,
    )
    brief = to_brief_queue(queue, items=[external])
    assert brief.items[0].name == "AirPlay"


_DEBUG_CLASSES = [
    ("PlayerInspect", {"player_id", "raw", "state", "truncated"}),
    ("QueueInspect", {"queue_id", "raw", "current_item", "truncated"}),
    ("ProviderInspect", {"instance_id", "raw", "manifest", "truncated"}),
    ("LogLine", {"timestamp", "level", "component", "message"}),
    (
        "LogTailResult",
        {
            "log_path",
            "lines",
            "bytes_scanned",
            "truncated",
            "has_more",
            "response_truncated",
            "next_call_hint",
        },
    ),
    ("ComponentCount", {"component", "count"}),
    (
        "LogStatsResult",
        {
            "log_path",
            "window_seconds",
            "total_records",
            "level_counts",
            "top_components",
            "first_timestamp",
            "last_timestamp",
            "bytes_scanned",
            "truncated",
        },
    ),
    ("EventRecord", {"timestamp", "event_type", "object_id", "data"}),
    ("EventSnapshot", {"events", "buffer_capacity", "total_seen"}),
    (
        "EventBufferStats",
        {"capacity", "current_size", "total_seen", "dropped", "subscribed_since", "by_type"},
    ),
    ("ProviderSummary", {"instance_id", "domain", "type", "name", "available", "last_error"}),
    ("ProviderList", {"providers"}),
    ("ConfigValueDump", {"key", "type", "value"}),
    ("ProviderConfigDump", {"instance_id", "domain", "values", "truncated"}),
    ("RouteEntry", {"method", "path", "registered_by"}),
    ("RouteList", {"routes"}),
    ("PackageVersions", {"packages"}),
    ("ReloadResult", {"instance_id", "duration_ms", "new_available", "last_error"}),
    (
        "HealthSummary",
        {
            "providers_loaded",
            "providers_disabled",
            "providers_error",
            "providers_error_details",
            "queues_total",
            "queues_with_active_playback",
            "queues_with_errors",
            "events_per_min_by_type",
            "log_errors_last_5min",
            "disabled_capabilities",
        },
    ),
]


@pytest.mark.parametrize(("name", "fields"), _DEBUG_CLASSES)
def test_debug_dataclass_shape(name: str, fields: set[str]) -> None:
    """Debug dataclasses are frozen, kw_only, and have the expected fields."""
    cls = cast("type", getattr(models, name))
    assert dataclasses.is_dataclass(cls), f"{name} is not a dataclass"
    assert cls.__dataclass_params__.frozen, f"{name} must be frozen"  # type: ignore[attr-defined]
    assert cls.__dataclass_params__.kw_only, f"{name} must be kw_only"  # type: ignore[attr-defined]
    actual = {f.name for f in dataclasses.fields(cls)}
    assert actual == fields, f"{name} fields drift: {actual - fields=} {fields - actual=}"


_CONFIG_CLASSES = [
    ("ConfigTarget", {"target_type", "target_id", "domain", "name", "enabled"}),
    ("ConfigTargetList", {"providers", "core", "players"}),
    ("CoreConfigDump", {"domain", "values", "truncated"}),
    ("PlayerConfigDump", {"player_id", "provider", "values", "truncated"}),
    (
        "ConfigEntryDump",
        {
            "key",
            "type",
            "label",
            "default_value",
            "required",
            "description",
            "options",
            "range",
            "advanced",
            "hidden",
            "requires_reload",
            "depends_on",
            "action",
            "current_value",
        },
    ),
    ("ConfigEntryList", {"target_type", "target_id", "entries", "truncated"}),
    ("DSPConfigDump", {"player_id", "enabled", "input_gain", "output_gain", "filters"}),
    ("ValueChange", {"key", "before", "after", "secret"}),
    ("DiffResult", {"target_type", "target_id", "changes"}),
    (
        "SetValueResult",
        {"target_type", "target_id", "key", "applied", "requires_reload", "audit_log_id", "diff"},
    ),
    (
        "SaveResult",
        {
            "target_type",
            "target_id",
            "applied",
            "changes",
            "requires_reload",
            "audit_log_id",
            "diff",
        },
    ),
    ("ActionResult", {"instance_id", "action_key", "new_entries", "extra_data", "audit_log_id"}),
]


@pytest.mark.parametrize(("name", "fields"), _CONFIG_CLASSES)
def test_config_dataclass_shape(name: str, fields: set[str]) -> None:
    """Config dataclasses are frozen, kw_only, and have the expected fields."""
    cls = cast("type", getattr(models, name))
    assert dataclasses.is_dataclass(cls), f"{name} is not a dataclass"
    assert cls.__dataclass_params__.frozen, f"{name} must be frozen"  # type: ignore[attr-defined]
    assert cls.__dataclass_params__.kw_only, f"{name} must be kw_only"  # type: ignore[attr-defined]
    actual = {f.name for f in dataclasses.fields(cls)}
    assert actual == fields, f"{name} fields drift: {actual - fields=} {fields - actual=}"
