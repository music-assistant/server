"""Tests for finding a track or album by an external id, in the library or on a music provider."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from contextlib import ExitStack
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import (
    ExternalID,
    MediaType,
    ProviderFeature,
    ProviderSharing,
    ProviderType,
)
from music_assistant_models.errors import (
    InvalidDataError,
    InvalidProviderID,
    MediaNotFoundError,
    ProviderUnavailableError,
    RetriesExhausted,
)
from music_assistant_models.media_items import Album, Track

from music_assistant.constants import EXTERNAL_ID_PROVIDER
from music_assistant.controllers.music import MusicController
from music_assistant.helpers.external_ids import external_item_id, normalize_external_id
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.musicbrainz.models import (
    MusicBrainzArtist,
    MusicBrainzArtistCredit,
    MusicBrainzRecording,
    MusicBrainzRelation,
    MusicBrainzRelease,
    MusicBrainzUrl,
)
from tests.common import set_music_source_access

from .helpers import ISRC, create_album, create_track

RECORDING_ID = "9e2ad5bc-c6f9-40d2-a36f-3122ee2072a3"
RELEASE_ID = "6b47c9a0-b9e1-3df9-a5e7-50a6ce0dbdbd"
RELEASE_GROUP_ID = "b1392450-e666-3926-a536-22c65f834433"
OTHER_ISRC = "GBUM71505078"
BARCODE = "634904032463"
SPOTIFY_TRACK_ID = "4uLU6hMCjMI75M1A2tKUQC"
SPOTIFY_TRACK_URL = f"https://open.spotify.com/track/{SPOTIFY_TRACK_ID}"
TIDAL_TRACK_ID = "77646168"
TIDAL_TRACK_URL = f"https://tidal.com/track/{TIDAL_TRACK_ID}"
SPOTIFY_ALBUM_ID = "7eyQXxuf2nGj9d2367Gi5f"
SPOTIFY_ALBUM_URL = f"https://open.spotify.com/album/{SPOTIFY_ALBUM_ID}"
TIDAL_ALBUM_ID = "79280548"

RECORDING_ITEM_ID = external_item_id(ExternalID.MB_RECORDING, RECORDING_ID)
RELEASE_ITEM_ID = external_item_id(ExternalID.MB_ALBUM, RELEASE_ID)

# the owner's private Spotify account, and the member's own accounts
THEIRS = "spotify_theirs"
MINE = "tidal_mine"
MY_SPOTIFY = "spotify_mine"
OWNER = User(user_id="user-owner", username="owner", role=UserRole.USER)
MEMBER = User(user_id="user-member", username="member", role=UserRole.USER)

# every request a music provider can be sent on the way
PROVIDER_REQUESTS = (
    "get_track",
    "get_album",
    "get_track_by_external_id",
    "get_album_by_external_id",
)


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


@pytest.fixture
async def music(mass_minimal: MusicAssistant) -> AsyncGenerator[MusicController]:
    """Return a music controller with a real library and cache, and no providers loaded."""
    controller = MusicController(mass_minimal)
    mass_minimal.music = controller
    mass_minimal.streams = MagicMock()
    mass_minimal.streams.audio_analysis.get_track_audio_metadata = AsyncMock(return_value=None)
    mass_minimal.metadata = MagicMock()
    await mass_minimal.cache._setup_database()
    await controller._setup_database()
    mass_minimal._providers = {}
    yield controller
    if controller._database:
        await controller._database.close()


def _provider(instance_id: str, *features: ProviderFeature) -> MagicMock:
    """Return a streaming provider that has no item and finds none by an external id."""
    provider = MagicMock(spec=MusicProvider)
    provider.instance_id = instance_id
    provider.domain = instance_id.split("_", maxsplit=1)[0]
    provider.name = provider.domain.title()
    provider.type = ProviderType.MUSIC
    provider.available = True
    provider.is_streaming_provider = True
    provider.supported_features = set(features)
    provider.supports_feature = lambda feature: feature in provider.supported_features
    provider.get_track = AsyncMock(side_effect=MediaNotFoundError("gone"))
    provider.get_album = AsyncMock(side_effect=MediaNotFoundError("gone"))
    provider.get_track_by_external_id = AsyncMock(return_value=None)
    provider.get_album_by_external_id = AsyncMock(return_value=None)
    return provider


def _has(item: Track | Album, isrc: str = ISRC) -> AsyncMock:
    """
    Return the external id lookup of a provider that has the item.

    As on a real provider a track is only found by its ISRC and an album by its barcode.

    :param item: The track or album the provider has.
    :param isrc: The ISRC the provider knows a track under.
    """
    known = (
        (ExternalID.ISRC, isrc)
        if isinstance(item, Track)
        else (ExternalID.BARCODE, normalize_external_id(ExternalID.BARCODE, BARCODE))
    )

    async def _lookup(external_id: str, external_id_type: ExternalID) -> Track | Album | None:
        asked = (external_id_type, normalize_external_id(external_id_type, external_id))
        return item if asked == known else None

    return AsyncMock(side_effect=_lookup)


def _track_provider(instance_id: str) -> MagicMock:
    """Return a streaming provider that looks tracks up by ISRC."""
    return _provider(instance_id, ProviderFeature.TRACK_BY_EXTERNAL_ID)


def _album_provider(instance_id: str) -> MagicMock:
    """Return a streaming provider that looks albums up by barcode."""
    return _provider(instance_id, ProviderFeature.ALBUM_BY_EXTERNAL_ID)


def _relations(urls: Sequence[str]) -> list[MusicBrainzRelation]:
    """Return streaming URL relations for the given links."""
    return [
        MusicBrainzRelation(type="free streaming", url=MusicBrainzUrl(resource=url)) for url in urls
    ]


def _credit(name: str) -> list[MusicBrainzArtistCredit]:
    """Return the artist credit of one artist."""
    artist = MusicBrainzArtist(id="a74b1b7f-71a5-4011-9441-d0b5e4122711", name=name, sort_name=name)
    return [MusicBrainzArtistCredit(name=name, artist=artist)]


def _recording(*, isrcs: Sequence[str] = (ISRC,), urls: Sequence[str] = ()) -> MusicBrainzRecording:
    """Return the recording under test, the track the helpers create."""
    return MusicBrainzRecording(
        id=RECORDING_ID,
        title="Test Track",
        artist_credit=_credit("Test Artist"),
        isrcs=list(isrcs),
        relations=_relations(urls),
    )


def _release(*, barcode: str | None = BARCODE, urls: Sequence[str] = ()) -> MusicBrainzRelease:
    """Return the release under test, the album the helpers create."""
    return MusicBrainzRelease(
        id=RELEASE_ID,
        title="Test Album",
        barcode=barcode,
        artist_credit=_credit("Test Artist"),
        relations=_relations(urls),
    )


def _load(
    music: MusicController,
    *providers: MagicMock,
    recording: MusicBrainzRecording | None = None,
    release: MusicBrainzRelease | None = None,
) -> MagicMock:
    """
    Load the music providers and a MusicBrainz provider, which is returned.

    :param music: The music controller under test.
    :param providers: The music providers to load, in the order they are asked.
    :param recording: The recording MusicBrainz knows; none unless given.
    :param release: The release MusicBrainz knows; none unless given.
    """
    unknown = InvalidDataError("unknown id")
    musicbrainz = MagicMock()
    musicbrainz.instance_id = musicbrainz.domain = "musicbrainz"
    musicbrainz.type = ProviderType.METADATA
    musicbrainz.available = True
    musicbrainz.get_recording_details = AsyncMock(
        return_value=recording, side_effect=None if recording else unknown
    )
    musicbrainz.get_release_details = AsyncMock(
        return_value=release, side_effect=None if release else unknown
    )
    music.mass._providers = {
        "musicbrainz": musicbrainz,
        **{provider.instance_id: provider for provider in providers},
    }
    return musicbrainz


def _requests(*providers: MagicMock) -> int:
    """Return how many requests the music providers were sent."""
    return sum(
        getattr(provider, request).await_count
        for provider in providers
        for request in PROVIDER_REQUESTS
    )


def _as_user(user: User | None) -> ExitStack:
    """Run the enclosed block as the given user (None for an internal caller)."""
    stack = ExitStack()
    for module in ("media.base", "controller"):
        stack.enter_context(
            patch(f"music_assistant.controllers.music.{module}.get_current_user", return_value=user)
        )
    return stack


def _private(owner: User) -> ProviderAccess:
    """Return the access record of a source only its owner may use."""
    return ProviderAccess(owner=owner.user_id, sharing=ProviderSharing.PRIVATE)


async def _resolve_recording(music: MusicController) -> Track | None:
    """Look the recording under test up."""
    return await music.tracks.get_item_by_external_id(RECORDING_ID, ExternalID.MB_RECORDING)


async def _resolve_release(music: MusicController) -> Album | None:
    """Look the release under test up."""
    return await music.albums.get_item_by_external_id(RELEASE_ID, ExternalID.MB_ALBUM)


# ---------------------------------------------------------------------------
# track by MusicBrainz recording id
# ---------------------------------------------------------------------------


async def test_recording_in_the_library_costs_no_request(music: MusicController) -> None:
    """A library track that carries the recording id is returned without asking anyone."""
    track = create_track("spotify_1", "t1")
    track.external_ids.add((ExternalID.MB_RECORDING, RECORDING_ID))
    library_track = await music.tracks.add_item_to_library(track)
    spotify = _track_provider("spotify_1")
    musicbrainz = _load(music, spotify, recording=_recording(urls=[SPOTIFY_TRACK_URL]))

    found = await _resolve_recording(music)

    assert found is not None
    assert (found.provider, found.item_id) == ("library", library_track.item_id)
    musicbrainz.get_recording_details.assert_not_awaited()
    assert _requests(spotify) == 0


async def test_recording_is_found_in_the_library_by_its_isrc(music: MusicController) -> None:
    """A library track with an ISRC of the recording costs the MusicBrainz lookup alone."""
    library_track = await music.tracks.add_item_to_library(create_track("spotify_1", "t1"))
    spotify = _track_provider("spotify_1")
    musicbrainz = _load(
        music, spotify, recording=_recording(isrcs=[OTHER_ISRC, ISRC], urls=[SPOTIFY_TRACK_URL])
    )

    found = await _resolve_recording(music)

    assert found is not None
    assert (found.provider, found.item_id) == ("library", library_track.item_id)
    musicbrainz.get_recording_details.assert_awaited_once_with(RECORDING_ID)
    assert _requests(spotify) == 0


async def test_library_track_of_another_name_is_not_taken_for_its_isrc(
    music: MusicController,
) -> None:
    """A library track that carries the ISRC but is another song gives way to the providers."""
    await music.tracks.add_item_to_library(create_track("spotify_1", "t1", name="Another Song"))
    spotify, tidal = _track_provider("spotify_1"), _track_provider("tidal_1")
    tidal.get_track_by_external_id = _has(create_track("tidal_1", "t9"))
    _load(music, spotify, tidal, recording=_recording())

    found = await _resolve_recording(music)

    assert found is not None
    assert (found.provider, found.item_id) == ("tidal_1", "t9")


async def test_recording_is_fetched_through_its_link(music: MusicController) -> None:
    """A recording MusicBrainz links to a service of the user costs one request to it."""
    spotify, tidal = _track_provider("spotify_1"), _track_provider("tidal_1")
    spotify.get_track = AsyncMock(return_value=create_track("spotify_1", SPOTIFY_TRACK_ID))
    musicbrainz = _load(
        music, tidal, spotify, recording=_recording(urls=[TIDAL_TRACK_URL, SPOTIFY_TRACK_URL])
    )

    found = await _resolve_recording(music)

    assert found is not None
    assert (found.provider, found.item_id) == ("spotify_1", SPOTIFY_TRACK_ID)
    musicbrainz.get_recording_details.assert_awaited_once_with(RECORDING_ID)
    spotify.get_track.assert_awaited_once_with(SPOTIFY_TRACK_ID)
    assert _requests(spotify, tidal) == 1


async def test_recording_routes_are_tried_in_order(music: MusicController) -> None:
    """MusicBrainz, the first link, then the ISRC on each provider until one has the track."""
    asked: list[str] = []

    def _answer(name: str, answer: Any) -> AsyncMock:
        async def _record(*_args: object) -> Any:
            asked.append(name)
            if isinstance(answer, Exception):
                raise answer
            return answer

        return AsyncMock(side_effect=_record)

    gone = MediaNotFoundError("gone")
    spotify, tidal, deezer = (
        _track_provider(instance_id) for instance_id in ("spotify_1", "tidal_1", "deezer_1")
    )
    spotify.get_track = _answer("spotify link", gone)
    tidal.get_track = _answer("tidal link", gone)
    spotify.get_track_by_external_id = _answer("spotify isrc", None)
    tidal.get_track_by_external_id = _answer("tidal isrc", create_track("tidal_1", "t9"))
    deezer.get_track_by_external_id = _answer("deezer isrc", create_track("deezer_1", "d9"))
    recording = _recording(isrcs=[OTHER_ISRC, ISRC], urls=[SPOTIFY_TRACK_URL, TIDAL_TRACK_URL])
    musicbrainz = _load(music, spotify, tidal, deezer, recording=recording)
    musicbrainz.get_recording_details = _answer("musicbrainz", recording)

    found = await _resolve_recording(music)

    assert found is not None
    assert (found.provider, found.item_id) == ("tidal_1", "t9")
    assert asked == ["musicbrainz", "spotify link", "spotify isrc", "tidal isrc"]
    # one ISRC of the recording is looked up, so no provider is asked twice
    spotify.get_track_by_external_id.assert_awaited_once_with(OTHER_ISRC, ExternalID.ISRC)


async def test_linked_track_of_another_name_is_not_trusted(music: MusicController) -> None:
    """A link to a track that is not the recording gives way to the ISRC lookup."""
    spotify, tidal = _track_provider("spotify_1"), _track_provider("tidal_1")
    spotify.get_track = AsyncMock(
        return_value=create_track("spotify_1", SPOTIFY_TRACK_ID, name="Another Song")
    )
    tidal.get_track_by_external_id = _has(create_track("tidal_1", "t9"))
    _load(music, spotify, tidal, recording=_recording(urls=[SPOTIFY_TRACK_URL]))

    found = await _resolve_recording(music)

    assert found is not None
    assert (found.provider, found.item_id) == ("tidal_1", "t9")
    spotify.get_track.assert_awaited_once_with(SPOTIFY_TRACK_ID)


async def test_isrc_answer_for_another_song_is_passed_over(music: MusicController) -> None:
    """A provider that answers the ISRC with another song is passed over for the next one."""
    spotify, tidal = _track_provider("spotify_1"), _track_provider("tidal_1")
    spotify.get_track_by_external_id = _has(create_track("spotify_1", "s9", name="Another Song"))
    tidal.get_track_by_external_id = _has(create_track("tidal_1", "t9"))
    _load(music, spotify, tidal, recording=_recording())

    found = await _resolve_recording(music)

    assert found is not None
    assert (found.provider, found.item_id) == ("tidal_1", "t9")


async def test_provider_in_trouble_does_not_stop_the_isrc_lookup(music: MusicController) -> None:
    """A provider that can not answer is passed over for the next one."""
    spotify, tidal = _track_provider("spotify_1"), _track_provider("tidal_1")
    spotify.get_track_by_external_id = AsyncMock(side_effect=RetriesExhausted("rate limited"))
    tidal.get_track_by_external_id = _has(create_track("tidal_1", "t9"))
    _load(music, spotify, tidal, recording=_recording())

    found = await _resolve_recording(music)

    assert found is not None
    assert (found.provider, found.item_id) == ("tidal_1", "t9")
    spotify.get_track_by_external_id.assert_awaited_once()


async def test_found_track_is_returned_as_its_library_track(music: MusicController) -> None:
    """A track found on a provider that the library already holds is the library track."""
    library_track = await music.tracks.add_item_to_library(
        create_track("spotify_1", SPOTIFY_TRACK_ID, isrc=OTHER_ISRC)
    )
    spotify = _track_provider("spotify_1")
    spotify.get_track = AsyncMock(return_value=create_track("spotify_1", SPOTIFY_TRACK_ID))
    _load(music, spotify, recording=_recording(urls=[SPOTIFY_TRACK_URL]))

    found = await _resolve_recording(music)

    assert found is not None
    assert (found.provider, found.item_id) == ("library", library_track.item_id)


async def test_recording_without_isrc_or_link_asks_no_provider(music: MusicController) -> None:
    """A recording that names neither a link nor an ISRC can not be asked for anywhere."""
    spotify = _track_provider("spotify_1")
    _load(music, spotify, recording=_recording(isrcs=["not-an-isrc"]))

    assert await _resolve_recording(music) is None
    assert _requests(spotify) == 0


async def test_recording_musicbrainz_does_not_know_is_not_found(music: MusicController) -> None:
    """A recording id MusicBrainz does not know is not found, with a message of its own."""
    spotify = _track_provider("spotify_1")
    musicbrainz = _load(music, spotify)

    assert await _resolve_recording(music) is None
    with pytest.raises(MediaNotFoundError) as excinfo:
        await music.tracks.get(RECORDING_ITEM_ID, EXTERNAL_ID_PROVIDER)

    assert excinfo.value.translation_key == "track_not_available_on_music_services"
    assert excinfo.value.translation_owner == "core.music"
    assert musicbrainz.get_recording_details.await_count == 2
    assert _requests(spotify) == 0


async def test_recording_needs_the_musicbrainz_provider(music: MusicController) -> None:
    """Without the MusicBrainz provider a recording can not be looked up at all."""
    _load(music, _track_provider("spotify_1"))
    del music.mass._providers["musicbrainz"]

    with pytest.raises(ProviderUnavailableError):
        await _resolve_recording(music)


async def test_malformed_musicbrainz_id_is_looked_up_nowhere(music: MusicController) -> None:
    """An id that can not be a MusicBrainz id is not sent to MusicBrainz or a provider."""
    spotify = _track_provider("spotify_1")
    musicbrainz = _load(music, spotify, recording=_recording())

    found = await music.tracks.get_item_by_external_id(
        f"../isrc/{RECORDING_ID}", ExternalID.MB_RECORDING
    )

    assert found is None
    musicbrainz.get_recording_details.assert_not_awaited()
    assert _requests(spotify) == 0


async def test_hidden_source_is_never_used(music: MusicController) -> None:
    """A library copy, a link and an ISRC lookup on another member's account are passed over."""
    await music.tracks.add_item_to_library(create_track(THEIRS, SPOTIFY_TRACK_ID))
    theirs, mine = _track_provider(THEIRS), _track_provider(MINE)
    theirs.get_track = AsyncMock(return_value=create_track(THEIRS, SPOTIFY_TRACK_ID))
    theirs.get_track_by_external_id = _has(create_track(THEIRS, "s9"))
    mine.get_track_by_external_id = _has(create_track(MINE, "t9"))
    _load(music, theirs, mine, recording=_recording(urls=[SPOTIFY_TRACK_URL]))
    set_music_source_access(music.mass, {THEIRS: _private(OWNER), MINE: _private(MEMBER)})

    with _as_user(MEMBER):
        found = await _resolve_recording(music)

    assert found is not None
    assert (found.provider, found.item_id) == (MINE, "t9")
    assert _requests(theirs) == 0


