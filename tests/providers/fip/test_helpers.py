"""Tests for the FIP helper functions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from music_assistant.providers.fip.constants import (
    FIP_STATIONS,
    STREAM_DELAY,
)
from music_assistant.providers.fip.helpers import (
    LiveDisplay,
    LiveItem,
    build_stream_metadata,
    parse_live,
    parse_live_item,
    parse_pull,
    parse_radio,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> Any:
    """Return a recorded answer, or sequence of answers, of the now-playing service."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _song(title: str, start: int, end: int) -> LiveItem:
    """Return a song entry."""
    return LiveItem(title, "Artist", f"cover-{title}", start, end, f"id-{title}")


PLACEHOLDER = LiveItem(
    "Le direct", "La radio la plus éclectique du monde", "visual", None, None, None
)


# ---------------------------------------------------------------------------
# parsing


def test_a_song_has_title_artist_cover_and_times() -> None:
    """A webradio song carries everything the player shows."""
    current, upcoming = parse_live(_fixture("webradio_song"))
    assert current == LiveItem(
        title="My Baby Just Cares For Me",
        subtitle="Nina Simone",
        image_id="776705ff-88c5-4484-ae30-9155aaaa0af2",
        start=1792779016,
        end=1792779226,
        song_id="06bf6232-8fb4-4ad2-b15a-a2599e8a6d57",
    )
    assert current.song_id is not None
    assert upcoming is not None
    assert upcoming.title == "Lawns"
    assert upcoming.start == current.end


def test_a_programme_is_timed_but_not_a_song() -> None:
    """A FIP programme has times and a picture, but no song id."""
    current, _ = parse_live(_fixture("fip_programme"))
    assert current is not None
    assert current.title == "Club Jazzafip"
    assert current.is_timed
    assert current.song_id is None


def test_a_placeholder_has_no_times() -> None:
    """The "Le direct" placeholder carries no start time."""
    current, upcoming = parse_live(_fixture("webradio_placeholder"))
    assert current is not None
    assert current.title == "Le direct"
    assert not current.is_timed
    assert upcoming is not None
    assert not upcoming.is_timed


def test_empty_entries_and_stringified_none() -> None:
    """Empty entries parse to None, and the service's "None" song id means no song."""
    assert parse_live_item(None) is None
    assert parse_live_item({"firstLine": "  "}) is None
    item = parse_live_item({"firstLine": "Show", "startTime": "10", "songUuid": "None"})
    assert item is not None
    assert item.start == 10
    assert item.song_id is None
    assert parse_live({"now": None, "next": []}) == (None, None)


# ---------------------------------------------------------------------------
# stream metadata


def test_song_metadata_with_cover_and_progress() -> None:
    """A song shows title, artist, a sized cover and its elapsed time."""
    song = _song("Song", start=1000, end=1200)
    metadata = build_stream_metadata(song, at=1000 + STREAM_DELAY + 30)
    assert metadata is not None
    assert metadata.title == "Song"
    assert metadata.artist == "Artist"
    assert metadata.album is None
    assert metadata.image_url == "https://www.radiofrance.fr/pikapi/images/cover-Song/1000x1000"
    assert metadata.duration == 200
    assert metadata.elapsed_time == 30


def test_programme_metadata_puts_the_tagline_in_place_of_an_artist() -> None:
    """A programme shows its name with the station's tagline, and leaves the album free."""
    current, _ = parse_live(_fixture("fip_programme"))
    metadata = build_stream_metadata(current, at=1792775000)
    assert metadata is not None
    assert metadata.title == "Club Jazzafip"
    assert metadata.artist == "La radio la plus éclectique du monde"
    assert metadata.album is None
    assert metadata.description is None
    assert metadata.duration == 3600


def test_a_placeholder_shows_the_station() -> None:
    """A placeholder gives no metadata, so the player shows the station itself."""
    assert build_stream_metadata(PLACEHOLDER, at=0) is None
    assert build_stream_metadata(None, at=0) is None


