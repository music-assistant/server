"""
Tests for the top tracks of a library artist that no music service ranks.

They come from the recordings ListenBrainz counts the most listens for: the user's library
track where there is one, otherwise a track known only by its recording id, which is looked
up when it is opened or played. ListenBrainz answers from a recorded answer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import (
    ExternalID,
    ImageType,
    MediaType,
    ProviderFeature,
    ProviderSharing,
    ProviderType,
    SortField,
)
from music_assistant_models.media_items import (
    Artist,
    ItemMapping,
    MediaItemImage,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.constants import EXTERNAL_ID_PROVIDER
from music_assistant.helpers.external_ids import external_item_id
from music_assistant.models.music_provider import MusicProvider
from tests.common import set_music_source_access
from tests.providers.musicbrainz.helpers import (
    RADIOHEAD_MBID,
    answer_listenbrainz,
    own_throttler,
    recorded_answer,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

FILES = "filesystem_local--files"
HIDDEN = "filesystem_local--hidden"
SPOTIFY = "spotify--1"
LASTFM = "lastfm_recommendations"
PJ_HARVEY_MBID = "e795e03d-b5d5-4a5f-834d-162cfb308a2c"
USER = User(user_id="user-a", username="user-a", role=UserRole.USER)
# every request a music provider or MusicBrainz could be sent for the listing
REQUESTS = (
    "get_artist_toptracks",
    "get_track",
    "get_track_by_external_id",
    "search",
    "get_recording_details",
    "get_release_details",
)


@pytest.fixture(name="mass")
def mass_fixture(music_mass_with_cache: MusicAssistant) -> MusicAssistant:
    """Return a library-only instance with a real cache, its ListenBrainz client unpaced."""
    music_mass_with_cache.music.artists._listenbrainz.throttler = own_throttler()
    return music_mass_with_cache


def _recording(name: str) -> dict[str, Any]:
    """Return the entry of the recorded answer with the given title."""
    return next(entry for entry in recorded_answer() if entry["recording_name"] == name)


def _source(instance_id: str, features: set[ProviderFeature] | None = None) -> MagicMock:
    """Return a loaded music source with the given features, recording every request."""
    provider = MagicMock(spec=MusicProvider)
    provider.instance_id = instance_id
    provider.domain = instance_id.split("--", maxsplit=1)[0]
    provider.name = provider.domain
    provider.type = ProviderType.MUSIC
    provider.available = True
    provider.is_streaming_provider = provider.domain == "spotify"
    provider.supported_features = features or set()
    provider.supports_feature = lambda feature: feature in provider.supported_features
    for request in REQUESTS[:4]:
        setattr(provider, request, AsyncMock(return_value=[]))
    return provider


def _load_providers(mass: MusicAssistant, *sources: MagicMock) -> dict[str, Any]:
    """
    Load the music sources, a Last.fm that ranks one library track and MusicBrainz.

    :return: Every loaded provider, by instance id.
    """
    lastfm = MagicMock()
    lastfm.instance_id = lastfm.domain = LASTFM
    lastfm.type = ProviderType.METADATA
    lastfm.available = True
    lastfm.supported_features = {ProviderFeature.ARTIST_TOPTRACKS}
    lastfm.get_artist_toptracks = AsyncMock(
        return_value=[
            Track(
                item_id="lastfm", provider="library", name="Last.fm pick", provider_mappings=set()
            )
        ]
    )
    musicbrainz = MagicMock()
    musicbrainz.instance_id = musicbrainz.domain = "musicbrainz"
    musicbrainz.type = ProviderType.METADATA
    musicbrainz.available = True
    musicbrainz.supported_features = set()
    for request in REQUESTS[4:]:
        setattr(musicbrainz, request, AsyncMock())
    providers = {
        LASTFM: lastfm,
        "musicbrainz": musicbrainz,
        **{source.instance_id: source for source in sources},
    }
    mass._providers = providers  # type: ignore[assignment]
    return providers


def _requests(providers: dict[str, Any]) -> int:
    """Return how many requests the music sources and MusicBrainz were sent."""
    return sum(
        request.await_count
        for provider in providers.values()
        if provider.instance_id != LASTFM
        for name in REQUESTS
        if isinstance(request := getattr(provider, name, None), AsyncMock)
    )


def _mapping(instance_id: str, item_id: str) -> ProviderMapping:
    return ProviderMapping(
        item_id=item_id,
        provider_domain=instance_id.split("--", maxsplit=1)[0],
        provider_instance=instance_id,
        in_library=True,
    )


async def _add_artist(
    mass: MusicAssistant,
    name: str = "Radiohead",
    mbid: str | None = RADIOHEAD_MBID,
    instance_id: str = FILES,
) -> Artist:
    """Add an artist from the given source to the library and return the library item."""
    return await mass.music.artists.add_item_to_library(
        Artist(
            item_id=name,
            provider=instance_id,
            name=name,
            external_ids={(ExternalID.MB_ARTIST, mbid)} if mbid else set(),
            provider_mappings={_mapping(instance_id, name)},
        )
    )


async def _add_track(
    mass: MusicAssistant,
    artist: Artist,
    name: str,
    recording_mbid: str | None = None,
    instance_id: str = FILES,
) -> Track:
    """Add a track of the artist from the given source to the library, return the library item."""
    return await mass.music.tracks.add_item_to_library(
        Track(
            item_id=name,
            provider=instance_id,
            name=name,
            artists=UniqueList([artist]),
            external_ids={(ExternalID.MB_RECORDING, recording_mbid)} if recording_mbid else set(),
            provider_mappings={_mapping(instance_id, name)},
        )
    )


def _external_item_id(name: str) -> str:
    return external_item_id(ExternalID.MB_RECORDING, _recording(name)["recording_mbid"])


# ---------------------------------------------------------------------------
# the request budget and the sources asked
# ---------------------------------------------------------------------------


async def test_opening_the_top_tracks_costs_one_listenbrainz_request_a_week(
    mass: MusicAssistant,
) -> None:
    """The listing is ListenBrainz's, asked once; MusicBrainz and the services are not asked."""
    artist = await _add_artist(mass)
    providers = _load_providers(mass, _source(FILES))
    get = answer_listenbrainz(mass)

    listing = await mass.music.artists.get_library_artist_toptracks(artist.item_id)
    again = await mass.music.artists.get_library_artist_toptracks(artist.item_id)

    assert listing.complete
    assert [track.name for track in listing.items] == [track.name for track in again.items]
    assert len(listing.items) == 50
    assert {track.provider for track in listing.items} == {EXTERNAL_ID_PROVIDER}
    assert get.call_count == 1
    assert _requests(providers) == 0
    providers[LASTFM].get_artist_toptracks.assert_not_awaited()


