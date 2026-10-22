"""Tests for the Audiobookshelf parsers."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from aioaudiobookshelf.schema.author import Author, AuthorExpanded
from aioaudiobookshelf.schema.podcast import PodcastEpisodeExpanded

from music_assistant.providers.audiobookshelf.parsers import (
    parse_audiobook,
    parse_author,
    parse_podcast_episode,
)

from .abs_items import expanded_book

if TYPE_CHECKING:
    from aioaudiobookshelf.schema.library import LibraryItemExpandedBook
    from music_assistant_models.media_items import Audiobook

BASE_URL = "http://abs.local"
INSTANCE_ID = "audiobookshelf--test123"


def _parse_book(book: LibraryItemExpandedBook) -> Audiobook:
    return parse_audiobook(
        abs_audiobook=book,
        instance_id=INSTANCE_ID,
        domain="audiobookshelf",
        base_url=BASE_URL,
        audiobook_narrators=set(),
    )


@pytest.mark.parametrize(
    ("subtitle", "name"),
    [(None, "Title"), ("", "Title"), ("Part One", "Title | Part One")],
)
def test_audiobook_name_with_subtitle(subtitle: str | None, name: str) -> None:
    """Only a non-empty subtitle is appended to the title."""
    assert _parse_book(expanded_book(subtitle=subtitle)).name == name


def test_audiobook_cover_url_has_no_token() -> None:
    """Abs serves covers without auth, so no credential is stored in the image url."""
    images = _parse_book(expanded_book()).metadata.images

    assert images is not None
    assert [image.path for image in images] == [f"{BASE_URL}/api/items/book1/cover?ts=2000"]


@pytest.mark.parametrize("author_type", [Author, AuthorExpanded])
def test_author_image(author_type: type[Author]) -> None:
    """The author image is taken from the plain author of get_author as well."""
    abs_author = author_type.from_dict(
        {
            "id": "author1",
            "name": "Ada Lovelace",
            "addedAt": 1000,
            "updatedAt": 2000,
            "imagePath": "/metadata/authors/author1.jpg",
            "numBooks": 1,
        }
    )

    artist = parse_author(
        abs_author=abs_author, instance_id=INSTANCE_ID, domain="audiobookshelf", base_url=BASE_URL
    )

    assert artist.metadata.images is not None
    assert [image.path for image in artist.metadata.images] == [
        f"{BASE_URL}/api/authors/author1/image"
    ]


def test_podcast_episode_mapping_has_no_file_url() -> None:
    """The episode's abs file url is not handed out with its provider mapping."""
    episode = PodcastEpisodeExpanded.from_dict(
        {
            "libraryItemId": "podcast1",
            "id": "episode1",
            "title": "Episode",
            "addedAt": 1000,
            "updatedAt": 2000,
            "audioTrack": {
                "index": 1,
                "startOffset": 0,
                "duration": 600,
                "title": "episode1.mp3",
                "contentUrl": "/api/items/podcast1/file/1",
                "metadata": None,
            },
            "duration": 600,
            "size": 0,
        }
    )

    mass_episode = parse_podcast_episode(
        episode=episode,
        prov_podcast_id="podcast1",
        prov_podcast_name="Podcast",
        instance_id=INSTANCE_ID,
        domain="audiobookshelf",
        base_url=BASE_URL,
    )

    assert [mapping.url for mapping in mass_episode.provider_mappings] == [None]
