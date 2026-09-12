"""Tests for how a user's music sources narrow what the MusicController serves."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from music_assistant_models.access import PlaylistAccess
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import (
    AlbumType,
    ArtistType,
    MediaType,
    ProviderFeature,
    ProviderSharing,
    ProviderType,
)
from music_assistant_models.errors import InsufficientPermissions, MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    Genre,
    Playlist,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.constants import DB_TABLE_PROVIDER_MAPPINGS
from music_assistant.controllers.music import MusicController
from music_assistant.mass import MusicAssistant
from tests.common import set_music_source_access

GET_CURRENT_USER = "music_assistant.controllers.music.media.base.get_current_user"
PROV_A = "prov_a_inst"
PROV_B = "prov_b_inst"
USER_A = "user-a"
USER_B = "user-b"
# a user both music sources are shared with, so it sees every source
USER_ALL = "user-all"


def _user(user_id: str, role: str = UserRole.USER) -> User:
    return User(user_id=user_id, username=user_id, role=role)


def _private(owner: str) -> ProviderAccess:
    return ProviderAccess(owner=owner, sharing=ProviderSharing.PRIVATE)


def _make_prov(
    instance_id: str,
    prov_type: ProviderType,
    features: set[ProviderFeature] | None = None,
) -> Mock:
    prov = Mock()
    prov.instance_id = instance_id
    prov.type = prov_type
    prov.supported_features = features or set()
    return prov


@patch("music_assistant.controllers.music.controller.get_current_user")
def test_apply_user_provider_filter_filters_music_providers_for_admin(
    mock_get_user: Mock,
) -> None:
    """An admin is narrowed to its own music sources just like anyone else (issue #5509)."""
    mock_get_user.return_value = _user("admin", UserRole.ADMIN)
    music_a = _make_prov("m_a", ProviderType.MUSIC)
    music_b = _make_prov("m_b", ProviderType.MUSIC)

    controller = MusicController.__new__(MusicController)
    controller.mass = Mock()
    set_music_source_access(controller.mass, {"m_a": None, "m_b": _private(USER_B)})
    result = controller._apply_user_provider_filter([music_a, music_b])

    assert [p.instance_id for p in result] == ["m_a"]


@patch("music_assistant.controllers.music.controller.get_current_user")
def test_apply_user_provider_filter_passes_non_music_providers(
    mock_get_user: Mock,
) -> None:
    """Metadata and plugin providers are never narrowed down."""
    mock_get_user.return_value = _user(USER_A)
    music_a = _make_prov("m_a", ProviderType.MUSIC)
    metadata = _make_prov("meta_a", ProviderType.METADATA)
    plugin = _make_prov("plug_a", ProviderType.PLUGIN)

    controller = MusicController.__new__(MusicController)
    controller.mass = Mock()
    set_music_source_access(controller.mass, {"m_a": None, "m_b": _private(USER_B)})
    result = controller._apply_user_provider_filter([music_a, metadata, plugin])

    assert [p.instance_id for p in result] == ["m_a", "meta_a", "plug_a"]


@patch("music_assistant.controllers.music.controller.get_current_user")
def test_apply_user_provider_filter_no_filter_returns_all(
    mock_get_user: Mock,
) -> None:
    """A user that may see every music source gets every provider."""
    mock_get_user.return_value = _user(USER_A)
    music_a = _make_prov("m_a", ProviderType.MUSIC)
    music_b = _make_prov("m_b", ProviderType.MUSIC)

    controller = MusicController.__new__(MusicController)
    controller.mass = Mock()
    set_music_source_access(controller.mass, {"m_a": None, "m_b": None})
    result = controller._apply_user_provider_filter([music_a, music_b])

    assert [p.instance_id for p in result] == ["m_a", "m_b"]


@patch("music_assistant.controllers.music.controller.get_current_user")
async def test_browse_root_honors_admin_music_sources(mock_get_user: Mock) -> None:
    """Regression for issue #5509: browse must honor an admin's music sources too."""
    mock_get_user.return_value = _user("admin", UserRole.ADMIN)
    mass = Mock()
    set_music_source_access(mass, {"m_a": None, "m_b": _private(USER_B)})
    music_a = _make_prov("m_a", ProviderType.MUSIC, {ProviderFeature.BROWSE})
    music_a.domain = "music_a"
    music_a.name = "Music A"
    music_b = _make_prov("m_b", ProviderType.MUSIC, {ProviderFeature.BROWSE})
    music_b.domain = "music_b"
    music_b.name = "Music B"
    mass.get_providers_supporting_feature.side_effect = lambda feature: (
        [music_a, music_b] if feature == ProviderFeature.BROWSE else []
    )

    controller = MusicController.__new__(MusicController)
    controller.mass = mass

    result = await controller.browse(path=None)

    assert [folder.path for folder in result] == ["m_a://"]  # type: ignore[union-attr]


@patch("music_assistant.controllers.music.controller.get_current_user")
async def test_browse_refuses_a_source_the_user_may_not_see(mock_get_user: Mock) -> None:
    """Browsing straight into another member's music source by path is refused."""
    mock_get_user.return_value = _user(USER_A)
    mass = Mock()
    set_music_source_access(mass, {"m_a": None, "m_b": _private(USER_B)})
    music_a = _make_prov("m_a", ProviderType.MUSIC, {ProviderFeature.BROWSE})
    music_a.browse = AsyncMock(return_value=[])
    music_b = _make_prov("m_b", ProviderType.MUSIC, {ProviderFeature.BROWSE})
    music_b.name = "Music B"
    mass.get_provider.side_effect = {"m_a": music_a, "m_b": music_b}.get

    controller = MusicController.__new__(MusicController)
    controller.mass = mass

    with pytest.raises(InsufficientPermissions):
        await controller.browse(path="m_b://")
    # the source the user may see still browses
    allowed = await controller.browse(path="m_a://")
    assert [folder.path for folder in allowed] == ["root"]  # type: ignore[union-attr]


@patch("music_assistant.controllers.music.controller.get_current_user")
async def test_verify_item_uri_bypasses_filter_for_plain_url(mock_get_user: Mock) -> None:
    """A plain URL resolves via the builtin provider and must bypass the filter (issue #6320)."""
    mock_get_user.return_value = _user(USER_A)
    controller = MusicController.__new__(MusicController)
    controller.mass = Mock()
    set_music_source_access(
        controller.mass, {"builtin": None, "spotify--TPf9JZ2K": _private(USER_B)}
    )

    with patch.object(MusicController, "get_item", AsyncMock(return_value=Mock())):
        assert await controller._handle_verify_item_uri("https://example.com/clip.mp3") is True


@patch("music_assistant.controllers.music.controller.get_current_user")
async def test_verify_item_uri_resolves_domain_against_instance_id(
    mock_get_user: Mock,
) -> None:
    """A uri naming a provider by domain must match the user's music sources by instance id."""
    mock_get_user.return_value = _user(USER_A)
    spotify = _make_prov("spotify--TPf9JZ2K", ProviderType.MUSIC)
    spotify.domain = "spotify"
    controller = MusicController.__new__(MusicController)
    controller.mass = Mock(providers=[spotify])
    set_music_source_access(
        controller.mass,
        {"spotify--TPf9JZ2K": _private(USER_A), "tidal--HidDeN01": _private(USER_B)},
    )

    with patch.object(MusicController, "get_item", AsyncMock(return_value=Mock())) as get_item:
        assert await controller._handle_verify_item_uri("spotify://track/abc") is True
        get_item.assert_awaited_once_with(
            media_type=MediaType.TRACK,
            item_id="abc",
            provider_instance_id_or_domain="spotify--TPf9JZ2K",
            allow_update_metadata=False,
        )


@patch("music_assistant.controllers.music.controller.get_current_user")
async def test_verify_item_uri_binds_lookup_to_allowed_instance(mock_get_user: Mock) -> None:
    """A domain uri must never be served by a same-domain instance the user may not use."""
    mock_get_user.return_value = _user(USER_A)
    denied = _make_prov("spotify--AAAAAAAA", ProviderType.MUSIC)
    denied.domain = "spotify"
    allowed = _make_prov("spotify--TPf9JZ2K", ProviderType.MUSIC)
    allowed.domain = "spotify"
    controller = MusicController.__new__(MusicController)
    # the denied instance is listed first, so an unbound domain lookup would resolve to it
    controller.mass = Mock(providers=[denied, allowed])
    set_music_source_access(
        controller.mass,
        {"spotify--AAAAAAAA": _private(USER_B), "spotify--TPf9JZ2K": _private(USER_A)},
    )

    with patch.object(MusicController, "get_item", AsyncMock(return_value=Mock())) as get_item:
        assert await controller._handle_verify_item_uri("spotify://track/abc") is True
        get_item.assert_awaited_once_with(
            media_type=MediaType.TRACK,
            item_id="abc",
            provider_instance_id_or_domain="spotify--TPf9JZ2K",
            allow_update_metadata=False,
        )


@patch("music_assistant.controllers.music.controller.get_current_user")
async def test_verify_item_uri_denies_provider_the_user_may_not_use(mock_get_user: Mock) -> None:
    """A uri for a music source the user may not use must verify False."""
    mock_get_user.return_value = _user(USER_A)
    spotify = _make_prov("spotify--TPf9JZ2K", ProviderType.MUSIC)
    spotify.domain = "spotify"
    controller = MusicController.__new__(MusicController)
    controller.mass = Mock(providers=[spotify])
    set_music_source_access(
        controller.mass,
        {"deezer--OwNeD001": _private(USER_A), "spotify--TPf9JZ2K": _private(USER_B)},
    )

    with patch.object(MusicController, "get_item", AsyncMock(return_value=Mock())) as get_item:
        assert await controller._handle_verify_item_uri("spotify://track/abc") is False
        get_item.assert_not_awaited()


def _controller_with_sources(
    access: dict[str, ProviderAccess | None], providers: list[Mock] | None = None
) -> MusicController:
    """Create a bare controller whose server has the given music sources."""
    loaded = providers or []
    controller = MusicController.__new__(MusicController)
    controller.mass = Mock(
        providers=loaded,
        get_provider=Mock(
            side_effect=lambda instance_id, **_kwargs: next(
                (prov for prov in loaded if prov.instance_id == instance_id), None
            )
        ),
        get_provider_instances=Mock(
            side_effect=lambda domain, **_kwargs: [prov for prov in loaded if prov.domain == domain]
        ),
    )
    set_music_source_access(controller.mass, access)
    return controller


def _music_source_prov(instance_id: str, available: bool = True, is_streaming: bool = True) -> Mock:
    """Create a loaded instance of the music service named in the given instance id."""
    prov = _make_prov(instance_id, ProviderType.MUSIC)
    prov.domain = instance_id.split("--", maxsplit=1)[0]
    prov.available = available
    prov.is_streaming_provider = is_streaming
    return prov


def _library_track(*provider_instances: str) -> Track:
    """Create a library track mapped to the given provider instances."""
    return Track(
        item_id="1",
        provider="library",
        name="Track",
        provider_mappings={_mapping(instance) for instance in provider_instances},
    )


def test_check_item_playable_accepts_a_reachable_library_item() -> None:
    """A library item with a mapping on one of the user's music sources plays."""
    controller = _controller_with_sources({PROV_A: _private(USER_A), PROV_B: _private(USER_B)})

    controller.check_item_playable_for_user(_library_track(PROV_A, PROV_B), _user(USER_A))


def test_check_item_playable_accepts_a_genre() -> None:
    """A genre carries no source mapping of its own, it expands to source-filtered tracks."""
    controller = _controller_with_sources({PROV_A: _private(USER_A), PROV_B: _private(USER_B)})
    genre = Genre(item_id="7", provider="library", name="Jazz", provider_mappings=set())

    controller.check_item_playable_for_user(genre, _user(USER_A))


def test_check_item_playable_rejects_a_library_item_without_a_reachable_mapping() -> None:
    """A library item that only maps to other members' sources is refused."""
    controller = _controller_with_sources({PROV_A: _private(USER_A), PROV_B: _private(USER_B)})

    with pytest.raises(MediaNotFoundError) as err:
        controller.check_item_playable_for_user(_library_track(PROV_B), _user(USER_A))
    assert err.value.translation_key == "media_not_available_for_user"


def test_check_item_playable_follows_the_access_record_of_a_playlist() -> None:
    """A personal Music Assistant playlist only plays for the users who may see it."""
    controller = _controller_with_sources({PROV_A: None})
    playlist = Playlist(
        item_id="9",
        provider="library",
        name="Mine",
        provider_mappings={
            ProviderMapping(item_id="mine", provider_domain="builtin", provider_instance="builtin")
        },
        access=PlaylistAccess(owner=USER_A),
    )

    controller.check_item_playable_for_user(playlist, _user(USER_A))
    with pytest.raises(MediaNotFoundError):
        controller.check_item_playable_for_user(playlist, _user(USER_B))
    with pytest.raises(MediaNotFoundError):
        controller.check_item_playable_for_user(playlist, None)


def test_check_item_playable_rejects_a_provider_item_on_a_hidden_source() -> None:
    """A provider item straight off a music source the user may not use is refused."""
    controller = _controller_with_sources({PROV_A: _private(USER_A), PROV_B: _private(USER_B)})
    item = Track(item_id="42", provider=PROV_B, name="Track", provider_mappings=set())

    with pytest.raises(MediaNotFoundError):
        controller.check_item_playable_for_user(item, _user(USER_A))


def test_check_item_playable_accepts_a_shared_account_of_an_own_service() -> None:
    """An item browsed on another member's account plays through the user's own account."""
    controller = _controller_with_sources(
        {
            "spotify--mine": _private(USER_A),
            "spotify--theirs": ProviderAccess(owner=USER_B, sharing=ProviderSharing.EVERYONE),
        },
        providers=[_music_source_prov("spotify--mine"), _music_source_prov("spotify--theirs")],
    )
    item = Track(item_id="42", provider="spotify--theirs", name="Track", provider_mappings=set())

    controller.check_item_playable_for_user(item, _user(USER_A))


def test_check_item_playable_accepts_a_library_item_mapped_to_a_shared_account_of_an_own_service() -> (
    None
):
    """A library item mapped only to another member's account plays through the user's own."""
    controller = _controller_with_sources(
        {
            "spotify--mine": _private(USER_A),
            "spotify--theirs": ProviderAccess(owner=USER_B, sharing=ProviderSharing.EVERYONE),
        },
        providers=[_music_source_prov("spotify--mine"), _music_source_prov("spotify--theirs")],
    )

    controller.check_item_playable_for_user(_library_track("spotify--theirs"), _user(USER_A))


def test_check_item_playable_rejects_a_private_account_of_an_own_service() -> None:
    """Another member's private account stays out of reach, own account of that service or not."""
    controller = _controller_with_sources(
        {"spotify--mine": _private(USER_A), "spotify--theirs": _private(USER_B)},
        providers=[_music_source_prov("spotify--mine"), _music_source_prov("spotify--theirs")],
    )
    item = Track(item_id="42", provider="spotify--theirs", name="Track", provider_mappings=set())

    with pytest.raises(MediaNotFoundError):
        controller.check_item_playable_for_user(item, _user(USER_A))


def test_check_item_playable_rejects_a_shared_account_without_a_playable_own_account() -> None:
    """A shared account is out of reach while the user's own account of it can not play."""
    controller = _controller_with_sources(
        {
            "spotify--mine": _private(USER_A),
            "spotify--theirs": ProviderAccess(owner=USER_B, sharing=ProviderSharing.EVERYONE),
        },
        providers=[_music_source_prov("spotify--theirs")],
    )
    item = Track(item_id="42", provider="spotify--theirs", name="Track", provider_mappings=set())

    with pytest.raises(MediaNotFoundError):
        controller.check_item_playable_for_user(item, _user(USER_A))


def test_check_item_playable_rejects_an_account_of_a_service_the_user_has_none_of() -> None:
    """Without an account of the service, another member's private one stays out of reach."""
    controller = _controller_with_sources(
        {"deezer--mine": _private(USER_A), "spotify--theirs": _private(USER_B)},
        providers=[_music_source_prov("deezer--mine"), _music_source_prov("spotify--theirs")],
    )
    item = Track(item_id="42", provider="spotify--theirs", name="Track", provider_mappings=set())

    with pytest.raises(MediaNotFoundError):
        controller.check_item_playable_for_user(item, _user(USER_A))


def test_check_item_playable_rejects_another_library_of_a_local_source() -> None:
    """Instances of a local source are libraries of their own, so neither stands in."""
    controller = _controller_with_sources(
        {"filesystem_local--mine": _private(USER_A), "filesystem_local--theirs": _private(USER_B)},
        providers=[
            _music_source_prov("filesystem_local--mine", is_streaming=False),
            _music_source_prov("filesystem_local--theirs", is_streaming=False),
        ],
    )
    item = Track(
        item_id="42", provider="filesystem_local--theirs", name="Track", provider_mappings=set()
    )

    with pytest.raises(MediaNotFoundError):
        controller.check_item_playable_for_user(item, _user(USER_A))


def test_check_item_playable_keeps_plugin_items_reachable() -> None:
    """Plugin providers carry no access record, so their items stay playable."""
    plugin = _make_prov("smart_playlist", ProviderType.PLUGIN)
    controller = _controller_with_sources(
        {PROV_A: _private(USER_A), PROV_B: _private(USER_B)}, providers=[plugin]
    )
    item = Track(item_id="42", provider="smart_playlist", name="Track", provider_mappings=set())

    controller.check_item_playable_for_user(item, _user(USER_B))


def test_check_item_playable_for_anonymous_playback() -> None:
    """Anonymous playback only reaches sources shared with everyone."""
    controller = _controller_with_sources(
        {
            PROV_A: ProviderAccess(owner=USER_A, sharing=ProviderSharing.EVERYONE),
            PROV_B: _private(USER_B),
        }
    )

    controller.check_item_playable_for_user(_library_track(PROV_A), None)
    with pytest.raises(MediaNotFoundError):
        controller.check_item_playable_for_user(_library_track(PROV_B), None)


async def test_a_play_report_moves_to_the_own_account_of_an_own_service() -> None:
    """A play served through the user's own account of a service is reported there too."""
    own_instance = "tidal--mine"
    other_instance = "tidal--theirs"
    own = _music_source_prov(own_instance, available=True)
    housemate = _music_source_prov(other_instance, available=True)
    controller = _controller_with_sources(
        {
            own_instance: _private(USER_A),
            other_instance: ProviderAccess(owner=USER_B, sharing=ProviderSharing.EVERYONE),
        },
        providers=[own, housemate],
    )
    mass: Any = controller.mass
    mass.get_provider = Mock(
        side_effect=lambda instance_id, **_kwargs: {
            own_instance: own,
            other_instance: housemate,
        }.get(instance_id)
    )
    mass.get_provider_instances = Mock(return_value=[own, housemate])
    mass.webserver.auth.get_user = AsyncMock(return_value=_user(USER_A))
    track = Track(
        item_id="1",
        provider="library",
        name="Track",
        provider_mappings={
            ProviderMapping(item_id="t1", provider_domain="tidal", provider_instance=other_instance)
        },
    )
    controller._resolve_playlog_item = AsyncMock(return_value=track)  # type: ignore[method-assign]

    await controller.mark_item_played(track, is_playing=True, userid=USER_A)

    own.on_played.assert_called_once()
    assert own.on_played.call_args.kwargs["prov_item_id"] == "t1"
    housemate.on_played.assert_not_called()


async def test_a_play_report_never_reaches_another_members_account() -> None:
    """
    A play on a source that is not loaded is not reported through another account of it.

    `get_provider` stands in another instance of the same streaming provider for one that
    is unavailable, which for a play report would credit a housemate's account.
    """
    own_instance = "tidal--mine"
    other_instance = "tidal--theirs"
    own = _music_source_prov(own_instance, available=False)
    housemate = _music_source_prov(other_instance, available=True)
    controller = _controller_with_sources(
        {own_instance: _private(USER_A), other_instance: _private(USER_B)},
        providers=[own, housemate],
    )
    mass: Any = controller.mass
    # the real lookup, so the test runs against the fallback it has to keep out
    mass._providers = {own_instance: own, other_instance: housemate}
    mass.get_provider = MusicAssistant.get_provider.__get__(mass)
    mass.webserver.auth.get_user = AsyncMock(return_value=_user(USER_A))
    track = Track(
        item_id="1",
        provider="library",
        name="Track",
        provider_mappings={
            ProviderMapping(item_id="t1", provider_domain="tidal", provider_instance=own_instance)
        },
    )
    controller._resolve_playlog_item = AsyncMock(return_value=track)  # type: ignore[method-assign]

    await controller.mark_item_played(track, is_playing=True, userid=USER_A)

    mass.create_task.assert_not_called()


def _mapping(provider_instance: str) -> ProviderMapping:
    """Create an in-library provider mapping with a unique provider item id."""
    return ProviderMapping(
        item_id=uuid4().hex,
        provider_domain=provider_instance.removesuffix("_inst"),
        provider_instance=provider_instance,
        in_library=True,
    )


@pytest.fixture(scope="module")
async def counted_mass(music_mass_module: MusicAssistant) -> MusicAssistant:
    """
    Return a database-only instance seeded with items spread over two providers.

    Every media type is seeded so that USER_A, who only sees PROV_A, excludes at least
    one item but keeps at least one, for each of the filter combinations the counts support.
    """
    mass = music_mass_module
    set_music_source_access(
        mass,
        {
            PROV_A: ProviderAccess(
                owner=USER_A, sharing=ProviderSharing.SELECTED, shared_users=[USER_ALL]
            ),
            PROV_B: ProviderAccess(
                owner=USER_B, sharing=ProviderSharing.SELECTED, shared_users=[USER_ALL]
            ),
        },
    )
    artists: list[Artist] = []
    for name, providers, favorite in (
        ("Artist 01", [PROV_A], True),
        ("Artist 02", [PROV_B], True),
        ("Artist 03", [PROV_A, PROV_B], False),
    ):
        artist = Artist(
            item_id="0",
            provider="library",
            name=name,
            provider_mappings={_mapping(prov) for prov in providers},
            favorite=favorite,
        )
        artists.append(await mass.music.artists.add_item_to_library(artist))

    albums: list[Album] = []
    for idx, (name, providers, album_type, favorite) in enumerate(
        (
            ("Album 01", [PROV_A], AlbumType.ALBUM, True),
            ("Album 02", [PROV_B], AlbumType.ALBUM, True),
            ("Album 03", [PROV_A, PROV_B], AlbumType.SINGLE, False),
            ("Album 04", [PROV_B], AlbumType.SINGLE, False),
        )
    ):
        album = Album(
            item_id="0",
            provider="library",
            name=name,
            album_type=album_type,
            provider_mappings={_mapping(prov) for prov in providers},
            # Artist 03 gets no album, so album_artists_only excludes something
            artists=UniqueList([artists[idx % 2]]),
            favorite=favorite,
        )
        albums.append(await mass.music.albums.add_item_to_library(album))

    for idx, (name, providers, favorite) in enumerate(
        (
            ("Track 01", [PROV_A], True),
            ("Track 02", [PROV_B], True),
            ("Track 03", [PROV_A, PROV_B], False),
            ("Track 04", [PROV_B], False),
            ("Track 05", [PROV_A], False),
        )
    ):
        track = Track(
            item_id="0",
            provider="library",
            name=name,
            provider_mappings={_mapping(prov) for prov in providers},
            artists=UniqueList([artists[idx % len(artists)]]),
            album=albums[idx % len(albums)],
            disc_number=1,
            track_number=idx + 1,
            favorite=favorite,
        )
        await mass.music.tracks.add_item_to_library(track)

    # a track whose only PROV_A mapping left the provider's library: it must be excluded
    # from both the filtered listing and the filtered count
    hidden = Track(
        item_id="0",
        provider="library",
        name="Track hidden",
        provider_mappings={_mapping(PROV_A)},
        artists=UniqueList([artists[0]]),
        album=albums[0],
        disc_number=1,
        track_number=99,
        favorite=True,
    )
    db_hidden = await mass.music.tracks.add_item_to_library(hidden)
    await mass.music.database.execute(
        f"UPDATE {DB_TABLE_PROVIDER_MAPPINGS} SET in_library = 0 "
        "WHERE item_id = :item_id AND media_type = 'track'",
        {"item_id": int(db_hidden.item_id)},
    )
    await mass.music.genres.add_item_to_library(
        Genre(item_id="0", provider="library", name="Test Genre", provider_mappings=set())
    )
    await mass.music.database.commit()
    return mass


@pytest.mark.parametrize(
    ("media_type", "count_kwargs"),
    [
        ("tracks", {}),
        ("tracks", {"favorite_only": True}),
        ("albums", {}),
        ("albums", {"favorite_only": True}),
        ("albums", {"album_types": [AlbumType.ALBUM]}),
        ("artists", {}),
        ("artists", {"favorite_only": True}),
        ("artists", {"album_artists_only": True}),
        ("artists", {"artist_type": ArtistType.SINGER}),
    ],
)
async def test_library_count_matches_list_for_filtered_user(
    counted_mass: MusicAssistant,
    media_type: str,
    count_kwargs: dict[str, Any],
) -> None:
    """A user's music sources must narrow library_count the same way they narrow the list."""
    controller = getattr(counted_mass.music, media_type)
    # library_items spells the favorite filter 'favorite', library_count 'favorite_only'
    list_kwargs = {
        ("favorite" if key == "favorite_only" else key): value
        for key, value in count_kwargs.items()
    }
    with patch(GET_CURRENT_USER, return_value=None):
        unfiltered_count = await controller.library_count(**count_kwargs)
    with patch(GET_CURRENT_USER, return_value=_user(USER_A)):
        filtered_count = await controller.library_count(**count_kwargs)
        # the limit must exceed the seeded row count, so the list is never truncated
        filtered_items = await controller.library_items(limit=500, **list_kwargs)

    assert filtered_count == len(filtered_items)
    # guard against a vacuous pass: the user's music sources must actually exclude something
    assert 0 < filtered_count < unfiltered_count


async def test_library_count_unchanged_without_user(counted_mass: MusicAssistant) -> None:
    """Without an authenticated user the counts stay the true library totals."""
    for media_type in ("tracks", "albums", "artists"):
        controller = getattr(counted_mass.music, media_type)
        total_rows = await counted_mass.music.database.get_count(controller.db_table)
        with patch(GET_CURRENT_USER, return_value=None):
            assert await controller.library_count() == total_rows


async def test_library_count_unchanged_for_unrestricted_user(
    counted_mass: MusicAssistant,
) -> None:
    """A user that may see every music source sees the true library totals."""
    total_rows = await counted_mass.music.database.get_count("tracks")
    with patch(GET_CURRENT_USER, return_value=_user(USER_ALL)):
        assert await counted_mass.music.tracks.library_count() == total_rows


async def test_genre_library_count_ignores_music_sources(
    counted_mass: MusicAssistant,
) -> None:
    """Genres have no provider mappings, so a restricted user must not zero their count."""
    with patch(GET_CURRENT_USER, return_value=None):
        unfiltered_count = await counted_mass.music.genres.library_count()
    with patch(GET_CURRENT_USER, return_value=_user(USER_A)):
        assert await counted_mass.music.genres.library_count() == unfiltered_count
    assert unfiltered_count > 0
