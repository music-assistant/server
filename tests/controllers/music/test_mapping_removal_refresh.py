"""Tests that removing one provider mapping rebuilds what was merged in from it."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ExternalID
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.media_items import Album, Artist, ProviderMapping, Track, UniqueList

from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider

FS = "filesystem_local--test"
SPOTIFY = "spotify--test"
TIDAL = "tidal--test"
RELEASE_MBID = "5f4e0a1c-0000-4000-8000-0000000000a1"
OLD = "Tchaikovsky"
NEW = "Pyotr Ilyich Tchaikovsky"


def _mapping(item_id: str, instance_id: str = FS, url: str | None = None) -> ProviderMapping:
    """Return a provider mapping for the given item id."""
    return ProviderMapping(
        item_id=item_id,
        provider_domain=instance_id.split("--", maxsplit=1)[0],
        provider_instance=instance_id,
        url=url,
    )


def _artist(name: str, instance_id: str = FS) -> Artist:
    """Return an artist as a provider reports it, without a MusicBrainz id."""
    return Artist(
        item_id=name,
        provider=instance_id,
        name=name,
        provider_mappings={_mapping(name, instance_id)},
    )


def _album(folder: str, album_artist: str, instance_id: str = FS) -> Album:
    """Return an album folder (or a streaming album) of the shared release."""
    album = Album(
        item_id=folder,
        provider=instance_id,
        name="Swan Lake",
        provider_mappings={
            _mapping(folder, instance_id, url=folder if instance_id == FS else None)
        },
        artists=UniqueList([_artist(album_artist, instance_id)]),
    )
    album.mbid = RELEASE_MBID
    return album


def _track(
    folder: str,
    artist: str,
    number: int = 1,
    instance_id: str = FS,
    album_artist: str | None = None,
) -> Track:
    """Return a track (a file in an album folder, or a streaming track) of the release."""
    item_id = f"{folder}/{number:02d}.flac" if instance_id == FS else f"{folder}-{number}"
    return Track(
        item_id=item_id,
        provider=instance_id,
        name=f"Scene {number}",
        duration=200,
        track_number=number,
        external_ids={(ExternalID.MB_RECORDING, f"5f4e0a1c-0000-4000-8000-{number:012d}")},
        provider_mappings={_mapping(item_id, instance_id)},
        artists=UniqueList([_artist(artist, instance_id)]),
        album=_album(folder, album_artist or artist, instance_id),
    )


class FakeProviders:
    """Providers that serve the given tracks and albums, and report everything else gone."""

    def __init__(self, tracks: list[Track], albums: list[Album]) -> None:
        """
        Set up a provider per instance id.

        :param tracks: The tracks the providers still have.
        :param albums: The albums the providers still have.
        """
        self.tracks = {(x.provider, x.item_id): x for x in tracks}
        self.albums = {(x.provider, x.item_id): x for x in albums}
        self.providers: dict[str, MagicMock] = {}
        for instance_id in (FS, SPOTIFY, TIDAL):
            provider = MagicMock(spec=MusicProvider)
            provider.instance_id = instance_id
            provider.domain = instance_id.split("--", maxsplit=1)[0]
            provider.available = True
            provider.get_track = AsyncMock(side_effect=self._getter(instance_id, self.tracks))
            provider.get_album = AsyncMock(side_effect=self._getter(instance_id, self.albums))
            self.providers[instance_id] = provider

    def get_provider(self, instance_id: str, *_args: Any, **_kwargs: Any) -> MagicMock | None:
        """Return the provider of the given instance id."""
        return self.providers.get(instance_id)

    @staticmethod
    def _getter(instance_id: str, items: dict[tuple[str, str], Any]) -> Any:
        """Return a lookup that raises not found for an item the provider no longer has."""

        async def _get(item_id: str) -> Any:
            if (instance_id, item_id) not in items:
                raise MediaNotFoundError(item_id)
            return items[(instance_id, item_id)]

        return _get


async def _add(mass: MusicAssistant, *tracks: Track) -> list[str]:
    """Add the tracks the way a sync adds new files and return their library ids."""
    return [(await mass.music.tracks.add_item_to_library(track)).item_id for track in tracks]


async def _track_artists(mass: MusicAssistant, track_id: str) -> set[str]:
    """Return the names of the artists of a library track."""
    return {x.name for x in (await mass.music.tracks.get_library_item(track_id)).artists}


async def _album_state(mass: MusicAssistant, track_id: str) -> tuple[set[str], set[str]]:
    """Return the artist names and mapped ids of the album of a library track."""
    track = await mass.music.tracks.get_library_item(track_id)
    assert track.album
    album = await mass.music.albums.get_library_item(track.album.item_id)
    return {x.name for x in album.artists}, {x.item_id for x in album.provider_mappings}


async def test_renamed_album_drops_the_old_artist_and_folder(mass: MusicAssistant) -> None:
    """A whole album moved and retagged keeps only the new artist and folder."""
    moved = [_track("New", NEW, 1), _track("New", NEW, 2)]
    fake = FakeProviders(moved, [moved[0].album])  # type: ignore[list-item]
    old_ids = await _add(mass, _track("Old", OLD, 1), _track("Old", OLD, 2))
    assert await _add(mass, *moved) == old_ids

    with patch.object(mass, "get_provider", side_effect=fake.get_provider):
        await mass.music.tracks.remove_provider_mapping(old_ids[0], FS, "Old/01.flac")
        fake.providers[FS].get_album.reset_mock()
        await mass.music.tracks.remove_provider_mapping(old_ids[1], FS, "Old/02.flac")

    for track_id in old_ids:
        assert await _track_artists(mass, track_id) == {NEW}
        assert await _album_state(mass, track_id) == ({NEW}, {"New"})
    # once the old folder is dropped, the album has nothing left to check
    fake.providers[FS].get_album.assert_not_called()


async def test_partly_moved_album_keeps_both_folders(mass: MusicAssistant) -> None:
    """A track still in the old folder keeps that folder and its artist on the album."""
    moved = _track("New", NEW, 1)
    stays = _track("Old", OLD, 2)
    fake = FakeProviders([moved, stays], [moved.album, stays.album])  # type: ignore[list-item]
    track_id, _ = await _add(mass, _track("Old", OLD, 1), stays)
    await _add(mass, moved)

    with patch.object(mass, "get_provider", side_effect=fake.get_provider):
        await mass.music.tracks.remove_provider_mapping(track_id, FS, "Old/01.flac")

    assert await _track_artists(mass, track_id) == {NEW}
    assert await _album_state(mass, track_id) == ({OLD, NEW}, {"Old", "New"})


async def test_two_files_left_keep_both_artists(mass: MusicAssistant) -> None:
    """With two copies still on disk, neither copy's artists are stale."""
    copies = [_track("New", NEW, 1), _track("Copy", NEW, 1)]
    fake = FakeProviders(copies, [])
    (track_id,) = await _add(mass, _track("Old", OLD, 1))
    await _add(mass, *copies)

    with patch.object(mass, "get_provider", side_effect=fake.get_provider):
        await mass.music.tracks.remove_provider_mapping(track_id, FS, "Old/01.flac")

    fake.providers[FS].get_track.assert_not_called()
    assert await _track_artists(mass, track_id) == {OLD, NEW}