async def test_music_providers_that_rank_the_artist_come_first(mass: MusicAssistant) -> None:
    """ListenBrainz is not asked for an artist a music provider ranks the tracks of."""
    artist = await _add_artist(mass, instance_id=SPOTIFY)
    providers = _load_providers(mass, _source(SPOTIFY, {ProviderFeature.ARTIST_TOPTRACKS}))
    get = answer_listenbrainz(mass)
    ranked = Track(item_id="1", provider=SPOTIFY, name="Ranked", provider_mappings=set())

    with patch.object(
        mass.music.artists, "get_provider_artist_toptracks", AsyncMock(return_value=[ranked])
    ):
        listing = await mass.music.artists.get_library_artist_toptracks(artist.item_id)

    assert [track.name for track in listing.items] == ["Ranked"]
    get.assert_not_called()
    providers[LASTFM].get_artist_toptracks.assert_not_awaited()


async def test_artist_without_musicbrainz_id_goes_to_lastfm(mass: MusicAssistant) -> None:
    """Without a MusicBrainz id there is nothing to ask ListenBrainz for."""
    artist = await _add_artist(mass, mbid=None)
    providers = _load_providers(mass, _source(FILES))
    get = answer_listenbrainz(mass)

    listing = await mass.music.artists.get_library_artist_toptracks(artist.item_id)

    assert [track.name for track in listing.items] == ["Last.fm pick"]
    assert listing.complete
    get.assert_not_called()
    providers[LASTFM].get_artist_toptracks.assert_awaited_once()