# ---------------------------------------------------------------------------
# track by ISRC
# ---------------------------------------------------------------------------


async def test_track_by_isrc_asks_the_library_then_the_providers(music: MusicController) -> None:
    """An ISRC is looked up in the library and then on the providers, never on MusicBrainz."""
    spotify, tidal, deezer = (
        _track_provider(instance_id) for instance_id in ("spotify_1", "tidal_1", "deezer_1")
    )
    tidal.get_track_by_external_id = _has(create_track("tidal_1", "t9"))
    musicbrainz = _load(music, spotify, tidal, deezer, recording=_recording())
    uri = f"external://track/{external_item_id(ExternalID.ISRC, ISRC)}"

    found = await music.get_item_by_uri(uri)

    assert (found.provider, found.item_id) == ("tidal_1", "t9")
    spotify.get_track_by_external_id.assert_awaited_once_with(ISRC, ExternalID.ISRC)
    tidal.get_track_by_external_id.assert_awaited_once_with(ISRC, ExternalID.ISRC)
    assert _requests(spotify, tidal, deezer) == 2

    library_track = await music.tracks.add_item_to_library(create_track("qobuz_1", "q1"))
    found = await music.get_item_by_uri(uri)

    assert (found.provider, found.item_id) == ("library", library_track.item_id)
    assert _requests(spotify, tidal, deezer) == 2
    musicbrainz.get_recording_details.assert_not_awaited()