# ---------------------------------------------------------------------------
# LiveDisplay, the rules that keep the display steady


def test_the_first_answer_is_shown() -> None:
    """Whatever the service says first is shown."""
    display = LiveDisplay()
    assert display.update(_song("A", 1000, 1200), None, at=1010)
    assert display.shown is not None
    assert display.shown.title == "A"


def test_a_placeholder_during_a_song_is_no_news() -> None:
    """The placeholder flickering in while a song is on air is ignored."""
    display = LiveDisplay()
    display.update(_song("A", 1000, 1200), None, at=1010)
    assert not display.update(PLACEHOLDER, None, at=1100)
    assert display.shown is not None
    assert display.shown.title == "A"


def test_a_placeholder_long_after_the_song_ended_is_shown() -> None:
    """When the service stays on the placeholder, the display follows it in the end."""
    display = LiveDisplay()
    display.update(_song("A", 1000, 1200), None, at=1010)
    assert display.update(PLACEHOLDER, None, at=1200 + STREAM_DELAY + 61)
    assert display.shown == PLACEHOLDER


def test_the_announced_song_is_shown_once_due() -> None:
    """The announced next song replaces the current one at its end plus the stream delay."""
    display = LiveDisplay()
    song_a, song_b = _song("A", 1000, 1200), _song("B", 1200, 1400)
    display.update(song_a, song_b, at=1010)
    assert not display.update(song_a, song_b, at=1200 + STREAM_DELAY - 1)
    assert display.update(song_a, None, at=1200 + STREAM_DELAY)
    assert display.shown == song_b


def test_the_old_song_after_a_promotion_is_no_news() -> None:
    """While the service still names the old song, the announced one stays."""
    display = LiveDisplay()
    song_a, song_b = _song("A", 1000, 1200), _song("B", 1200, 1400)
    display.update(song_a, song_b, at=1010)
    display.update(song_a, None, at=1210)
    assert not display.update(song_a, song_a, at=1220)
    assert display.shown == song_b
    # the service catches up with the song on display
    display.update(song_b, None, at=1240)
    assert display.shown == song_b


def test_an_earlier_song_is_no_news() -> None:
    """The service going back to the song before does not move the display back."""
    display = LiveDisplay()
    song_a, song_b = _song("A", 1000, 1200), _song("B", 1200, 1400)
    display.update(song_a, None, at=1010)
    display.update(song_b, None, at=1230)
    assert not display.update(song_a, None, at=1240)
    assert display.shown == song_b


def test_a_changed_end_time_is_taken_over() -> None:
    """The service correcting a song's end time updates the display."""
    display = LiveDisplay()
    display.update(_song("A", 1000, 1200), None, at=1010)
    assert display.update(_song("A", 1000, 1210), None, at=1100)
    assert display.shown is not None
    assert display.shown.end == 1210


def test_a_next_that_turns_into_the_old_song_is_ignored() -> None:
    """The service's "next" falling back to the current song does not erase the announcement."""
    display = LiveDisplay()
    song_a, song_b = _song("A", 1000, 1200), _song("B", 1200, 1400)
    display.update(song_a, song_b, at=1010)
    display.update(song_a, song_a, at=1150)
    display.update(song_a, None, at=1210)
    assert display.shown == song_b


def test_a_programme_hands_over_to_its_first_song() -> None:
    """A song announced inside a programme is shown when it starts, not when the programme ends."""
    display = LiveDisplay()
    current, _ = parse_live(_fixture("fip_programme"))
    assert current is not None
    song = _song("Maré mansa", 1792778160, 1792778547)
    display.update(current, song, at=1792778000)
    assert not display.update(current, None, at=1792778160 + STREAM_DELAY - 1)
    assert display.update(current, None, at=1792778160 + STREAM_DELAY)
    assert display.shown == song


def test_overdue_once_the_song_should_be_over() -> None:
    """The display is overdue from the song's end plus the stream delay."""
    display = LiveDisplay()
    display.update(_song("A", 1000, 1200), None, at=1010)
    assert not display.overdue(1200 + STREAM_DELAY - 1)
    assert display.overdue(1200 + STREAM_DELAY)


