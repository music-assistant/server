"""Tests for the MetaDataController public API."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import ProviderFeature, ProviderSharing, ProviderType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import MediaItemPalette

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
    from music_assistant.controllers.metadata import MetaDataController
    from music_assistant.mass import MusicAssistant

# every test here registers image ids, which are persisted to the cache database
pytestmark = pytest.mark.usefixtures("cache_database")


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
    metadata_controller: MetaDataController, mass_minimal: MusicAssistant
) -> None:
    """The caller's copy of a library track is never written back, the stored item is used."""
    owner = User(user_id="user-owner", username="owner", role=UserRole.USER)
    member = User(user_id="user-member", username="member", role=UserRole.USER)
    mass_minimal.music = MusicController(mass_minimal)
    mass_minimal.streams = MagicMock()
    mass_minimal.streams.audio_analysis.get_track_audio_metadata = AsyncMock(return_value=None)
    mass_minimal.webserver = MagicMock()
    mass_minimal.webserver.auth.get_user = AsyncMock(return_value=None)
    mass_minimal.webserver.auth.list_users = AsyncMock(return_value=[])
    # the minimal server runs no cache database
    mass_minimal.cache.get = AsyncMock(return_value=None)  # type: ignore[method-assign]
    mass_minimal.cache.set = AsyncMock()  # type: ignore[method-assign]
    await mass_minimal.music._setup_database()
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