# ---------------------------------------------------------------------------
# album by MusicBrainz release id, release group id and barcode
# ---------------------------------------------------------------------------


async def test_release_in_the_library_costs_no_request(music: MusicController) -> None:
    """A library album that carries the release id is returned without asking anyone."""
    library_album = await music.albums.add_item_to_library(
        create_album("spotify_1", "a1", external_ids={(ExternalID.MB_ALBUM, RELEASE_ID)})
    )
    spotify = _album_provider("spotify_1")
    musicbrainz = _load(music, spotify, release=_release(urls=[SPOTIFY_ALBUM_URL]))

    found = await _resolve_release(music)

    assert found is not None
    assert (found.provider, found.item_id) == ("library", library_album.item_id)
    musicbrainz.get_release_details.assert_not_awaited()
    assert _requests(spotify) == 0


async def test_release_is_fetched_through_its_link(music: MusicController) -> None:
    """A release costs one MusicBrainz lookup and one request to the service it links to."""
    spotify, tidal = _album_provider("spotify_1"), _album_provider("tidal_1")
    spotify.get_album = AsyncMock(return_value=create_album("spotify_1", SPOTIFY_ALBUM_ID))
    musicbrainz = _load(music, spotify, tidal, release=_release(urls=[SPOTIFY_ALBUM_URL]))

    found = await _resolve_release(music)

    assert found is not None
    assert (found.provider, found.item_id) == ("spotify_1", SPOTIFY_ALBUM_ID)
    musicbrainz.get_release_details.assert_awaited_once_with(RELEASE_ID)
    spotify.get_album.assert_awaited_once_with(SPOTIFY_ALBUM_ID)
    assert _requests(spotify, tidal) == 1


