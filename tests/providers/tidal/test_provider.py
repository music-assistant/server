"""Test Tidal Provider integration."""

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import (
    ExternalID,
    MediaType,
    ProviderFeature,
    ProviderSharing,
)
from music_assistant_models.errors import LoginFailed, MediaNotFoundError, SetupFailedError
from music_assistant_models.media_items import Album, Artist, Playlist, Track

from music_assistant.providers.tidal.accounts import is_nameable
from music_assistant.providers.tidal.provider import SUPPORTED_FEATURES, TidalProvider
from tests.common import use_real_create_task


@pytest.fixture
def mass_mock() -> Mock:
    """Return a mock MusicAssistant instance."""
    mass = Mock()
    mass.http_session = AsyncMock()
    mass.metadata.locale = "en_US"
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.cache.set = AsyncMock()
    mass.cache.delete = AsyncMock()
    mass.config.get = Mock(return_value={})
    use_real_create_task(mass)
    return mass


@pytest.fixture
def manifest_mock() -> Mock:
    """Return a mock provider manifest."""
    manifest = Mock()
    manifest.domain = "tidal"
    return manifest


@pytest.fixture
def config_mock() -> Mock:
    """Return a mock provider config."""
    config = Mock()
    config.name = "Tidal Test"
    config.instance_id = "tidal_test"
    config.enabled = True
    config.get_value.side_effect = lambda key: {
        "auth_token": "mock_access_token",
        "refresh_token": "mock_refresh_token",
        "expiry_time": 1234567890,
        "user_id": "12345",
        "log_level": "INFO",
    }.get(key, "INFO" if "log" in key else None)
    return config


@pytest.fixture
def provider(mass_mock: Mock, manifest_mock: Mock, config_mock: Mock) -> TidalProvider:
    """Return a TidalProvider instance."""
    return TidalProvider(mass_mock, manifest_mock, config_mock)


async def test_provider_initialization(
    mass_mock: Mock, manifest_mock: Mock, config_mock: Mock
) -> None:
    """Test provider initialization creates all managers."""
    provider = TidalProvider(mass_mock, manifest_mock, config_mock)

    assert provider.auth is not None
    assert provider.api is not None
    assert provider.library is not None
    assert provider.media is not None
    assert provider.playlists is not None
    assert provider.recommendations_manager is not None
    assert provider.streaming is not None


_SETUP_VALUES = {
    "auth_token": "mock_access_token",
    "refresh_token": "mock_refresh_token",
    "expiry_time": 1234567890,
    "user_id": "12345",
}


async def test_handle_async_init_success(provider: TidalProvider) -> None:
    """Test successful async initialization."""
    with (
        patch.object(
            provider,
            "get_setup_value",
            side_effect=lambda key, default=None: _SETUP_VALUES.get(key, default),
        ),
        patch.object(provider.auth, "initialize", new_callable=AsyncMock) as mock_init,
        patch.object(provider.api, "get", new_callable=AsyncMock) as mock_get,
        patch.object(provider, "get_user", new_callable=AsyncMock) as mock_get_user,
        patch.object(provider.auth, "update_user_info", new_callable=AsyncMock),
    ):
        mock_init.return_value = True
        mock_get.return_value = {"userId": "12345", "sessionId": "session_123"}
        mock_get_user.return_value = {"id": "12345", "username": "testuser"}

        await provider.handle_async_init()

        mock_init.assert_called_once()
        mock_get.assert_called_with("sessions")


def _set_other_instances(
    mass_mock: Mock,
    others: dict[str, tuple[str | None, bool]],
    names: dict[str, str] | None = None,
    default_names: dict[str, str] | None = None,
    access: dict[str, dict[str, Any]] | None = None,
    loaded: frozenset[str] = frozenset(),
) -> None:
    """Store the other Tidal instances, as {instance_id: (user_id, enabled)}, as raw configs."""
    names = names or {}
    default_names = default_names or {}
    access = access or {}
    raw_configs = {
        iid: {
            "type": "music",
            "domain": "tidal",
            "instance_id": iid,
            "enabled": enabled,
            "name": names.get(iid),
            "default_name": default_names.get(iid),
            **({"access": access[iid]} if iid in access else {}),
        }
        for iid, (_, enabled) in others.items()
    }
    mass_mock.config.get = Mock(return_value=raw_configs)
    mass_mock.config.get_provider_configs = AsyncMock(side_effect=AssertionError)
    mass_mock.config.get_provider_setup_value = Mock(side_effect=lambda iid, _key: others[iid][0])
    mass_mock.get_provider = Mock(
        side_effect=lambda iid, *_args, **_kwargs: Mock() if iid in loaded else None
    )


@pytest.mark.parametrize(
    ("others", "own_user_id", "conflict", "winner"),
    [
        ({"tidal_a": ("12345", True)}, "12345", True, "Tidal A"),
        ({"tidal_z": ("12345", True)}, "12345", False, None),
        ({"tidal_a": ("12345", False)}, "12345", False, None),
        ({"tidal_a": ("999", True)}, "12345", False, None),
        ({"tidal_a": ("12345", True)}, None, False, None),
        ({"tidal_a": ("12345", False), "tidal_b": ("12345", True)}, "12345", True, "Tidal B"),
    ],
    ids=[
        "lower_id_wins",
        "higher_id_ignored",
        "disabled_ignored",
        "other_account",
        "no_user_id",
        "disabled_and_enabled_holders",
    ],
)
async def test_handle_async_init_account_conflict(
    provider: TidalProvider,
    mass_mock: Mock,
    others: dict[str, tuple[str | None, bool]],
    own_user_id: str | None,
    conflict: bool,
    winner: str | None,
) -> None:
    """Only a lower-id enabled instance on the same account blocks this one."""
    _set_other_instances(mass_mock, others, {"tidal_a": "Tidal A", "tidal_b": "Tidal B"})
    values = {**_SETUP_VALUES, "user_id": own_user_id}

    with (
        patch.object(
            provider,
            "get_setup_value",
            side_effect=lambda key, default=None: values.get(key, default),
        ),
        patch.object(provider.auth, "initialize", new_callable=AsyncMock) as mock_init,
        patch.object(provider.api, "get", new_callable=AsyncMock) as mock_get,
        patch.object(provider, "get_user", new_callable=AsyncMock),
        patch.object(provider.auth, "update_user_info", new_callable=AsyncMock),
    ):
        mock_init.return_value = True
        mock_get.return_value = {"userId": "12345", "sessionId": "session_123"}

        if not conflict:
            await provider.handle_async_init()
            mock_init.assert_called_once()
            return

        with pytest.raises(SetupFailedError) as exc_info:
            await provider.handle_async_init()

    assert exc_info.value.translation_key == "account_used_by_other_instance"
    assert list(exc_info.value.translation_args) == [winner]
    assert exc_info.value.translation_owner == provider.translation_owner
    mock_init.assert_not_called()


