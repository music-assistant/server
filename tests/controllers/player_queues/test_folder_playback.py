"""Tests for resolving a browse folder into playable queue items."""

from __future__ import annotations

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
    ):
        resolved = await mass.player_queues._media_resolver._resolve_media_items(
            folder, userid=user.user_id
        )

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


async def test_album_folder_and_its_disc_subfolder_play_as_the_album(mass: MusicAssistant) -> None:
    """
    A folder a library album was scanned from records that album as where its files play from.

    The same goes for a disc subfolder of it, while any other subfolder is a plain folder.
    """
    artist = await mass.music.artists.add_item_to_library(
        Artist(
            item_id="miles",
            provider=PROVIDER,
            name="Miles Davis",
            provider_mappings={
                ProviderMapping(
                    item_id="miles", provider_domain=PROVIDER, provider_instance=PROVIDER
                )
            },
        )
    )
    album_path = "Music/Kind of Blue"
    album = await mass.music.albums.add_item_to_library(
        Album(
            item_id=album_path,
            provider=PROVIDER,
            name="Kind of Blue",
            artists=UniqueList([artist]),
            provider_mappings={
                ProviderMapping(
                    item_id=album_path, provider_domain=PROVIDER, provider_instance=PROVIDER
                )
            },
        )
    )
    track = Track(
        item_id=f"{album_path}/Disc 1/01.flac",
        provider=PROVIDER,
        name="So What",
        provider_mappings={
            ProviderMapping(
                item_id=f"{album_path}/Disc 1/01.flac",
                provider_domain=PROVIDER,
                provider_instance=PROVIDER,
            )
        },
    )
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
