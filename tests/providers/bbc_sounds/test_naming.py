"""Tests for the display names of BBC Sounds programmes and stations."""

from datetime import UTC, datetime

import pytest
from auntie_sounds.history import SegmentHistory
from auntie_sounds.models import (
    LiveStation,
    Network,
    PlayableItem,
    PodcastEpisode,
    RadioClip,
    ScheduleItem,
    Segment,
    SegmentOffset,
    Station,
    Synopses,
    Titles,
)
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

import music_assistant.helpers.datetime as dt
from music_assistant.providers.bbc_sounds import BBCSoundsProvider
from music_assistant.providers.bbc_sounds.live_rewind import LiveProgrammeId
from music_assistant.providers.bbc_sounds.naming import (
    UNKNOWN_STATION,
    TitleParts,
    full_station_name,
    programme_title,
    station_programme_title,
)
from music_assistant.providers.bbc_sounds.now_playing import NowPlaying


@pytest.mark.parametrize(
    ("station", "expected"),
    [
        (Station(id="bbc_radio_four", title="Radio 4"), "BBC Radio 4"),
        (Station(id="bbc_burmese_radio", title="ဘီဘီစီ မြန်မာပိုင်း ရေဒီယို"), "ဘီဘီစီ မြန်မာပိုင်း ရေဒီယို"),
        (
            LiveStation(
                id="bbc_radio_one", network=Network(id="bbc_radio_one", short_title="Radio 1")
            ),
            "BBC Radio 1",
        ),
        (
            LiveStation(
                id="cbeebies_radio",
                network=Network(id="cbeebies_radio", short_title="CBeebies Radio"),
            ),
            "CBeebies Radio",
        ),
        (LiveStation(id="bbc_radio_one"), UNKNOWN_STATION),
    ],
)
def test_full_station_name(station: Station | LiveStation, expected: str) -> None:
    """A station is named by its title, or its network for a live station."""
    assert full_station_name(station=station) == expected


# The same programme everywhere, so the table below compares like with like.
# 05:30 UTC is 06:30 in the UK in October.
ON_AIR = ScheduleItem(
    id="p01",
    titles=Titles(primary="Today", secondary="Business news", entity_title="Business news"),
    synopses=Synopses(short="News and current affairs"),
    network=Network(id="bbc_radio_four", short_title="Radio 4"),
    start=datetime(2026, 10, 12, 5, 30, tzinfo=UTC),
    end=datetime(2026, 10, 12, 6, 0, tzinfo=UTC),
)
# An episode of a podcast with seasons, where secondary is the season
SEASONAL = PodcastEpisode(
    id="p02",
    titles=Titles(
        primary="Intrigue",
        secondary="Chasing Mr Sunshine",
        tertiary="1. Mr Sunshine and Sean from Accounts",
        entity_title="1. Mr Sunshine and Sean from Accounts",
    ),
    synopses=Synopses(
        short="A billion-pound scandal fuelled by ambition, secrecy and the mysterious Mr Sunshine."
    ),
    release={"date": "2026-11-17T06:00:00Z"},
)
# An episode whose title starts with its brand
BULLETIN = PodcastEpisode(
    id="p04",
    titles=Titles(primary="BBC News", entity_title="BBC News 20:00"),
    synopses=Synopses(short="The latest news from the BBC."),
    release={"date": "2026-11-19T20:00:00Z"},
)
# Clips, whose titles hold only the headline
LOCAL_CLIP = RadioClip(
    id="c01",
    titles=Titles(primary="Gas leak shuts Didcot line", entity_title="Gas leak shuts Didcot line"),
    synopses=Synopses(short="Severe disruption is expected."),
    network=Network(id="bbc_radio_oxford", short_title="Radio Oxford"),
    availability={"from": "2026-09-23T10:02:00Z"},
)
WEB_CLIP = RadioClip(
    id="c02",
    titles=Titles(
        primary="Harper Lee first edition found", entity_title="Harper Lee first edition found"
    ),
    network=Network(id="bbc_webonly", short_title="BBC"),
)
# An episode whose title ends in its own punctuation
EXCLAIMED = PodcastEpisode(
    id="p03",
    titles=Titles(primary="6 Music Artist in Residence", entity_title="Up Against The Clock!"),
    synopses=Synopses(short="Shygirl takes over the residency."),
    release={"date": "2026-11-14T23:00:00Z"},
)
# How the station listing describes the programme on air
ON_AIR_STATION = LiveStation(
    id="bbc_radio_four",
    titles=Titles(primary="Today", secondary="06:30 - 07:00"),
    network=Network(id="bbc_radio_four", short_title="Radio 4"),
)
TRACK = Segment(
    id="track",
    segment_type="music",
    uris=[],
    titles=Titles(primary="Artist", secondary="Song"),
    image_url="https://example.com/song.jpg",
    offset=SegmentOffset(start=0, end=600),
)
PROGRAMME_ID = LiveProgrammeId(
    "bbc_radio_four",
    "p01",
    int(ON_AIR.start.timestamp()) if ON_AIR.start else 0,
    int(ON_AIR.end.timestamp()) if ON_AIR.end else 0,
)
STATION = "BBC Radio 4"
# A list row, which has one line
CATCH_UP = "06:30 Today • Business news"
# Now playing, where the programme is on now
NOW_PLAYING = "Today • Business news: News and current affairs"


