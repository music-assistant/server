"""
Tests for tracks known only by an external id in the player queue.

Such a track is enqueued as it is, without a lookup, and is looked up on the music providers
when the queue loads it: one lookup, for the item that is loaded, on the music sources of the
user the queue plays for.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable, Iterator
from contextlib import contextmanager
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock, patch

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
    QueueOption,
)
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    MusicAssistantError,
    RateLimited,
    RetriesExhausted,
)
from music_assistant_models.media_items import (
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    ProviderMapping,
    Track,
    UniqueList,
)
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem, QueueItemOrigin

from music_assistant.constants import EXTERNAL_ID_PROVIDER
from music_assistant.controllers.music import MusicController
from music_assistant.controllers.music.constants import CACHE_CATEGORY_EXTERNAL_IDS
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.helpers import build_queue_item
from music_assistant.controllers.player_queues.managed_pool import ManagedPool
from music_assistant.controllers.player_queues.media_resolver import ResolvedItem
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    set_current_user,
    set_impersonated_user,
    user_auth_context,
)
from music_assistant.helpers.external_ids import external_item_id
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.musicbrainz.models import (
    MusicBrainzArtist,
    MusicBrainzArtistCredit,
    MusicBrainzRecording,
    MusicBrainzRelation,
    MusicBrainzUrl,
)
from tests.common import set_music_source_access
from tests.controllers.music.helpers import ISRC, create_album

GET_CURRENT_USER = "music_assistant.controllers.player_queues.queue_loader.get_current_user"
QUEUE_ID = "q1"
LIST_LENGTH = 50
RECORDING_IDS = [f"9e2ad5bc-c6f9-40d2-a36f-{index:012d}" for index in range(LIST_LENGTH)]
RECORDING_ID = RECORDING_IDS[0]
RELEASE_ID = "6b47c9a0-b9e1-3df9-a5e7-50a6ce0dbdbd"
SPOTIFY_TRACK_ID = "4uLU6hMCjMI75M1A2tKUQC"
SPOTIFY_TRACK_URL = f"https://open.spotify.com/track/{SPOTIFY_TRACK_ID}"

# the owner's private Spotify account, the member's own Tidal account and a Tidal account
# the owner shares with everyone
THEIRS = "spotify--theirs"
MINE = "tidal--mine"
SHARED = "tidal--shared"
OWNER = User(user_id="user-owner", username="owner", role=UserRole.USER)
MEMBER = User(user_id="user-member", username="member", role=UserRole.USER)
USERS = {user.user_id: user for user in (OWNER, MEMBER)}

# a provider that could not answer, which says nothing about whether it has the track
COULD_NOT_ANSWER = [
    RetriesExhausted("Not retrying, asked to wait 3600 seconds"),
    RateLimited("slow down", backoff_time=3600),
]
# every request a music provider can be sent while a track is looked up or its details fetched
PROVIDER_REQUESTS = (
    "get_track",
    "get_album",
    "get_artist",
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


def _provider(instance_id: str) -> MagicMock:
    """Return a streaming provider that looks tracks up by ISRC and has none."""
    provider = MagicMock(spec=MusicProvider)
    provider.instance_id = instance_id
    provider.domain = instance_id.split("--", maxsplit=1)[0]
    provider.name = provider.domain.title()
    provider.type = ProviderType.MUSIC
    provider.available = True
    provider.is_streaming_provider = True
    provider.supported_features = {ProviderFeature.TRACK_BY_EXTERNAL_ID}
    provider.supports_feature = lambda feature: feature in provider.supported_features
    gone = MediaNotFoundError("gone")
    provider.get_track = AsyncMock(side_effect=gone)
    provider.get_album = AsyncMock(side_effect=gone)
    provider.get_artist = AsyncMock(side_effect=gone)
    provider.get_track_by_external_id = AsyncMock(return_value=None)
    provider.get_album_by_external_id = AsyncMock(return_value=None)
    return provider


def _provider_track(
    instance_id: str, item_id: str, name: str = "Test Track", image: bool = True
) -> Track:
    """
    Return a track as a provider serves it, with an artist and an album of that provider.

    :param instance_id: The provider instance the track, its artist and its album are on.
    :param item_id: The item id of the track on that provider.
    :param name: The name of the track.
    :param image: Whether the track comes with a cover.
    """

    def _mapping(mapped_item_id: str) -> ProviderMapping:
        return ProviderMapping(
            item_id=mapped_item_id,
            provider_domain=instance_id.split("--", maxsplit=1)[0],
            provider_instance=instance_id,
            audio_format=AudioFormat(),
        )

    track = Track(
        item_id=item_id,
        provider=instance_id,
        name=name,
        duration=215,
        external_ids={(ExternalID.ISRC, ISRC)},
        provider_mappings={_mapping(item_id)},
        artists=UniqueList(
            [
                Artist(
                    item_id=f"{item_id}_artist",
                    provider=instance_id,
                    name="Test Artist",
                    provider_mappings={_mapping(f"{item_id}_artist")},
                )
            ]
        ),
        album=ItemMapping(
            item_id=f"{item_id}_album",
            provider=instance_id,
            name="Album",
            media_type=MediaType.ALBUM,
        ),
    )
    if image:
        track.metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=f"https://covers.example/{item_id}.jpg",
                provider=instance_id,
                remotely_accessible=True,
            )
        )
    return track


def _recording(recording_id: str, linked: bool = False) -> MusicBrainzRecording:
    """Return a recording of the track the helpers create, known by the ISRC they give it."""
    artist = MusicBrainzArtist(
        id="a74b1b7f-71a5-4011-9441-d0b5e4122711", name="Test Artist", sort_name="Test Artist"
    )
    relation = MusicBrainzRelation(
        type="free streaming", url=MusicBrainzUrl(resource=SPOTIFY_TRACK_URL)
    )
    return MusicBrainzRecording(
        id=recording_id,
        title="Test Track",
        artist_credit=[MusicBrainzArtistCredit(name="Test Artist", artist=artist)],
        isrcs=[ISRC],
        relations=[relation] if linked else [],
    )


def _load_providers(music: MusicController, *providers: MagicMock, linked: bool = False) -> Mock:
    """
    Load the music providers and a MusicBrainz provider, which is returned.

    :param music: The music controller under test.
    :param providers: The music providers to load, in the order they are asked.
    :param linked: Whether MusicBrainz links every recording to the track on Spotify.
    """
    musicbrainz = MagicMock()
    musicbrainz.instance_id = musicbrainz.domain = "musicbrainz"
    musicbrainz.type = ProviderType.METADATA
    musicbrainz.available = True

    async def _recording_details(recording_id: str) -> MusicBrainzRecording:
        # a request takes a moment, in which another load of the same item can start
        await asyncio.sleep(0)
        return _recording(recording_id, linked)

    musicbrainz.get_recording_details = AsyncMock(side_effect=_recording_details)
    musicbrainz.get_release_details = AsyncMock(side_effect=InvalidDataError("unknown id"))
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


def _item_id(recording_id: str = RECORDING_ID) -> str:
    """Return the item id of the external track of a recording."""
    return external_item_id(ExternalID.MB_RECORDING, recording_id)


def _uri(recording_id: str = RECORDING_ID) -> str:
    """Return the uri of the external track of a recording."""
    return f"{EXTERNAL_ID_PROVIDER}://track/{_item_id(recording_id)}"


def _external_track(recording_id: str = RECORDING_ID, name: str = "Listed Track") -> Track:
    """Return a track known only by its recording id, as a listing would hold it."""
    return Track(
        item_id=_item_id(recording_id),
        provider=EXTERNAL_ID_PROVIDER,
        name=name,
        duration=200,
        artists=UniqueList(
            [
                ItemMapping(
                    item_id="1", provider="library", name="Listed", media_type=MediaType.ARTIST
                )
            ]
        ),
        external_ids={(ExternalID.MB_RECORDING, recording_id)},
        provider_mappings=set(),
    )


def _controller(music: MusicController, userid: str | None = None) -> Any:
    """
    Build a bare queue controller on a single queue, over the real music controller.

    A play request ends up as the requested items on the queue, and nothing is started:
    loading an item is left to the test.

    :param music: The music controller the queue looks its tracks up with.
    :param userid: The user the queue plays for; None for anonymous playback.
    """
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = Mock()
    ctrl.mass = MagicMock()
    ctrl.mass.music = music
    ctrl.mass.config = music.mass.config
    ctrl.mass.providers = []
    ctrl.mass.player_queues = ctrl
    ctrl.mass.create_task = music.mass.create_task
    ctrl.mass.webserver.auth.get_user = AsyncMock(side_effect=USERS.get)
    ctrl.mass.streams.audio.get_stream_details = AsyncMock(return_value=MagicMock(duration=None))
    ctrl.mass.players.get_player = Mock(
        return_value=Mock(extra_data={}, offline_by_power_control=False)
    )
    lock_cm = MagicMock()
    lock_cm.__aenter__ = AsyncMock(return_value=None)
    lock_cm.__aexit__ = AsyncMock(return_value=None)
    ctrl.mass.players.get_group_and_player_lock = Mock(return_value=lock_cm)
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.on_player_update = Mock()  # type: ignore[method-assign]
    ctrl.play_index = AsyncMock()  # type: ignore[method-assign]
    ctrl.get_config_value = Mock(return_value=QueueOption.REPLACE.value)  # type: ignore[method-assign]
    ctrl._managed_pool = Mock()
    ctrl._smart_shuffle = Mock()
    ctrl._smart_shuffle.is_enabled = Mock(return_value=False)
    ctrl._media_resolver = Mock()
    ctrl._media_resolver._resolve_media_items = AsyncMock(
        side_effect=lambda media_item, *_args, **_kwargs: [ResolvedItem(media_item)]
    )
    queue = PlayerQueue(queue_id=QUEUE_ID, active=True, display_name="Q1", available=True, items=0)
    ctrl._queue_data = {QUEUE_ID: PlayerQueueData(queue=queue, userid=userid)}
    return ctrl


def _queue(ctrl: Any, *tracks: Track, origin: QueueItemOrigin | None = None) -> list[QueueItem]:
    """Put the tracks on the queue, the first one as the current item, and return its items."""
    queue_data = cast("PlayerQueueData", ctrl._queue_data[QUEUE_ID])
    queue_data.items = [build_queue_item(QUEUE_ID, track, origin=origin) for track in tracks]
    queue_data.enqueued_media_items = list(tracks)
    queue_data.queue.items = len(tracks)
    queue_data.queue.current_index = 0
    queue_data.queue.current_item = queue_data.items[0]
    return queue_data.items


def _media_items(ctrl: Any) -> list[Track]:
    """Return the media items of the queue, in order."""
    return [cast("Track", item.media_item) for item in ctrl._queue_data[QUEUE_ID].items]


@contextmanager
def _recorded_fetches(music: MusicController) -> Iterator[list[tuple[str, User | None]]]:
    """Record the uri of every item that is fetched with its details, and the user it is for."""
    fetches: list[tuple[str, User | None]] = []
    get_item_by_uri = music.get_item_by_uri

    async def _record(uri: str) -> Any:
        fetches.append((uri, get_current_user()))
        return await get_item_by_uri(uri)

    with patch.object(music, "get_item_by_uri", AsyncMock(side_effect=_record)):
        yield fetches


def _members_private_source(music: MusicController, *tracks: Track) -> MagicMock:
    """Load the member's private account, which is returned, holding the given tracks."""
    mine = _provider(MINE)
    by_id = {track.item_id: track for track in tracks}
    mine.get_track = AsyncMock(side_effect=lambda item_id: by_id[item_id])
    _load_providers(music, mine)
    set_music_source_access(
        music.mass,
        {MINE: ProviderAccess(owner=MEMBER.user_id, sharing=ProviderSharing.PRIVATE)},
    )
    return mine