@pytest.mark.parametrize(
    ("default_names", "expected"),
    [
        ({"tidal_a": "Tidal [1]"}, "Tidal [1]"),
        ({}, "tidal_a"),
    ],
    ids=["default_name", "instance_id"],
)
async def test_account_conflict_names_an_unnamed_winner(
    provider: TidalProvider,
    mass_mock: Mock,
    default_names: dict[str, str],
    expected: str,
) -> None:
    """An instance without a custom name is reported by its default name, else its id."""
    _set_other_instances(mass_mock, {"tidal_a": ("12345", True)}, default_names=default_names)
    values = {**_SETUP_VALUES, "user_id": "12345"}

    with (
        patch.object(
            provider,
            "get_setup_value",
            side_effect=lambda key, default=None: values.get(key, default),
        ),
        pytest.raises(SetupFailedError) as exc_info,
    ):
        await provider.handle_async_init()

    assert list(exc_info.value.translation_args) == [expected]


_PRIVATE_ACCESS = {"owner": "user-1", "sharing": "private"}


async def _init_expecting_conflict(provider: TidalProvider) -> SetupFailedError:
    """Run the init with the own account set to 12345 and return the error it raises."""
    values = {**_SETUP_VALUES, "user_id": "12345"}
    with (
        patch.object(
            provider,
            "get_setup_value",
            side_effect=lambda key, default=None: values.get(key, default),
        ),
        pytest.raises(SetupFailedError) as exc_info,
    ):
        await provider.handle_async_init()
    return exc_info.value


