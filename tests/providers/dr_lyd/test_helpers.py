"""Tests for the helper functions of the DR LYD provider."""

from __future__ import annotations

import pytest
from music_assistant_models.streamdetails import StreamMetadata

from music_assistant.providers.dr_lyd.constants import IMAGE_URL
from music_assistant.providers.dr_lyd.helpers import (
    find_programme,
    find_track,
    get_audio,
    get_image_url,
    get_stream,
    get_stream_metadata,
    search_channels,
)
from music_assistant.providers.dr_lyd.models import (
    Channel,
    DRAudioAsset,
    DRChannel,
    DRChannelRef,
    DRImageAsset,
    DRProgramme,
    DRTrack,
)

from .conftest import (
    ASSET,
    CHANNEL_LIST,
    CHANNELS,
    CHANNELS_DOCUMENT,
    CLASSICAL_TRACKS,
    ICY_STREAM,
    P3_PICTURE,
    PROGRAMME_LIST,
    STREAM,
    TRACKS,
    at,
    episode,
    track_item,
)


def square(image_id: str, target: str = "Default", ratio: str = "1:1") -> DRImageAsset:
    """Return an image asset."""
    return DRImageAsset(id=image_id, target=target, ratio=ratio)


def audio(target: str, format_: str, bitrate: int, url: str) -> DRAudioAsset:
    """Return an audio asset."""
    return DRAudioAsset(target=target, format=format_, bitrate=bitrate, url=url)


def channel_audio(slug: str) -> list[DRAudioAsset]:
    """Return the audio assets of a channel of the channels document."""
    item = next(item for item in CHANNELS_DOCUMENT if item["slug"] == slug)
    return DRChannel.from_dict(item).audio_assets


def slugs(channels: list[Channel]) -> list[str]:
    """Return the slugs of the channels."""
    return [channel.slug for channel in channels]


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


def test_no_picture_without_assets() -> None:
    """Without image assets there is no picture."""
    assert get_image_url([]) is None


def test_the_mp3_with_the_highest_bitrate_is_played() -> None:
    """The progressive mp3 with the highest bitrate is the audio of an episode."""
    chosen = get_audio(episode("13332646601").audio_assets)
    assert chosen is not None
    assert chosen.format == "mp3"
    assert chosen.url == ASSET.format(
        "6ad74f730a53387c03882d19",
        "347bb34d0e730032a186bf6590b2fda614a7a95e664d67b1f6a3573a91059063",
    )


def test_the_mp3_is_found_wherever_the_asset_is_listed() -> None:
    """The order of the audio assets makes no difference."""
    assets = episode("16122293321").audio_assets
    expected = ASSET.format(
        "633ff9514c57f9f719d27463",
        "674ea56ead83b9491cd27e4cc0acc945f30f042284cd96999c28ef75b2ca9ea8",
    )
    for ordered in (assets, assets[::-1]):
        chosen = get_audio(ordered)
        assert chosen is not None
        assert chosen.url == expected


def test_the_hls_stream_when_there_is_no_mp3() -> None:
    """An episode with no mp3 is played from its HLS stream."""
    chosen = get_audio(episode("13332646424").audio_assets)
    assert chosen is not None
    assert chosen.format == "HLS"
    assert chosen.url == ASSET.format(
        "6ac618f3842017fe110f01f4",
        "20cc9a1c4b7f4100884bae9aa2ec925d3b6ca09dec292d9a85f32681dafc5afc",
    )


def test_the_mp3_is_preferred_to_the_hls_stream() -> None:
    """An mp3 wins over an HLS stream, whatever their bitrates."""
    assets = [
        audio("Stream", "HLS", 9999, "https://example.com/stream.m3u8"),
        audio("Progressive", "mp3", 0, "https://example.com/a.mp3"),
    ]
    chosen = get_audio(assets)
    assert chosen is not None
    assert chosen.url == "https://example.com/a.mp3"


@pytest.mark.parametrize(
    "assets",
    [
        [],
        [audio("Progressive", "mp4", 192, "https://example.com/a.mp4")],
        [audio("Stream", "DASH", 0, "https://example.com/a.mpd")],
        [audio("Stream", "mp3", 192, "https://example.com/a.mp3")],
    ],
)
def test_no_audio_to_play(assets: list[DRAudioAsset]) -> None:
    """An episode needs a progressive mp3 or an HLS stream."""
    assert get_audio(assets) is None