async def _remembered(music: MusicController, recording_id: str = RECORDING_ID) -> Any:
    """Return what is kept as the track a recording was found as."""
    return await music.mass.cache.get(
        music.tracks._external_id_cache_key(recording_id, ExternalID.MB_RECORDING),
        provider=music.domain,
        category=CACHE_CATEGORY_EXTERNAL_IDS,
    )


# ---------------------------------------------------------------------------
# enqueueing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry_form", ["bare uris", "objects", "item mappings"])
async def test_playing_a_list_looks_nothing_up_until_an_item_is_loaded(
    music: MusicController, entry_form: str
) -> None:
    """A list of external tracks costs no request when played, and one lookup per loaded item."""
    spotify = _provider("spotify--1")
    spotify.get_track = AsyncMock(
        side_effect=lambda item_id: _provider_track("spotify--1", item_id)
    )
    musicbrainz = _load_providers(music, spotify, linked=True)
    ctrl = _controller(music)
    entry_of: dict[str, Callable[[str], Any]] = {
        "bare uris": _uri,
        "objects": _external_track,
        "item mappings": lambda recording_id: ItemMapping.from_item(_external_track(recording_id)),
    }
    entries = [entry_of[entry_form](recording_id) for recording_id in RECORDING_IDS]

    await ctrl.play_media(QUEUE_ID, entries, QueueOption.REPLACE)

    queued = _media_items(ctrl)
    assert [track.item_id for track in queued] == [_item_id(x) for x in RECORDING_IDS]
    assert {track.provider for track in queued} == {EXTERNAL_ID_PROVIDER}
    musicbrainz.get_recording_details.assert_not_awaited()
    assert _requests(spotify) == 0

    # loading one item, the fourth, looks up that item alone
    items = ctrl._queue_data[QUEUE_ID].items
    await ctrl._load_item(items[3])

    musicbrainz.get_recording_details.assert_awaited_once_with(RECORDING_IDS[3])
    spotify.get_track.assert_awaited_once_with(SPOTIFY_TRACK_ID)
    assert _requests(spotify) == 1
    assert [track.provider for track in _media_items(ctrl)] == [
        "spotify--1" if index == 3 else EXTERNAL_ID_PROVIDER for index in range(LIST_LENGTH)
    ]