async def test_item_mapped_to_another_provider_is_not_replaced(mass: MusicAssistant) -> None:
    """With a file and a streaming copy left, the merged data stays and nothing is fetched."""
    moved = _track("New", NEW, 1)
    streaming = _track("sp", NEW, 1, instance_id=SPOTIFY)
    fake = FakeProviders([moved, streaming], [moved.album, streaming.album])  # type: ignore[list-item]
    (track_id,) = await _add(mass, _track("Old", OLD, 1))
    await _add(mass, moved, streaming)

    with patch.object(mass, "get_provider", side_effect=fake.get_provider):
        await mass.music.tracks.remove_provider_mapping(track_id, FS, "Old/01.flac")

    fake.providers[FS].get_track.assert_not_called()
    fake.providers[SPOTIFY].get_track.assert_not_called()
    assert await _track_artists(mass, track_id) == {OLD, NEW}


async def test_album_is_only_checked_on_the_provider_that_lost_the_track(
    mass: MusicAssistant,
) -> None:
    """A filesystem removal never asks a streaming service about the album."""
    moved = _track("New", NEW, 1)
    fake = FakeProviders([moved], [moved.album])  # type: ignore[list-item]
    (track_id,) = await _add(mass, _track("Old", OLD, 1))
    await _add(mass, moved)
    album_id = (await mass.music.tracks.get_library_item(track_id)).album.item_id  # type: ignore[union-attr]
    await mass.music.albums.add_provider_mappings(album_id, [_mapping("a1", SPOTIFY)])

    with patch.object(mass, "get_provider", side_effect=fake.get_provider):
        await mass.music.tracks.remove_provider_mapping(track_id, FS, "Old/01.flac")

    fake.providers[SPOTIFY].get_album.assert_not_called()
    # two sources are left, so the album keeps what both contributed
    assert await _album_state(mass, track_id) == ({OLD, NEW}, {"New", "a1"})


async def test_unreachable_album_source_is_not_dropped(mass: MusicAssistant) -> None:
    """An album folder that can not be checked is kept, only not found means gone."""
    moved = _track("New", NEW, 1)
    fake = FakeProviders([moved], [moved.album])  # type: ignore[list-item]
    fake.providers[FS].get_album.side_effect = ProviderUnavailableError("share went away")
    (track_id,) = await _add(mass, _track("Old", OLD, 1))
    await _add(mass, moved)

    with patch.object(mass, "get_provider", side_effect=fake.get_provider):
        await mass.music.tracks.remove_provider_mapping(track_id, FS, "Old/01.flac")

    assert await _track_artists(mass, track_id) == {NEW}
    assert await _album_state(mass, track_id) == ({OLD, NEW}, {"Old", "New"})