def test_a_channel_is_streamed_from_hls() -> None:
    """The HLS stream of a channel is preferred to its ICY streams."""
    stream = get_stream(channel_audio("p3"))
    assert stream is not None
    assert (stream.format, stream.url) == ("HLS", STREAM.format("p3"))


def test_the_icy_stream_of_the_highest_bitrate_when_there_is_no_hls_one() -> None:
    """Without an HLS stream a channel is streamed from its ICY stream of the highest bitrate."""
    assets = [asset for asset in channel_audio("p3") if asset.format != "HLS"]
    stream = get_stream(assets)
    assert stream is not None
    assert (stream.format, stream.url) == ("ICY", ICY_STREAM.format("A05H"))


@pytest.mark.parametrize("assets", [[], [audio("Stream", "DASH", 0, "https://example.com/a.mpd")]])
def test_no_stream_without_hls_or_icy(assets: list[DRAudioAsset]) -> None:
    """A channel needs an HLS or an ICY stream."""
    assert get_stream(assets) is None


def test_channels_match_the_name_and_the_slug_ignoring_case() -> None:
    """The name and the slug of a channel are searched, ignoring case."""
    assert slugs(search_channels(CHANNEL_LIST, "P3", 10)) == ["p3"]
    assert slugs(search_channels(CHANNEL_LIST, "JAZZ", 10)) == ["p8jazz"]
    assert slugs(search_channels(CHANNEL_LIST, "beat", 10)) == ["p6beat"]


def test_every_word_must_match_a_channel() -> None:
    """Every word of the query must be in the name or the slug of the channel."""
    assert slugs(search_channels(CHANNEL_LIST, "københavn", 10)) == ["p4kbh", "p5kbh"]
    assert slugs(search_channels(CHANNEL_LIST, "p4 københavn", 10)) == ["p4kbh"]
    assert search_channels(CHANNEL_LIST, "p3 jazz", 10) == []


def test_districts_are_found_too() -> None:
    """The district channels are searched like the others."""
    assert slugs(search_channels(CHANNEL_LIST, "p4", 10)) == ["p4kbh", "p4vest", "p4aarhus"]


def test_channel_search_limit_and_blank_query() -> None:
    """No more channels than the limit are returned, and a blank query finds none."""
    assert len(search_channels(CHANNEL_LIST, "p", 3)) == 3
    assert search_channels(CHANNEL_LIST, "  ", 10) == []


def title_at(time_of_day: str) -> str | None:
    """Return the title of the track of the fixture that plays at the time."""
    track = find_track(TRACKS, at(time_of_day))
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
    assert find_track([], at("22:30:00")) is None


def programme(slug: str, title: str, end: str = "23:00:00") -> DRProgramme:
    """Return a programme on air on a channel until the end."""
    return DRProgramme(
        title=title, end_time=at(end), channel=DRChannelRef(slug=slug), image_assets=[]
    )


def test_the_programme_on_air() -> None:
    """The programme on air is the one of the channel."""
    on_air = find_programme(PROGRAMME_LIST, "p3", at("22:30:00"))
    assert on_air is not None
    assert on_air.title == "Den sidste time"
    assert on_air.end_time == at("23:03:00")
    on_air = find_programme(PROGRAMME_LIST, "p2", at("22:30:00"))
    assert on_air is not None
    assert on_air.title == "Natsværmeren: Poesi"


def test_the_channel_is_picked_by_its_whole_slug() -> None:
    """A slug that starts like the one of the channel is another channel."""
    programmes = [programme("p4bornholm", "Bornholm"), programme("p4kbh", "Før natten på P4")]
    on_air = find_programme(programmes, "p4kbh", at("22:30:00"))
    assert on_air is not None
    assert on_air.title == "Før natten på P4"


def test_no_programme_once_it_is_over() -> None:
    """A programme is on air until its end."""
    assert find_programme(PROGRAMME_LIST, "p3", at("23:02:59")) is not None
    assert find_programme(PROGRAMME_LIST, "p3", at("23:03:00")) is None


