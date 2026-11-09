"""Tests for the helper functions of the DR LYD provider."""

from __future__ import annotations

from typing import Any

import pytest
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.dr_lyd.constants import IMAGE_URL
from music_assistant.providers.dr_lyd.helpers import (
    clean_text,
    find_programme,
    find_track,
    get_image_url,
    get_items,
    get_now_playing,
    parse_id,
    parse_time,
    recently_updated,
    require_id,
    search_channels,
    search_series,
)
from music_assistant.providers.dr_lyd.models import NowPlaying, Programme, Series
from music_assistant.providers.dr_lyd.parsers import parse_tracks

from .conftest import (
    CHANNEL_LIST,
    CHANNELS,
    NOT_AN_ID,
    P3_PICTURE,
    PROGRAMME_LIST,
    SERIES,
    TRACKS,
    TRACKS_P2,
    epoch,
    make_series,
)


def titles(series: list[Series]) -> list[str]:
    """Return the titles of the series."""
    return [entry.title for entry in series]


def slugs(channels: Any) -> list[str]:
    """Return the slugs of the channels."""
    return [channel.slug for channel in channels]


def square(image_id: str, target: str = "Default", ratio: str = "1:1") -> dict[str, str]:
    """Return an image asset."""
    return {"id": image_id, "target": target, "ratio": ratio}


def test_clean_text_strips_text() -> None:
    """Text is stripped."""
    assert clean_text("  P3 \n") == "P3"


@pytest.mark.parametrize("value", [None, "", "  ", 5, ["x"], {}])
def test_clean_text_is_none_for_what_is_not_text_with_something_in_it(value: Any) -> None:
    """Whatever is no text, or only blanks, gives none."""
    assert clean_text(value) is None


def test_parse_time_with_an_offset() -> None:
    """A time with a UTC offset is epoch seconds."""
    assert parse_time("2026-11-05T22:03:09+00:00") == 1793916189.0
    assert parse_time("2026-11-05T23:03:09+01:00") == 1793916189.0


@pytest.mark.parametrize("value", ["2026-11-05T22:03:09", "a while ago", "", None, 5])
def test_parse_time_is_none_without_an_offset_or_a_time(value: Any) -> None:
    """A time without a UTC offset, or no time at all, gives none."""
    assert parse_time(value) is None


def test_parse_time_before_1970() -> None:
    """A time before 1970 is negative."""
    value = parse_time("1952-10-23T19:55:00+00:00")
    assert value is not None
    assert value < 0


def test_parse_id_is_made_of_digits() -> None:
    """A production number is text made of digits."""
    assert parse_id("6641303469000") == "6641303469000"
    for value in (None, 5, True, *NOT_AN_ID):
        assert parse_id(value) is None, value


def test_require_id_returns_an_id() -> None:
    """A production number is returned as it is."""
    assert require_id("12", "podcast") == "12"


@pytest.mark.parametrize("item_id", NOT_AN_ID)
def test_require_id_raises_for_what_cannot_be_an_id(item_id: str) -> None:
    """Something else than a production number is not found, which keeps it out of a url."""
    with pytest.raises(MediaNotFoundError, match="is not the id of a DR podcast"):
        require_id(item_id, "podcast")


def test_get_items_of_a_list_document() -> None:
    """The items of a list document are its items, none for any other document."""
    assert get_items({"items": [1, 2]}) == [1, 2]
    documents: list[Any] = [None, [], "text", {}, {"items": None}, {"items": "x"}]
    for document in documents:
        assert get_items(document) == []


def test_a_square_picture() -> None:
    """The picture of an image asset is served by DR as a square of 512 pixels."""
    url = get_image_url([square("urn:dr:radio:image:a")])
    assert url == IMAGE_URL.format("urn:dr:radio:image:a")
    assert url is not None
    assert url.startswith("https://asset.dr.dk/drlyd/images/urn:dr:radio:image:a?")


def test_other_shapes_are_not_used() -> None:
    """Only a picture with the ratio 1:1 is used."""
    assert get_image_url([square("a", ratio="16:9"), square("b", ratio="4:3")]) is None


def test_the_target_named_first_wins() -> None:
    """A picture whose target is named earlier is preferred."""
    assets = [square("a", "Default"), square("b", "SquareImage"), square("c", "Podcast")]
    assert get_image_url(assets, "Podcast", "SquareImage") == IMAGE_URL.format("c")
    assert get_image_url(assets, "SquareImage") == IMAGE_URL.format("b")


def test_a_square_picture_of_another_target_beats_none() -> None:
    """A square picture of a target that is not named is used, the first by id."""
    assets = [square("b", "Default"), square("a", "Other")]
    assert get_image_url(assets, "Podcast") == IMAGE_URL.format("a")


