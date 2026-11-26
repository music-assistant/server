"""Tests for the MetaDataController public API."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import ProviderFeature, ProviderSharing, ProviderType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    MediaItemMetadata,
    MediaItemPalette,
    ProviderMapping,
)

from music_assistant.controllers.music import MusicController
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    current_user,
    get_current_user,
    impersonated_user,
)
from music_assistant.models.music_provider import MusicProvider
from tests.common import set_music_source_access
from tests.controllers.music.helpers import create_track

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.media_items import Track

    from music_assistant.controllers.metadata import MetaDataController
    from music_assistant.mass import MusicAssistant

# every test here registers image ids, which are persisted to the cache database
pytestmark = pytest.mark.usefixtures("cache_database")


@pytest.fixture
async def music_controller(mass_minimal: MusicAssistant) -> AsyncGenerator[MusicController]:
    """Set a music controller on the minimal server, and close its library database afterwards."""
    controller = MusicController(mass_minimal)
    mass_minimal.music = controller
    yield controller
    await controller.close()


async def test_get_image_palette_resolves_registered_id(
    metadata_controller: MetaDataController, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registered image id resolves to its (provider, path) and yields its palette."""
    palette = MediaItemPalette(primary=(1, 2, 3))
    calls: dict[str, object] = {}

    async def fake_get_palette(_mass: object, path: str, provider: str) -> MediaItemPalette:
        calls["get_palette"] = (path, provider)
        return palette

    monkeypatch.setattr("music_assistant.controllers.metadata.images.get_palette", fake_get_palette)

    image_id = metadata_controller.compute_image_id("spotify", "cover.jpg")
    assert await metadata_controller.get_image_palette(image_id) is palette
    assert calls["get_palette"] == ("cover.jpg", "spotify")


