"""Test the renderer parsers of the native YouTube Music provider."""

from datetime import UTC, datetime
from typing import Any

from music_assistant.providers.ytmusic_native import parsers


def _browse(browse_id: str, page_type: str | None = None) -> dict[str, Any]:
    """Return a navigationEndpoint that browses to the given page."""
    endpoint: dict[str, Any] = {"browseId": browse_id}
    if page_type:
        endpoint["browseEndpointContextSupportedConfigs"] = {
            "browseEndpointContextMusicConfig": {"pageType": page_type}
        }
    return {"browseEndpoint": endpoint}


def _watch(video_id: str, video_type: str = "MUSIC_VIDEO_TYPE_ATV") -> dict[str, Any]:
    """Return a navigationEndpoint that plays the given video."""
    return {
        "watchEndpoint": {
            "videoId": video_id,
            "watchEndpointMusicSupportedConfigs": {
                "watchEndpointMusicConfig": {"musicVideoType": video_type}
            },
        }
    }


def _run(text: str, endpoint: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"text": text, "navigationEndpoint": endpoint} if endpoint else {"text": text}


def _row(title: str, subtitle: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    """Return a musicResponsiveListItemRenderer with a title and a subtitle column."""
    return {
        "musicResponsiveListItemRenderer": {
            "flexColumns": [
                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [_run(title)]}}},
                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": subtitle}}},
            ],
            **extra,
        }
    }


def _song_row(video_id: str, title: str, set_video_id: str | None = None) -> dict[str, Any]:
    item_data = {"videoId": video_id}
    if set_video_id:
        item_data["playlistSetVideoId"] = set_video_id
    return _row(
        title,
        [_run("Daft Punk", _browse("UCartist", "MUSIC_PAGE_TYPE_ARTIST"))],
        playlistItemData=item_data,
    )


def _panel(video_id: str, title: str, video_type: str, artist_id: str | None) -> dict[str, Any]:
    """Return a playlistPanelVideoRenderer (a watch queue row)."""
    byline = [
        _run("Daft Punk", _browse(artist_id, "MUSIC_PAGE_TYPE_ARTIST") if artist_id else None)
    ]
    return {
        "playlistPanelVideoRenderer": {
            "videoId": video_id,
            "title": {"runs": [_run(title)]},
            "longBylineText": {"runs": byline},
            "lengthText": {"simpleText": "4:09"},
            "navigationEndpoint": _watch(video_id, video_type),
        }
    }


def test_rows_are_classified_by_their_navigation_target() -> None:
    """A row's own browse target decides its kind, and library/playlist prefixes are dropped."""
    response = {
        "contents": [
            _row(
                "Discovery",
                [
                    _run("Album"),
                    _run(" • "),
                    _run("Daft Punk", _browse("UCa", "MUSIC_PAGE_TYPE_ARTIST")),
                    _run("2001"),
                ],
                navigationEndpoint=_browse("MPREb_album", "MUSIC_PAGE_TYPE_ALBUM"),
            ),
            _row(
                "Daft Punk",
                [],
                navigationEndpoint=_browse("MPLAUCartist", "MUSIC_PAGE_TYPE_LIBRARY_ARTIST"),
            ),
            _row("Mix", [], navigationEndpoint=_browse("VLPLplaylist", "MUSIC_PAGE_TYPE_PLAYLIST")),
            _row(
                "Show",
                [],
                navigationEndpoint=_browse(
                    "MPSPPLshow", "MUSIC_PAGE_TYPE_PODCAST_SHOW_DETAIL_PAGE"
                ),
            ),
            _row(
                "Da Funk",
                [
                    _run("Daft Punk", _browse("UCa", "MUSIC_PAGE_TYPE_ARTIST")),
                    _run(" • "),
                    _run("Homework", _browse("MPREb_hw", "MUSIC_PAGE_TYPE_ALBUM")),
                    _run(" • "),
                    _run("5:29"),
                ],
                playlistItemData={"videoId": "vid1"},
            ),
        ]
    }

    items = parsers.parse_items(response)

    assert [item["kind"] for item in items] == ["album", "artist", "playlist", "podcast", "track"]
    album, artist, playlist, podcast, track = items
    assert (album["browse_id"], album["album_type"], album["year"]) == (
        "MPREb_album",
        "album",
        2001,
    )
    assert artist["channel_id"] == "UCartist"
    assert playlist["playlist_id"] == "PLplaylist"
    assert podcast["podcast_id"] == "PLshow"
    assert track["artists"] == [{"id": "UCa", "name": "Daft Punk"}]
    assert track["album"] == {"id": "MPREb_hw", "name": "Homework"}
    assert track["duration"] == 329


def test_episode_rows_are_not_parsed_as_tracks() -> None:
    """A row playing a podcast episode becomes an episode linked to its show."""
    row = _row(
        "Episode 31",
        [
            _run("Mar 2, 2021"),
            _run(" • "),
            _run("Some Podcast", _browse("MPSPPLshow", "MUSIC_PAGE_TYPE_PODCAST_SHOW_DETAIL_PAGE")),
        ],
        playlistItemData={"videoId": "ep1"},
        overlay=_watch("ep1", "MUSIC_VIDEO_TYPE_PODCAST_EPISODE"),
    )

    item = parsers.parse_row(row["musicResponsiveListItemRenderer"])

    assert item is not None
    assert item["kind"] == "episode"
    assert item["podcast"] == {"id": "PLshow", "name": "Some Podcast"}
    assert item["date"] == datetime(2021, 3, 2, tzinfo=UTC)