@pytest.mark.parametrize(
    "assets", [None, [], "text", {}, [None, 5, "x", {}, {"ratio": "1:1"}, square(" ")]]
)
def test_no_picture_for_unreadable_assets(assets: Any) -> None:
    """Whatever is no list of image assets gives no picture."""
    assert get_image_url(assets) is None


def test_search_finds_the_series_by_its_title() -> None:
    """The title of a series is searched."""
    assert titles(search_series(SERIES.values(), "den sidste time", 5)) == ["Den sidste time"]


def test_search_ignores_case_and_extra_spaces() -> None:
    """Case and extra spaces make no difference."""
    assert titles(search_series(SERIES.values(), "  DEN   sidste TIME ", 5)) == ["Den sidste time"]


def test_search_needs_every_word() -> None:
    """Every word of the query must match."""
    assert titles(search_series(SERIES.values(), "sidste time", 5)) == ["Den sidste time"]
    assert search_series(SERIES.values(), "sidste zebra", 5) == []


def test_search_reads_the_description_too() -> None:
    """The description of a series is searched as well."""
    assert titles(search_series(SERIES.values(), "nyhedspodcast", 5)) == ["Genstart"]


def test_the_words_may_be_split_between_the_title_and_the_description() -> None:
    """A series matches when its title and description together hold every word."""
    everything = [make_series(title="Genstart", description="Dagens nyheder")]
    assert titles(search_series(everything, "genstart nyheder", 5)) == ["Genstart"]


def test_a_title_that_starts_with_the_query_comes_first_then_other_titles() -> None:
    """Titles starting with the query come first, then other titles, then descriptions."""
    everything = [
        make_series(title="Det store hus", description="tid"),
        make_series(title="Tidens tegn"),
        make_series(title="Kære tid"),
        make_series(title="Om en tid", description="x"),
        make_series(title="Tid til alt"),
        make_series(title="Alt andet", description="Her handler det om tid"),
    ]
    assert titles(search_series(everything, "tid", 10)) == [
        "Tid til alt",
        "Tidens tegn",
        "Kære tid",
        "Om en tid",
        "Alt andet",
        "Det store hus",
    ]


def test_search_limit() -> None:
    """No more series than the limit are returned."""
    everything = [make_series(title=f"Tid {number}") for number in range(10)]
    assert len(search_series(everything, "tid", 3)) == 3


def test_a_blank_query_matches_no_series() -> None:
    """A query of blanks finds nothing."""
    assert search_series(SERIES.values(), "   ", 5) == []


def test_the_series_with_the_newest_episode_come_first() -> None:
    """The recently updated series are ordered by their latest episode."""
    assert titles(recently_updated(SERIES.values(), 20)) == [
        "Radioavisen",
        "Genstart",
        "Musikquizzen",
        "Sara & Monopolet - podcast",
        "Tættere på himlen",
        "Den sidste time",
        "De Lovløse",
        "Barneliv gennem tiden",
    ]


def test_recently_updated_limit() -> None:
    """No more series than the limit are returned."""
    assert len(recently_updated(SERIES.values(), 3)) == 3


def test_recently_updated_leaves_out_series_without_episodes_or_a_time() -> None:
    """A series without episodes or without a time of its latest episode is left out."""
    everything = [
        make_series(title="Old", latest=100.0),
        make_series(title="Empty", episodes=0, latest=900.0),
        make_series(title="Undated", latest=None),
        make_series(title="New", latest=500.0),
    ]
    assert titles(recently_updated(everything, 10)) == ["New", "Old"]


def test_channels_match_the_name_ignoring_case() -> None:
    """The name of a channel is searched, ignoring case."""
    assert slugs(search_channels(CHANNEL_LIST, "dr P3", 10)) == ["p3"]
    assert slugs(search_channels(CHANNEL_LIST, "jazz", 10)) == ["p8jazz"]


def test_every_word_must_match_the_name_of_a_channel() -> None:
    """Every word of the query must be in the name of the channel."""
    assert slugs(search_channels(CHANNEL_LIST, "københavn", 10)) == ["p4kbh", "p5kbh"]
    assert slugs(search_channels(CHANNEL_LIST, "p4 københavn", 10)) == ["p4kbh"]
    assert search_channels(CHANNEL_LIST, "p3 jazz", 10) == []


def test_districts_are_found_too() -> None:
    """The district channels are searched like the others."""
    assert slugs(search_channels(CHANNEL_LIST, "p4", 10)) == ["p4kbh", "p4vest", "p4aarhus"]


def test_channel_search_limit_and_blank_query() -> None:
    """No more channels than the limit are returned, and a blank query finds none."""
    assert len(search_channels(CHANNEL_LIST, "dr", 3)) == 3
    assert search_channels(CHANNEL_LIST, "  ", 10) == []