async def test_release_without_a_working_link_is_found_by_its_barcode(
    music: MusicController,
) -> None:
    """A release whose link is stale is looked up on the providers by its barcode."""
    spotify, tidal = _album_provider("spotify_1"), _album_provider("tidal_1")
    tidal_album = create_album("tidal_1", TIDAL_ALBUM_ID)
    tidal.get_album_by_external_id = _has(tidal_album)
    tidal.get_album = AsyncMock(return_value=tidal_album)
    _load(music, spotify, tidal, release=_release(urls=[SPOTIFY_ALBUM_URL]))

    found = await _resolve_release(music)

    assert found is not None
    assert (found.provider, found.item_id) == ("tidal_1", TIDAL_ALBUM_ID)
    spotify.get_album.assert_awaited_once_with(SPOTIFY_ALBUM_ID)
    tidal.get_album_by_external_id.assert_awaited_once_with(BARCODE, ExternalID.BARCODE)


async def test_release_no_provider_has_is_not_found(music: MusicController) -> None:
    """A release none of the providers has, or MusicBrainz does not know, is not found."""
    spotify = _album_provider("spotify_1")
    _load(music, spotify, release=_release(urls=[SPOTIFY_ALBUM_URL]))

    assert await _resolve_release(music) is None
    with pytest.raises(MediaNotFoundError) as excinfo:
        await music.albums.get(RELEASE_ITEM_ID, EXTERNAL_ID_PROVIDER)
    assert excinfo.value.translation_key == "album_not_available_on_music_services"
    assert excinfo.value.translation_owner == "core.music"

    _load(music, spotify)
    assert await _resolve_release(music) is None