async def test_bare_uri_is_enqueued_in_the_form_it_was_listed_in(music: MusicController) -> None:
    """A uri the server listed a track for plays as that track, another as a placeholder."""
    _load_providers(music)
    ctrl = _controller(music)
    await music.tracks.store_listed_external_item(_external_track(RECORDING_IDS[0]))

    await ctrl.play_media(
        QUEUE_ID, [_uri(RECORDING_IDS[0]), _uri(RECORDING_IDS[1])], QueueOption.REPLACE
    )

    listed, placeholder = ctrl._queue_data[QUEUE_ID].items
    assert listed.media_item == _external_track(RECORDING_IDS[0])
    assert listed.name == "Listed - Listed Track"
    assert listed.duration == 200
    # nothing is known of the other one but its id, which names it
    assert placeholder.media_item.uri == _uri(RECORDING_IDS[1])
    assert placeholder.media_item.provider == EXTERNAL_ID_PROVIDER
    assert placeholder.media_item.name == RECORDING_IDS[1]
    assert placeholder.media_item.external_ids == {(ExternalID.MB_RECORDING, RECORDING_IDS[1])}
    assert placeholder.media_item.provider_mappings == set()


@patch(GET_CURRENT_USER)
async def test_external_track_is_enqueued_for_a_user_with_own_music_sources(
    mock_get_user: Mock, music: MusicController
) -> None:
    """A track that is on no music source yet is not refused for the sources of the user."""
    mock_get_user.return_value = MEMBER
    _load_providers(music)
    set_music_source_access(
        music.mass,
        {
            THEIRS: ProviderAccess(owner=OWNER.user_id, sharing=ProviderSharing.PRIVATE),
            MINE: ProviderAccess(owner=MEMBER.user_id, sharing=ProviderSharing.PRIVATE),
        },
    )
    ctrl = _controller(music)

    await ctrl.play_media(QUEUE_ID, [_external_track(), _uri(RECORDING_IDS[1])])

    assert [track.item_id for track in _media_items(ctrl)] == [
        _item_id(RECORDING_IDS[0]),
        _item_id(RECORDING_IDS[1]),
    ]
    assert ctrl._queue_data[QUEUE_ID].userid == MEMBER.user_id