@pytest.mark.parametrize(
    ("status", "body", "headers", "complete"),
    [
        (200, [], None, True),
        (401, {"code": 401}, None, True),
        (500, None, None, False),
        (429, None, {"X-RateLimit-Reset-In": "9"}, False),
    ],
    ids=["empty answer", "401", "server error", "rate limited"],
)
async def test_without_a_list_lastfm_is_asked(
    mass: MusicAssistant,
    status: int,
    body: Any,
    headers: dict[str, str] | None,
    complete: bool,
) -> None:
    """
    Without a list the metadata providers rank the tracks, as before.

    When ListenBrainz could not answer, the listing is not complete, so it is not kept.
    """
    artist = await _add_artist(mass)
    providers = _load_providers(mass, _source(FILES))
    get = answer_listenbrainz(mass, status=status, body=body, headers=headers)

    listing = await mass.music.artists.get_library_artist_toptracks(artist.item_id)

    assert [track.name for track in listing.items] == ["Last.fm pick"]
    assert listing.complete is complete
    get.assert_called_once()
    providers[LASTFM].get_artist_toptracks.assert_awaited_once()


async def test_a_provider_filter_leaves_listenbrainz_out(mass: MusicAssistant) -> None:
    """A listing limited to a provider is that provider's, ListenBrainz is none of them."""
    artist = await _add_artist(mass)
    providers = _load_providers(mass, _source(FILES))
    get = answer_listenbrainz(mass)

    listing = await mass.music.artists.get_library_artist_toptracks(
        artist.item_id, provider_filter=LASTFM
    )

    assert [track.name for track in listing.items] == ["Last.fm pick"]
    get.assert_not_called()
    providers[LASTFM].get_artist_toptracks.assert_awaited_once()


# ---------------------------------------------------------------------------
# the tracks listed
# ---------------------------------------------------------------------------


async def test_library_tracks_take_the_place_of_their_recordings(mass: MusicAssistant) -> None:
    """A recording the user has is listed as the library track, in its place in the ranking."""
    artist = await _add_artist(mass)
    # found by its recording id alone: the title differs, and it is credited to another
    # library artist, so it is none of the artist's own tracks
    other_artist = await _add_artist(mass, "Radiohead (2)", mbid=None)
    by_id = await _add_track(
        mass, other_artist, "Creep (Remastered)", _recording("Creep")["recording_mbid"]
    )
    # found by its title, it carries no recording id
    by_title = await _add_track(mass, artist, "Paranoid Android")
    _load_providers(mass, _source(FILES))
    answer_listenbrainz(mass)

    listing = await mass.music.artists.get_library_artist_toptracks(artist.item_id)

    names = [track.name for track in listing.items]
    assert names[:6] == [
        "Karma Police",
        "Paranoid Android",
        "No Surprises",
        "All I Need",
        "Creep (Remastered)",
        "15 Step",
    ]
    assert listing.items[1].uri == by_title.uri
    assert listing.items[4].uri == by_id.uri
    assert [track.provider for track in listing.items].count("library") == 2
    assert len(listing.items) == 50


async def test_library_track_on_a_hidden_source_does_not_count(mass: MusicAssistant) -> None:
    """A user is listed the recording itself where the library track is on a hidden source."""
    set_music_source_access(
        mass,
        {FILES: None, HIDDEN: ProviderAccess(owner="user-b", sharing=ProviderSharing.PRIVATE)},
    )
    artist = await _add_artist(mass)
    hidden = await _add_track(
        mass, artist, "No Surprises", _recording("No Surprises")["recording_mbid"], HIDDEN
    )
    _load_providers(mass, _source(FILES), _source(HIDDEN))
    answer_listenbrainz(mass)

    owner_listing = await mass.music.artists.get_library_artist_toptracks(artist.item_id)
    with patch("music_assistant.controllers.music.media.base.get_current_user", return_value=USER):
        user_listing = await mass.music.artists.get_library_artist_toptracks(artist.item_id)

    assert owner_listing.items[2].uri == hidden.uri
    assert user_listing.items[2].item_id == _external_item_id("No Surprises")
    assert user_listing.items[2].provider == EXTERNAL_ID_PROVIDER