async def test_account_conflict_sees_a_holder_hidden_from_the_caller(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """The check reads the stored configs, so a private holder of another member still blocks."""
    _set_other_instances(
        mass_mock, {"tidal_a": ("12345", True)}, access={"tidal_a": _PRIVATE_ACCESS}
    )

    error = await _init_expecting_conflict(provider)

    assert error.translation_key == "account_used_by_private_instance"


async def test_private_holder_is_not_named(provider: TidalProvider, mass_mock: Mock) -> None:
    """A holder that is private to its owner is reported without a name."""
    _set_other_instances(
        mass_mock,
        {"tidal_a": ("12345", True)},
        names={"tidal_a": "Secret"},
        access={"tidal_a": _PRIVATE_ACCESS},
    )

    error = await _init_expecting_conflict(provider)

    assert error.translation_key == "account_used_by_private_instance"
    assert not error.translation_args
    assert "Secret" not in str(error)


async def test_holder_shared_with_everyone_is_named(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """A holder shared with every user is reported by name."""
    _set_other_instances(
        mass_mock,
        {"tidal_a": ("12345", True)},
        names={"tidal_a": "Family"},
        access={"tidal_a": {"owner": "user-1", "sharing": "everyone"}},
    )

    error = await _init_expecting_conflict(provider)

    assert error.translation_key == "account_used_by_other_instance"
    assert list(error.translation_args) == ["Family"]


async def test_members_only_holder_is_not_named(provider: TidalProvider, mass_mock: Mock) -> None:
    """Members-only excludes guests, so the holder stays unnamed."""
    _set_other_instances(
        mass_mock,
        {"tidal_a": ("12345", True)},
        names={"tidal_a": "Members"},
        access={"tidal_a": {"owner": "user-1", "sharing": "members"}},
    )

    error = await _init_expecting_conflict(provider)

    assert error.translation_key == "account_used_by_private_instance"


async def test_selected_holder_is_not_named(provider: TidalProvider, mass_mock: Mock) -> None:
    """A holder shared with only some users is not visible to everyone, so it stays unnamed."""
    _set_other_instances(
        mass_mock,
        {"tidal_a": ("12345", True)},
        names={"tidal_a": "Some"},
        access={"tidal_a": {"owner": "user-1", "sharing": "selected", "shared_users": ["u2"]}},
    )

    error = await _init_expecting_conflict(provider)

    assert error.translation_key == "account_used_by_private_instance"


async def test_a_not_loaded_lower_holder_refuses_even_with_a_loaded_higher_one(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """Only the lowest-id rule refuses at load: the lower holder is named, loaded or not."""
    _set_other_instances(
        mass_mock,
        {"tidal_a": ("12345", True), "tidal_z": ("12345", True)},
        names={"tidal_a": "Tidal A", "tidal_z": "Tidal Z"},
        loaded=frozenset({"tidal_z"}),
    )
    provider.config.instance_id = "tidal_m"

    error = await _init_expecting_conflict(provider)

    assert list(error.translation_args) == ["Tidal A"]


@pytest.mark.parametrize(
    ("enabled", "loaded"),
    [(True, False), (False, True), (True, True)],
    ids=["not_loaded", "disabled_but_loaded", "enabled_and_loaded"],
)
async def test_a_higher_instance_never_blocks_the_init(
    provider: TidalProvider, mass_mock: Mock, enabled: bool, loaded: bool
) -> None:
    """A higher-id holder, loaded or not, leaves the account to this instance."""
    _set_other_instances(
        mass_mock,
        {"tidal_z": ("12345", enabled)},
        loaded=frozenset({"tidal_z"}) if loaded else frozenset(),
    )
    values = {**_SETUP_VALUES, "user_id": "12345"}

    with (
        patch.object(
            provider,
            "get_setup_value",
            side_effect=lambda key, default=None: values.get(key, default),
        ),
        patch.object(provider.auth, "initialize", new_callable=AsyncMock) as mock_init,
        patch.object(provider.api, "get", new_callable=AsyncMock) as mock_get,
        patch.object(provider, "get_user", new_callable=AsyncMock),
        patch.object(provider.auth, "update_user_info", new_callable=AsyncMock),
    ):
        mock_init.return_value = True
        mock_get.return_value = {"userId": "12345", "sessionId": "session_123"}

        await provider.handle_async_init()

    mock_init.assert_called_once()
    cast("AsyncMock", mass_mock.unload_provider_with_error).assert_not_called()


async def test_a_failed_login_does_not_unload_the_running_holder(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """The handover only happens after a load: a failed login leaves the holder running."""
    _set_other_instances(mass_mock, {"tidal_z": ("12345", True)}, loaded=frozenset({"tidal_z"}))
    values = {**_SETUP_VALUES, "user_id": "12345"}

    with (
        patch.object(
            provider,
            "get_setup_value",
            side_effect=lambda key, default=None: values.get(key, default),
        ),
        patch.object(provider.auth, "initialize", new_callable=AsyncMock, return_value=False),
        pytest.raises(LoginFailed),
    ):
        await provider.handle_async_init()

    cast("AsyncMock", mass_mock.unload_provider_with_error).assert_not_called()


async def _hand_over(
    provider: TidalProvider,
    mass_mock: Mock,
    others: dict[str, tuple[str | None, bool]],
    loaded: frozenset[str],
    own_user_id: str | None = "12345",
) -> AsyncMock:
    """Run the post-load hook and return the unload mock."""
    _set_other_instances(mass_mock, others, loaded=loaded)
    mass_mock.unload_provider_with_error = AsyncMock()
    values = {**_SETUP_VALUES, "user_id": own_user_id}
    with patch.object(
        provider,
        "get_setup_value",
        side_effect=lambda key, default=None: values.get(key, default),
    ):
        await provider.loaded_in_mass()
    return cast("AsyncMock", mass_mock.unload_provider_with_error)


async def test_loading_unloads_a_running_higher_holder_with_an_error_naming_this_instance(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """Enabling the lower instance hands the account over and tells the other one why."""
    provider.config.access = None
    unload = await _hand_over(
        provider, mass_mock, {"tidal_z": ("12345", True)}, frozenset({"tidal_z"})
    )

    unload.assert_awaited_once()
    assert unload.await_args is not None
    instance_id, error = unload.await_args.args
    assert instance_id == "tidal_z"
    assert error.translation_key == "account_used_by_other_instance"
    assert list(error.translation_args) == ["Tidal Test"]
    assert error.translation_owner == provider.translation_owner


async def test_the_handover_never_unloads_a_lower_instance(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """A lower instance that registered while this one finished loading keeps the account."""
    unload = await _hand_over(
        provider,
        mass_mock,
        {"tidal_a": ("12345", True), "tidal_z": ("12345", True)},
        frozenset({"tidal_a", "tidal_z"}),
    )

    unload.assert_awaited_once()
    assert unload.await_args is not None
    assert unload.await_args.args[0] == "tidal_z"


async def test_the_handover_error_does_not_name_a_private_instance(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """The unloaded holder may belong to another user, so a private keeper stays unnamed."""
    provider.config.access = ProviderAccess(owner="user-1", sharing=ProviderSharing.PRIVATE)
    unload = await _hand_over(
        provider, mass_mock, {"tidal_z": ("12345", True)}, frozenset({"tidal_z"})
    )

    assert unload.await_args is not None
    error = unload.await_args.args[1]
    assert error.translation_key == "account_used_by_private_instance"
    assert not error.translation_args


@pytest.mark.parametrize(
    ("others", "loaded", "own_user_id"),
    [
        ({"tidal_z": ("12345", False)}, frozenset({"tidal_z"}), "12345"),
        ({"tidal_z": ("12345", True)}, frozenset(), "12345"),
        ({"tidal_z": ("999", True)}, frozenset({"tidal_z"}), "12345"),
        ({}, frozenset(), "12345"),
        ({"tidal_z": ("12345", True)}, frozenset({"tidal_z"}), None),
    ],
    ids=["disabled", "not_loaded", "other_account", "no_holders", "no_user_id"],
)
async def test_the_handover_leaves_everyone_else_alone(
    provider: TidalProvider,
    mass_mock: Mock,
    others: dict[str, tuple[str | None, bool]],
    loaded: frozenset[str],
    own_user_id: str | None,
) -> None:
    """Only enabled, running holders of the same account are unloaded."""
    unload = await _hand_over(provider, mass_mock, others, loaded, own_user_id)

    unload.assert_not_called()


async def test_enabling_the_lower_instance_while_the_higher_one_runs_cannot_dead_end(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """The lower instance loads and takes over, so the higher one is the one left refused."""
    provider.config.access = None
    others: dict[str, tuple[str | None, bool]] = {"tidal_z": ("12345", True)}
    _set_other_instances(mass_mock, others, loaded=frozenset({"tidal_z"}))
    values = {**_SETUP_VALUES, "user_id": "12345"}

    with (
        patch.object(
            provider,
            "get_setup_value",
            side_effect=lambda key, default=None: values.get(key, default),
        ),
        patch.object(provider.auth, "initialize", new_callable=AsyncMock, return_value=True),
        patch.object(provider.api, "get", new_callable=AsyncMock, return_value={}),
        patch.object(provider, "get_user", new_callable=AsyncMock),
        patch.object(provider.auth, "update_user_info", new_callable=AsyncMock),
    ):
        await provider.handle_async_init()
        mass_mock.unload_provider_with_error = AsyncMock()
        await provider.loaded_in_mass()

    cast("AsyncMock", mass_mock.unload_provider_with_error).assert_awaited_once()


async def test_a_lower_instance_enabled_during_the_login_refuses_at_the_late_check(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """The early check passes, the lower holder appears during the login, the late check fires."""
    _set_other_instances(mass_mock, {"tidal_a": ("12345", False)})
    provider.config.instance_id = "tidal_z"
    values = {**_SETUP_VALUES, "user_id": "12345"}

    async def enable_lower_holder(_auth_data: str) -> bool:
        _set_other_instances(mass_mock, {"tidal_a": ("12345", True)}, names={"tidal_a": "Tidal A"})
        return True

    with (
        patch.object(
            provider,
            "get_setup_value",
            side_effect=lambda key, default=None: values.get(key, default),
        ),
        patch.object(provider.auth, "initialize", side_effect=enable_lower_holder) as mock_init,
        patch.object(provider.api, "get", new_callable=AsyncMock, return_value={}),
        patch.object(provider, "get_user", new_callable=AsyncMock),
        patch.object(provider.auth, "update_user_info", new_callable=AsyncMock) as mock_update,
        pytest.raises(SetupFailedError) as exc_info,
    ):
        await provider.handle_async_init()

    mock_init.assert_called_once()
    mock_update.assert_awaited_once()
    assert exc_info.value.translation_key == "account_used_by_other_instance"
    assert list(exc_info.value.translation_args) == ["Tidal A"]


async def test_cancelling_the_post_load_task_does_not_abort_the_handover(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """The unload is shielded, so a cancelled post-load task cannot leave the holder running."""
    _set_other_instances(mass_mock, {"tidal_z": ("12345", True)}, loaded=frozenset({"tidal_z"}))
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def slow_unload(*_args: Any) -> None:
        started.set()
        await release.wait()
        finished.set()

    mass_mock.unload_provider_with_error = AsyncMock(side_effect=slow_unload)
    values = {**_SETUP_VALUES, "user_id": "12345"}
    with patch.object(
        provider,
        "get_setup_value",
        side_effect=lambda key, default=None: values.get(key, default),
    ):
        task = asyncio.ensure_future(provider.loaded_in_mass())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=1)


@pytest.mark.parametrize(
    ("access", "nameable"),
    [
        (None, True),
        (ProviderAccess(owner="u", sharing=ProviderSharing.EVERYONE), True),
        (ProviderAccess(owner="u", sharing=ProviderSharing.MEMBERS), False),
        (ProviderAccess(owner="u", sharing=ProviderSharing.PRIVATE), False),
    ],
    ids=["no_access", "everyone", "members", "private"],
)
def test_is_nameable(access: ProviderAccess | None, nameable: bool) -> None:
    """Only an instance every user can see, guests included, may be named."""
    config = Mock(access=access)

    assert is_nameable(config) is nameable


async def test_handle_async_init_migrates_iso_expiry(provider: TidalProvider) -> None:
    """Test a legacy ISO-string expiry_time is converted to a timestamp and persisted."""
    values = dict(_SETUP_VALUES)
    values["expiry_time"] = "2026-01-01T12:00:00+00:00"

    with (
        patch.object(
            provider,
            "get_setup_value",
            side_effect=lambda key, default=None: values.get(key, default),
        ),
        patch.object(provider, "_update_setup_data") as mock_update,
        patch.object(provider.auth, "initialize", new_callable=AsyncMock) as mock_init,
        patch.object(provider.api, "get", new_callable=AsyncMock) as mock_get,
        patch.object(provider, "get_user", new_callable=AsyncMock) as mock_get_user,
        patch.object(provider.auth, "update_user_info", new_callable=AsyncMock),
    ):
        mock_init.return_value = True
        mock_get.return_value = {"userId": "12345", "sessionId": "session_123"}
        mock_get_user.return_value = {"id": "12345"}

        await provider.handle_async_init()

    expected_ts = datetime.fromisoformat("2026-01-01T12:00:00+00:00").timestamp()
    mock_update.assert_called_once_with("expiry_time", expected_ts)
    auth_blob = json.loads(mock_init.call_args[0][0])
    assert auth_blob["expires_at"] == expected_ts


async def test_handle_async_init_missing_auth() -> None:
    """Test async initialization fails with missing auth."""
    mass = Mock()
    mass.http_session = AsyncMock()
    mass.metadata.locale = "en_US"

    manifest = Mock()
    manifest.domain = "tidal"

    config = Mock()
    config.name = "Tidal Test"
    config.instance_id = "tidal_test"
    config.enabled = True
    config.get_value.side_effect = lambda key: "INFO" if "log" in key else None  # Missing auth data

    provider = TidalProvider(mass, manifest, config)

    with (
        patch.object(provider, "get_setup_value", return_value=None),
        pytest.raises(LoginFailed, match="Missing authentication data"),
    ):
        await provider.handle_async_init()


async def test_handle_async_init_auth_failed(provider: TidalProvider) -> None:
    """Test async initialization fails when auth initialize fails."""
    with (
        patch.object(
            provider,
            "get_setup_value",
            side_effect=lambda key, default=None: _SETUP_VALUES.get(key, default),
        ),
        patch.object(provider.auth, "initialize", new_callable=AsyncMock) as mock_init,
    ):
        mock_init.return_value = False

        with pytest.raises(LoginFailed, match="Failed to authenticate with Tidal"):
            await provider.handle_async_init()


async def test_search_delegates_to_media(provider: TidalProvider) -> None:
    """Test search delegates to media manager."""
    with patch.object(provider.media, "search", new_callable=AsyncMock) as mock_search:
        mock_search.return_value = Mock()

        await provider.search("test query", [MediaType.ARTIST], limit=10)

        mock_search.assert_called_with("test query", [MediaType.ARTIST], 10)


async def test_get_similar_tracks_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_similar_tracks delegates to media manager."""
    with patch.object(provider.media, "get_similar_tracks", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = []

        result = await provider.get_similar_tracks("123", limit=30)

        mock_get.assert_called_with("123", 30)
        assert result == []


async def test_get_artist_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_artist delegates to media manager."""
    with patch.object(provider.media, "get_artist", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = Mock(spec=Artist)

        result = await provider.get_artist("123")

        mock_get.assert_called_with("123")
        assert result is not None


async def test_get_album_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_album delegates to media manager."""
    with patch.object(provider.media, "get_album", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = Mock(spec=Album)

        result = await provider.get_album("123")

        mock_get.assert_called_with("123")
        assert result is not None


async def test_get_track_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_track delegates to media manager."""
    with patch.object(provider.media, "get_track", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = Mock(spec=Track)

        result = await provider.get_track("123")

        mock_get.assert_called_with("123")
        assert result is not None


async def test_get_track_by_external_id_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_track_by_external_id delegates to media manager."""
    with patch.object(
        provider.media, "get_track_by_external_id", new_callable=AsyncMock
    ) as mock_get:
        mock_get.return_value = Mock(spec=Track)

        result = await provider.get_track_by_external_id("US1234567890", ExternalID.ISRC)

        mock_get.assert_called_with("US1234567890", ExternalID.ISRC)
        assert result is not None


async def test_get_album_by_external_id_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_album_by_external_id delegates to media manager."""
    with patch.object(
        provider.media, "get_album_by_external_id", new_callable=AsyncMock
    ) as mock_get:
        mock_get.return_value = Mock(spec=Album)

        result = await provider.get_album_by_external_id("00602547852748", ExternalID.BARCODE)

        mock_get.assert_called_with("00602547852748", ExternalID.BARCODE)
        assert result is not None


def test_supported_features_include_external_id_lookups() -> None:
    """Test the track/album external-id lookup features are advertised."""
    assert ProviderFeature.TRACK_BY_EXTERNAL_ID in SUPPORTED_FEATURES
    assert ProviderFeature.ALBUM_BY_EXTERNAL_ID in SUPPORTED_FEATURES


async def test_get_playlist_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_playlist delegates to media manager."""
    with patch.object(provider.media, "get_playlist", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = Mock(spec=Playlist)

        result = await provider.get_playlist("123")

        mock_get.assert_called_with("123")
        assert result is not None


async def test_get_album_tracks_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_album_tracks delegates to media manager."""
    with patch.object(provider.media, "get_album_tracks", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = []

        result = await provider.get_album_tracks("123")

        mock_get.assert_called_with("123")
        assert result == []


async def test_get_artist_albums_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_artist_albums delegates to media manager."""
    with patch.object(provider.media, "get_artist_albums", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = []

        result = await provider.get_artist_albums("123")

        mock_get.assert_called_with("123")
        assert result == []


async def test_get_artist_tracks_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_artist_tracks delegates to media manager."""
    with patch.object(provider.media, "get_artist_tracks", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = []

        result = await provider.get_artist_tracks("123")

        mock_get.assert_called_with("123")
        assert result == []


async def test_get_artist_toptracks_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_artist_toptracks delegates to media manager."""
    with patch.object(provider.media, "get_artist_toptracks", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = []

        await provider.get_artist_toptracks("123")

        mock_get.assert_called_with("123")


async def test_get_playlist_tracks_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_playlist_tracks delegates to media manager."""
    with patch.object(provider.media, "get_playlist_tracks", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = []

        await provider.get_playlist_tracks("123", page=2)

        mock_get.assert_called_with("123", 2)


async def test_get_stream_details_delegates_to_streaming(provider: TidalProvider) -> None:
    """Test get_stream_details delegates to streaming manager."""
    with patch.object(provider.streaming, "get_stream_details", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = Mock()

        result = await provider.get_stream_details("123")

        mock_get.assert_called_with("123")
        assert result is not None


async def test_get_item_mapping(provider: TidalProvider) -> None:
    """Test get_item_mapping creates correct ItemMapping."""
    mapping = provider.get_item_mapping(MediaType.ARTIST, "123", "Test Artist")

    assert mapping.media_type == MediaType.ARTIST
    assert mapping.item_id == "123"
    assert mapping.provider == provider.instance_id
    assert mapping.name == "Test Artist"


async def test_get_library_artists_delegates_to_library(provider: TidalProvider) -> None:
    """Test get_library_artists delegates to library manager."""

    async def mock_generator() -> AsyncGenerator[Any]:
        yield Mock(spec=Artist)
        yield Mock(spec=Artist)

    with patch.object(provider.library, "get_artists", return_value=mock_generator()):
        artists = []
        async for artist in provider.get_library_artists():
            artists.append(artist)

        assert len(artists) == 2


async def test_get_library_albums_delegates_to_library(provider: TidalProvider) -> None:
    """Test get_library_albums delegates to library manager."""

    async def mock_generator() -> AsyncGenerator[Any]:
        yield Mock(spec=Album)

    with patch.object(provider.library, "get_albums", return_value=mock_generator()):
        albums = []
        async for album in provider.get_library_albums():
            albums.append(album)

        assert len(albums) == 1


async def test_get_library_tracks_delegates_to_library(provider: TidalProvider) -> None:
    """Test get_library_tracks delegates to library manager."""

    async def mock_generator() -> AsyncGenerator[Any]:
        yield Mock(spec=Track)
        yield Mock(spec=Track)
        yield Mock(spec=Track)

    with patch.object(provider.library, "get_tracks", return_value=mock_generator()):
        tracks = []
        async for track in provider.get_library_tracks():
            tracks.append(track)

        assert len(tracks) == 3


async def test_get_library_playlists_delegates_to_library(provider: TidalProvider) -> None:
    """Test get_library_playlists delegates to library manager."""

    async def mock_generator() -> AsyncGenerator[Any]:
        yield Mock(spec=Playlist)

    with patch.object(provider.library, "get_playlists", return_value=mock_generator()):
        playlists = []
        async for playlist in provider.get_library_playlists():
            playlists.append(playlist)

        assert len(playlists) == 1


async def test_library_add_delegates_to_library(provider: TidalProvider) -> None:
    """Test library_add delegates to library manager."""
    with patch.object(provider.library, "add_item", new_callable=AsyncMock) as mock_add:
        mock_add.return_value = True
        item = Mock()

        result = await provider.library_add(item)

        assert result is True
        mock_add.assert_called_with(item)


async def test_library_remove_delegates_to_library(provider: TidalProvider) -> None:
    """Test library_remove delegates to library manager."""
    with patch.object(provider.library, "remove_item", new_callable=AsyncMock) as mock_remove:
        mock_remove.return_value = True

        result = await provider.library_remove("123", MediaType.TRACK)

        assert result is True
        mock_remove.assert_called_with("123", MediaType.TRACK)


async def test_create_playlist_delegates_to_playlists(provider: TidalProvider) -> None:
    """Test create_playlist delegates to playlist manager."""
    with patch.object(provider.playlists, "create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = Mock(spec=Playlist)

        await provider.create_playlist("New Playlist", media_types={MediaType.TRACK})

        mock_create.assert_called_with("New Playlist")


async def test_add_playlist_tracks_delegates_to_playlists(provider: TidalProvider) -> None:
    """Test add_playlist_tracks delegates to playlist manager."""
    with patch.object(provider.playlists, "add_tracks", new_callable=AsyncMock) as mock_add:
        await provider.add_playlist_tracks("123", ["track1", "track2"])

        mock_add.assert_called_with("123", ["track1", "track2"])


async def test_remove_playlist_tracks_delegates_to_playlists(provider: TidalProvider) -> None:
    """Test remove_playlist_tracks delegates to playlist manager."""
    with patch.object(provider.playlists, "remove_tracks", new_callable=AsyncMock) as mock_remove:
        await provider.remove_playlist_tracks("123", (1, 2, 3))

        mock_remove.assert_called_with("123", (1, 2, 3))


async def test_get_user(provider: TidalProvider) -> None:
    """Test get_user fetches user data."""
    with patch.object(provider.api, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = {"id": "123", "username": "testuser"}

        user = await provider.get_user("123")

        assert user["id"] == "123"
        mock_get.assert_called_with("users/123")


async def test_redirect_cached_id_hit(provider: TidalProvider, mass_mock: Mock) -> None:
    """Test redirect_cached_id returns the cached live id on a cache hit."""
    mass_mock.cache.get = AsyncMock(return_value="live_456")

    result = await provider.redirect_cached_id("stale_123")

    assert result == "live_456"
    mass_mock.cache.get.assert_called_with(
        "stale_123",
        provider=provider.instance_id,
        category=2,  # CACHE_CATEGORY_ISRC_MAP
    )


async def test_redirect_cached_id_miss(provider: TidalProvider, mass_mock: Mock) -> None:
    """Test redirect_cached_id returns the original id on a cache miss."""
    mass_mock.cache.get = AsyncMock(return_value=None)

    result = await provider.redirect_cached_id("stale_123")

    assert result == "stale_123"


async def test_note_replaced_track_schedules_healing(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """Test a REPLACED projection is turned into a cached redirect and a mapping heal."""
    item = {
        "id": "live_456",
        "type": "tracks",
        "meta": {
            "replacement": {"status": "REPLACED", "original": {"id": "stale_123", "type": "tracks"}}
        },
    }

    mass_mock.create_task = Mock(side_effect=lambda coro, **_kw: coro.close())

    with patch.object(provider, "_apply_replacement", new_callable=AsyncMock) as apply_mock:
        provider.note_replaced_track(item)

    mass_mock.create_task.assert_called_once()
    apply_mock.assert_called_once_with("stale_123", "live_456")


async def test_apply_replacement_caches_and_heals(provider: TidalProvider, mass_mock: Mock) -> None:
    """Test _apply_replacement stores the redirect and heals an existing library mapping."""
    lib_track = Mock()
    lib_track.item_id = 42
    mass_mock.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=lib_track)

    with patch.object(provider, "_heal_track_mapping", new_callable=AsyncMock) as heal_mock:
        await provider._apply_replacement("stale_123", "live_456")

    mass_mock.cache.set.assert_called_once_with(
        key="stale_123",
        data="live_456",
        provider=provider.instance_id,
        category=2,  # CACHE_CATEGORY_ISRC_MAP
        persistent=True,
        expiration=86400 * 90,
    )
    heal_mock.assert_called_once_with(42, "stale_123", "live_456")


async def test_apply_replacement_without_library_track(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """Test _apply_replacement still caches the redirect when no library track exists."""
    mass_mock.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=None)

    with patch.object(provider, "_heal_track_mapping", new_callable=AsyncMock) as heal_mock:
        await provider._apply_replacement("stale_123", "live_456")

    mass_mock.cache.set.assert_called_once()
    heal_mock.assert_not_called()


@pytest.mark.parametrize(
    "meta",
    [
        {},
        {"replacement": {"status": "ORIGINAL"}},
        {"replacement": {"status": "NOT_REPLACED", "original": {"id": "live_456"}}},
        {"replacement": {"status": "REPLACED", "original": {"id": "live_456"}}},
    ],
    ids=["no-meta", "original", "not-replaced", "replaced-with-same-id"],
)
async def test_note_replaced_track_ignores_non_replacements(
    provider: TidalProvider, mass_mock: Mock, meta: dict[str, Any]
) -> None:
    """Test nothing is scheduled unless the id actually changed."""
    provider.note_replaced_track({"id": "live_456", "type": "tracks", "meta": meta})

    mass_mock.create_task.assert_not_called()


async def test_resolve_live_track_id_cache_hit_different(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """Test resolve_live_track_id returns the cached id when it is still live."""
    mass_mock.cache.get = AsyncMock(return_value="live_456")

    # The liveness check must bypass the cached provider wrapper, so it is
    # served by the media manager directly; the wrapper must not be touched.
    with (
        patch.object(provider.media, "get_track", new_callable=AsyncMock),
        patch.object(provider, "get_track", new_callable=AsyncMock) as mock_cached,
    ):
        result = await provider.resolve_live_track_id("stale_123")

    assert result == "live_456"
    mock_cached.assert_not_called()


async def test_resolve_live_track_id_cache_hit_dead_reresolves(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """Test a dead cached id is dropped and re-resolved from the ISRC (double churn)."""
    mass_mock.cache.get = AsyncMock(return_value="dead_456")
    mass_mock.cache.delete = AsyncMock()
    mass_mock.cache.set = AsyncMock()
    lib_track = Mock()
    lib_track.item_id = 1
    lib_track.external_ids = [(ExternalID.ISRC, "US1234567890")]
    mass_mock.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=lib_track)
    mass_mock.create_task = Mock(side_effect=lambda coro, **_kw: coro.close())

    with (
        patch.object(
            provider.media,
            "get_track",
            new_callable=AsyncMock,
            side_effect=MediaNotFoundError("gone"),
        ),
        patch.object(provider, "get_track", new_callable=AsyncMock) as mock_cached,
        patch.object(provider.media, "get_track_id_by_isrc", new_callable=AsyncMock) as mock_get,
        patch.object(provider, "_heal_track_mapping", new_callable=AsyncMock),
    ):
        mock_get.return_value = "new_789"

        result = await provider.resolve_live_track_id("stale_123")

    assert result == "new_789"
    mass_mock.cache.delete.assert_called_once()
    # the liveness check must not consult the cached wrapper, which would keep
    # serving a stale track for a dead id
    mock_cached.assert_not_called()


async def test_resolve_live_track_id_cache_hit_same(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """Test resolve_live_track_id returns None when the cached id equals the input."""
    mass_mock.cache.get = AsyncMock(return_value="123")

    result = await provider.resolve_live_track_id("123")

    assert result is None


async def test_resolve_live_track_id_no_library_item(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """Test resolve_live_track_id returns None when there is no library item."""
    mass_mock.cache.get = AsyncMock(return_value=None)
    mass_mock.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=None)

    result = await provider.resolve_live_track_id("123")

    assert result is None


async def test_resolve_live_track_id_no_isrc(provider: TidalProvider, mass_mock: Mock) -> None:
    """Test resolve_live_track_id returns None when the library item has no ISRC."""
    mass_mock.cache.get = AsyncMock(return_value=None)
    lib_track = Mock()
    lib_track.external_ids = [(ExternalID.BARCODE, "some-id")]
    mass_mock.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=lib_track)

    result = await provider.resolve_live_track_id("123")

    assert result is None


async def test_resolve_live_track_id_isrc_lookup_empty(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """Test resolve_live_track_id returns None when the ISRC lookup returns no data."""
    mass_mock.cache.get = AsyncMock(return_value=None)
    lib_track = Mock()
    lib_track.external_ids = [(ExternalID.ISRC, "US1234567890")]
    mass_mock.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=lib_track)

    with patch.object(provider.media, "get_track_id_by_isrc", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = None

        result = await provider.resolve_live_track_id("123")

    assert result is None


async def test_resolve_live_track_id_not_stale(provider: TidalProvider, mass_mock: Mock) -> None:
    """Test resolve_live_track_id returns None when the resolved id equals the input."""
    mass_mock.cache.get = AsyncMock(return_value=None)
    lib_track = Mock()
    lib_track.external_ids = [(ExternalID.ISRC, "US1234567890")]
    mass_mock.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=lib_track)

    with patch.object(provider.media, "get_track_id_by_isrc", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = "123"

        result = await provider.resolve_live_track_id("123")

    assert result is None


async def test_resolve_live_track_id_stale_caches_and_schedules_heal(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """Test resolve_live_track_id returns the live id, caches it, and schedules a heal."""
    mass_mock.cache.get = AsyncMock(return_value=None)
    mass_mock.cache.set = AsyncMock()

    lib_track = Mock()
    lib_track.item_id = 1
    lib_track.external_ids = [(ExternalID.ISRC, "US1234567890")]
    mass_mock.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=lib_track)

    # Discard the scheduled coroutine (avoid "never awaited" warnings) while
    # still recording the call for assertions.
    mass_mock.create_task = Mock(side_effect=lambda coro, **_kw: coro.close())

    with (
        patch.object(provider.media, "get_track_id_by_isrc", new_callable=AsyncMock) as mock_get,
        patch.object(provider, "_heal_track_mapping", new_callable=AsyncMock) as mock_heal,
    ):
        mock_get.return_value = "NEW"

        result = await provider.resolve_live_track_id("123")

    assert result == "NEW"
    mass_mock.cache.set.assert_called_once_with(
        key="123",
        data="NEW",
        provider=provider.instance_id,
        category=2,  # CACHE_CATEGORY_ISRC_MAP
        persistent=True,
        expiration=86400 * 90,
    )
    mass_mock.create_task.assert_called_once()
    mock_heal.assert_called_once_with(1, "123", "NEW")


async def test_heal_track_mapping_adds_before_removing(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """Test _heal_track_mapping adds the new mapping before removing the stale one."""
    call_order: list[str] = []

    async def _fake_add(*_args: Any, **_kwargs: Any) -> None:
        call_order.append("add")

    async def _fake_remove(*_args: Any, **_kwargs: Any) -> None:
        call_order.append("remove")

    new_mapping = Mock()
    new_mapping.provider_instance = provider.instance_id

    live_track = Mock()
    live_track.provider_mappings = [new_mapping]

    mass_mock.music.tracks.add_provider_mappings = AsyncMock(side_effect=_fake_add)
    mass_mock.music.tracks.remove_provider_mapping = AsyncMock(side_effect=_fake_remove)

    with patch.object(provider, "get_track", new_callable=AsyncMock) as mock_get_track:
        mock_get_track.return_value = live_track

        await provider._heal_track_mapping(1, "stale_123", "NEW")

    assert call_order == ["add", "remove"]
    mass_mock.music.tracks.add_provider_mappings.assert_called_once_with(1, [new_mapping])
    mass_mock.music.tracks.remove_provider_mapping.assert_called_once_with(
        1, provider.instance_id, "stale_123"
    )


async def test_heal_track_mapping_no_matching_mapping_does_nothing(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """Test _heal_track_mapping does nothing when the live track has no matching mapping."""
    other_mapping = Mock()
    other_mapping.provider_instance = "some_other_instance"

    live_track = Mock()
    live_track.provider_mappings = [other_mapping]

    mass_mock.music.tracks.add_provider_mappings = AsyncMock()
    mass_mock.music.tracks.remove_provider_mapping = AsyncMock()

    with patch.object(provider, "get_track", new_callable=AsyncMock) as mock_get_track:
        mock_get_track.return_value = live_track

        await provider._heal_track_mapping(1, "stale_123", "NEW")

    mass_mock.music.tracks.add_provider_mappings.assert_not_called()
    mass_mock.music.tracks.remove_provider_mapping.assert_not_called()


async def test_heal_track_mapping_swallows_errors(provider: TidalProvider) -> None:
    """Test _heal_track_mapping swallows expected errors without raising."""
    with patch.object(provider, "get_track", new_callable=AsyncMock) as mock_get_track:
        mock_get_track.side_effect = MediaNotFoundError("Track not found")

        # Should not raise.
        await provider._heal_track_mapping(1, "stale_123", "NEW")


@pytest.fixture
def track_mock() -> Mock:
    """Return a mock track with a duration, for on_played tests."""
    track = Mock(spec=Track)
    track.duration = 200
    return track


def _capture_create_task(provider: TidalProvider) -> list[Any]:
    """Replace mass.create_task with one that captures the coroutine instead of running it."""
    captured: list[Any] = []
    provider.mass.create_task = captured.append  # type: ignore[method-assign,assignment]
    return captured


async def test_on_played_ignores_non_track_media_type(provider: TidalProvider) -> None:
    """Test on_played schedules nothing for a non-TRACK media type."""
    captured = _capture_create_task(provider)

    await provider.on_played(
        media_type=MediaType.ALBUM,
        prov_item_id="123",
        fully_played=True,
        position=200,
        media_item=Mock(spec=Album),
        is_playing=False,
    )

    assert captured == []


async def test_on_played_ignores_progress_pings(provider: TidalProvider, track_mock: Mock) -> None:
    """Test on_played schedules nothing for the periodic is_playing=True progress pings."""
    captured = _capture_create_task(provider)

    await provider.on_played(
        media_type=MediaType.TRACK,
        prov_item_id="123",
        fully_played=False,
        position=30,
        media_item=track_mock,
        is_playing=True,
    )

    assert captured == []


async def test_on_played_ignores_non_track_media_item(provider: TidalProvider) -> None:
    """Test the isinstance guard: a non-Track media_item is never reported."""
    captured = _capture_create_task(provider)

    await provider.on_played(
        media_type=MediaType.TRACK,
        prov_item_id="123",
        fully_played=True,
        position=200,
        media_item=Mock(spec=Album),
        is_playing=False,
    )

    assert captured == []


async def test_on_played_schedules_report_for_terminal_calls(
    provider: TidalProvider, track_mock: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test a terminal TRACK call schedules report_played with matching positional args."""
    report_played_mock = AsyncMock()
    monkeypatch.setattr(provider.play_reporting, "report_played", report_played_mock)
    captured = _capture_create_task(provider)

    await provider.on_played(
        media_type=MediaType.TRACK,
        prov_item_id="123",
        fully_played=True,
        position=195,
        media_item=track_mock,
        is_playing=False,
    )

    assert len(captured) == 1
    await captured[0]
    report_played_mock.assert_awaited_once_with("123", 200, 195, True)


async def test_on_played_schedules_report_for_stop_or_skip_too(
    provider: TidalProvider, track_mock: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test a stop/skip still reaches report_played, which decides itself whether to send."""
    report_played_mock = AsyncMock()
    monkeypatch.setattr(provider.play_reporting, "report_played", report_played_mock)
    captured = _capture_create_task(provider)

    await provider.on_played(
        media_type=MediaType.TRACK,
        prov_item_id="123",
        fully_played=False,
        position=30,
        media_item=track_mock,
        is_playing=False,
    )

    assert len(captured) == 1
    await captured[0]
    report_played_mock.assert_awaited_once_with("123", 200, 30, False)