async def test_malformed_external_uri_is_not_enqueued(music: MusicController) -> None:
    """A uri that names no external id is refused when it is played."""
    _load_providers(music)
    ctrl = _controller(music)

    with pytest.raises(MusicAssistantError):
        await ctrl.play_media(QUEUE_ID, f"{EXTERNAL_ID_PROVIDER}://track/isrc:not-an-isrc")

    assert ctrl._queue_data[QUEUE_ID].items == []


async def test_external_album_and_radio_mode_are_resolved_at_once(music: MusicController) -> None:
    """An album, and the seed of a radio mode request, is looked up when it is played."""
    _load_providers(music)
    ctrl = _controller(music)
    album_uri = f"{EXTERNAL_ID_PROVIDER}://album/musicbrainz_albumid:{RELEASE_ID}"
    album = create_album("spotify_1", "a1")
    seed_track = _provider_track("spotify--1", "t1")
    # what the album and the radio expand to is of no concern here
    ctrl._media_resolver._resolve_media_items = AsyncMock(
        return_value=[ResolvedItem(_provider_track("spotify--1", "s1"))]
    )

    with patch.object(music, "get_item_by_uri", AsyncMock(return_value=album)) as get_item:
        await ctrl.play_media(QUEUE_ID, album_uri, QueueOption.REPLACE)
    get_item.assert_awaited_once_with(album_uri)
    ctrl._media_resolver._resolve_media_items.assert_awaited_once()
    assert ctrl._media_resolver._resolve_media_items.await_args.args[0] is album

    with patch.object(music, "get_item_by_uri", AsyncMock(return_value=seed_track)) as get_item:
        await ctrl.play_media(QUEUE_ID, [_uri(), _external_track()], radio_mode=True)
    assert [call.args for call in get_item.await_args_list] == [
        (f"radio_playlist://playlist/{_uri()}",)
    ] * 2


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------


async def test_found_track_replaces_the_external_track(music: MusicController) -> None:
    """The queue item keeps its id and origin; what it shows follows the track that was found."""
    spotify, tidal = _provider("spotify--1"), _provider("tidal--1")
    found = _provider_track("spotify--1", SPOTIFY_TRACK_ID, name="Test Track")
    spotify.get_track = AsyncMock(return_value=found)
    musicbrainz = _load_providers(music, spotify, tidal, linked=True)
    ctrl = _controller(music)
    container = ItemMapping(
        item_id="7", provider="library", name="Artist", media_type=MediaType.ARTIST
    )
    origin = QueueItemOrigin(container=container)
    external_track = _external_track()
    (item,) = _queue(ctrl, external_track, origin=origin)
    queue_item_id = item.queue_item_id

    await ctrl._load_item(item)

    assert item.media_item is found
    assert item.queue_item_id == queue_item_id
    assert item.origin is origin
    assert item.name == "Test Artist - Test Track"
    assert item.duration == 215
    assert item.image is not None
    assert item.image.path == f"https://covers.example/{SPOTIFY_TRACK_ID}.jpg"
    # the play of it still counts as picked by the user, and clients get the changed item
    assert ctrl._queue_data[QUEUE_ID].enqueued_media_items == [found]
    ctrl.signal_update.assert_called_with(QUEUE_ID, items_changed=True)
    # the stream is asked for the track that was found
    stream_details = cast("AsyncMock", ctrl.mass.streams.audio.get_stream_details)
    stream_details.assert_awaited_once()
    assert stream_details.await_args_list[0].kwargs["queue_item"].media_item is found
    # one lookup and one fetch of the track, none of its album or artists
    musicbrainz.get_recording_details.assert_awaited_once_with(RECORDING_ID)
    spotify.get_track.assert_awaited_once_with(SPOTIFY_TRACK_ID)
    spotify.get_album.assert_not_awaited()
    spotify.get_artist.assert_not_awaited()
    assert _requests(spotify, tidal) == 1


