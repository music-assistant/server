"""Tests that the built-in favorites playlist is the asking user's own."""

from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.auth import User, UserRole

from music_assistant.providers.builtin import BuiltinProvider

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
