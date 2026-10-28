"""Tests for resolving a browse folder into playable queue items."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import (
    Album,
    Artist,
    BrowseFolder,
    ItemMapping,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    Radio,
    Track,
    UniqueList,
)
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.constants import DB_TABLE_ALBUMS
from music_assistant.controllers.player_queues.helpers import build_queue_item
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.mass import MusicAssistant

PROVIDER = "test_folder_prov"


def _provider_mapping() -> set[ProviderMapping]:
    """Create a single provider mapping with a unique item id."""
    return {
        ProviderMapping(item_id=uuid4().hex, provider_domain=PROVIDER, provider_instance=PROVIDER)
    }


async def test_folder_plays_every_playable_item(mass: MusicAssistant) -> None:
    """
    Episodes and radios in a folder are queued alongside tracks, with resume applied.

    Subfolders and dynamic stations are left out.
    """
    user = await mass.webserver.auth.create_user("folderplayback")
    podcast = Podcast(
        item_id="show-1", provider=PROVIDER, name="Show", provider_mappings=_provider_mapping()
    )
    episode = PodcastEpisode(
        item_id="ep-1",
        provider=PROVIDER,
        name="Episode 1",
        provider_mappings=_provider_mapping(),
        position=1,
        podcast=podcast,
    )
    radio = Radio(
        item_id="radio-1", provider=PROVIDER, name="Radio", provider_mappings=_provider_mapping()
    )
    track = Track(
        item_id="track-1", provider=PROVIDER, name="Track", provider_mappings=_provider_mapping()
    )
    subfolder = BrowseFolder(item_id="sub", provider=PROVIDER, name="Sub", is_playable=False)
    station = Radio(
        item_id="station-1",
        provider=PROVIDER,
        name="Station",
        provider_mappings=_provider_mapping(),
        is_dynamic=True,
    )
    await mass.music.mark_item_played(
        episode,
        fully_played=False,
        seconds_played=90,
        user_initiated=True,
        userid=user.user_id,
    )
    folder = BrowseFolder(item_id="up_next", provider=PROVIDER, name="Up Next")

    with patch.object(
        mass.music, "browse", AsyncMock(return_value=[subfolder, station, episode, radio, track])
    ) as browse:
        resolved = await mass.player_queues._media_resolver._resolve_media_items(
            folder, userid=user.user_id
        )

    # the whole folder, not the first page of its listing
    browse.assert_awaited_once_with(folder.path, limit=None)
    items = [x.item for x in resolved]
    assert [x.media_type for x in items] == [
        MediaType.PODCAST_EPISODE,
        MediaType.RADIO,
        MediaType.TRACK,
    ]
    assert isinstance(items[0], PodcastEpisode)
    assert items[0].resume_position_ms == 90_000


async def test_folder_start_from_beginning_ignores_saved_progress(mass: MusicAssistant) -> None:
    """Starting a folder from the beginning plays its episodes from position zero."""
    user = await mass.webserver.auth.create_user("folderfromstart")
    podcast = Podcast(
        item_id="show-2", provider=PROVIDER, name="Show", provider_mappings=_provider_mapping()
    )
    episode = PodcastEpisode(
        item_id="ep-2",
        provider=PROVIDER,
        name="Episode 2",
        provider_mappings=_provider_mapping(),
        position=1,
        podcast=podcast,
    )
    await mass.music.mark_item_played(
        episode,
        fully_played=False,
        seconds_played=90,
        user_initiated=True,
        userid=user.user_id,
    )
    folder = BrowseFolder(item_id="up_next", provider=PROVIDER, name="Up Next")

    with patch.object(mass.music, "browse", AsyncMock(return_value=[episode])):
        resolved = await mass.player_queues._media_resolver._resolve_media_items(
            folder, userid=user.user_id, start_from_beginning=True
        )

    assert isinstance(resolved[0].item, PodcastEpisode)
    assert resolved[0].item.resume_position_ms == 0


async def test_folder_skips_dynamic_station_behind_item_mapping(mass: MusicAssistant) -> None:
    """A dynamic station listed as an ItemMapping is left out once it is resolved."""
    station = Radio(
        item_id="station-2",
        provider=PROVIDER,
        name="Station",
        provider_mappings=_provider_mapping(),
        is_dynamic=True,
    )
    mapping = ItemMapping(
        item_id="station-2", provider=PROVIDER, name="Station", media_type=MediaType.RADIO
    )
    track = Track(
        item_id="track-2", provider=PROVIDER, name="Track", provider_mappings=_provider_mapping()
    )
    folder = BrowseFolder(item_id="stations", provider=PROVIDER, name="Stations")

    with (
        patch.object(mass.music, "browse", AsyncMock(return_value=[mapping, track])),
        patch.object(mass.music, "get_item_by_uri", AsyncMock(return_value=station)),
    ):
        resolved = await mass.player_queues._media_resolver._resolve_media_items(folder)

    assert [x.item.media_type for x in resolved] == [MediaType.TRACK]


ALBUM_PATH = "Music/Kind of Blue"


def _file(path: str) -> Track:
    """Build the track the folder listing substitutes for the file at the given path."""
    return Track(
        item_id=path,
        provider=PROVIDER,
        name=path.rsplit("/", maxsplit=1)[-1],
        provider_mappings={
            ProviderMapping(item_id=path, provider_domain=PROVIDER, provider_instance=PROVIDER)
        },
    )


async def _scanned_album(mass: MusicAssistant, name: str, album_path: str) -> Album:
    """Add the library album the provider scanned from the given folder path."""
    artist = await mass.music.artists.add_item_to_library(
        Artist(
            item_id=f"artist-{name}",
            provider=PROVIDER,
            name=name,
            provider_mappings={
                ProviderMapping(
                    item_id=f"artist-{name}", provider_domain=PROVIDER, provider_instance=PROVIDER
                )
            },
        )
    )
    return await mass.music.albums.add_item_to_library(
        Album(
            item_id=album_path,
            provider=PROVIDER,
            name=name,
            artists=UniqueList([artist]),
            provider_mappings={
                ProviderMapping(
                    item_id=album_path, provider_domain=PROVIDER, provider_instance=PROVIDER
                )
            },
        )
    )


async def test_album_folder_and_its_disc_subfolder_play_as_the_album(mass: MusicAssistant) -> None:
    """
    A folder a library album was scanned from records that album as where its files play from.

    The same goes for a disc subfolder of it, while any other subfolder is a plain folder.
    """
    album_path = ALBUM_PATH
    album = await _scanned_album(mass, "Kind of Blue", album_path)
    track = _file(f"{album_path}/Disc 1/01.flac")
    album_folder = BrowseFolder(
        item_id=album_path, provider=PROVIDER, name="Kind of Blue", is_playable=True
    )
    disc_folder = BrowseFolder(
        item_id=f"{album_path}/Disc 1", provider=PROVIDER, name="Disc 1", is_playable=True
    )
    bonus_folder = BrowseFolder(
        item_id=f"{album_path}/Bonus", provider=PROVIDER, name="Bonus", is_playable=True
    )
    resolver = mass.player_queues._media_resolver

    with patch.object(mass.music, "browse", AsyncMock(return_value=[track])):
        album_play = await resolver._resolve_media_items(album_folder)
        disc_play = await resolver._resolve_media_items(disc_folder)
        bonus_play = await resolver._resolve_media_items(bonus_folder)

    for resolved in (album_play, disc_play):
        origin = resolved[0].origin
        assert origin is not None
        assert origin.container is not None
        assert origin.container.media_type == MediaType.ALBUM
        assert (origin.container.provider, origin.container.item_id) == ("library", album.item_id)
        # the album is only where the play is recorded, the folder's own files still play
        assert (origin.provider_instance, origin.item_id) == (PROVIDER, track.item_id)
    bonus_origin = bonus_play[0].origin
    assert bonus_origin is not None
    assert bonus_origin.container is not None
    assert bonus_origin.container.media_type == MediaType.FOLDER
    assert bonus_origin.container.item_id == f"{album_path}/Bonus"


async def test_an_album_folder_plays_its_disc_subfolders_as_the_album(mass: MusicAssistant) -> None:
    """The files in the disc subfolders of a played album folder play as that album."""
    album = await _scanned_album(mass, "In a Silent Way", "Music/In a Silent Way")
    album_folder = BrowseFolder(
        item_id="Music/In a Silent Way", provider=PROVIDER, name="In a Silent Way", is_playable=True
    )
    disc_folder = BrowseFolder(
        item_id="Music/In a Silent Way/CD1", provider=PROVIDER, name="CD1", is_playable=True
    )
    file = _file("Music/In a Silent Way/CD1/01.flac")
    browse = AsyncMock(
        side_effect=lambda path, **_kwargs: [disc_folder] if path == album_folder.path else [file]
    )
    with patch.object(mass.music, "browse", browse):
        resolved = await mass.player_queues._media_resolver._resolve_media_items(album_folder)

    origin = resolved[0].origin
    assert origin is not None
    assert origin.container is not None
    assert (origin.container.media_type, origin.container.item_id) == (
        MediaType.ALBUM,
        album.item_id,
    )
    assert (origin.provider_instance, origin.item_id) == (PROVIDER, file.item_id)


async def test_an_album_folder_play_counts_as_a_play_of_the_album(mass: MusicAssistant) -> None:
    """The first file of an album folder to play to the end counts the album as played, once."""
    user = await mass.webserver.auth.create_user("folderalbumcredit")
    album = await _scanned_album(mass, "Miles Smiles", "Music/Miles Smiles")
    album_folder = BrowseFolder(
        item_id="Music/Miles Smiles", provider=PROVIDER, name="Miles Smiles", is_playable=True
    )
    files = [_file("Music/Miles Smiles/01.flac"), _file("Music/Miles Smiles/02.flac")]
    with patch.object(mass.music, "browse", AsyncMock(return_value=files)):
        resolved = await mass.player_queues._media_resolver._resolve_media_items(album_folder)
    items = [
        build_queue_item("q1", cast("Track", entry.item), origin=entry.origin) for entry in resolved
    ]
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)
    queue_data = PlayerQueueData(queue=queue, items=items, userid=user.user_id)
    tracker = mass.player_queues

    credit = tracker._claim_album_credit(queue_data, items[0])
    assert credit is not None
    assert tracker._claim_album_credit(queue_data, items[1]) is None
    await tracker._mark_album_played(credit, cast("Track", items[0].media_item), queue_data)

    row = await mass.music.database.get_row(DB_TABLE_ALBUMS, {"item_id": album.item_id})
    assert row is not None
    assert row["play_count"] == 1


async def test_an_album_folder_played_with_its_parent_is_no_album_play(
    mass: MusicAssistant,
) -> None:
    """Playing the folder above an album folder does not count that album as played."""
    await _scanned_album(mass, "Nefertiti", "Music/Nefertiti")
    parent = BrowseFolder(item_id="Music", provider=PROVIDER, name="Music", is_playable=True)
    album_folder = BrowseFolder(
        item_id="Music/Nefertiti", provider=PROVIDER, name="Nefertiti", is_playable=True
    )
    file = _file("Music/Nefertiti/01.flac")
    browse = AsyncMock(
        side_effect=lambda path, **_kwargs: [album_folder] if path == parent.path else [file]
    )
    with patch.object(mass.music, "browse", browse):
        resolved = await mass.player_queues._media_resolver._resolve_media_items(parent)
    item = build_queue_item("q1", cast("Track", resolved[0].item), origin=resolved[0].origin)
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)
    queue_data = PlayerQueueData(queue=queue, items=[item])

    assert mass.player_queues._claim_album_credit(queue_data, item) is None


async def test_a_disc_subfolder_played_by_uri_plays_as_the_album(mass: MusicAssistant) -> None:
    """A folder played by its URI is named after its whole path, which must not hide the disc."""
    album = await _scanned_album(mass, "Bitches Brew", "Music/Bitches Brew")
    disc_folder = await mass.music.get_item_by_uri(f"{PROVIDER}://folder/Music/Bitches Brew/CD2")
    track = _file("Music/Bitches Brew/CD2/01.flac")

    with patch.object(mass.music, "browse", AsyncMock(return_value=[track])):
        resolved = await mass.player_queues._media_resolver._resolve_media_items(disc_folder)

    origin = resolved[0].origin
    assert origin is not None
    assert origin.container is not None
    assert (origin.container.media_type, origin.container.item_id) == (
        MediaType.ALBUM,
        album.item_id,
    )


async def test_a_providers_root_folder_plays_its_files(mass: MusicAssistant) -> None:
    """The root of a provider is a folder without a path, so there is no album to look up."""
    root_folder = await mass.music.get_item_by_uri(f"{PROVIDER}://folder/")
    track = _file("01.flac")

    with patch.object(mass.music, "browse", AsyncMock(return_value=[track])):
        resolved = await mass.player_queues._media_resolver._resolve_media_items(root_folder)

    origin = resolved[0].origin
    assert origin is not None
    assert origin.container is not None
    assert origin.container.media_type == MediaType.FOLDER
    assert (origin.provider_instance, origin.item_id) == (PROVIDER, "01.flac")