def title_at(time_of_day: str) -> str | None:
    """Return the title of the track of the fixture that plays at the time."""
    track = find_track(TRACKS, epoch(time_of_day))
    return track.title if track else None


def test_the_track_playing() -> None:
    """The track playing is the one whose time it is."""
    assert title_at("22:30:00") == "Min egen værste ven"
    assert title_at("22:26:00") == "DtMF"


def test_a_track_plays_from_its_start_until_its_end() -> None:
    """A track plays from the second it starts until the second it ends."""
    assert title_at("22:28:35") == "DtMF"
    assert title_at("22:28:36") == "Min egen værste ven"
    assert title_at("22:31:29") == "Min egen værste ven"
    assert title_at("22:31:30") is None


def test_the_latest_track_wins_when_it_starts_before_the_last_one_is_over() -> None:
    """When two tracks overlap, the one that started last is playing."""
    assert title_at("22:29:00") == "Min egen værste ven"


def test_no_track_between_tracks_or_without_tracks() -> None:
    """Between tracks, before the oldest track and without tracks nothing is playing."""
    assert title_at("22:24:00") is None
    assert title_at("21:00:00") is None
    assert find_track([], epoch("22:30:00")) is None


def find(
    slug: str, time_of_day: str = "22:30:00", programmes: list[Programme] | None = None
) -> Any:
    """Return the programme that is on air on the channel at the time."""
    return find_programme(programmes or PROGRAMME_LIST, slug, epoch(time_of_day))


def test_the_programme_on_air() -> None:
    """The programme on air is the one of the channel."""
    programme = find("p3")
    assert programme.title == "Den sidste time"
    assert programme.end == epoch("23:03:00")
    assert find("p2").title == "Natsværmeren: Poesi"


def test_the_channel_is_picked_by_its_whole_slug() -> None:
    """A slug that starts like the one of the channel is another channel."""
    programmes = [
        Programme("p4bornholm", "Bornholm", None, epoch("23:00:00")),
        Programme("p4kbh", "Før natten på P4", None, epoch("23:00:00")),
    ]
    assert find("p4kbh", programmes=programmes).title == "Før natten på P4"


def test_no_programme_once_it_is_over() -> None:
    """A programme is on air until its end."""
    assert find("p3", "23:02:59") is not None
    assert find("p3", "23:03:00") is None


def test_no_programme_for_a_channel_that_is_not_listed() -> None:
    """A channel that is not in the schedules has no programme."""
    assert find("p8jazz") is None


def test_now_playing_is_the_track_with_the_picture_of_the_programme() -> None:
    """A track playing is shown with its artist and the picture of the programme."""
    playing = get_now_playing(CHANNELS["p3"], TRACKS, PROGRAMME_LIST, epoch("22:30:00"))
    assert playing == NowPlaying("Min egen værste ven", "Friða Saiya", P3_PICTURE)


def test_now_playing_is_classical_music_with_its_composer() -> None:
    """A classical track is shown with its composer."""
    playing = get_now_playing(
        CHANNELS["p2"], parse_tracks(TRACKS_P2), PROGRAMME_LIST, epoch("22:20:00")
    )
    assert playing.title == "Suor Angelica - Intermezzo"
    assert playing.artist == "Giacomo Puccini"


def test_now_playing_is_the_programme_between_tracks() -> None:
    """Between tracks the programme is shown."""
    playing = get_now_playing(CHANNELS["p3"], TRACKS, PROGRAMME_LIST, epoch("22:24:00"))
    assert playing == NowPlaying("Den sidste time", None, P3_PICTURE)


def test_now_playing_is_the_track_without_a_picture_without_programmes() -> None:
    """Without programmes a track is shown without a picture."""
    playing = get_now_playing(CHANNELS["p3"], TRACKS, [], epoch("22:30:00"))
    assert playing == NowPlaying("Min egen værste ven", "Friða Saiya", None)


def test_now_playing_is_the_channel_when_nothing_is_known() -> None:
    """Without tracks and programmes the name of the channel is shown."""
    assert get_now_playing(CHANNELS["p3"], [], [], epoch("22:30:00")) == NowPlaying("DR P3")
    assert get_now_playing(CHANNELS["p8jazz"], [], PROGRAMME_LIST, epoch("22:30:00")) == NowPlaying(
        "DR P8 Jazz"
    )


def test_now_playing_is_the_channel_once_the_programme_is_over() -> None:
    """A programme that is over is not shown."""
    playing = get_now_playing(CHANNELS["p3"], [], PROGRAMME_LIST, epoch("23:03:00"))
    assert playing == NowPlaying("DR P3")
