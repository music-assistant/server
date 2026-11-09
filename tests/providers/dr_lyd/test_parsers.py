"""Tests for the parsers of the DR LYD provider."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from music_assistant_models.enums import ContentType, ImageType, LinkType, MediaType
from music_assistant_models.media_items import ItemMapping

from music_assistant.constants import DEFAULT_AUDIOBOOK_PODCAST_GENRE
from music_assistant.helpers.json import json_dumps
from music_assistant.providers.dr_lyd.constants import IMAGE_URL
from music_assistant.providers.dr_lyd.models import Track
from music_assistant.providers.dr_lyd.parsers import (
    parse_channels,
    parse_episode,
    parse_podcast,
    parse_podcast_episode,
    parse_podcast_reference,
    parse_programmes,
    parse_radio,
    parse_series,
    parse_tracks,
)

from .conftest import (
    ASSET,
    BARNELIV,
    CHANNEL_LIST,
    CHANNELS,
    CHANNELS_DOCUMENT,
    DE_LOVLOESE,
    DEN_SIDSTE_TIME,
    DOMAIN,
    GENSTART,
    ICY_STREAM,
    INSTANCE_ID,
    MUSIKQUIZZEN,
    NOT_AN_ID,
    P3_LOGO,
    P3_PICTURE,
    PROGRAMMES,
    RADIOAVISEN,
    SARA_OG_MONOPOLET,
    SERIES,
    STREAM,
    TAETTERE_PAA_HIMLEN,
    TRACKS_P2,
    TRACKS_P3,
    UMBRELLA_EPISODES,
    asset,
    channel_item,
    channels_document_with,
    episode_item,
    episode_items,
    epoch,
    make_episode,
    make_series,
    series_item,
    track_item,
    without_picture,
)


def test_a_series() -> None:
    """A series carries what DR tells about it."""
    series = SERIES[DEN_SIDSTE_TIME]
    assert series.id == DEN_SIDSTE_TIME
    assert series.title == "Den sidste time"
    assert series.description is not None
    assert series.description.startswith("Chano Jørgensen og Kathrine")
    assert series.episodes == 2
    assert series.latest == epoch("22:00:00", "2026-10-22")
    assert series.genres == ("Underholdning",)
    assert not series.explicit
    assert series.page_url == "https://www.dr.dk/lyd/p3/den-sidste-time-6641303469000"


def test_every_series_of_the_fixture() -> None:
    """All the series of the recorded document are read."""
    assert sorted(SERIES) == sorted(
        [
            DEN_SIDSTE_TIME,
            GENSTART,
            SARA_OG_MONOPOLET,
            MUSIKQUIZZEN,
            TAETTERE_PAA_HIMLEN,
            DE_LOVLOESE,
            RADIOAVISEN,
            BARNELIV,
        ]
    )


def test_series_picture_is_the_podcast_one_else_the_square_one() -> None:
    """The picture of a series is its podcast picture, else the square one, else none."""
    assert SERIES[DEN_SIDSTE_TIME].image_url == IMAGE_URL.format(
        "urn:dr:radio:image:6ad9f2f072191fe5360b406b"
    )
    assert SERIES[BARNELIV].image_url == IMAGE_URL.format(
        "urn:dr:radio:image:68b840807964fa5412a0ecf6"
    )
    series = parse_series(dict(series_item(GENSTART), imageAssets=[]))
    assert series is not None
    assert series.image_url is None


def test_series_genres() -> None:
    """The genres are the names of the categories, each one once."""
    assert SERIES[MUSIKQUIZZEN].genres == ("Musik", "Underholdning")
    item = dict(series_item(MUSIKQUIZZEN), categories=["Musik", " ", "Musik", None, 5, "Quiz"])
    series = parse_series(item)
    assert series is not None
    assert series.genres == ("Musik", "Quiz")


@pytest.mark.parametrize("categories", [None, "Musik", []])
def test_series_without_usable_categories_have_no_genres(categories: Any) -> None:
    """Categories that are not a list of names give no genres."""
    series = parse_series(dict(series_item(MUSIKQUIZZEN), categories=categories))
    assert series is not None
    assert series.genres == ()


def test_the_number_of_episodes() -> None:
    """The number of episodes is what DR counts, zero when that is no count."""
    assert SERIES[GENSTART].episodes == 1537
    for value in (None, "12", -3, 2.5):
        series = parse_series(dict(series_item(GENSTART), numberOfEpisodes=value))
        assert series is not None
        assert series.episodes == 0, value


def test_the_time_of_the_latest_episode() -> None:
    """The latest episode has a time when DR gives one with a UTC offset."""
    assert SERIES[GENSTART].latest == epoch("02:00:00")
    latest = SERIES[BARNELIV].latest
    assert latest is not None
    assert latest < 0
    for value in (None, "", "yesterday", "2026-11-05T02:00:00"):
        series = parse_series(dict(series_item(GENSTART), latestEpisodeStartTime=value))
        assert series is not None
        assert series.latest is None, value


def test_explicit_content() -> None:
    """A series is explicit only when DR says so with a true."""
    series = parse_series(dict(series_item(GENSTART), explicitContent=True))
    assert series is not None
    assert series.explicit
    series = parse_series(dict(series_item(GENSTART), explicitContent="yes"))
    assert series is not None
    assert not series.explicit


def test_leaves_out_a_series_that_is_not_available() -> None:
    """A series that cannot be listened to on demand is left out."""
    item = dict(series_item(GENSTART), isAvailableOnDemand=False)
    assert parse_series(item) is None
    del item["isAvailableOnDemand"]
    assert parse_series(item) is not None


@pytest.mark.parametrize(
    "change",
    [
        *({"productionNumber": value} for value in (None, 2642056922000, True, *NOT_AN_ID)),
        {"title": None},
        {"title": "  "},
    ],
)
def test_leaves_out_a_series_without_an_id_or_a_title(change: dict[str, Any]) -> None:
    """A series needs a production number made of digits and a title."""
    assert parse_series(dict(series_item(GENSTART), **change)) is None


@pytest.mark.parametrize("item", [None, 5, "x", [], {}])
def test_unreadable_series_items(item: Any) -> None:
    """Whatever is no series is left out."""
    assert parse_series(item) is None


def test_an_episode() -> None:
    """An episode carries what DR tells about it."""
    episode = parse_episode(episode_item("13332646601"))
    assert episode is not None
    assert episode.id == "13332646601"
    assert episode.title == episode_item("13332646601")["title"]
    assert episode.description is not None
    assert episode.description.startswith("Chano Jørgensen og Kathrine")
    assert episode.duration == 2104
    assert episode.published == epoch("22:00:00", "2026-10-22")
    assert episode.image_url == IMAGE_URL.format("urn:dr:radio:image:6ad9f2f072191fe5360b406b")
    assert not episode.explicit
    assert episode.page_url is not None
    assert episode.page_url.endswith("/fredag-23-oktober-2026-13332646601")
    assert episode.series_id == DEN_SIDSTE_TIME
    assert episode.series_title == "Den sidste time"


def test_the_mp3_with_the_highest_bitrate_is_played() -> None:
    """The progressive mp3 with the highest bitrate is the audio of an episode."""
    episode = parse_episode(episode_item("13332646601"))
    assert episode is not None
    assert episode.audio_url == ASSET.format(
        "6ad74f730a53387c03882d19",
        "347bb34d0e730032a186bf6590b2fda614a7a95e664d67b1f6a3573a91059063",
    )
    assert not episode.hls


def test_the_mp3_is_found_wherever_the_asset_is_listed() -> None:
    """The order of the audio assets makes no difference."""
    item = copy.deepcopy(episode_item("16122293321"))
    expected = ASSET.format(
        "633ff9514c57f9f719d27463",
        "674ea56ead83b9491cd27e4cc0acc945f30f042284cd96999c28ef75b2ca9ea8",
    )
    for assets in (item["audioAssets"], item["audioAssets"][::-1]):
        episode = parse_episode(dict(item, audioAssets=assets))
        assert episode is not None
        assert (episode.audio_url, episode.hls) == (expected, False)


def test_the_hls_stream_when_there_is_no_mp3() -> None:
    """An episode with no mp3 is played from its HLS stream."""
    episode = parse_episode(episode_item("13332646424"))
    assert episode is not None
    assert episode.hls
    assert episode.audio_url == ASSET.format(
        "6ac618f3842017fe110f01f4",
        "20cc9a1c4b7f4100884bae9aa2ec925d3b6ca09dec292d9a85f32681dafc5afc",
    )
    assert episode.duration == 7200


def test_the_mp3_is_preferred_to_the_hls_stream() -> None:
    """An mp3 wins over an HLS stream, whatever their bitrates."""
    item = dict(
        episode_item("13332646424"),
        audioAssets=[
            asset("Stream", "HLS", 9999, "https://example.com/stream.m3u8"),
            asset("Progressive", "mp3", 0, "https://example.com/a.mp3"),
            asset("Progressive", "mp3", None, "https://example.com/b.mp3"),
        ],
    )
    episode = parse_episode(item)
    assert episode is not None
    assert (episode.audio_url, episode.hls) == ("https://example.com/a.mp3", False)


@pytest.mark.parametrize(
    "assets",
    [
        None,
        [],
        "text",
        [None, 5, {}],
        [asset("Progressive", "mp4", 192, "https://example.com/a.mp4")],
        [asset("Progressive", "mp3", 192, " ")],
        [asset("Progressive", "mp3", 192, "ftp://example.com/a.mp3")],
        [asset("Progressive", "mp3", 192, None)],
    ],
)
def test_leaves_out_an_episode_without_audio_to_play(assets: Any) -> None:
    """An episode needs an mp3 or an HLS stream on http or https."""
    assert parse_episode(dict(episode_item("13332646601"), audioAssets=assets)) is None


def test_the_time_of_an_episode_is_when_it_started_else_when_it_was_published() -> None:
    """The time of an episode is its start time, else its publish time, else none."""
    item = dict(episode_item("13332646601"), startTime=None)
    episode = parse_episode(item)
    assert episode is not None
    assert episode.published == epoch("22:00:00", "2026-10-22")
    item = dict(item, publishTime="2026-10-24T10:00:00+00:00")
    episode = parse_episode(item)
    assert episode is not None
    assert episode.published == epoch("10:00:00", "2026-10-24")
    episode = parse_episode(dict(item, publishTime="soon"))
    assert episode is not None
    assert episode.published is None


@pytest.mark.parametrize(
    ("value", "seconds"), [(1500, 1), (60000, 60), (None, 0), (0, 0), (-5, 0), ("60000", 0)]
)
def test_the_duration_in_seconds(value: Any, seconds: int) -> None:
    """The duration is whole seconds, zero when DR gives no usable one."""
    episode = parse_episode(dict(episode_item("13332646601"), durationMilliseconds=value))
    assert episode is not None
    assert episode.duration == seconds


def test_an_episode_of_an_umbrella_names_the_series_it_belongs_to() -> None:
    """An episode of a series made of other series names the series it belongs to."""
    episode = parse_episode(episode_item("16122593411"))
    assert episode is not None
    assert episode.title == "1:3 - Odderhusets hemmelighed: Familien i skoven"
    assert episode.series_id == "6640504587581"
    assert episode.series_title == "Odderhusets hemmelighed"


@pytest.mark.parametrize("series", [None, "x", {}, {"productionNumber": "x"}])
def test_an_episode_without_a_series_has_no_series(series: Any) -> None:
    """An episode that names no usable series has none."""
    episode = parse_episode(dict(episode_item("13332646601"), series=series))
    assert episode is not None
    assert (episode.series_id, episode.series_title) == (None, None)


def test_leaves_out_an_episode_that_is_not_available() -> None:
    """An episode that cannot be listened to on demand is left out."""
    assert parse_episode(dict(episode_item("13332646601"), isAvailableOnDemand=False)) is None


@pytest.mark.parametrize(
    "change",
    [
        *({"productionNumber": value} for value in (None, 13332646601, *NOT_AN_ID)),
        {"title": None},
        {"title": " "},
    ],
)
def test_leaves_out_an_episode_without_an_id_or_a_title(change: dict[str, Any]) -> None:
    """An episode needs a production number made of digits and a title."""
    assert parse_episode(dict(episode_item("13332646601"), **change)) is None


@pytest.mark.parametrize("item", [None, 5, "x", [], {}])
def test_unreadable_episode_items(item: Any) -> None:
    """Whatever is no episode is left out."""
    assert parse_episode(item) is None


def test_the_flow_channels_in_the_order_of_dr() -> None:
    """Only the stations that play round the clock are channels."""
    assert [channel.slug for channel in CHANNEL_LIST] == [
        "p1",
        "p2",
        "p3",
        "p4kbh",
        "p4vest",
        "p4aarhus",
        "p5kbh",
        "p6beat",
        "p8jazz",
    ]
    for slug in ("ly1", "p7mix", "special-radio", "mcrweb1", "dr-web-3", "p3webcam"):
        assert slug not in CHANNELS


def test_the_channel_names() -> None:
    """A channel is named DR and its title."""
    assert [channel.name for channel in CHANNEL_LIST] == [
        "DR P1",
        "DR P2",
        "DR P3",
        "DR P4 København",
        "DR P4 Midt & Vest",
        "DR P4 Østjylland",
        "DR P5 København",
        "DR P6 Beat",
        "DR P8 Jazz",
    ]


@pytest.mark.parametrize(
    ("slug", "title", "name"),
    [
        ("p6beat", "P6", "DR P6 Beat"),
        ("p8jazz", "P8", "DR P8 Jazz"),
        ("p4kbh", "P4 København", "DR P4 København"),
        ("p3", "P3", "DR P3"),
        ("p4vest", "P4 Midt & Vest", "DR P4 Midt & Vest"),
    ],
)
def test_names_follow_the_slug_where_dr_titles_a_channel_short(
    slug: str, title: str, name: str
) -> None:
    """DR titles P6 and P8 without what their slugs say."""
    [channel] = parse_channels([channel_item(slug, title=title)])
    assert channel.name == name


def test_the_districts_stand_in_for_p4_and_p5() -> None:
    """The districts of P4 and P5 are listed in place of P4 and P5."""
    assert "p4" not in CHANNELS
    assert "p5" not in CHANNELS
    assert [channel.slug for channel in CHANNEL_LIST if channel.district] == [
        "p4kbh",
        "p4vest",
        "p4aarhus",
        "p5kbh",
    ]


def test_a_channel_with_districts_is_listed_with_them_else_alone() -> None:
    """A channel that has districts is replaced by them, one that has none stays."""
    document = [channel_item("p4", districts=[channel_item("p4a"), channel_item("p4b")])]
    assert [channel.slug for channel in parse_channels(document)] == ["p4a", "p4b"]
    document[0]["districts"] = []
    assert [channel.slug for channel in parse_channels(document)] == ["p4"]


def test_the_hls_stream_is_preferred() -> None:
    """A channel is played from its HLS stream."""
    assert CHANNELS["p3"].stream_url == STREAM.format("p3")
    assert CHANNELS["p3"].hls
    assert CHANNELS["p4kbh"].stream_url == STREAM.format("p4kobenhavn")
    assert CHANNELS["p6beat"].stream_url == STREAM.format("p6")
    assert CHANNELS["p8jazz"].stream_url == STREAM.format("p8")


def test_the_best_icy_stream_when_there_is_no_hls_one() -> None:
    """Without an HLS stream a channel is played from its ICY stream of the highest bitrate."""
    p3 = next(item for item in CHANNELS_DOCUMENT if item["slug"] == "p3")
    assets = [item for item in p3["audioAssets"] if item["format"] != "HLS"]
    [channel] = parse_channels([channel_item("p3", audioAssets=assets)])
    assert channel.stream_url == ICY_STREAM.format("A05H")
    assert not channel.hls


@pytest.mark.parametrize(
    "assets",
    [
        None,
        [],
        "text",
        [None, 5, {}],
        [{"format": "HLS"}, {"format": "HLS", "url": " "}],
        [{"format": "HLS", "url": "rtmp://example.com/live"}],
        [{"format": "DASH", "url": STREAM.format("p9")}],
    ],
)
def test_leaves_out_a_channel_without_a_usable_stream(assets: Any) -> None:
    """A channel needs a stream on http or https."""
    assert parse_channels([channel_item(audioAssets=assets)]) == []


@pytest.mark.parametrize(
    "change", [{"slug": None}, {"title": " "}, {"id": None}, {"isFlowChannel": None}]
)
def test_leaves_out_a_channel_without_a_slug_a_title_or_an_id(change: dict[str, Any]) -> None:
    """A channel needs a slug, a title and an id, and must be a flow channel."""
    assert parse_channels([channel_item(**change)]) == []


def test_the_first_channel_of_a_slug_wins() -> None:
    """A slug is listed once."""
    document = [channel_item("p9", title="First"), channel_item("p9", title="Second")]
    assert [channel.name for channel in parse_channels(document)] == ["DR First"]


def test_each_channel_has_an_urn_of_its_own() -> None:
    """The urn of a channel names its list of tracks, so every district has one."""
    assert len({channel.urn for channel in CHANNEL_LIST}) == len(CHANNEL_LIST)
    assert CHANNELS["p4kbh"].urn == "urn:dr:radio:channel:5fa156d3da351264f87b4651"


def test_the_channel_picture_is_the_logo_else_the_square_picture() -> None:
    """The picture of a channel is its logo, else its square picture, else none."""
    assert CHANNELS["p3"].image_url == P3_LOGO
    [channel] = [
        item
        for item in parse_channels(channels_document_with("p3", channelLogos=[]))
        if item.slug == "p3"
    ]
    assert channel.image_url == IMAGE_URL.format("urn:dr:radio:image:6499a86d2222ea4246b8340d")
    [channel] = [
        item
        for item in parse_channels(channels_document_with("p3", channelLogos=[], imageAssets=[]))
        if item.slug == "p3"
    ]
    assert channel.image_url is None


@pytest.mark.parametrize(
    "document", [None, {}, "text", 5, [], [None, 5, "x", []], [{"districts": "x"}]]
)
def test_unreadable_channel_documents_have_no_channels(document: Any) -> None:
    """Whatever is no channels document has no channels."""
    assert parse_channels(document) == []


def test_a_track() -> None:
    """A track has its title, its artist and the time it plays."""
    assert parse_tracks(TRACKS_P3)[0] == Track(
        "Min egen værste ven", "Friða Saiya", epoch("22:28:36"), epoch("22:31:30")
    )
    assert len(parse_tracks(TRACKS_P3)) == 12


def test_the_artist_line_names_the_guests() -> None:
    """The artist of a track is the line of DR, with the guests."""
    tracks = {track.title: track for track in parse_tracks(TRACKS_P3)}
    assert tracks["Raindance"].artist == "Dave feat. Tems"


def test_classical_music_names_the_composers() -> None:
    """The artist of a classical track is its composers."""
    tracks = {track.title: track for track in parse_tracks(TRACKS_P2)}
    assert tracks["Ave Maris Stella"].artist == "Claudio Monteverdi"
    assert tracks["Suor Angelica - Intermezzo"].artist == "Giacomo Puccini"


def test_each_composer_is_named_once() -> None:
    """A composer is named once, whatever the case of the role."""
    roles = [
        {"role": "Komponist", "name": "Carl Nielsen"},
        {"role": "Dirigent", "name": "Bo Holten"},
        {"role": "komponist", "name": "Wayne Siegel"},
        {"role": "Komponist", "name": "Carl Nielsen"},
    ]
    [track] = parse_tracks({"items": [track_item(classical=True, roles=roles)]})
    assert track.artist == "Carl Nielsen, Wayne Siegel"


def test_classical_music_without_a_composer_keeps_the_credits() -> None:
    """A classical track without a composer keeps the credits of DR."""
    track = next(
        track for track in parse_tracks(TRACKS_P2) if track.title.startswith("La peau douce")
    )
    assert track.artist is not None
    assert track.artist.startswith("KOMPONIST: Georges Delerue.")


def test_other_music_on_a_classical_channel_keeps_the_artist_line() -> None:
    """A track that is not classical keeps the artist line of DR."""
    tracks = {track.title: track for track in parse_tracks(TRACKS_P2)}
    assert tracks["Kære fisk"].artist == "Ursula Andkjær Olsen"


@pytest.mark.parametrize("description", [None, "", "  "])
def test_no_artist_line_gives_no_artist(description: str | None) -> None:
    """A track without an artist line has no artist."""
    [track] = parse_tracks({"items": [track_item(description=description)]})
    assert track.artist is None


def test_leaves_out_what_is_not_a_complete_track() -> None:
    """A track needs a title, a start with a UTC offset and a duration."""
    items = [
        track_item(title="Good"),
        track_item(title="A chapter", type="Chapter"),
        track_item(title=None),
        track_item(title="  "),
        track_item(title="No start", playedTime=None),
        track_item(title="Start without offset", playedTime="2026-11-05T22:03:09"),
        track_item(title="Start in words", playedTime="a while ago"),
        track_item(title="No duration", durationMilliseconds=None),
        track_item(title="Empty duration", durationMilliseconds=0),
        track_item(title="Duration in words", durationMilliseconds="205000"),
    ]
    assert [track.title for track in parse_tracks({"items": items})] == ["Good"]


@pytest.mark.parametrize(
    "document", [None, [], "text", 5, {}, {"items": None}, {"items": "x"}, {"items": [None, 5]}]
)
def test_unreadable_track_documents_have_no_tracks(document: Any) -> None:
    """Whatever is no live index points document has no tracks."""
    assert parse_tracks(document) == []


def test_the_programmes_on_air() -> None:
    """The programme document lists the programme on air on a channel."""
    programmes = {programme.channel_slug: programme for programme in parse_programmes(PROGRAMMES)}
    assert programmes["p3"].title == "Den sidste time"
    assert programmes["p3"].end == epoch("23:03:00")
    assert programmes["p2"].title == "Natsværmeren: Poesi"


def test_the_picture_of_a_programme_is_the_square_one() -> None:
    """The picture of a programme is the square one, preferring the target SquareImage."""
    programmes = {programme.channel_slug: programme for programme in parse_programmes(PROGRAMMES)}
    assert programmes["p3"].image_url == P3_PICTURE
    document = copy.deepcopy(PROGRAMMES)
    entry = next(entry for entry in document if entry["channel"]["slug"] == "p3")
    entry["imageAssets"] = [
        {"id": "urn:dr:radio:image:a", "target": "Podcast", "ratio": "1:1"},
        {"id": "urn:dr:radio:image:b", "target": "SquareImage", "ratio": "1:1"},
        {"id": "urn:dr:radio:image:c", "target": "Default", "ratio": "16:9"},
    ]
    [programme] = [item for item in parse_programmes(document) if item.channel_slug == "p3"]
    assert programme.image_url == IMAGE_URL.format("urn:dr:radio:image:b")
    entry["imageAssets"] = [item for item in entry["imageAssets"] if item["ratio"] != "1:1"]
    [programme] = [item for item in parse_programmes(document) if item.channel_slug == "p3"]
    assert programme.image_url is None


@pytest.mark.parametrize(
    "change", [{"title": None}, {"title": " "}, {"endTime": None}, {"endTime": "late"}]
)
def test_leaves_out_a_programme_without_a_title_or_an_end(change: dict[str, Any]) -> None:
    """A programme needs a title and an end."""
    document = copy.deepcopy(PROGRAMMES)
    next(entry for entry in document if entry["channel"]["slug"] == "p3").update(change)
    assert "p3" not in {programme.channel_slug for programme in parse_programmes(document)}


@pytest.mark.parametrize(
    "document", [None, {}, "text", [None, 5, "x", {"channel": None}, {"channel": []}]]
)
def test_unreadable_programme_documents_have_no_programmes(document: Any) -> None:
    """Whatever is no schedules document has no programmes."""
    assert parse_programmes(document) == []


def test_a_radio_is_made_of_a_channel() -> None:
    """The radio of a channel has the slug as its id and the logo as its picture."""
    radio = parse_radio(CHANNELS["p3"], INSTANCE_ID, DOMAIN)
    assert radio.item_id == "p3"
    assert radio.name == "DR P3"
    assert radio.provider == INSTANCE_ID
    [mapping] = radio.provider_mappings
    assert (mapping.item_id, mapping.provider_domain, mapping.provider_instance) == (
        "p3",
        DOMAIN,
        INSTANCE_ID,
    )
    assert mapping.url is None
    [image] = radio.metadata.images or []
    assert (image.type, image.path, image.provider) == (ImageType.THUMB, P3_LOGO, INSTANCE_ID)
    assert image.remotely_accessible
    assert not parse_radio(without_picture(CHANNELS["p3"]), INSTANCE_ID, DOMAIN).metadata.images


def test_a_podcast_is_made_of_a_series() -> None:
    """The podcast of a series is published by DR and links to the page of the series."""
    podcast = parse_podcast(SERIES[MUSIKQUIZZEN], INSTANCE_ID, DOMAIN)
    assert podcast.item_id == MUSIKQUIZZEN
    assert podcast.name == "Musikquizzen"
    assert podcast.publisher == "DR"
    assert podcast.total_episodes == 24
    assert podcast.metadata.genres == {"Musik", "Underholdning"}
    assert podcast.metadata.description == SERIES[MUSIKQUIZZEN].description
    [link] = podcast.metadata.links or []
    assert (link.type, link.url) == (LinkType.WEBSITE, SERIES[MUSIKQUIZZEN].page_url)
    [mapping] = podcast.provider_mappings
    assert (mapping.item_id, mapping.provider_instance) == (MUSIKQUIZZEN, INSTANCE_ID)
    assert mapping.url is None


def test_a_podcast_without_genres_has_the_default_one() -> None:
    """A podcast that DR gives no category has the default genre."""
    podcast = parse_podcast(make_series(), INSTANCE_ID, DOMAIN)
    assert podcast.metadata.genres == {DEFAULT_AUDIOBOOK_PODCAST_GENRE}
    assert not podcast.metadata.links
    assert not podcast.metadata.images


def test_an_episode_is_made_of_an_episode_of_dr() -> None:
    """The episode names its podcast, its position and the format of its audio."""
    episode = parse_episode(episode_item("13332646601"))
    assert episode is not None
    podcast = parse_podcast_reference(DEN_SIDSTE_TIME, "Den sidste time", INSTANCE_ID)
    assert isinstance(podcast, ItemMapping)
    assert podcast.media_type == MediaType.PODCAST
    mass_episode = parse_podcast_episode(episode, podcast, "cover", 2, INSTANCE_ID, DOMAIN)
    assert mass_episode.item_id == "13332646601"
    assert mass_episode.name == episode.title
    assert mass_episode.duration == 2104
    assert mass_episode.position == 2
    assert mass_episode.podcast == podcast
    assert mass_episode.metadata.release_date is not None
    assert mass_episode.metadata.release_date.timestamp() == epoch("22:00:00", "2026-10-22")
    [mapping] = mass_episode.provider_mappings
    assert mapping.audio_format.content_type == ContentType.MP3
    [image] = mass_episode.metadata.images or []
    assert image.path == episode.image_url


def test_the_audio_address_stays_out_of_the_episode() -> None:
    """The address of the audio is not part of the episode, only of its stream details."""
    episode = parse_episode(episode_item("13332646601"))
    assert episode is not None
    podcast = parse_podcast_reference(DEN_SIDSTE_TIME, "Den sidste time", INSTANCE_ID)
    mass_episode = parse_podcast_episode(episode, podcast, None, 1, INSTANCE_ID, DOMAIN)
    assert mass_episode.provider_mappings
    for mapping in mass_episode.provider_mappings:
        assert mapping.url is None
    assert episode.audio_url not in json_dumps(mass_episode.to_dict())


def test_an_hls_episode_has_the_format_of_hls() -> None:
    """The format of an episode that is played from HLS is AAC."""
    podcast = parse_podcast_reference("1", "Title", INSTANCE_ID)
    mass_episode = parse_podcast_episode(
        make_episode(hls=True), podcast, None, 1, INSTANCE_ID, DOMAIN
    )
    [mapping] = mass_episode.provider_mappings
    assert mapping.audio_format.content_type == ContentType.AAC


def test_an_episode_without_a_picture_has_the_cover_of_the_podcast() -> None:
    """The cover of the podcast is the picture of an episode that has none."""
    podcast = parse_podcast_reference("1", "Title", INSTANCE_ID)
    with_cover = parse_podcast_episode(make_episode(), podcast, "cover", 1, INSTANCE_ID, DOMAIN)
    without_cover = parse_podcast_episode(make_episode(), podcast, None, 1, INSTANCE_ID, DOMAIN)
    assert [image.path for image in with_cover.metadata.images or []] == ["cover"]
    assert not without_cover.metadata.images


def test_the_umbrella_episodes_of_the_fixture_are_all_read() -> None:
    """The episodes of an umbrella series are read from every group."""
    episodes = [parse_episode(item) for item in episode_items(DE_LOVLOESE)]
    assert [episode.id for episode in episodes if episode] == UMBRELLA_EPISODES