async def test_track_in_the_library_is_played_from_the_library(music: MusicController) -> None:
    """An external track the library holds costs no request and plays as the library track."""
    track = _provider_track("spotify--1", "t1")
    track.external_ids.add((ExternalID.MB_RECORDING, RECORDING_ID))
    library_track = await music.tracks.add_item_to_library(track)
    spotify = _provider("spotify--1")
    musicbrainz = _load_providers(music, spotify)
    ctrl = _controller(music)
    (item,) = _queue(ctrl, _external_track())

    await ctrl._load_item(item)

    assert item.media_item is not None
    assert (item.media_item.provider, item.media_item.item_id) == (
        "library",
        library_track.item_id,
    )
    musicbrainz.get_recording_details.assert_not_awaited()
    assert _requests(spotify) == 0


async def test_track_no_service_has_is_skipped(music: MusicController) -> None:
    """An external track no music provider has is skipped and the queue moves on."""
    spotify = _provider("spotify--1")
    found = _provider_track("spotify--1", "s9")

    async def _lookup(_isrc: str, _external_id_type: ExternalID) -> Track | None:
        return found

    musicbrainz = _load_providers(music, spotify)
    # MusicBrainz does not know the second recording, so nothing can be asked for it
    recordings = {RECORDING_IDS[2]: _recording(RECORDING_IDS[2])}

    def _recording_details(recording_id: str) -> MusicBrainzRecording:
        if recording := recordings.get(recording_id):
            return recording
        raise InvalidDataError("unknown id")

    musicbrainz.get_recording_details = AsyncMock(side_effect=_recording_details)
    spotify.get_track_by_external_id = AsyncMock(side_effect=_lookup)
    ctrl = _controller(music)
    current, missing, available = _queue(
        ctrl,
        _provider_track("spotify--1", "s1"),
        _external_track(RECORDING_IDS[1]),
        _external_track(RECORDING_IDS[2]),
    )

    with pytest.raises(MediaNotFoundError) as excinfo:
        await ctrl._load_item(missing)
    assert excinfo.value.translation_key == "track_not_available_on_music_services"
    ctrl.mass.streams.audio.get_stream_details.assert_not_awaited()

    next_item = await ctrl.load_next_queue_item(QUEUE_ID, current.queue_item_id)

    assert next_item is available
    assert next_item.media_item is found
    assert missing.available is False
    assert cast("Track", missing.media_item).provider == EXTERNAL_ID_PROVIDER


@pytest.mark.parametrize("error", COULD_NOT_ANSWER)
async def test_service_that_could_not_answer_leaves_the_track_available(
    music: MusicController, error: MusicAssistantError
) -> None:
    """A rate limited provider stops the load with its error and condemns nothing."""
    spotify, tidal = _provider("spotify--1"), _provider("tidal--1")
    spotify.get_track_by_external_id = AsyncMock(side_effect=error)
    _load_providers(music, spotify, tidal)
    ctrl = _controller(music)
    current, limited, following = _queue(
        ctrl,
        _provider_track("spotify--1", "s1"),
        _external_track(RECORDING_IDS[1]),
        _external_track(RECORDING_IDS[2]),
    )

    with pytest.raises(type(error)) as excinfo:
        await ctrl._load_item(limited)
    assert excinfo.value is error
    ctrl.mass.streams.audio.get_stream_details.assert_not_awaited()

    # the next item is not tried in its place, as for a rate limited stream
    with pytest.raises(type(error)):
        await ctrl.load_next_queue_item(QUEUE_ID, current.queue_item_id)

    assert limited.available is True
    assert cast("Track", limited.media_item).provider == EXTERNAL_ID_PROVIDER
    assert cast("Track", following.media_item).provider == EXTERNAL_ID_PROVIDER
    assert await _remembered(music, RECORDING_IDS[1]) is None
    assert await _remembered(music, RECORDING_IDS[2]) is None

    # once the provider answers again the track is found and plays
    found = _provider_track("spotify--1", "s9")
    spotify.get_track_by_external_id = AsyncMock(return_value=found)
    assert await ctrl.load_next_queue_item(QUEUE_ID, current.queue_item_id) is limited
    assert limited.media_item is found


@pytest.mark.parametrize("acting_as_owner", [False, True], ids=["as itself", "acting as"])
async def test_track_is_looked_up_on_the_sources_of_the_queue_user(
    music: MusicController, acting_as_owner: bool
) -> None:
    """The track comes from a source of the user the queue plays for, whoever has it loaded."""
    theirs, mine = _provider(THEIRS), _provider(MINE)
    theirs.get_track = AsyncMock(return_value=_provider_track(THEIRS, SPOTIFY_TRACK_ID))
    theirs.get_track_by_external_id = AsyncMock(return_value=_provider_track(THEIRS, "s9"))
    found = _provider_track(MINE, "t9")
    mine.get_track_by_external_id = AsyncMock(return_value=found)
    _load_providers(music, theirs, mine, linked=True)
    set_music_source_access(
        music.mass,
        {
            THEIRS: ProviderAccess(owner=OWNER.user_id, sharing=ProviderSharing.PRIVATE),
            MINE: ProviderAccess(owner=MEMBER.user_id, sharing=ProviderSharing.PRIVATE),
        },
    )
    ctrl = _controller(music, userid=MEMBER.user_id)
    (item,) = _queue(ctrl, _external_track())

    # the owner skips to the track on the queue the member started, as itself or through a
    # service account that acts for it
    if acting_as_owner:
        set_current_user(User(user_id="service", username="service", role=UserRole.SERVICE))
        set_impersonated_user(OWNER)
    else:
        set_current_user(OWNER)
    await ctrl._load_item(item)

    assert get_current_user() is OWNER
    assert item.media_item is found
    assert _requests(theirs) == 0
    assert _requests(mine) == 1


