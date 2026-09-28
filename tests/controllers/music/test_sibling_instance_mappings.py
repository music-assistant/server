"""Tests for the mappings a library item gets on the other instances of a music service."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import MediaType, ProviderType
from music_assistant_models.media_items import Artist, ProviderMapping, Track, UniqueList
from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import DB_TABLE_PROVIDER_MAPPINGS, DB_TABLE_TRACKS
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

SPOTIFY_A = "spotify--a"
SPOTIFY_B = "spotify--b"
TIDAL = "tidal--test"


class _StreamingStub(MusicProvider):
    """A loaded, available instance of a streaming music service."""

    def __init__(self, mass: MusicAssistant, instance_id: str) -> None:
        domain = instance_id.split("--", maxsplit=1)[0]
        manifest = ProviderManifest(
            type=ProviderType.MUSIC,
            domain=domain,
            name=domain,
            description=domain,
            codeowners=["@music-assistant"],
        )
        config = ProviderConfig(
            values={},
            type=ProviderType.MUSIC,
            domain=domain,
            instance_id=instance_id,
            name=instance_id,
        )
        super().__init__(mass, manifest, config)
        self.available = True


def _load(mass: MusicAssistant, instance_id: str) -> None:
    """Load a streaming service instance into the running server."""
    mass._providers[instance_id] = _StreamingStub(mass, instance_id)


def _mapping(provider_instance: str, item_id: str) -> ProviderMapping:
    """Return the library mapping of an item that came in from the given instance."""
    return ProviderMapping(
        item_id=item_id,
        provider_domain=provider_instance.split("--", maxsplit=1)[0],
        provider_instance=provider_instance,
        in_library=True,
    )


async def _add_track(mass: MusicAssistant, name: str, mapping: ProviderMapping) -> Track:
    """Store a track that came in from one provider instance and return it."""
    artist = await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0",
            provider="library",
            name=f"{name} Artist",
            provider_mappings={_mapping(mapping.provider_instance, f"{mapping.item_id}-artist")},
        )
    )
    return await mass.music.tracks.add_item_to_library(
        Track(
            item_id="0",
            provider="library",
            name=name,
            provider_mappings={mapping},
            artists=UniqueList([artist]),
        )
    )


async def _track_owners(mass: MusicAssistant) -> dict[tuple[str, str], int]:
    """Return the library track id holding each (provider instance, provider item id) row."""
    rows = await mass.music.database.get_rows(
        DB_TABLE_PROVIDER_MAPPINGS, {"media_type": MediaType.TRACK.value}
    )
    return {(row["provider_instance"], row["provider_item_id"]): row["item_id"] for row in rows}


async def _track_exists(mass: MusicAssistant, item_id: str) -> bool:
    """Return whether the library track row is still there."""
    return await mass.music.database.get_row(DB_TABLE_TRACKS, {"item_id": int(item_id)}) is not None


async def test_adding_a_mapping_merges_the_item_holding_it_on_a_sibling_instance(
    mass: MusicAssistant,
) -> None:
    """A mapping whose copy for another instance belongs to another item merges that item."""
    _load(mass, SPOTIFY_B)
    _load(mass, TIDAL)
    spotify_track = await _add_track(mass, "Spotify Track", _mapping(SPOTIFY_B, "x"))
    tidal_track = await _add_track(mass, "Tidal Track", _mapping(TIDAL, "y"))
    # the second account is added later, so the first track is only mapped on the first
    _load(mass, SPOTIFY_A)

    await mass.music.tracks.add_provider_mappings(tidal_track.item_id, [_mapping(SPOTIFY_A, "x")])

    assert not await _track_exists(mass, spotify_track.item_id)
    merged_id = int(tidal_track.item_id)
    assert await _track_owners(mass) == {
        (TIDAL, "y"): merged_id,
        (SPOTIFY_A, "x"): merged_id,
        (SPOTIFY_B, "x"): merged_id,
    }


async def test_an_update_never_takes_over_a_sibling_instance_mapping_of_another_item(
    mass: MusicAssistant,
) -> None:
    """The copy an update makes for another instance leaves the row another item holds alone."""
    _load(mass, SPOTIFY_B)
    _load(mass, TIDAL)
    spotify_track = await _add_track(mass, "Spotify Track", _mapping(SPOTIFY_B, "x"))
    tidal_track = await _add_track(mass, "Tidal Track", _mapping(TIDAL, "y"))
    # the tidal track was linked to the same spotify item on a second account that is
    # loaded later, so the two library tracks each hold the item on one instance
    await mass.music.tracks.set_provider_mappings(
        tidal_track.item_id, {_mapping(TIDAL, "y"), _mapping(SPOTIFY_A, "x")}
    )
    _load(mass, SPOTIFY_A)
    tidal_track = await mass.music.tracks.get_library_item(tidal_track.item_id)

    await mass.music.tracks.update_item_in_library(tidal_track.item_id, tidal_track)

    assert await _track_owners(mass) == {
        (SPOTIFY_B, "x"): int(spotify_track.item_id),
        (TIDAL, "y"): int(tidal_track.item_id),
        (SPOTIFY_A, "x"): int(tidal_track.item_id),
    }