def test_no_programme_for_a_channel_that_is_not_listed() -> None:
    """A channel that is not in the schedules has no programme."""
    assert find_programme(PROGRAMME_LIST, "p8jazz", at("22:30:00")) is None


def test_the_stream_metadata_is_the_track_with_the_picture_of_the_programme() -> None:
    """A track playing is shown with its artist and the picture of the programme."""
    metadata = get_stream_metadata(CHANNELS["p3"], TRACKS, PROGRAMME_LIST, at("22:30:00"))
    assert metadata == StreamMetadata(
        title="Min egen værste ven", artist="Friða Saiya", image_url=P3_PICTURE
    )


def test_the_stream_metadata_is_classical_music_with_its_composer() -> None:
    """A classical track is shown with its composer."""
    metadata = get_stream_metadata(CHANNELS["p2"], CLASSICAL_TRACKS, PROGRAMME_LIST, at("22:20:00"))
    assert metadata.title == "Suor Angelica - Intermezzo"
    assert metadata.artist == "Giacomo Puccini"


def test_each_composer_is_named_once() -> None:
    """A composer is named once, whatever the case of the role."""
    roles = [
        {"role": "Komponist", "name": "Carl Nielsen"},
        {"role": "Dirigent", "name": "Bo Holten"},
        {"role": "komponist", "name": "Wayne Siegel"},
        {"role": "Komponist", "name": "Carl Nielsen"},
    ]
    track = DRTrack.from_dict(track_item(classical=True, roles=roles))
    metadata = get_stream_metadata(CHANNELS["p2"], [track], [], track.played_time)
    assert metadata.artist == "Carl Nielsen, Wayne Siegel"


def test_classical_music_without_a_composer_keeps_the_credits() -> None:
    """A classical track without a composer keeps the credits of DR."""
    track = next(track for track in CLASSICAL_TRACKS if track.title.startswith("La peau douce"))
    metadata = get_stream_metadata(CHANNELS["p2"], [track], [], track.played_time)
    assert metadata.artist is not None
    assert metadata.artist.startswith("KOMPONIST: Georges Delerue.")


def test_other_music_on_a_classical_channel_keeps_the_artist_line() -> None:
    """A track that is not classical keeps the artist line of DR."""
    track = next(track for track in CLASSICAL_TRACKS if track.title == "Kære fisk")
    metadata = get_stream_metadata(CHANNELS["p2"], [track], [], track.played_time)
    assert metadata.artist == "Ursula Andkjær Olsen"


def test_a_track_without_an_artist_line_has_no_artist() -> None:
    """A track whose artist line is empty has no artist."""
    track = DRTrack.from_dict(track_item(description=""))
    metadata = get_stream_metadata(CHANNELS["p3"], [track], [], track.played_time)
    assert metadata == StreamMetadata(title="Raindance", artist=None)


def test_the_stream_metadata_is_the_programme_between_tracks() -> None:
    """Between tracks the programme is shown."""
    metadata = get_stream_metadata(CHANNELS["p3"], TRACKS, PROGRAMME_LIST, at("22:24:00"))
    assert metadata == StreamMetadata(title="Den sidste time", image_url=P3_PICTURE)


def test_the_stream_metadata_is_the_track_without_a_picture_without_programmes() -> None:
    """Without programmes a track is shown without a picture."""
    metadata = get_stream_metadata(CHANNELS["p3"], TRACKS, [], at("22:30:00"))
    assert metadata == StreamMetadata(title="Min egen værste ven", artist="Friða Saiya")


def test_the_stream_metadata_is_the_channel_when_nothing_is_known() -> None:
    """Without tracks and programmes the name of the channel is shown."""
    assert get_stream_metadata(CHANNELS["p3"], [], [], at("22:30:00")) == StreamMetadata(title="P3")
    assert get_stream_metadata(
        CHANNELS["p8jazz"], [], PROGRAMME_LIST, at("22:30:00")
    ) == StreamMetadata(title="P8")


def test_the_stream_metadata_is_the_channel_once_the_programme_is_over() -> None:
    """A programme that is over is not shown."""
    metadata = get_stream_metadata(CHANNELS["p3"], [], PROGRAMME_LIST, at("23:03:00"))
    assert metadata == StreamMetadata(title="P3")