async def test_queue_without_a_user_is_looked_up_on_the_sources_its_stream_may_use(
    music: MusicController,
) -> None:
    """Anonymous playback asks the shared sources alone, in the library and on the providers."""
    library_copy = _provider_track(THEIRS, "s1")
    library_copy.external_ids.add((ExternalID.MB_RECORDING, RECORDING_ID))
    await music.tracks.add_item_to_library(library_copy)
    theirs, shared = _provider(THEIRS), _provider(SHARED)
    theirs.get_track = AsyncMock(return_value=_provider_track(THEIRS, SPOTIFY_TRACK_ID))
    theirs.get_track_by_external_id = AsyncMock(return_value=_provider_track(THEIRS, "s9"))
    found = _provider_track(SHARED, "t9")
    shared.get_track_by_external_id = AsyncMock(return_value=found)
    # the private account is loaded first, so it is the first to be asked when it may be
    _load_providers(music, theirs, shared, linked=True)
    set_music_source_access(
        music.mass,
        {
            THEIRS: ProviderAccess(owner=OWNER.user_id, sharing=ProviderSharing.PRIVATE),
            SHARED: ProviderAccess(owner=OWNER.user_id, sharing=ProviderSharing.EVERYONE),
        },
    )
    ctrl = _controller(music, userid=None)
    (item,) = _queue(ctrl, _external_track())

    await ctrl._load_item(item)

    assert item.media_item is found
    assert _requests(theirs) == 0
    assert _requests(shared) == 1
    # and whoever had the item loaded is the caller it was before
    assert get_current_user() is None


@pytest.mark.parametrize(
    ("instance_id", "image"),
    [("spotify--1", False), ("ytmusic--1", True)],
    ids=["without an image", "on YouTube Music"],
)
async def test_looked_up_track_is_not_fetched_again(
    music: MusicController, instance_id: str, image: bool
) -> None:
    """The track that was just found is played as it is: no details of it are fetched."""
    provider = _provider(instance_id)
    found = _provider_track(instance_id, "x9", image=image)
    provider.get_track_by_external_id = AsyncMock(return_value=found)
    provider.get_track = AsyncMock(return_value=found)
    _load_providers(music, provider)
    ctrl = _controller(music)
    (item,) = _queue(ctrl, _external_track())

    with patch.object(
        music, "get_item_by_uri", AsyncMock(wraps=music.get_item_by_uri)
    ) as get_item_by_uri:
        await ctrl._load_item(item)

    assert item.media_item is found
    get_item_by_uri.assert_not_awaited()
    provider.get_track.assert_not_awaited()
    provider.get_album.assert_not_awaited()
    provider.get_artist.assert_not_awaited()
    assert _requests(provider) == 1


async def test_load_by_another_user_than_the_queues_fetches_nothing_as_that_user(
    music: MusicController,
) -> None:
    """A track found on a private source of the queue's user loads, whoever has it loaded."""
    mine = _provider(MINE)
    found = _provider_track(MINE, "t9", image=False)
    mine.get_track_by_external_id = AsyncMock(return_value=found)
    mine.get_track = AsyncMock(return_value=found)
    _load_providers(music, mine)
    set_music_source_access(
        music.mass,
        {MINE: ProviderAccess(owner=MEMBER.user_id, sharing=ProviderSharing.PRIVATE)},
    )
    ctrl = _controller(music, userid=MEMBER.user_id)
    (item,) = _queue(ctrl, _external_track())

    # the owner, who may not use the member's account, skips to the track
    with user_auth_context(OWNER):
        await ctrl._load_item(item)

    assert item.media_item is found
    assert _requests(mine) == 1


async def test_looked_up_track_is_loaded_again_by_another_user(music: MusicController) -> None:
    """A track found for the queue's user loads again for whoever selects it afterwards."""
    found = _provider_track(MINE, "t9", image=False)
    mine = _members_private_source(music, found)
    mine.get_track_by_external_id = AsyncMock(return_value=found)
    ctrl = _controller(music, userid=MEMBER.user_id)
    current, item = _queue(ctrl, _provider_track(MINE, "t1"), _external_track())

    with _recorded_fetches(music) as fetches:
        # the queue preloads the track, which looks it up
        assert await ctrl.load_next_queue_item(QUEUE_ID, current.queue_item_id) is item
        assert fetches == []
        # the owner, who may not use the member's account, then has it loaded once more
        with user_auth_context(OWNER):
            assert await ctrl.load_next_queue_item(QUEUE_ID, current.queue_item_id) is item
            assert get_current_user() is OWNER

    # its details are fetched for the user the queue plays for
    assert fetches == [(f"{MINE}://track/t9", MEMBER)]
    assert item.available is True
    assert cast("Track", item.media_item).provider == MINE