async def test_streaming_mapping_loss_refreshes_from_the_other_service(
    mass: MusicAssistant,
) -> None:
    """A track on Spotify and Tidal that loses its Spotify mapping is rebuilt from Tidal."""
    tidal = _track("td", NEW, 1, instance_id=TIDAL)
    fake = FakeProviders([tidal], [tidal.album])  # type: ignore[list-item]
    (track_id,) = await _add(mass, _track("sp", OLD, 1, instance_id=SPOTIFY))
    await _add(mass, tidal)

    with patch.object(mass, "get_provider", side_effect=fake.get_provider):
        await mass.music.tracks.remove_provider_mapping(track_id, SPOTIFY, "sp-1")

    fake.providers[TIDAL].get_track.assert_awaited_once_with("td-1")
    assert await _track_artists(mass, track_id) == {NEW}


@pytest.mark.parametrize("error", [MediaNotFoundError("gone"), OSError("share went away")])
async def test_unreadable_source_keeps_the_stored_data(
    mass: MusicAssistant, error: Exception
) -> None:
    """A source that can not be read again leaves the track as stored."""
    fake = FakeProviders([], [])
    fake.providers[FS].get_track.side_effect = error
    (track_id,) = await _add(mass, _track("Old", OLD, 1))
    await _add(mass, _track("New", NEW, 1))

    with patch.object(mass, "get_provider", side_effect=fake.get_provider):
        await mass.music.tracks.remove_provider_mapping(track_id, FS, "Old/01.flac")

    assert await _track_artists(mass, track_id) == {OLD, NEW}


async def test_unexpected_error_is_not_hidden(mass: MusicAssistant) -> None:
    """Only a source that can not be read is skipped, a bug still surfaces."""
    fake = FakeProviders([], [])
    fake.providers[FS].get_track.side_effect = RuntimeError("bug")
    (track_id,) = await _add(mass, _track("Old", OLD, 1))
    await _add(mass, _track("New", NEW, 1))

    with (
        patch.object(mass, "get_provider", side_effect=fake.get_provider),
        pytest.raises(RuntimeError),
    ):
        await mass.music.tracks.remove_provider_mapping(track_id, FS, "Old/01.flac")


async def test_both_files_removed_in_one_pass(mass: MusicAssistant) -> None:
    """Removing both files of one track removes it, the second removal fetches nothing."""
    fake = FakeProviders([], [])
    (track_id,) = await _add(mass, _track("Old", OLD, 1))
    await _add(mass, _track("New", NEW, 1))

    with patch.object(mass, "get_provider", side_effect=fake.get_provider):
        await mass.music.tracks.remove_provider_mapping(track_id, FS, "Old/01.flac")
        fake.providers[FS].get_track.reset_mock()
        await mass.music.tracks.remove_provider_mapping(track_id, FS, "New/01.flac")

    fake.providers[FS].get_track.assert_not_called()
    with pytest.raises(MediaNotFoundError):
        await mass.music.tracks.get_library_item(track_id)


async def test_removing_a_whole_provider_fetches_nothing(mass: MusicAssistant) -> None:
    """Dropping every mapping of a provider leaves the merged data and calls no provider."""
    fake = FakeProviders([_track("td", NEW, 1, instance_id=TIDAL)], [])
    (track_id,) = await _add(mass, _track("sp", OLD, 1, instance_id=SPOTIFY))
    await _add(mass, _track("td", NEW, 1, instance_id=TIDAL))

    with patch.object(mass, "get_provider", side_effect=fake.get_provider):
        await mass.music.tracks.remove_provider_mappings(track_id, SPOTIFY)

    fake.providers[TIDAL].get_track.assert_not_called()
    assert await _track_artists(mass, track_id) == {OLD, NEW}


async def test_library_merge_keeps_both_artists(mass: MusicAssistant) -> None:
    """Merging two library tracks still combines their artists and calls no provider."""
    fake = FakeProviders([], [])
    (old_id,) = await _add(mass, _track("Old", OLD, 1))
    (new_id,) = await _add(mass, _track("Other", NEW, 2))

    with patch.object(mass, "get_provider", side_effect=fake.get_provider):
        await mass.music.tracks.merge_library_items(old_id, new_id)

    fake.providers[FS].get_track.assert_not_called()
    assert await _track_artists(mass, old_id) == {OLD, NEW}


async def test_refresh_keeps_the_in_library_state(mass: MusicAssistant) -> None:
    """A rebuilt item keeps the remaining mapping in the library."""
    tidal = _track("td", NEW, 1, instance_id=TIDAL)
    fake = FakeProviders([tidal], [tidal.album])  # type: ignore[list-item]
    synced = _track("td", NEW, 1, instance_id=TIDAL)
    for mapping in synced.provider_mappings:
        mapping.in_library = True
    (track_id,) = await _add(mass, _track("sp", OLD, 1, instance_id=SPOTIFY))
    await _add(mass, synced)

    with patch.object(mass, "get_provider", side_effect=fake.get_provider):
        await mass.music.tracks.remove_provider_mapping(track_id, SPOTIFY, "sp-1")

    (mapping,) = (await mass.music.tracks.get_library_item(track_id)).provider_mappings
    assert mapping.in_library