# ---------------------------------------------------------------------------
# stations


def test_every_station_parses_with_its_picture() -> None:
    """Every station becomes a radio with its description and picture."""
    for station_id, station in FIP_STATIONS.items():
        radio = parse_radio(station_id, "fip_test", "fip")
        assert radio.name == station["name"]
        assert radio.metadata.description == station["description"]
        assert radio.metadata.images
        assert station["image_id"] in radio.metadata.images[0].path


# ---------------------------------------------------------------------------
# a recorded stretch of FIP Groove, replayed as the player would poll it


def _replay(name: str, every: int = 10) -> list[tuple[int, str]]:
    """
    Replay a recorded sequence of answers, polled every few seconds.

    Return each change of the display as (time, title), with "-" for the station itself.
    """
    answers = _fixture(name)
    display = LiveDisplay()
    shown: list[tuple[int, str]] = []
    at = answers[0]["at"]
    while at <= answers[-1]["at"]:
        answer = [a for a in answers if a["at"] <= at][-1]
        if display.update(*parse_live(answer), at=at):
            item = display.shown
            title = item.title if item and item.is_timed else "-"
            if not shown or shown[-1][1] != title:
                shown.append((at, title))
        at += every
    return shown


def test_a_recorded_stretch_shows_each_song_once_in_order() -> None:
    """
    Twenty noisy minutes of FIP Groove show each song once, in order, on time.

    The recording holds some sixty placeholders and a step back to a previous song,
    none of which may show. Each song must show within one poll of its start plus the
    stream delay, mostly before the service names it. The station itself shows once,
    after the service gave "Butterflies" an end time eleven seconds after its start.
    """
    shown = _replay("groove_sequence")
    assert [title for _, title in shown] == [
        "Tout moun boule",
        "A night in Tunisia",
        "Molly",
        "Dizzy",
        "No more trouble",
        "Butterflies",
        "-",
        "Adjust like dat",
    ]
    starts = {
        entry["firstLine"]: entry["startTime"]
        for answer in _fixture("groove_sequence")
        for entry in [answer["now"], *answer["next"]]
        if entry and entry["startTime"]
    }
    for at, title in shown[1:]:
        if title != "-":
            assert at <= starts[title] + STREAM_DELAY + 10, title


def test_the_older_service_names_the_song_on_air_and_the_next() -> None:
    """The older service's song on air is the one whose time spans the moment."""
    current, upcoming = parse_pull(_fixture("rock_pull"), at=1792838425)
    assert current is not None
    assert (current.title, current.subtitle) == ("Rise", "Public Image Limited")
    assert current.image_id == "b225d0d5-191a-461a-ae94-3c9ae6ad07c6"
    assert (current.start, current.end) == (1792838295, 1792838649)
    assert upcoming is not None
    assert upcoming.title == "Phantom"


def test_the_older_service_knows_no_song_past_its_last() -> None:
    """After its last song the older service names nothing on air."""
    assert parse_pull(_fixture("rock_pull"), at=2_000_000_000) == (None, None)


def test_the_older_service_names_the_song_the_listener_hears() -> None:
    """Just after a song started in the studio, the listener still hears the one before it."""
    current, upcoming = parse_pull(_fixture("rock_pull"), at=1792838296)
    assert current is not None
    assert current.title == "Thinking of you"
    assert upcoming is not None
    assert upcoming.title == "Rise"


def test_the_older_service_names_a_next_song_that_overlaps_the_current() -> None:
    """A song starting just before the current one ends is still the next one."""
    # "Thinking of you" starts two seconds before "She's out there" ends
    current, upcoming = parse_pull(_fixture("rock_pull"), at=1792838100 + STREAM_DELAY)
    assert current is not None
    assert current.title == "She's out there"
    assert upcoming is not None
    assert upcoming.title == "Thinking of you"
