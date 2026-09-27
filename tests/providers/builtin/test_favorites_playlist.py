"""Tests that the built-in favorites playlist is the asking user's own."""

from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.auth import User, UserRole
from music_assistant_models.media_items import Track

from music_assistant.providers.builtin import BuiltinProvider
from music_assistant.providers.builtin.constants import RANDOM_TRACKS

MODULE = "music_assistant.providers.builtin"


def _make_provider() -> BuiltinProvider:
    """Return a BuiltinProvider instance with a minimal mock mass."""
    provider = BuiltinProvider.__new__(BuiltinProvider)
    provider.mass = MagicMock()
    provider.config = MagicMock()
    provider.config.instance_id = "builtin"
    provider.manifest = MagicMock()
    provider.manifest.domain = "builtin"
    return provider


async def test_the_favorites_playlist_is_cached_per_user(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The cached tracks are looked up under the id of the user asking."""
    provider = _make_provider()
    cached = AsyncMock(return_value=[])
    monkeypatch.setattr(provider, "_random_favorite_tracks", cached)
    monkeypatch.setattr(
        f"{MODULE}.get_current_user",
        lambda: User(user_id="user-a", username="a", role=UserRole.USER),
    )

    await provider._get_builtin_playlist_random_favorite_tracks()

    cached.assert_awaited_once_with("user-a")


async def test_the_favorites_playlist_of_nobody_is_cached_as_such(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Without a user the lookup carries no user id."""
    provider = _make_provider()
    cached = AsyncMock(return_value=[])
    monkeypatch.setattr(provider, "_random_favorite_tracks", cached)
    monkeypatch.setattr(f"{MODULE}.get_current_user", lambda: None)

    await provider._get_builtin_playlist_random_favorite_tracks()

    cached.assert_awaited_once_with(None)


async def test_a_cached_playlist_is_served_with_the_asking_users_state(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The state whoever filled the cache left on the tracks is not the asking user's."""
    provider = _make_provider()
    cached = Track(item_id="1", provider="library", name="Cached", provider_mappings=set())
    cached.favorite = True
    monkeypatch.setattr(provider, "_get_builtin_playlist_tracks", AsyncMock(return_value=[cached]))
    monkeypatch.setattr(f"{MODULE}.get_current_user", lambda: None)

    tracks = await provider.get_playlist_tracks(RANDOM_TRACKS)

    assert [x.favorite for x in tracks] == [None]