def test_watch_queue_keeps_the_song_and_borrows_artist_ids_from_its_video() -> None:
    """
    A wrapper pairs a song (ATV) with its music video: keep the song once, with ids.

    The song row credits its artist without a link, the video row links it.
    """
    response = {
        "playlistPanelRenderer": {
            "contents": [
                {
                    "playlistPanelVideoWrapperRenderer": {
                        "primaryRenderer": _panel(
                            "omv1", "Get Lucky (Video)", "MUSIC_VIDEO_TYPE_OMV", "UCdp"
                        ),
                        "counterpart": [
                            {
                                "counterpartRenderer": _panel(
                                    "atv1", "Get Lucky", "MUSIC_VIDEO_TYPE_ATV", None
                                )
                            }
                        ],
                    }
                },
                _panel("omv2", "Lone Video", "MUSIC_VIDEO_TYPE_OMV", "UCdp"),
                _panel("omv2", "Lone Video", "MUSIC_VIDEO_TYPE_OMV", "UCdp"),
            ]
        }
    }

    tracks = parsers.parse_watch_queue(response)

    assert [track["video_id"] for track in tracks] == ["atv1", "omv2"]
    assert tracks[0]["artists"] == [{"id": "UCdp", "name": "Daft Punk"}]
    assert tracks[0]["duration"] == 249


def test_playlist_entries_exclude_the_suggestions_shelf() -> None:
    """An owned playlist's page also lists suggested songs; they are not entries."""
    response = {
        "contents": {
            "twoColumnBrowseResultsRenderer": {
                "secondaryContents": {
                    "sectionListRenderer": {
                        "contents": [
                            {
                                "musicPlaylistShelfRenderer": {
                                    "contents": [
                                        _song_row("a", "First", "set-a"),
                                        _song_row("b", "Second", "set-b"),
                                        {
                                            "continuationItemRenderer": {
                                                "continuationEndpoint": {
                                                    "continuationCommand": {
                                                        "token": "playlist-page-2"
                                                    }
                                                }
                                            }
                                        },
                                    ]
                                }
                            },
                            {
                                "musicShelfRenderer": {
                                    "contents": [_song_row("s", "Suggested")],
                                    "continuations": [
                                        {
                                            "nextContinuationData": {
                                                "continuation": "suggestions-refresh"
                                            }
                                        }
                                    ],
                                }
                            },
                        ]
                    }
                }
            }
        }
    }

    entries = parsers.parse_playlist_tracks(response)

    assert [(e["video_id"], e["set_video_id"]) for e in entries] == [("a", "set-a"), ("b", "set-b")]
    assert parsers.playlist_continuation(response) == "playlist-page-2"


def test_continuation_tokens_in_both_styles() -> None:
    """Browse pages page on with either a legacy or a command-style token."""
    legacy = {"continuations": [{"nextContinuationData": {"continuation": "legacy"}}]}
    command = {
        "continuationItemRenderer": {
            "continuationEndpoint": {"continuationCommand": {"token": "cmd"}}
        }
    }

    assert parsers.continuation_token(legacy) == "legacy"
    assert parsers.continuation_token(command) == "cmd"
    assert parsers.continuation_token({"contents": []}) is None


def test_image_url_asks_for_the_stored_original() -> None:
    """Google image URLs lose their resize spec; video thumbnails are left alone."""
    art = [
        {"url": "https://lh3.googleusercontent.com/abc=w60-h60-l90-rj", "width": 60},
        {"url": "https://lh3.googleusercontent.com/abc=w544-h544-l90-rj", "width": 544},
    ]
    video = [{"url": "https://i.ytimg.com/vi/x/hq720.jpg?sqp=abc", "width": 853}]

    assert parsers.image_url(art) == "https://lh3.googleusercontent.com/abc"
    assert parsers.image_url(video) == "https://i.ytimg.com/vi/x/hq720.jpg?sqp=abc"
    assert parsers.image_url([]) is None


def test_durations() -> None:
    """Clock-style and spoken durations both become seconds."""
    assert parsers.parse_duration("4:36") == 276
    assert parsers.parse_duration("1:02:03") == 3723
    assert parsers.parse_duration("54M plays") is None
    assert parsers.parse_spoken_duration(" • 1 hr 55 min") == 6900
    assert parsers.parse_spoken_duration("45 min") == 2700


def test_episode_page() -> None:
    """A single episode page yields the episode with its show, date and length."""
    response = {
        "musicResponsiveHeaderRenderer": {
            "title": {"runs": [_run("Episode 21")]},
            "subtitle": {"runs": [_run("17 views"), _run(" • "), _run("Nov 2, 2025")]},
            "straplineTextOne": {"runs": [_run("The Show", _browse("MPSPPLshow"))]},
            "progress": {
                "musicPlaybackProgressRenderer": {
                    "playbackProgressText": {"runs": [_run(" • "), _run("1 hr 55 min")]}
                }
            },
        },
        "musicDescriptionShelfRenderer": {"description": {"runs": [_run("About it")]}},
    }

    episode = parsers.parse_episode_page(response, "ep21")

    assert episode is not None
    assert episode["podcast"] == {"id": "PLshow", "name": "The Show"}
    assert episode["date"] == datetime(2025, 11, 2, tzinfo=UTC)
    assert episode["duration"] == 6900
    assert episode["description"] == "About it"