async def test_release_group_takes_the_release_group_route(music: MusicController) -> None:
    """A release group id, in the external form or the MusicBrainz one, is resolved one way."""
    album = create_album("spotify_1", SPOTIFY_ALBUM_ID)
    _load(music, _album_provider("spotify_1"))
    item_id = external_item_id(ExternalID.MB_RELEASEGROUP, RELEASE_GROUP_ID)

    with patch.object(
        music.albums, "resolve_musicbrainz_release_group", AsyncMock(return_value=album)
    ) as resolve:
        assert await music.get_item_by_uri(f"external://album/{item_id}") is album
        assert await music.get_item_by_uri(f"musicbrainz://album/{RELEASE_GROUP_ID}") is album

    assert [call.args for call in resolve.await_args_list] == [(RELEASE_GROUP_ID,)] * 2

    not_found = MediaNotFoundError("no provider has it")
    with patch.object(
        music.albums, "resolve_musicbrainz_release_group", AsyncMock(side_effect=not_found)
    ):
        assert (
            await music.albums.get_item_by_external_id(RELEASE_GROUP_ID, ExternalID.MB_RELEASEGROUP)
            is None
        )
        with pytest.raises(MediaNotFoundError) as excinfo:
            await music.albums.get(item_id, EXTERNAL_ID_PROVIDER)
    assert excinfo.value.translation_key == "album_not_available_on_music_services"