def _details(item_id: str) -> StreamDetails:
    return StreamDetails(
        provider="bbc_sounds",
        item_id=item_id,
        audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
    )


async def _live_rewind_now_playing(
    provider: BBCSoundsProvider, monkeypatch: pytest.MonkeyPatch, behind: int, track: bool
) -> StreamMetadata | None:
    """Return what a live programme shows a few minutes in, the given seconds behind live."""
    elapsed = 300 if track else 600
    history = SegmentHistory()
    if track:
        history.merge([TRACK], programme_start=ON_AIR.start, now=ON_AIR.end)  # type: ignore[arg-type]
    provider.client.schedules.refresh_history.return_value = history  # type: ignore[attr-defined]
    monkeypatch.setattr(dt, "utc_timestamp", lambda: PROGRAMME_ID.start + elapsed + behind)
    details = _details(PROGRAMME_ID.item_id)
    await provider.stream_manager.now_playing.update_live_programme(
        NowPlaying(fallback=None, album=STATION), PROGRAMME_ID, ON_AIR, details, elapsed
    )
    return details.stream_metadata


class TestDisplayNames:
    """
    What each place shows, as one table of the agreed forms.

    The ids name the place, and the TitleParts combination it uses where it has one.
    """

    @pytest.mark.parametrize(
        ("item", "parts", "expected"),
        [
            pytest.param(
                ON_AIR,
                TitleParts.LISTING,
                "Today • Business news: News and current affairs · 12 Oct 2026",
                id="search-collections-player:LISTING",
            ),
            pytest.param(ON_AIR, TitleParts.SCHEDULE, CATCH_UP, id="catch-up:SCHEDULE"),
            pytest.param(ON_AIR, TitleParts.LIVE, f"«LIVE» {CATCH_UP}", id="catch-up-on-air:LIVE"),
            pytest.param(
                SEASONAL,
                TitleParts.PODCAST,
                "Chasing Mr Sunshine • 1. Mr Sunshine and Sean from Accounts: A billion-pound "
                "scandal fuelled by ambition, secrecy and the mysterious Mr Sunshine · 17 Nov 2026",
                id="episodes-with-seasons:PODCAST",
            ),
            pytest.param(
                ON_AIR,
                TitleParts.PODCAST,
                "Business news: News and current affairs · 12 Oct 2026",
                id="episodes-without-seasons:PODCAST",
            ),
            pytest.param(
                EXCLAIMED,
                TitleParts.PODCAST,
                "Up Against The Clock! Shygirl takes over the residency · 14 Nov 2026",
                id="title-ending-in-punctuation:PODCAST",
            ),
            pytest.param(
                BULLETIN,
                TitleParts.LISTING,
                "BBC News 20:00: The latest news from the BBC · 19 Nov 2026",
                id="episode-names-its-brand:LISTING",
            ),
            pytest.param(
                ON_AIR, TitleParts.PLAYLIST, "Today • Business news", id="playlist:PLAYLIST"
            ),
            pytest.param(
                LOCAL_CLIP,
                TitleParts.PLAYLIST,
                "BBC Radio Oxford • Gas leak shuts Didcot line",
                id="clip-names-its-station:PLAYLIST",
            ),
            pytest.param(
                LOCAL_CLIP,
                TitleParts.LISTING,
                "BBC Radio Oxford • Gas leak shuts Didcot line: Severe disruption is expected"
                " · 23 Sep 2026",
                id="clip-names-its-station:LISTING",
            ),
            pytest.param(
                WEB_CLIP,
                TitleParts.PLAYLIST,
                "Harper Lee first edition found",
                id="web-clip-is-only-its-headline:PLAYLIST",
            ),
            pytest.param(
                BULLETIN, TitleParts.PLAYLIST, "BBC News 20:00", id="playlist-brand:PLAYLIST"
            ),
            pytest.param(ON_AIR, TitleParts.STATION, NOW_PLAYING, id="now-playing-radio:STATION"),
            pytest.param(
                ON_AIR, TitleParts.LIVE_EDGE, f"«LIVE» {NOW_PLAYING}", id="live-edge:LIVE_EDGE"
            ),
            pytest.param(
                ON_AIR,
                TitleParts.LIVE_REWIND,
                f"«Rewound» {NOW_PLAYING}",
                id="rewound:LIVE_REWIND",
            ),
        ],
    )
    def test_programme_title(self, item: PlayableItem, parts: TitleParts, expected: str) -> None:
        """Each combination gives the form agreed for its place."""
        assert programme_title(item, parts) == expected

    async def test_on_air_in_catch_up(self, provider: BBCSoundsProvider) -> None:
        """A programme on air now, in the catch-up folder and the queue."""
        episode = await provider.adaptor.new_live_programme(ON_AIR)
        assert episode.name == f"«LIVE» {CATCH_UP}"

    @pytest.mark.parametrize(
        ("behind", "marker"),
        [
            pytest.param(30, "«LIVE»", id="at-live-edge"),
            pytest.param(600, "«Rewound»", id="behind"),
        ],
    )
    async def test_live_rewind_between_tracks(
        self, provider: BBCSoundsProvider, monkeypatch: pytest.MonkeyPatch, behind: int, marker: str
    ) -> None:
        """A live programme's now playing between tracks, marked for the live edge."""
        assert await _live_rewind_now_playing(provider, monkeypatch, behind, track=False) == (
            StreamMetadata(title=f"{marker} {NOW_PLAYING}", album=STATION)
        )

    async def test_song(self, provider: BBCSoundsProvider, monkeypatch: pytest.MonkeyPatch) -> None:
        """A song's now playing: the frontend shows the artist and album as artist • station."""
        assert await _live_rewind_now_playing(provider, monkeypatch, 30, track=True) == (
            StreamMetadata(
                title="Song",
                artist="Artist",
                album=STATION,
                image_url="https://example.com/song.jpg",
            )
        )

    async def test_station_between_tracks(self, provider: BBCSoundsProvider) -> None:
        """A live station's now playing between tracks, with the station on line 2."""
        provider.client.schedules.refresh_history.return_value = SegmentHistory()  # type: ignore[attr-defined]
        provider.client.schedules.on_air.return_value = ON_AIR  # type: ignore[attr-defined]
        details = _details("bbc_radio_four")

        await provider.stream_manager.now_playing.update_station(
            NowPlaying(fallback=None, album=STATION), details, 0
        )

        assert details.stream_metadata == StreamMetadata(title=NOW_PLAYING, album=STATION)

    def test_live_station_folder(self) -> None:
        """A station in the live folder: its description is the programme on air."""
        assert station_programme_title(ON_AIR_STATION) == "06:30 - 07:00 • Today"