async def test_get_image_palette_rejects_imageproxy_url(
    metadata_controller: MetaDataController, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a bare image id is accepted; a full /imageproxy/<id> URL yields None."""
    fetched = False

    async def fake_get_palette(_mass: object, _path: str, _provider: str) -> MediaItemPalette:
        nonlocal fetched
        fetched = True
        return MediaItemPalette(primary=(4, 5, 6))

    monkeypatch.setattr("music_assistant.controllers.metadata.images.get_palette", fake_get_palette)

    image_id = metadata_controller.compute_image_id("filesystem", "/x.jpg")
    url = f"http://mass.local/imageproxy/{image_id}?size=256"
    assert await metadata_controller.get_image_palette(url) is None
    assert fetched is False


async def test_get_image_palette_rejects_unregistered_input(
    metadata_controller: MetaDataController, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown ids and arbitrary URLs yield None and never trigger a fetch (no SSRF)."""
    fetched = False

    async def fake_get_palette(_mass: object, _path: str, _provider: str) -> MediaItemPalette:
        nonlocal fetched
        fetched = True
        return MediaItemPalette(primary=(0, 0, 0))

    monkeypatch.setattr("music_assistant.controllers.metadata.images.get_palette", fake_get_palette)

    # an id that was never registered
    assert await metadata_controller.get_image_palette("0" * 64) is None
    # an arbitrary URL must not be resolved or fetched
    assert await metadata_controller.get_image_palette("http://169.254.169.254/latest") is None
    assert fetched is False


async def test_get_image_palette_returns_none_for_unreadable_image(
    metadata_controller: MetaDataController, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registered image whose data can't be read yields None instead of raising."""

    async def boom(_mass: object, path: str, _provider: str) -> MediaItemPalette:
        raise FileNotFoundError(path)

    monkeypatch.setattr("music_assistant.controllers.metadata.images.get_palette", boom)
    image_id = metadata_controller.compute_image_id("filesystem", "missing.jpg")
    assert await metadata_controller.get_image_palette(image_id) is None


async def test_update_metadata_runs_as_the_server(
    metadata_controller: MetaDataController,
) -> None:
    """The refresh fills the household's library item from all sources, not the caller's."""
    service = User(user_id="user-service", username="ha", role=UserRole.SERVICE)
    member = User(user_id="user-member", username="member", role=UserRole.USER)
    refreshing_users: list[User | None] = []

    async def _refresh(*_: object, **__: object) -> None:
        refreshing_users.append(get_current_user())

    metadata_controller._update_track_metadata = AsyncMock(side_effect=_refresh)  # type: ignore[method-assign]
    track = create_track("spotify_theirs", "t1")
    track.item_id, track.provider = "1", "library"

    current_user_token = current_user.set(service)
    impersonated_user_token = impersonated_user.set(member)
    try:
        await metadata_controller.update_metadata(track)
        assert refreshing_users == [None]
        assert get_current_user() is member
    finally:
        impersonated_user.reset(impersonated_user_token)
        current_user.reset(current_user_token)


async def test_lyrics_come_only_from_a_source_the_user_may_see(
    metadata_controller: MetaDataController, mass_minimal: MusicAssistant
) -> None:
    """A track's own provider is asked for lyrics only when it is one of the user's sources."""
    owner = User(user_id="user-owner", username="owner", role=UserRole.USER)
    member = User(user_id="user-member", username="member", role=UserRole.USER)
    theirs = MagicMock(spec=MusicProvider)
    theirs.instance_id, theirs.domain, theirs.type = "spotify_theirs", "spotify", ProviderType.MUSIC
    theirs.available, theirs.is_streaming_provider = True, True
    theirs.supported_features = {ProviderFeature.LYRICS}
    theirs.initialized = MagicMock(is_set=MagicMock(return_value=True))
    full_track = create_track("spotify_theirs", "t1")
    full_track.metadata.lyrics = "secret"
    theirs.get_track = AsyncMock(return_value=full_track)
    mass_minimal.music = MusicController(mass_minimal)
    mass_minimal._providers = {"spotify_theirs": theirs}
    set_music_source_access(
        mass_minimal,
        {"spotify_theirs": ProviderAccess(owner=owner.user_id, sharing=ProviderSharing.PRIVATE)},
    )
    track = create_track("spotify_theirs", "t1")

    for user, lyrics in ((owner, "secret"), (member, None)):
        with (
            patch(
                "music_assistant.controllers.music.controller.get_current_user", return_value=user
            ),
            patch(
                "music_assistant.controllers.music.media.base.get_current_user", return_value=user
            ),
        ):
            assert (await metadata_controller.get_track_lyrics(track))[0] == lyrics


async def test_lyrics_of_a_library_track_refresh_the_stored_item(
    metadata_controller: MetaDataController,
    mass_minimal: MusicAssistant,
    music_controller: MusicController,
) -> None:
    """The caller's copy of a library track is never written back, the stored item is used."""
    owner = User(user_id="user-owner", username="owner", role=UserRole.USER)
    member = User(user_id="user-member", username="member", role=UserRole.USER)
    mass_minimal.streams = MagicMock()
    mass_minimal.streams.audio_analysis.get_track_audio_metadata = AsyncMock(return_value=None)
    mass_minimal.webserver = MagicMock()
    mass_minimal.webserver.auth.get_user = AsyncMock(return_value=None)
    mass_minimal.webserver.auth.list_users = AsyncMock(return_value=[])
    # the minimal server runs no cache database
    mass_minimal.cache.get = AsyncMock(return_value=None)  # type: ignore[method-assign]
    mass_minimal.cache.set = AsyncMock()  # type: ignore[method-assign]
    await music_controller._setup_database()
    set_music_source_access(
        mass_minimal,
        {"spotify_theirs": ProviderAccess(owner=owner.user_id, sharing=ProviderSharing.PRIVATE)},
    )
    with patch.object(mass_minimal, "metadata", MagicMock()):
        stored = await mass_minimal.music.tracks.add_item_to_library(
            create_track("spotify_theirs", "t1")
        )
    refreshing_users: list[User | None] = []

    async def _refresh(*_: object, **__: object) -> None:
        refreshing_users.append(get_current_user())

    refresh = AsyncMock(side_effect=_refresh)
    metadata_controller._update_track_metadata = refresh  # type: ignore[method-assign]
    crafted = create_track("spotify_theirs", "t1", name="Crafted")
    crafted.item_id, crafted.provider = stored.item_id, "library"

    for user in (member, owner):
        user_token = current_user.set(user)
        try:
            if user is member:
                # the only source of the track is hidden from the member
                with pytest.raises(MediaNotFoundError):
                    await metadata_controller.get_track_lyrics(crafted)
                refresh.assert_not_awaited()
            else:
                assert await metadata_controller.get_track_lyrics(crafted) == (None, None)
        finally:
            current_user.reset(user_token)
    assert refresh.await_args is not None
    refreshed = refresh.await_args.args[0]
    assert (refreshed.item_id, refreshed.name) == (stored.item_id, "Test Track")
    # the refresh fills the household's item, so it runs as the server, not as the caller
    assert refreshing_users == [None]
    assert get_current_user() is None


def _lyrics_provider(result: MediaItemMetadata | Exception | None) -> MagicMock:
    """Return a metadata provider that looks up lyrics."""
    provider = MagicMock()
    provider.name = "lyrics"
    provider.priority = 0
    provider.supported_features = {ProviderFeature.LYRICS}
    if isinstance(result, Exception):
        provider.get_track_metadata = AsyncMock(side_effect=result)
    else:
        provider.get_track_metadata = AsyncMock(return_value=result)
    return provider


async def test_track_lyrics_prefers_lrc_from_later_provider(
    metadata_controller: MetaDataController,
    mass_minimal: MusicAssistant,
) -> None:
    """An earlier plain result does not prevent looking for synchronized lyrics."""
    mass_minimal.music = MusicController(mass_minimal)
    first = _lyrics_provider(MediaItemMetadata(lyrics="first plain"))
    second = _lyrics_provider(MediaItemMetadata(lrc_lyrics="[00:01.00]sync"))
    track = create_track("spotify", "t1")

    with patch.object(mass_minimal, "get_providers", return_value=[first, second]):
        result = await metadata_controller.get_track_lyrics(track)

    assert result == ("first plain", "[00:01.00]sync")
    first.get_track_metadata.assert_awaited_once()
    second.get_track_metadata.assert_awaited_once()
    first_lookup = first.get_track_metadata.await_args.args[0]
    second_lookup = second.get_track_metadata.await_args.args[0]
    assert first_lookup is not track
    assert second_lookup is not track
    assert first_lookup is not second_lookup
    assert (first_lookup.item_id, first_lookup.name, first_lookup.provider) == (
        track.item_id,
        track.name,
        track.provider,
    )
    assert first_lookup.metadata.lyrics is None
    assert second_lookup.metadata.lyrics == "first plain"
    assert second_lookup.metadata.lrc_lyrics is None
    assert track.metadata.lyrics is None


async def test_track_lyrics_passes_full_track_to_metadata_providers(
    metadata_controller: MetaDataController,
    mass_minimal: MusicAssistant,
) -> None:
    """Metadata providers receive the full track fetched from its own provider."""
    owner = User(user_id="user-owner", username="owner", role=UserRole.USER)
    mass_minimal.music = MusicController(mass_minimal)
    own_provider = MagicMock(spec=MusicProvider)
    own_provider.instance_id = "spotify_theirs"
    own_provider.domain = "spotify"
    own_provider.type = ProviderType.MUSIC
    own_provider.available = True
    own_provider.is_streaming_provider = True
    own_provider.supported_features = {ProviderFeature.LYRICS}
    own_provider.initialized = MagicMock(is_set=MagicMock(return_value=True))
    full_track = create_track("spotify_theirs", "t1")
    full_track.metadata.lyrics = "own provider plain"
    own_provider.get_track = AsyncMock(return_value=full_track)
    mass_minimal._providers = {"spotify_theirs": own_provider}
    set_music_source_access(
        mass_minimal,
        {"spotify_theirs": ProviderAccess(owner=owner.user_id, sharing=ProviderSharing.PRIVATE)},
    )
    track = create_track("spotify_theirs", "t1")
    metadata_provider = _lyrics_provider(MediaItemMetadata(lrc_lyrics="[00:01.00]sync"))
    user_token = current_user.set(owner)
    try:
        with patch.object(mass_minimal, "get_providers", return_value=[metadata_provider]):
            result = await metadata_controller.get_track_lyrics(track)
    finally:
        current_user.reset(user_token)

    assert result == ("own provider plain", "[00:01.00]sync")
    metadata_provider.get_track_metadata.assert_awaited_once()
    lookup_track = metadata_provider.get_track_metadata.await_args.args[0]
    assert lookup_track is not full_track
    assert (lookup_track.item_id, lookup_track.name, lookup_track.provider) == (
        full_track.item_id,
        full_track.name,
        full_track.provider,
    )
    assert lookup_track.metadata.lyrics == "own provider plain"
    assert full_track.metadata.lyrics == "own provider plain"
    assert track.metadata.lyrics is None


async def test_track_lyrics_returns_first_plain_result_after_searching_providers(
    metadata_controller: MetaDataController,
    mass_minimal: MusicAssistant,
) -> None:
    """The first plain result is retained when no provider supplies LRC."""
    mass_minimal.music = MusicController(mass_minimal)
    first = _lyrics_provider(MediaItemMetadata(lyrics="first plain"))
    second = _lyrics_provider(MediaItemMetadata(lyrics="second plain"))
    track = create_track("spotify", "t1")
    track.metadata.lyrics = "track plain"

    with patch.object(mass_minimal, "get_providers", return_value=[first, second]):
        result = await metadata_controller.get_track_lyrics(track)

    assert result == ("track plain", None)
    first.get_track_metadata.assert_awaited_once()
    second.get_track_metadata.assert_awaited_once()


async def test_track_lyrics_returns_lrc_in_plain_field_immediately(
    metadata_controller: MetaDataController,
    mass_minimal: MusicAssistant,
) -> None:
    """LRC-formatted text in the plain field is recognized as synchronized lyrics."""
    mass_minimal.music = MusicController(mass_minimal)
    first = _lyrics_provider(MediaItemMetadata(lyrics="[00:01.00]sync"))
    second = _lyrics_provider(MediaItemMetadata(lrc_lyrics="[00:02.00]later"))
    track = create_track("spotify", "t1")

    with patch.object(mass_minimal, "get_providers", return_value=[first, second]):
        result = await metadata_controller.get_track_lyrics(track)

    assert result == ("[00:01.00]sync", "[00:01.00]sync")
    first.get_track_metadata.assert_awaited_once()
    second.get_track_metadata.assert_not_awaited()


async def test_track_lyrics_continues_after_provider_error(
    metadata_controller: MetaDataController,
    mass_minimal: MusicAssistant,
) -> None:
    """A provider failure does not stop the search or discard a plain fallback."""
    mass_minimal.music = MusicController(mass_minimal)
    failed = _lyrics_provider(RuntimeError("lookup failed"))
    plain = _lyrics_provider(MediaItemMetadata(lyrics="plain fallback"))
    track = create_track("spotify", "t1")

    with patch.object(mass_minimal, "get_providers", return_value=[failed, plain]):
        result = await metadata_controller.get_track_lyrics(track)

    assert result == ("plain fallback", None)
    failed.get_track_metadata.assert_awaited_once()
    plain.get_track_metadata.assert_awaited_once()


async def test_track_lyrics_returns_none_when_no_provider_finds_lyrics(
    metadata_controller: MetaDataController,
    mass_minimal: MusicAssistant,
) -> None:
    """An empty lookup returns no plain or synchronized lyrics."""
    mass_minimal.music = MusicController(mass_minimal)
    empty = _lyrics_provider(None)
    track = create_track("spotify", "t1")

    with patch.object(mass_minimal, "get_providers", return_value=[empty]):
        result = await metadata_controller.get_track_lyrics(track)

    assert result == (None, None)
    empty.get_track_metadata.assert_awaited_once()


async def test_track_lyrics_continues_after_plain_lyrics_from_own_provider(
    metadata_controller: MetaDataController,
    mass_minimal: MusicAssistant,
) -> None:
    """Plain lyrics from a track's own provider do not prevent searching metadata providers."""
    mass_minimal.music = MusicController(mass_minimal)
    own_provider = MagicMock(spec=MusicProvider)
    own_provider.supported_features = {ProviderFeature.LYRICS}
    own_provider.is_streaming_provider = True
    full_track = create_track("spotify", "t1")
    full_track.metadata.lyrics = "own plain"
    lyrics_provider = _lyrics_provider(MediaItemMetadata(lrc_lyrics="[00:01.00]sync"))
    track = create_track("spotify", "t1")

    with (
        patch.object(mass_minimal.music, "get_visible_provider", return_value=own_provider),
        patch.object(
            mass_minimal.music.tracks, "get_provider_item", new=AsyncMock(return_value=full_track)
        ),
        patch.object(mass_minimal, "get_providers", return_value=[lyrics_provider]),
    ):
        result = await metadata_controller.get_track_lyrics(track)

    assert result == ("own plain", "[00:01.00]sync")
    lyrics_provider.get_track_metadata.assert_awaited_once()


async def test_track_lyrics_keeps_plain_lyrics_from_fetched_local_track(
    metadata_controller: MetaDataController,
    mass_minimal: MusicAssistant,
) -> None:
    """Subsonic and Feiniu may return plain lyrics only when their full track is fetched."""
    mass_minimal.music = MusicController(mass_minimal)
    own_provider = MagicMock(spec=MusicProvider)
    own_provider.supported_features = {ProviderFeature.LYRICS}
    own_provider.is_streaming_provider = False
    full_track = create_track("subsonic", "t1")
    full_track.metadata.lyrics = "local plain"
    provider_item_lookup = AsyncMock(return_value=full_track)
    lyrics_provider = _lyrics_provider(MediaItemMetadata(lrc_lyrics="[00:01.00]sync"))
    track = create_track("subsonic", "t1")

    with (
        patch.object(mass_minimal.music, "get_visible_provider", return_value=own_provider),
        patch.object(
            mass_minimal.music.tracks,
            "get_provider_item",
            new=provider_item_lookup,
        ),
        patch.object(mass_minimal, "get_providers", return_value=[lyrics_provider]),
    ):
        result = await metadata_controller.get_track_lyrics(track)

    assert result == ("local plain", None)
    provider_item_lookup.assert_awaited_once_with(track.item_id, track.provider)
    lyrics_provider.get_track_metadata.assert_not_awaited()


@pytest.mark.parametrize(
    ("local_lyrics", "expected", "lookup_expected"),
    [
        ("embedded plain", ("embedded plain", None), False),
        (None, (None, "[00:01.00]sync"), True),
    ],
)
async def test_track_lyrics_skips_metadata_lookup_for_local_lyrics(
    metadata_controller: MetaDataController,
    mass_minimal: MusicAssistant,
    local_lyrics: str | None,
    expected: tuple[str | None, str | None],
    lookup_expected: bool,
) -> None:
    """Embedded local lyrics are authoritative, while an empty local source falls back online."""
    mass_minimal.music = MusicController(mass_minimal)
    own_provider = MagicMock(spec=MusicProvider)
    own_provider.supported_features = set()
    own_provider.is_streaming_provider = False
    lyrics_provider = _lyrics_provider(MediaItemMetadata(lrc_lyrics="[00:01.00]sync"))
    track = create_track("filesystem_local", "local-track")
    track.metadata.lyrics = local_lyrics
    provider_item_lookup = AsyncMock()

    with (
        patch.object(mass_minimal.music, "get_visible_provider", return_value=own_provider),
        patch.object(
            mass_minimal.music.tracks,
            "get_provider_item",
            new=provider_item_lookup,
        ),
        patch.object(mass_minimal, "get_providers", return_value=[lyrics_provider]),
    ):
        result = await metadata_controller.get_track_lyrics(track)

    assert result == expected
    provider_item_lookup.assert_not_awaited()
    if lookup_expected:
        lyrics_provider.get_track_metadata.assert_awaited_once()
    else:
        lyrics_provider.get_track_metadata.assert_not_awaited()


@pytest.mark.parametrize(
    ("refreshed_lyrics", "refreshed_lrc", "expected"),
    [
        ("refreshed plain", None, ("refreshed plain", None)),
        ("refreshed plain", "[00:02.00]sync", ("refreshed plain", "[00:02.00]sync")),
        ("[00:03.00]sync", None, ("[00:03.00]sync", "[00:03.00]sync")),
        (None, None, (None, None)),
    ],
)
async def test_track_lyrics_returns_library_lyrics_after_refresh(
    metadata_controller: MetaDataController,
    mass_minimal: MusicAssistant,
    refreshed_lyrics: str | None,
    refreshed_lrc: str | None,
    expected: tuple[str | None, str | None],
) -> None:
    """A library track returns lyrics updated by refresh without an additional lookup."""
    mass_minimal.music = MusicController(mass_minimal)
    track = create_track("library", "1")
    track.provider = "library"
    track.metadata.lyrics = "caller plain"
    stored_track = create_track("spotify", "t1")
    stored_track.item_id, stored_track.provider = "1", "library"
    stored_track.metadata.lyrics = "stored plain"

    async def _refresh(refreshed_track: Track, **_kwargs: object) -> None:
        refreshed_track.metadata.lyrics = refreshed_lyrics
        refreshed_track.metadata.lrc_lyrics = refreshed_lrc

    refresh = AsyncMock(side_effect=_refresh)
    own_provider_lookup = MagicMock(return_value=None)
    metadata_provider = _lyrics_provider(MediaItemMetadata(lrc_lyrics="[00:01.00]live sync"))

    with (
        patch.object(mass_minimal.music.tracks, "get", new=AsyncMock(return_value=stored_track)),
        patch.object(metadata_controller, "_update_track_metadata", new=refresh),
        patch.object(mass_minimal.music, "get_visible_provider", new=own_provider_lookup),
        patch.object(mass_minimal, "get_providers", return_value=[metadata_provider]),
    ):
        result = await metadata_controller.get_track_lyrics(track)

    assert result == expected
    refresh.assert_awaited_once_with(stored_track, force_refresh=False)
    own_provider_lookup.assert_not_called()
    metadata_provider.get_track_metadata.assert_not_awaited()


@pytest.mark.parametrize(
    ("provider_available", "is_streaming", "stored_lyrics", "expected", "refresh_expected"),
    [
        (True, False, "local plain", ("local plain", None), False),
        (True, False, None, ("refreshed plain", None), True),
        (True, True, "streaming plain", ("refreshed plain", None), True),
        (False, False, "stored plain", ("refreshed plain", None), True),
    ],
)
async def test_track_lyrics_only_skips_library_refresh_for_local_lyrics(
    metadata_controller: MetaDataController,
    mass_minimal: MusicAssistant,
    provider_available: bool,
    is_streaming: bool,
    stored_lyrics: str | None,
    expected: tuple[str | None, str | None],
    refresh_expected: bool,
) -> None:
    """Only lyrics from an available local source make a library refresh unnecessary."""
    mass_minimal.music = MusicController(mass_minimal)
    provider_instance = "spotify_1" if is_streaming else "local_1"
    mapping = ProviderMapping(
        item_id="source-track",
        provider_domain="spotify" if is_streaming else "filesystem_local",
        provider_instance=provider_instance,
    )
    track = create_track("library", "1")
    track.provider = "library"
    stored_track = create_track("library", "1")
    stored_track.provider = "library"
    stored_track.provider_mappings = {mapping}
    stored_track.metadata.lyrics = stored_lyrics

    async def _refresh(refreshed_track: Track, **_kwargs: object) -> None:
        refreshed_track.metadata.lyrics = "refreshed plain"
        refreshed_track.metadata.lrc_lyrics = None

    refresh = AsyncMock(side_effect=_refresh)
    source_provider = MagicMock(spec=MusicProvider)
    source_provider.is_streaming_provider = is_streaming
    get_provider = MagicMock(return_value=source_provider if provider_available else None)

    with (
        patch.object(
            mass_minimal.music.tracks,
            "get",
            new=AsyncMock(return_value=stored_track),
        ),
        patch.object(metadata_controller, "_update_track_metadata", new=refresh),
        patch.object(mass_minimal, "get_provider", new=get_provider),
    ):
        result = await metadata_controller.get_track_lyrics(track)

    assert result == expected
    if refresh_expected:
        refresh.assert_awaited_once_with(stored_track, force_refresh=False)
    else:
        refresh.assert_not_awaited()