async def test_album_by_barcode_asks_the_library_then_the_providers(
    music: MusicController,
) -> None:
    """A barcode is looked up in the library and then on the providers, never on MusicBrainz."""
    spotify, tidal, deezer = (
        _album_provider(instance_id) for instance_id in ("spotify_1", "tidal_1", "deezer_1")
    )
    tidal.get_album_by_external_id = _has(create_album("tidal_1", TIDAL_ALBUM_ID))
    musicbrainz = _load(music, spotify, tidal, deezer, release=_release())
    item_id = external_item_id(ExternalID.BARCODE, BARCODE)

    found = await music.get_item(MediaType.ALBUM, item_id, EXTERNAL_ID_PROVIDER)

    assert (found.provider, found.item_id) == ("tidal_1", TIDAL_ALBUM_ID)
    assert _requests(spotify, tidal, deezer) == 2

    library_album = await music.albums.add_item_to_library(
        create_album("qobuz_1", "q1", external_ids={(ExternalID.BARCODE, BARCODE)})
    )
    found = await music.get_item(MediaType.ALBUM, item_id, EXTERNAL_ID_PROVIDER)

    assert (found.provider, found.item_id) == ("library", library_album.item_id)
    assert _requests(spotify, tidal, deezer) == 2
    musicbrainz.get_release_details.assert_not_awaited()