async def test_details_of_a_track_are_fetched_as_the_queue_user(music: MusicController) -> None:
    """A track without a cover on the queue user's own account loads for another user too."""
    listed = _provider_track(MINE, "t9", image=False)
    _members_private_source(music, _provider_track(MINE, "t9", image=False))
    ctrl = _controller(music, userid=MEMBER.user_id)
    (item,) = _queue(ctrl, listed)

    with _recorded_fetches(music) as fetches, user_auth_context(OWNER):
        await ctrl._load_item(item)
        assert get_current_user() is OWNER

    assert fetches == [(f"{MINE}://track/t9", MEMBER)]
    assert item.media_item is not listed
    assert cast("Track", item.media_item).uri == f"{MINE}://track/t9"


async def test_details_of_a_track_are_fetched_as_before_for_the_queue_user(
    music: MusicController,
) -> None:
    """For the user the queue plays for, a track is fetched, or left as it is, as ever."""
    without_cover = _provider_track(MINE, "t8", image=False)
    with_cover = _provider_track(MINE, "t9")
    # the cover a queued track keeps is the one of its album
    cast("ItemMapping", with_cover.album).image = with_cover.image
    full_track = _provider_track(MINE, "t8", image=False)
    mine = _members_private_source(music, full_track)
    ctrl = _controller(music, userid=MEMBER.user_id)
    fetched, kept = _queue(ctrl, without_cover, with_cover)

    with _recorded_fetches(music) as fetches, user_auth_context(MEMBER):
        await ctrl._load_item(fetched)
        await ctrl._load_item(kept)
        assert get_current_user() is MEMBER

    # the track without a cover is fetched, with its album; the other one is not
    assert fetches == [(f"{MINE}://track/t8", MEMBER)]
    assert fetched.media_item is full_track
    assert kept.media_item is with_cover
    mine.get_track.assert_awaited_once_with("t8")
    mine.get_album.assert_awaited_once_with("t8_album")
    assert _requests(mine) == 2


async def test_loads_of_the_same_item_at_once_share_one_lookup(music: MusicController) -> None:
    """A preload and a user action that load the same item cost one lookup between them."""
    spotify = _provider("spotify--1")
    found = _provider_track("spotify--1", "s9")
    spotify.get_track_by_external_id = AsyncMock(return_value=found)
    musicbrainz = _load_providers(music, spotify)
    ctrl = _controller(music)
    (item,) = _queue(ctrl, _external_track())

    await asyncio.gather(ctrl._load_item(item), ctrl._load_item(item))

    assert item.media_item is found
    musicbrainz.get_recording_details.assert_awaited_once_with(RECORDING_ID)
    spotify.get_track_by_external_id.assert_awaited_once()
    assert _requests(spotify) == 1
    assert ctrl.mass.streams.audio.get_stream_details.await_count == 2
    # nothing is kept of the lookup once it is done
    await asyncio.sleep(0)
    assert not [
        task_id
        for task_id in music.mass._tracked_tasks
        if "_external_track_on_a_provider" in task_id
    ]


async def test_item_found_missing_is_not_looked_up_again(music: MusicController) -> None:
    """A speaker that asks again for a track the queue found missing costs no second lookup."""
    spotify = _provider("spotify--1")
    musicbrainz = _load_providers(music, spotify)
    ctrl = _controller(music)
    (item,) = _queue(ctrl, _external_track())
    audio = StreamsAudio(ctrl.mass)

    with pytest.raises(MediaNotFoundError):
        await audio.get_stream_details(item)
    # what the stream request does with an item that can not be played
    item.available = False
    lookups = musicbrainz.get_recording_details.await_count
    requests = _requests(spotify)
    assert (lookups, requests) == (1, 1)

    with pytest.raises(MediaNotFoundError):
        await audio.get_stream_details(item)

    assert musicbrainz.get_recording_details.await_count == lookups
    assert _requests(spotify) == requests


async def test_stream_details_are_never_asked_for_an_external_track() -> None:
    """A track the queue did not load is looked up before a source is picked for it."""
    order: list[str] = []
    mass = MagicMock()
    mass.player_queues.look_up_external_track = AsyncMock(
        side_effect=lambda _item: order.append("look up")
    )
    audio = StreamsAudio(mass)
    details = MagicMock()

    async def _details(*_args: object) -> MagicMock:
        order.append("stream details")
        return details

    not_loaded = build_queue_item(QUEUE_ID, _external_track())
    loaded = build_queue_item(QUEUE_ID, _provider_track("spotify--1", "s1"))
    with patch.object(audio, "_get_stream_details", AsyncMock(side_effect=_details)):
        assert await audio.get_stream_details(not_loaded) is details
        assert order == ["look up", "stream details"]
        mass.player_queues.look_up_external_track.assert_awaited_once_with(not_loaded)

        assert await audio.get_stream_details(loaded) is details
        mass.player_queues.look_up_external_track.assert_awaited_once()


# ---------------------------------------------------------------------------
# what else reads the queue's items
# ---------------------------------------------------------------------------