@pytest.mark.parametrize("co_artist_in_library", [True, False])
async def test_external_track_carries_its_artists_album_and_cover(
    mass: MusicAssistant, co_artist_in_library: bool
) -> None:
    """An external track names the artist of the page, its release and its cover as answered."""
    artist = await _add_artist(mass)
    expected_artists = [ItemMapping.from_item(artist)]
    if co_artist_in_library:
        pj_harvey = await _add_artist(mass, "PJ Harvey", PJ_HARVEY_MBID)
        expected_artists.append(ItemMapping.from_item(pj_harvey))
    _load_providers(mass, _source(FILES))
    answer_listenbrainz(mass)

    listing = await mass.music.artists.get_library_artist_toptracks(artist.item_id)

    tracks = {track.name: track for track in listing.items}
    karma_police = _recording("Karma Police")
    assert tracks["Karma Police"].to_dict() == Track(
        item_id=_external_item_id("Karma Police"),
        provider=EXTERNAL_ID_PROVIDER,
        name="Karma Police",
        duration=262,
        artists=UniqueList([ItemMapping.from_item(artist)]),
        album=ItemMapping(
            media_type=MediaType.ALBUM,
            item_id=external_item_id(ExternalID.MB_ALBUM, karma_police["release_mbid"]),
            provider=EXTERNAL_ID_PROVIDER,
            name="OK Computer",
        ),
        external_ids={(ExternalID.MB_RECORDING, karma_police["recording_mbid"])},
        provider_mappings=set(),
    ).to_dict() | {"metadata": tracks["Karma Police"].metadata.to_dict()}
    assert tracks["Karma Police"].metadata.images == UniqueList(
        [
            MediaItemImage(
                type=ImageType.THUMB,
                path=(
                    "https://coverartarchive.org/release/"
                    f"{karma_police['caa_release_mbid']}/{karma_police['caa_id']}-500.jpg"
                ),
                provider="coverartarchive",
                remotely_accessible=True,
            )
        ]
    )
    assert tracks["Karma Police"].uri == (
        f"external://track/musicbrainz_recordingid:{karma_police['recording_mbid']}"
    )
    # without release, cover or length
    assert tracks["You (acoustic)"].album is None
    assert not tracks["You (acoustic)"].metadata.images
    assert tracks["You (acoustic)"].duration == 0
    # Radiohead feat. PJ Harvey: the other artist only when it is in the library
    assert list(tracks["This Mess We\u2019re In"].artists) == expected_artists


async def test_external_tracks_are_kept_as_listed_once_per_answer(mass: MusicAssistant) -> None:
    """Each external track is kept in its listed form when the answer is fetched, not again."""
    artist = await _add_artist(mass)
    _load_providers(mass, _source(FILES))
    answer_listenbrainz(mass)
    tracks = mass.music.tracks
    store = AsyncMock(side_effect=tracks.store_listed_external_item)

    with patch.object(tracks, "store_listed_external_item", store):
        listing = await mass.music.artists.get_library_artist_toptracks(artist.item_id)
        assert store.await_count == 50
        # the kept answer lists the same forms again, so nothing is written
        await mass.music.artists.get_library_artist_toptracks(artist.item_id)
        assert store.await_count == 50

    for track in listing.items:
        listed = await tracks.get_listed_external_item(track.item_id)
        assert listed is not None
        assert listed.to_dict() == track.to_dict()


# ---------------------------------------------------------------------------
# the listing
# ---------------------------------------------------------------------------


async def test_external_tracks_survive_search_sort_and_paging(mass: MusicAssistant) -> None:
    """A user with a hidden source searches, sorts and pages the listing like any other."""
    set_music_source_access(
        mass,
        {FILES: None, HIDDEN: ProviderAccess(owner="user-b", sharing=ProviderSharing.PRIVATE)},
    )
    artist = await _add_artist(mass)
    _load_providers(mass, _source(FILES), _source(HIDDEN))
    answer_listenbrainz(mass)
    artist_id = str(artist.item_id)

    with (
        patch("music_assistant.controllers.music.media.base.get_current_user", return_value=USER),
        patch(
            "music_assistant.controllers.music.media.artists.get_current_user", return_value=USER
        ),
    ):
        top_tracks = mass.music.artists.top_tracks
        everything = await top_tracks(artist_id, "library", limit=0)
        searched = await top_tracks(artist_id, "library", search="ok computer", limit=3)
        by_name = await top_tracks(artist_id, "library", sort_field=SortField.NAME, limit=2)
        second_page = await top_tracks(artist_id, "library", limit=2, offset=2)

    names = [track.name for track in everything]
    assert len(names) == 50
    assert {track.provider for track in everything} == {EXTERNAL_ID_PROVIDER}
    # searched by the name of its album too
    assert [track.name for track in searched] == [
        "Karma Police",
        "Paranoid Android",
        "No Surprises",
    ]
    assert [track.name for track in by_name] == sorted(names, key=str.casefold)[:2]
    assert [track.name for track in second_page] == names[2:4]
