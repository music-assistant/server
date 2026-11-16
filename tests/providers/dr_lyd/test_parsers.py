"""Tests for the parsers of the DR LYD provider."""

from __future__ import annotations

from dataclasses import replace

import pytest
from music_assistant_models.enums import ContentType, ImageType, LinkType, MediaType

from music_assistant.constants import DEFAULT_AUDIOBOOK_PODCAST_GENRE
from music_assistant.helpers.json import json_dumps
from music_assistant.providers.dr_lyd.constants import IMAGE_URL
from music_assistant.providers.dr_lyd.helpers import get_audio
from music_assistant.providers.dr_lyd.models import DRSeries
from music_assistant.providers.dr_lyd.parsers import (
    parse_podcast,
    parse_podcast_episode,
    parse_radio,
)

from .conftest import (
    BARNELIV,
    CHANNEL_LIST,
    CHANNELS,
    CHANNELS_DOCUMENT,
    DEN_SIDSTE_TIME,
    DEN_SIDSTE_TIME_COVER,
    DOMAIN,
    GENSTART,
    ICY_STREAM,
    INSTANCE_ID,
    MUSIKQUIZZEN,
    P3_LOGO,
    SERIES,
    STREAM,
    at,
    channel_item,
    channels_document_with,
    channels_of,
    episode,
    podcast_mapping,
    series_item,
    without_picture,
)


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


def test_a_channel_is_named_by_its_title_at_dr() -> None:
    """The name of a channel is the title DR gives it."""
    assert [channel.name for channel in CHANNEL_LIST] == [
        "P1",
        "P2",
        "P3",
        "P4 København",
        "P4 Midt & Vest",
        "P4 Østjylland",
        "P5 København",
        "P6",
        "P8",
    ]


def test_the_districts_stand_in_for_p4_and_p5() -> None:
    """The districts of P4 and P5 are listed in place of P4 and P5."""
    assert "p4" not in CHANNELS
    assert "p5" not in CHANNELS
    assert {"p4kbh", "p4vest", "p4aarhus", "p5kbh"} <= set(CHANNELS)


def test_a_channel_with_districts_is_listed_with_them_else_alone() -> None:
    """A channel that has districts is replaced by them, one that has none stays."""
    document = [channel_item("p4", districts=[channel_item("p4a"), channel_item("p4b")])]
    assert [channel.slug for channel in channels_of(document)] == ["p4a", "p4b"]
    document[0]["districts"] = []
    assert [channel.slug for channel in channels_of(document)] == ["p4"]


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
    [channel] = channels_of([channel_item("p3", audioAssets=assets)])
    assert channel.stream_url == ICY_STREAM.format("A05H")
    assert not channel.hls


@pytest.mark.parametrize(
    "assets", [[], [{"format": "DASH", "target": "Stream", "url": STREAM.format("p9")}]]
)
def test_leaves_out_a_channel_without_a_stream(assets: list[dict[str, str]]) -> None:
    """A channel needs an HLS or an ICY stream."""
    assert channels_of([channel_item(audioAssets=assets)]) == []


def test_leaves_out_a_channel_that_is_not_a_flow_channel() -> None:
    """Only the flow channels play round the clock."""
    assert channels_of([channel_item(isFlowChannel=False)]) == []


def test_each_channel_has_an_urn_of_its_own() -> None:
    """The urn of a channel names its list of tracks, so every district has one."""
    assert len({channel.urn for channel in CHANNEL_LIST}) == len(CHANNEL_LIST)
    assert CHANNELS["p4kbh"].urn == "urn:dr:radio:channel:5fa156d3da351264f87b4651"


def test_the_channel_picture_is_the_logo_else_the_square_picture() -> None:
    """The picture of a channel is its logo, else its square picture, else none."""
    assert CHANNELS["p3"].image_url == P3_LOGO
    [channel] = [
        item
        for item in channels_of(channels_document_with("p3", channelLogos=[]))
        if item.slug == "p3"
    ]
    assert channel.image_url == IMAGE_URL.format("urn:dr:radio:image:6499a86d2222ea4246b8340d")
    [channel] = [
        item
        for item in channels_of(channels_document_with("p3", channelLogos=[], imageAssets=[]))
        if item.slug == "p3"
    ]
    assert channel.image_url is None


def test_a_radio_is_made_of_a_channel() -> None:
    """The radio of a channel has the slug as its id and the logo as its picture."""
    radio = parse_radio(CHANNELS["p3"], INSTANCE_ID, DOMAIN)
    assert radio.item_id == "p3"
    assert radio.name == "P3"
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
    series = SERIES[MUSIKQUIZZEN]
    podcast = parse_podcast(series, INSTANCE_ID, DOMAIN)
    assert podcast.item_id == MUSIKQUIZZEN
    assert podcast.name == "Musikquizzen"
    assert podcast.publisher == "DR"
    assert podcast.provider == INSTANCE_ID
    assert podcast.metadata.genres == {"Musik", "Underholdning"}
    assert podcast.metadata.description == series.description
    assert not podcast.metadata.explicit
    [link] = podcast.metadata.links or []
    assert (link.type, link.url) == (LinkType.WEBSITE, series.presentation_url)
    [mapping] = podcast.provider_mappings
    assert (mapping.item_id, mapping.provider_domain, mapping.provider_instance) == (
        MUSIKQUIZZEN,
        DOMAIN,
        INSTANCE_ID,
    )
    assert mapping.url is None