async def test_external_track_is_looked_up_after_a_restart(music: MusicController) -> None:
    """A queue restored from the cache holds the track as it was enqueued, and finds it on load."""
    spotify = _provider("spotify--1")
    found = _provider_track("spotify--1", "s9")
    spotify.get_track_by_external_id = AsyncMock(return_value=found)
    _load_providers(music, spotify)
    ctrl = _controller(music)
    _queue(ctrl, _external_track())
    saved = cast("PlayerQueueData", ctrl._queue_data[QUEUE_ID])

    restored = PlayerQueueData.from_cache(saved.to_cache(), saved.items_to_cache())
    ctrl._queue_data[QUEUE_ID] = restored

    (item,) = restored.items
    assert isinstance(item.media_item, Track)
    assert item.media_item.provider == EXTERNAL_ID_PROVIDER
    assert item.uri == _uri()
    assert [track.uri for track in restored.enqueued_media_items] == [_uri()]

    await ctrl._load_item(item)

    assert item.media_item is found
    # the enqueued item is another object than the queue's since the restart
    assert restored.enqueued_media_items == [found]


async def test_external_track_stays_a_source_of_a_dynamic_queue(music: MusicController) -> None:
    """A track added to a mix is kept in its pool, though no music source has it yet."""
    ctrl = _controller(music)
    playing = _provider_track("spotify--1", "s1")
    gone = _provider_track("spotify--1", "s2")
    for mapping in gone.provider_mappings:
        mapping.available = False
    first, second = _external_track(RECORDING_IDS[1]), _external_track(RECORDING_IDS[2])
    ctrl.get_tracks_for_playback = AsyncMock(return_value=[first, gone])

    # as a source of the pool
    assert await ManagedPool(ctrl)._fetch_tracks(first) == [first]

    # when the queue turns into a mix
    _queue(ctrl, playing)
    ctrl._set_transitioning = Mock()
    ctrl.update_next_item_on_player = Mock()
    ctrl.is_smart_shuffle_active = Mock(return_value=False)
    ctrl._managed_pool.fill = AsyncMock(return_value=[first, gone])
    await ctrl._enter_dynamic_mode(QUEUE_ID, QueueOption.ADD)
    assert _media_items(ctrl) == [playing, first]

    # and when the mix is topped up
    ctrl._managed_pool.fill = AsyncMock(return_value=[gone, second])
    await ctrl._fill_dynamic_tracks(QUEUE_ID)
    assert _media_items(ctrl) == [playing, first, second]


async def test_autoplay_is_not_seeded_with_tracks_that_are_not_looked_up(
    music: MusicController,
) -> None:
    """A refill of similar tracks is seeded with the tracks the queue found, never the others."""
    ctrl = _controller(music)
    played = _provider_track("spotify--1", "s1")
    _queue(ctrl, played, _external_track(RECORDING_IDS[1]), _external_track(RECORDING_IDS[2]))
    radio = MagicMock()
    radio.get_dynamic_tracks = AsyncMock(return_value=[])
    ctrl.mass.get_provider = Mock(return_value=radio)
    seeds = ctrl._queue_data[QUEUE_ID].enqueued_media_items

    await ctrl._get_similar_tracks(QUEUE_ID, is_initial=True, seed_items=seeds)

    radio.get_dynamic_tracks.assert_awaited_once()
    assert radio.get_dynamic_tracks.await_args.args[0] == [played]

    # a refill from a single track is seeded with what the queue holds instead
    radio.get_dynamic_tracks.reset_mock()
    await ctrl._get_similar_tracks(QUEUE_ID, seed_items=[played])
    assert radio.get_dynamic_tracks.await_args.args[0] == [played]

    radio.get_dynamic_tracks.reset_mock()
    assert await ctrl._get_similar_tracks(QUEUE_ID, seed_items=seeds[1:]) == []
    radio.get_dynamic_tracks.assert_not_awaited()


async def test_queue_with_tracks_not_looked_up_is_not_saved_as_playlist(
    music: MusicController,
) -> None:
    """Saving the queue is refused, with its reason, while it holds tracks to look up."""
    spotify = _provider("spotify--1")
    musicbrainz = _load_providers(music, spotify)
    ctrl = _controller(music)
    ctrl._check_player_permission = Mock()
    ctrl.get = Mock(return_value=ctrl._queue_data[QUEUE_ID].queue)
    played = _provider_track("spotify--1", "s1")
    _, not_looked_up = _queue(ctrl, played, _external_track(RECORDING_IDS[1]))

    with (
        patch.object(music.playlists, "create_playlist", AsyncMock()) as create_playlist,
        patch.object(music.playlists, "add_playlist_tracks", AsyncMock()) as add_tracks,
        pytest.raises(InvalidDataError) as excinfo,
    ):
        await ctrl.save_as_playlist(QUEUE_ID, "Tonight")

    assert excinfo.value.translation_key == "queue_holds_tracks_not_looked_up"
    assert excinfo.value.translation_owner == "core.player_queues"
    create_playlist.assert_not_awaited()
    add_tracks.assert_not_awaited()
    musicbrainz.get_recording_details.assert_not_awaited()
    assert _requests(spotify) == 0

    # once the track was found, the queue is saved like any other
    not_looked_up.media_item = _provider_track("spotify--1", "s2")
    assert ctrl._playlist_uris(QUEUE_ID) == ["spotify--1://track/s1", "spotify--1://track/s2"]