# ---------------------------------------------------------------------------
# the external item, opened through get_item and get
# ---------------------------------------------------------------------------


async def test_external_item_is_opened_through_get_item_and_get(music: MusicController) -> None:
    """An external item resolves by its uri, through the item command and the track command."""
    spotify = _track_provider("spotify_1")
    spotify.get_track = AsyncMock(return_value=create_track("spotify_1", SPOTIFY_TRACK_ID))
    _load(music, spotify, recording=_recording(urls=[SPOTIFY_TRACK_URL]))

    for found in (
        await music.get_item_by_uri(f"external://track/{RECORDING_ITEM_ID}"),
        await music.get_item(MediaType.TRACK, RECORDING_ITEM_ID, EXTERNAL_ID_PROVIDER),
        await music.tracks.get(RECORDING_ITEM_ID, EXTERNAL_ID_PROVIDER),
    ):
        assert isinstance(found, Track)
        assert (found.provider, found.item_id) == ("spotify_1", SPOTIFY_TRACK_ID)


async def test_opening_an_external_library_item_refreshes_its_metadata(
    music: MusicController,
) -> None:
    """A library item opened by an external id gets its metadata refreshed like any other."""
    track = create_track("spotify_1", "t1")
    track.external_ids.add((ExternalID.MB_RECORDING, RECORDING_ID))
    await music.tracks.add_item_to_library(track)
    _load(music)
    schedule = cast("MagicMock", music.mass.metadata.schedule_update_metadata)

    await music.tracks.get(RECORDING_ITEM_ID, EXTERNAL_ID_PROVIDER, allow_update_metadata=False)
    schedule.assert_not_called()

    found = await music.tracks.get(RECORDING_ITEM_ID, EXTERNAL_ID_PROVIDER)
    schedule.assert_called_once_with(found)


@pytest.mark.parametrize("item_id", ["isrc", f"spotify:{SPOTIFY_TRACK_ID}", "isrc:not-an-isrc"])
async def test_malformed_external_item_id_is_refused(music: MusicController, item_id: str) -> None:
    """An item id that names no external id is refused before anything is looked up."""
    spotify = _track_provider("spotify_1")
    _load(music, spotify)

    with pytest.raises(InvalidProviderID):
        await music.get_item(MediaType.TRACK, item_id, EXTERNAL_ID_PROVIDER)

    assert _requests(spotify) == 0


# ---------------------------------------------------------------------------
# remembering what an external id was found as
# ---------------------------------------------------------------------------


async def test_found_item_is_remembered(music: MusicController) -> None:
    """What an id was found as is fetched by its own id the next time, with no lookup."""
    spotify, tidal = _track_provider("spotify_1"), _track_provider("tidal_1")
    tidal_track = create_track("tidal_1", "t9")
    tidal.get_track_by_external_id = _has(tidal_track)
    tidal.get_track = AsyncMock(return_value=tidal_track)
    musicbrainz = _load(music, spotify, tidal, recording=_recording())

    assert await _resolve_recording(music) is tidal_track
    assert _requests(spotify, tidal) == 2

    assert await _resolve_recording(music) is tidal_track

    musicbrainz.get_recording_details.assert_awaited_once()
    spotify.get_track_by_external_id.assert_awaited_once()
    tidal.get_track_by_external_id.assert_awaited_once()
    tidal.get_track.assert_awaited_once_with("t9")