def test_a_podcast_without_categories_has_the_default_genre() -> None:
    """A podcast that DR gives no category has the default genre."""
    series = DRSeries.from_dict({**series_item(GENSTART), "categories": []})
    podcast = parse_podcast(series, INSTANCE_ID, DOMAIN)
    assert podcast.metadata.genres == {DEFAULT_AUDIOBOOK_PODCAST_GENRE}


def test_a_podcast_is_explicit_when_dr_says_so() -> None:
    """The explicit flag of DR is the one of the podcast."""
    series = DRSeries.from_dict({**series_item(GENSTART), "explicitContent": True})
    assert parse_podcast(series, INSTANCE_ID, DOMAIN).metadata.explicit


def test_the_picture_of_a_podcast_is_the_podcast_one_else_the_square_one() -> None:
    """The picture of a podcast is its podcast picture, else the square one, else none."""
    podcast = parse_podcast(SERIES[DEN_SIDSTE_TIME], INSTANCE_ID, DOMAIN)
    assert [image.path for image in podcast.metadata.images or []] == [DEN_SIDSTE_TIME_COVER]
    podcast = parse_podcast(SERIES[BARNELIV], INSTANCE_ID, DOMAIN)
    assert [image.path for image in podcast.metadata.images or []] == [
        IMAGE_URL.format("urn:dr:radio:image:68b840807964fa5412a0ecf6")
    ]
    series = DRSeries.from_dict({**series_item(GENSTART), "imageAssets": []})
    assert not parse_podcast(series, INSTANCE_ID, DOMAIN).metadata.images


def test_an_episode_is_made_of_an_episode_of_dr() -> None:
    """The episode names its podcast, its position and the format of its audio."""
    dr_episode = episode("13332646601")
    podcast = podcast_mapping()
    assert podcast.media_type == MediaType.PODCAST
    mass_episode = parse_podcast_episode(dr_episode, podcast, 2, INSTANCE_ID, DOMAIN)
    assert mass_episode.item_id == "13332646601"
    assert mass_episode.provider == INSTANCE_ID
    assert mass_episode.name == dr_episode.title
    assert mass_episode.duration == 2104
    assert mass_episode.position == 2
    assert mass_episode.podcast == podcast
    assert mass_episode.metadata.description == dr_episode.description
    assert not mass_episode.metadata.explicit
    assert mass_episode.metadata.release_date == at("22:00:00", "2026-10-22")
    [link] = mass_episode.metadata.links or []
    assert (link.type, link.url) == (LinkType.WEBSITE, dr_episode.presentation_url)
    [mapping] = mass_episode.provider_mappings
    assert (mapping.item_id, mapping.provider_domain, mapping.provider_instance) == (
        "13332646601",
        DOMAIN,
        INSTANCE_ID,
    )
    assert mapping.audio_format.content_type == ContentType.MP3
    [image] = mass_episode.metadata.images or []
    assert image.path == DEN_SIDSTE_TIME_COVER


def test_the_audio_address_stays_out_of_the_episode() -> None:
    """The address of the audio is not part of the episode, only of its stream details."""
    dr_episode = episode("13332646601")
    audio = get_audio(dr_episode.audio_assets)
    assert audio is not None
    mass_episode = parse_podcast_episode(dr_episode, podcast_mapping(), 1, INSTANCE_ID, DOMAIN)
    assert mass_episode.provider_mappings
    for mapping in mass_episode.provider_mappings:
        assert mapping.url is None
    assert audio.url not in json_dumps(mass_episode.to_dict())


def test_an_hls_episode_has_the_format_of_hls() -> None:
    """The format of an episode that is played from HLS is AAC."""
    mass_episode = parse_podcast_episode(
        episode("13332646424"), podcast_mapping(), 1, INSTANCE_ID, DOMAIN
    )
    [mapping] = mass_episode.provider_mappings
    assert mapping.audio_format.content_type == ContentType.AAC


def test_an_episode_with_a_picture_of_its_own_keeps_it() -> None:
    """An episode that has a picture of its own keeps it, whatever the cover of its podcast."""
    mass_episode = parse_podcast_episode(
        episode("13332646601"), podcast_mapping(GENSTART), 1, INSTANCE_ID, DOMAIN
    )
    [image] = mass_episode.metadata.images or []
    assert image.path == DEN_SIDSTE_TIME_COVER


def test_an_episode_without_a_picture_has_the_cover_of_the_podcast() -> None:
    """The cover of the podcast is the picture of an episode that has none."""
    bare = episode("13332646601", imageAssets=[])
    with_cover = parse_podcast_episode(bare, podcast_mapping(), 1, INSTANCE_ID, DOMAIN)
    assert [image.path for image in with_cover.metadata.images or []] == [DEN_SIDSTE_TIME_COVER]
    podcast = replace(podcast_mapping(), image=None)
    without_cover = parse_podcast_episode(bare, podcast, 1, INSTANCE_ID, DOMAIN)
    assert not without_cover.metadata.images
