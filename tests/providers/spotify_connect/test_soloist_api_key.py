"""Tests for the Soloist API key a new setup takes over from another one on the server."""

from __future__ import annotations

from typing import Any
from unittest import mock

from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import ProviderSharing

from music_assistant.constants import CONF_PROVIDERS
from music_assistant.models.setup_flow import SetupFlowContext, SetupSession
from music_assistant.providers.spotify_connect.soloist import find_reusable_api_key

ADMIN = User(user_id="admin", username="admin", role=UserRole.ADMIN)
MEMBER = User(user_id="member", username="member", role=UserRole.USER)


def test_an_admin_takes_over_the_key_of_a_shared_spotify() -> None:
    """A setup by an admin takes over the key of a Spotify instance without an owner."""
    session = _session({"spotify--a": _soloist("spotify", "shared-key")}, ADMIN)
    assert find_reusable_api_key(session) == "shared-key"


def test_spotify_takes_over_the_key_of_spotify_connect() -> None:
    """The Spotify Connect plugin counts as a shared setup."""
    session = _session({"spotify_connect": _soloist("spotify_connect", "connect-key")}, ADMIN)
    assert find_reusable_api_key(session) == "connect-key"


def test_a_members_personal_spotify_takes_over_the_shared_key() -> None:
    """A member without a key of their own takes over the shared one."""
    session = _session({"spotify--a": _soloist("spotify", "shared-key")}, MEMBER)
    assert find_reusable_api_key(session) == "shared-key"


def test_a_members_own_key_wins_over_the_shared_one() -> None:
    """The caller's own personal Spotify comes before the shared setups."""
    session = _session(
        {
            "spotify--a": _soloist("spotify", "shared-key"),
            "spotify_connect": _soloist("spotify_connect", "connect-key"),
            "spotify--b": _soloist("spotify", "member-key", owner=MEMBER.user_id),
        },
        MEMBER,
    )
    assert find_reusable_api_key(session) == "member-key"


def test_another_members_personal_key_is_never_taken() -> None:
    """A personal key of someone else is skipped, also when it is the only key around."""
    providers = {"spotify--b": _soloist("spotify", "other-key", owner="someone-else")}
    assert find_reusable_api_key(_session(providers, MEMBER)) is None
    assert find_reusable_api_key(_session(providers, ADMIN)) is None


def test_different_keys_in_the_first_group_are_not_guessed() -> None:
    """Two shared setups with different keys leave the choice to the user."""
    session = _session(
        {
            "spotify--a": _soloist("spotify", "key-one"),
            "spotify_connect": _soloist("spotify_connect", "key-two"),
        },
        ADMIN,
    )
    assert find_reusable_api_key(session) is None


def test_the_same_key_on_several_setups_is_taken_over() -> None:
    """Several shared setups that hold one and the same key agree on it."""
    session = _session(
        {
            "spotify--a": _soloist("spotify", "shared-key"),
            "spotify_connect": _soloist("spotify_connect", "shared-key"),
        },
        ADMIN,
    )
    assert find_reusable_api_key(session) == "shared-key"


def test_setups_without_a_key_or_consent_are_skipped() -> None:
    """Only a Soloist setup with both a key and the download consent is taken over."""
    session = _session(
        {
            "spotify--a": _soloist("spotify", "no-consent", consent=False),
            "spotify--b": {"domain": "spotify", "setup_data": {}},
            "tidal--c": _soloist("tidal", "not-soloist"),
            "spotify_connect": _soloist("spotify_connect", "connect-key"),
        },
        ADMIN,
    )
    assert find_reusable_api_key(session) == "connect-key"


def test_the_instance_being_set_up_is_skipped() -> None:
    """A reconfigured instance never takes over its own (stored) key."""
    session = _session(
        {"spotify_connect": _soloist("spotify_connect", "own-key")},
        ADMIN,
        instance_id="spotify_connect",
    )
    assert find_reusable_api_key(session) is None


def _soloist(
    domain: str, api_key: str, owner: str | None = None, consent: bool = True
) -> dict[str, Any]:
    """Return a raw provider config holding a Soloist setup."""
    access = ProviderAccess(owner=owner, sharing=ProviderSharing.PRIVATE) if owner else None
    return {
        "domain": domain,
        "setup_data": {"soloist_api_key": api_key, "soloist_download_consent": consent},
        "access": access.to_dict() if access else None,
    }


def _session(
    providers: dict[str, dict[str, Any]], user: User | None, instance_id: str | None = None
) -> SetupSession:
    """Return a setup session whose server holds the given raw provider configs."""
    mass = mock.Mock()

    def _get(key: str, default: Any = None) -> Any:
        if key == CONF_PROVIDERS:
            return providers
        return providers.get(key.removeprefix(f"{CONF_PROVIDERS}/"), default)

    def _get_setup_value(instance_id: str, key: str, default: Any = None) -> Any:
        return providers[instance_id]["setup_data"].get(key, default)

    mass.config.get = _get
    mass.config.get_provider_setup_value = _get_setup_value
    context = SetupFlowContext(
        kind="reconfigure" if instance_id else "setup",
        reason="user",
        domain="spotify",
        instance_id=instance_id,
        user=user,
    )
    return SetupSession(mass, "flow-test", context, mock.AsyncMock())