async def test_remembered_item_added_to_the_library_is_the_library_item(
    music: MusicController,
) -> None:
    """Once the remembered item is in the library, it is returned from there."""
    spotify = _track_provider("spotify_1")
    spotify_track = create_track("spotify_1", SPOTIFY_TRACK_ID, isrc=OTHER_ISRC)
    spotify.get_track = AsyncMock(return_value=spotify_track)
    _load(music, spotify, recording=_recording(urls=[SPOTIFY_TRACK_URL]))

    assert await _resolve_recording(music) is spotify_track
    library_track = await music.tracks.add_item_to_library(spotify_track)
    found = await _resolve_recording(music)

    assert found is not None
    assert (found.provider, found.item_id) == ("library", library_track.item_id)
    spotify.get_track.assert_awaited_once()


async def test_remembered_item_the_service_dropped_is_looked_up_again(
    music: MusicController,
) -> None:
    """A remembered item its service no longer has is forgotten and the id looked up again."""
    spotify, tidal = _track_provider("spotify_1"), _track_provider("tidal_1")
    tidal_track = create_track("tidal_1", "t9")
    tidal.get_track_by_external_id = _has(tidal_track)
    musicbrainz = _load(music, spotify, tidal, recording=_recording())
    assert await _resolve_recording(music) is tidal_track

    # Tidal dropped the track and no service has it: the id is looked up again and not found
    tidal.get_track_by_external_id = AsyncMock(return_value=None)
    assert await _resolve_recording(music) is None
    tidal.get_track.assert_awaited_once_with("t9")
    assert musicbrainz.get_recording_details.await_count == 2

    # what Tidal dropped is forgotten, so it is not asked for again
    assert await _resolve_recording(music) is None
    tidal.get_track.assert_awaited_once()

    # and once Spotify has the track, that is what is remembered
    spotify_track = create_track("spotify_1", "s9")
    spotify.get_track_by_external_id = _has(spotify_track)
    spotify.get_track = AsyncMock(return_value=spotify_track)
    assert await _resolve_recording(music) is spotify_track
    assert await _resolve_recording(music) is spotify_track
    spotify.get_track.assert_awaited_once_with("s9")
    assert musicbrainz.get_recording_details.await_count == 4


async def test_remembered_item_is_served_through_the_users_own_account(
    music: MusicController,
) -> None:
    """What one member found is fetched for another member through that member's account."""
    theirs, my_spotify = _track_provider(THEIRS), _track_provider(MY_SPOTIFY)
    theirs.get_track = AsyncMock(return_value=create_track(THEIRS, SPOTIFY_TRACK_ID))
    my_spotify.get_track = AsyncMock(return_value=create_track(MY_SPOTIFY, SPOTIFY_TRACK_ID))
    musicbrainz = _load(music, theirs, my_spotify, recording=_recording(urls=[SPOTIFY_TRACK_URL]))
    set_music_source_access(music.mass, {THEIRS: _private(OWNER), MY_SPOTIFY: _private(MEMBER)})

    with _as_user(OWNER):
        found = await _resolve_recording(music)
    assert found is not None
    assert found.provider == THEIRS

    with _as_user(MEMBER):
        found = await _resolve_recording(music)

    assert found is not None
    assert found.provider == MY_SPOTIFY
    theirs.get_track.assert_awaited_once()
    my_spotify.get_track.assert_awaited_once_with(SPOTIFY_TRACK_ID)
    musicbrainz.get_recording_details.assert_awaited_once()


async def test_remembered_item_on_a_hidden_service_is_not_handed_out(
    music: MusicController,
) -> None:
    """What one member found on a service another member has no account of is looked up anew."""
    theirs, mine = _track_provider(THEIRS), _track_provider(MINE)
    theirs.get_track = AsyncMock(return_value=create_track(THEIRS, SPOTIFY_TRACK_ID))
    mine.get_track_by_external_id = _has(create_track(MINE, "t9"))
    _load(music, theirs, mine, recording=_recording(urls=[SPOTIFY_TRACK_URL]))
    set_music_source_access(music.mass, {THEIRS: _private(OWNER), MINE: _private(MEMBER)})

    with _as_user(OWNER):
        found = await _resolve_recording(music)
    assert found is not None
    assert found.provider == THEIRS

    with _as_user(MEMBER):
        found = await _resolve_recording(music)

    assert found is not None
    assert (found.provider, found.item_id) == (MINE, "t9")
    assert _requests(theirs) == 1
