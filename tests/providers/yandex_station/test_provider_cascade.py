"""
Tests for the Yandex Station provider credential cascade and silent re-auth.

Covers:

* ``_init_session`` cascade: fast path, x_token→music refresh, refresh_token
  rotation, remember-session disabled, terminal failure.
* ``_silent_reauth`` runtime refresh.
* ``_get_speakers_with_reauth`` one-retry semantics on Quasar 401/403.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast
from unittest import mock

import pytest
from music_assistant_models.errors import LoginFailed, ResourceTemporarilyUnavailable
from ya_passport_auth import SecretStr

from music_assistant.providers.yandex_station.constants import (
    CONF_MUSIC_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_REMEMBER_SESSION,
    CONF_X_TOKEN,
)
from music_assistant.providers.yandex_station.provider import YandexStationProvider

_MOD = "music_assistant.providers.yandex_station.provider"


# ── Test harness ───────────────────────────────────────────────────


class _StubConfigValue:
    """Minimal stand-in for a ``ConfigValue`` so ``.value = ...`` works."""

    def __init__(self, value: Any) -> None:
        self.value = value


class _StubConfig:
    """Minimal ProviderConfig stub that supports get_value / .values[key].value=."""

    instance_id = "test_instance"

    def __init__(self, values: dict[str, Any]) -> None:
        self.values: dict[str, _StubConfigValue] = {
            k: _StubConfigValue(v) for k, v in values.items()
        }
        # in-memory setup_data mirror kept in sync by Provider._update_setup_data
        self.setup_data: dict[str, Any] = {}

    def get_value(self, key: str, default: Any = None) -> Any:
        entry = self.values.get(key)
        return entry.value if entry is not None else default


class _StubCoreConfig:
    """Records every set_raw_provider_config_value call for assertions."""

    def __init__(self) -> None:
        self.updates: list[tuple[str, str, Any, bool]] = []
        self._raw: dict[tuple[str, str], Any] = {}

    def set_raw_provider_config_value(
        self,
        instance_id: str,
        key: str,
        value: Any,
        encrypted: bool,
        immediate: bool = False,
    ) -> None:
        _ = immediate  # accepted for signature parity with MA core
        self.updates.append((instance_id, key, value, encrypted))
        self._raw[(instance_id, key)] = value

    def get_raw_provider_config_value(self, instance_id: str, key: str) -> Any:
        """Read-back used by Provider._update_config_value on current MA core."""
        return self._raw.get((instance_id, key))

    def get(self, path: str) -> Any:
        """
        Serve config paths used by Provider.get_setup_value / _update_setup_data.

        Returns an empty setup_data dict so setup-data reads fall through to the
        legacy config value (config.get_value), and a truthy marker for the
        provider-exists precondition in _update_setup_data.
        """
        if path.endswith("/setup_data"):
            return {}
        return {"exists": True}

    def set(self, path: str, value: Any, immediate: bool = False) -> None:
        """Record a setup_data write (mirrors set_raw_provider_config_value)."""
        _ = immediate
        parts = path.split("/")
        instance_id, key = parts[1], parts[-1]
        self.updates.append((instance_id, key, value, True))
        self._raw[(instance_id, key)] = value

    def encrypt_string(self, value: str) -> str:
        """Identity encrypt for tests."""
        return value

    def decrypt_string(self, value: str) -> str:
        """Identity decrypt for tests."""
        return value


class _StubMass:
    """Minimal MusicAssistant stub: only ``.config`` is exercised."""

    def __init__(self) -> None:
        self.config = _StubCoreConfig()


def _updates(provider: YandexStationProvider) -> list[tuple[str, str, Any, bool]]:
    """Return the update log recorded by the test's ``_StubCoreConfig``."""
    return cast("_StubCoreConfig", provider.mass.config).updates


def _make_provider(config_values: dict[str, Any]) -> YandexStationProvider:
    """Instantiate a provider with a stub mass and config for cascade tests."""
    provider = YandexStationProvider.__new__(YandexStationProvider)
    provider.mass = _StubMass()  # type: ignore[assignment]
    provider.config = _StubConfig(config_values)  # type: ignore[assignment]
    # NB: ``instance_id`` is a read-only @property on the real Provider that
    # delegates to ``config.instance_id`` — we set it on the stub config above.
    provider.logger = logging.getLogger("test_provider")
    provider._session = None
    provider._quasar = None
    provider._http_session = None
    provider._passport_client = None
    provider._pending_discoveries = set()
    provider._mdns_players = {}
    provider._discovery_done = False
    provider._init_lock = asyncio.Lock()
    provider._cascade = provider._build_cascade()
    provider._borrow_source = provider._build_borrow_source()
    return provider


@pytest.fixture
def fake_session_cls() -> Any:
    """Patch YandexSession with a MagicMock class returning a configurable instance."""
    with mock.patch(f"{_MOD}.YandexSession") as cls:
        instance = mock.MagicMock()
        instance.login_token = mock.AsyncMock(return_value=True)
        instance.ensure_music_token = mock.AsyncMock()
        instance.x_token = None
        instance.music_token = None
        instance.refresh_token = None
        cls.return_value = instance
        yield cls


@pytest.fixture(autouse=True)
def fake_http_plumbing() -> Any:
    """Patch ClientSession + PassportClient so _init_session doesn't open real sockets."""
    with (
        mock.patch(f"{_MOD}.ClientSession") as http_cls,
        mock.patch(f"{_MOD}.PassportClient") as pc_cls,
    ):
        http_cls.return_value = mock.MagicMock(closed=False, close=mock.AsyncMock())
        pc_cls.return_value = mock.MagicMock()
        yield http_cls, pc_cls


# ── _init_session cascade ─────────────────────────────────────────


async def test_fast_path_with_music_and_x_token(fake_session_cls: Any) -> None:
    """music_token + x_token valid → fast-path login_token succeeds, no refresh called."""
    provider = _make_provider(
        {
            CONF_MUSIC_TOKEN: "mt",
            CONF_X_TOKEN: "xt",
            CONF_REFRESH_TOKEN: None,
            CONF_REMEMBER_SESSION: True,
        }
    )
    session = fake_session_cls.return_value
    session.login_token.return_value = True

    with (
        mock.patch("ya_passport_auth.ma.cascade.refresh_music_token") as rmt,
        mock.patch("ya_passport_auth.ma.cascade.refresh_credentials") as rcp,
    ):
        ok = await provider._init_session()

    assert ok is True
    session.login_token.assert_awaited()
    session.ensure_music_token.assert_awaited()
    rmt.assert_not_called()
    rcp.assert_not_called()
    assert _updates(provider) == []


async def test_refresh_via_x_token(fake_session_cls: Any) -> None:
    """Fast path fails → refresh_music_token rotates CONF_MUSIC_TOKEN and re-logs in."""
    provider = _make_provider(
        {
            CONF_MUSIC_TOKEN: "mt_stale",
            CONF_X_TOKEN: "xt_good",
            CONF_REFRESH_TOKEN: None,
            CONF_REMEMBER_SESSION: True,
        }
    )
    session = fake_session_cls.return_value
    session.login_token.side_effect = [False, True]  # fast-path fails, retry ok

    with (
        mock.patch("ya_passport_auth.ma.cascade.refresh_music_token") as rmt,
        mock.patch("ya_passport_auth.ma.cascade.refresh_credentials") as rcp,
    ):
        rmt.return_value = SecretStr("mt_fresh")
        ok = await provider._init_session()

    assert ok is True
    rmt.assert_awaited_once()
    rcp.assert_not_called()
    # Config updated with new music_token
    keys_written = [(k, v) for (_inst, k, v, _enc) in _updates(provider)]
    assert (CONF_MUSIC_TOKEN, "mt_fresh") in keys_written


async def test_refresh_via_refresh_token(fake_session_cls: Any) -> None:
    """x_token expired + refresh_token present → full triple rotated & persisted."""
    provider = _make_provider(
        {
            CONF_MUSIC_TOKEN: None,
            CONF_X_TOKEN: "xt_stale",
            CONF_REFRESH_TOKEN: "rt_good",
            CONF_REMEMBER_SESSION: True,
        }
    )
    session = fake_session_cls.return_value
    session.login_token.return_value = True

    new_creds = mock.MagicMock()
    new_creds.x_token = SecretStr("xt_new")
    new_creds.music_token = SecretStr("mt_new")
    new_creds.refresh_token = SecretStr("rt_new")

    with (
        mock.patch("ya_passport_auth.ma.cascade.refresh_music_token") as rmt,
        mock.patch("ya_passport_auth.ma.cascade.refresh_credentials") as rcp,
    ):
        rmt.side_effect = LoginFailed("x_token expired")
        rcp.return_value = new_creds
        ok = await provider._init_session()

    assert ok is True
    rmt.assert_awaited_once()
    rcp.assert_awaited_once()
    keys_written = {k: v for (_inst, k, v, _enc) in _updates(provider)}
    assert keys_written[CONF_X_TOKEN] == "xt_new"
    assert keys_written[CONF_MUSIC_TOKEN] == "mt_new"
    assert keys_written[CONF_REFRESH_TOKEN] == "rt_new"


async def test_terminal_failure_clears_creds(fake_session_cls: Any) -> None:
    """x_token expired and no refresh_token → clear all three config values."""
    provider = _make_provider(
        {
            CONF_MUSIC_TOKEN: "mt_stale",
            CONF_X_TOKEN: "xt_stale",
            CONF_REFRESH_TOKEN: None,
            CONF_REMEMBER_SESSION: True,
        }
    )
    session = fake_session_cls.return_value
    session.login_token.return_value = False  # fast path dead

    with (
        mock.patch("ya_passport_auth.ma.cascade.refresh_music_token") as rmt,
        mock.patch("ya_passport_auth.ma.cascade.refresh_credentials") as rcp,
    ):
        rmt.side_effect = LoginFailed("x_token expired")
        ok = await provider._init_session()

    assert ok is False
    rcp.assert_not_called()
    cleared = {k for (_inst, k, v, _enc) in _updates(provider) if v is None}
    assert cleared == {CONF_MUSIC_TOKEN, CONF_X_TOKEN, CONF_REFRESH_TOKEN}


async def test_remember_session_disabled_skips_refresh(fake_session_cls: Any) -> None:
    """Remember session=False → step 2/3 skipped even if x_token would be present."""
    provider = _make_provider(
        {
            CONF_MUSIC_TOKEN: "mt",
            CONF_X_TOKEN: None,
            CONF_REFRESH_TOKEN: None,
            CONF_REMEMBER_SESSION: False,
        }
    )
    session = fake_session_cls.return_value
    session.login_token.return_value = False  # would fail but shouldn't be hit

    with (
        mock.patch("ya_passport_auth.ma.cascade.refresh_music_token") as rmt,
        mock.patch("ya_passport_auth.ma.cascade.refresh_credentials") as rcp,
    ):
        ok = await provider._init_session()

    assert ok is True
    rmt.assert_not_called()
    rcp.assert_not_called()
    # Nothing written — music_token alone is the entire state
    assert _updates(provider) == []


async def test_music_token_only_with_remember_session_default(fake_session_cls: Any) -> None:
    """music_token without x_token + Remember session on (default) → run as music_token-only."""
    provider = _make_provider(
        {
            CONF_MUSIC_TOKEN: "mt",
            CONF_X_TOKEN: None,
            CONF_REFRESH_TOKEN: None,
            CONF_REMEMBER_SESSION: True,
        }
    )
    session = fake_session_cls.return_value
    session.login_token.return_value = False  # would fail but must not be reached

    with (
        mock.patch("ya_passport_auth.ma.cascade.refresh_music_token") as rmt,
        mock.patch("ya_passport_auth.ma.cascade.refresh_credentials") as rcp,
    ):
        ok = await provider._init_session()

    assert ok is True
    rmt.assert_not_called()
    rcp.assert_not_called()
    assert _updates(provider) == []


async def test_no_credentials_returns_false(fake_session_cls: Any) -> None:  # noqa: ARG001
    """No music_token and no x_token → cannot discover, return False cleanly."""
    provider = _make_provider(
        {
            CONF_MUSIC_TOKEN: None,
            CONF_X_TOKEN: None,
            CONF_REFRESH_TOKEN: None,
            CONF_REMEMBER_SESSION: True,
        }
    )
    ok = await provider._init_session()
    assert ok is False


async def test_network_error_raises_provider_unavailable(
    fake_session_cls: Any,  # noqa: ARG001
) -> None:
    """Generic exception from refresh_music_token → ResourceTemporarilyUnavailable (transient)."""
    provider = _make_provider(
        {
            CONF_MUSIC_TOKEN: None,
            CONF_X_TOKEN: "xt",
            CONF_REFRESH_TOKEN: None,
            CONF_REMEMBER_SESSION: True,
        }
    )
    with mock.patch("ya_passport_auth.ma.cascade.refresh_music_token") as rmt:
        rmt.side_effect = RuntimeError("boom")
        with pytest.raises(ResourceTemporarilyUnavailable):
            await provider._init_session()


async def test_init_session_reuses_existing_healthy_session(
    fake_session_cls: Any,  # noqa: ARG001
) -> None:
    """If _session + http_session already exist and are open, _init_session is a no-op."""
    provider = _make_provider(
        {
            CONF_MUSIC_TOKEN: "mt",
            CONF_X_TOKEN: "xt",
            CONF_REFRESH_TOKEN: None,
            CONF_REMEMBER_SESSION: True,
        }
    )
    # Simulate a prior init that already produced a live session
    # (e.g. via an mDNS-triggered _create_player call racing discover_players).
    existing_http = mock.MagicMock(closed=False, close=mock.AsyncMock())
    existing_session = mock.MagicMock()
    provider._http_session = existing_http
    provider._session = existing_session

    ok = await provider._init_session()

    assert ok is True
    # Existing session must not be replaced or closed.
    assert provider._http_session is existing_http
    assert provider._session is existing_session
    existing_http.close.assert_not_called()


async def test_transient_refresh_failure_does_not_wipe_tokens(fake_session_cls: Any) -> None:
    """ResourceTemporarilyUnavailable from refresh must propagate without clearing creds."""
    provider = _make_provider(
        {
            CONF_MUSIC_TOKEN: "mt",
            CONF_X_TOKEN: "xt",
            CONF_REFRESH_TOKEN: "rt",
            CONF_REMEMBER_SESSION: True,
        }
    )
    # Force fast-path to fail so the cascade calls refresh_music_token.
    fake_session_cls.return_value.login_token = mock.AsyncMock(return_value=False)

    with (
        mock.patch("ya_passport_auth.ma.cascade.refresh_music_token") as rmt,
        mock.patch("ya_passport_auth.ma.cascade.refresh_credentials") as rcp,
    ):
        rmt.side_effect = ResourceTemporarilyUnavailable("network")
        with pytest.raises(ResourceTemporarilyUnavailable):
            await provider._init_session()

    rcp.assert_not_called()
    # No token-clearing writes happened.
    cleared = {k for (_inst, k, v, _enc) in _updates(provider) if v is None}
    assert cleared == set()


# ── Silent runtime re-auth ───────────────────────────────────────


async def test_silent_reauth_via_x_token(fake_session_cls: Any) -> None:  # noqa: ARG001
    """_silent_reauth refreshes music_token from x_token without calling refresh_credentials."""
    provider = _make_provider(
        {
            CONF_MUSIC_TOKEN: "mt_stale",
            CONF_X_TOKEN: "xt_good",
            CONF_REFRESH_TOKEN: None,
            CONF_REMEMBER_SESSION: True,
        }
    )
    # Pretend we already have a session
    provider._session = mock.MagicMock()
    provider._session.login_token = mock.AsyncMock(return_value=True)

    with (
        mock.patch("ya_passport_auth.ma.cascade.refresh_music_token") as rmt,
        mock.patch("ya_passport_auth.ma.cascade.refresh_credentials") as rcp,
    ):
        rmt.return_value = SecretStr("mt_new")
        ok = await provider._silent_reauth()

    assert ok is True
    rmt.assert_awaited_once()
    rcp.assert_not_called()
    written = {k: v for (_inst, k, v, _enc) in _updates(provider)}
    assert written[CONF_MUSIC_TOKEN] == "mt_new"


async def test_silent_reauth_falls_back_to_refresh_token(
    fake_session_cls: Any,  # noqa: ARG001
) -> None:
    """x_token is also expired but refresh_token works → triple rotation wins."""
    provider = _make_provider(
        {
            CONF_MUSIC_TOKEN: "mt_stale",
            CONF_X_TOKEN: "xt_stale",
            CONF_REFRESH_TOKEN: "rt_good",
            CONF_REMEMBER_SESSION: True,
        }
    )
    provider._session = mock.MagicMock()
    provider._session.login_token = mock.AsyncMock(return_value=True)

    new_creds = mock.MagicMock()
    new_creds.x_token = SecretStr("xt_new")
    new_creds.music_token = SecretStr("mt_new")
    new_creds.refresh_token = SecretStr("rt_new")

    with (
        mock.patch("ya_passport_auth.ma.cascade.refresh_music_token") as rmt,
        mock.patch("ya_passport_auth.ma.cascade.refresh_credentials") as rcp,
    ):
        rmt.side_effect = LoginFailed("expired")
        rcp.return_value = new_creds
        ok = await provider._silent_reauth()

    assert ok is True
    rcp.assert_awaited_once()
    written = {k: v for (_inst, k, v, _enc) in _updates(provider)}
    assert written[CONF_X_TOKEN] == "xt_new"
    assert written[CONF_REFRESH_TOKEN] == "rt_new"


async def test_silent_reauth_no_x_token_returns_false(
    fake_session_cls: Any,  # noqa: ARG001
) -> None:
    """No x_token → silent refresh isn't possible."""
    provider = _make_provider(
        {
            CONF_MUSIC_TOKEN: "mt",
            CONF_X_TOKEN: None,
            CONF_REFRESH_TOKEN: None,
            CONF_REMEMBER_SESSION: True,
        }
    )
    ok = await provider._silent_reauth()
    assert ok is False


async def test_silent_reauth_reads_tokens_inside_lock(
    fake_session_cls: Any,  # noqa: ARG001
) -> None:
    """Tokens are read inside the lock so a waiter picks up freshly rotated values."""
    provider = _make_provider(
        {
            CONF_MUSIC_TOKEN: "mt_stale",
            CONF_X_TOKEN: "xt_original",
            CONF_REFRESH_TOKEN: None,
            CONF_REMEMBER_SESSION: True,
        }
    )
    provider._session = mock.MagicMock()
    provider._session.login_token = mock.AsyncMock(return_value=True)

    # Acquire the cascade's rotation lock first so the reauth has to wait.
    # (Reaching into the engine's lock pins the serialization contract the
    # provider relies on for 401 storms.)
    await provider._cascade._lock.acquire()

    with mock.patch("ya_passport_auth.ma.cascade.refresh_music_token") as rmt:
        rmt.return_value = SecretStr("mt_new")
        task = asyncio.create_task(provider._silent_reauth())
        await asyncio.sleep(0)  # let the task reach the lock
        # Rotate x_token while the reauth is blocked on the lock.
        provider.config.values[CONF_X_TOKEN].value = "xt_rotated"
        provider._cascade._lock.release()
        ok = await task

    assert ok is True
    assert rmt.await_args is not None
    assert rmt.await_args.args[0].get_secret() == "xt_rotated"


async def test_rotation_persists_creds_even_when_cookie_refresh_fails(
    fake_session_cls: Any,  # noqa: ARG001
) -> None:
    """
    New creds are stored even when session cookies won't refresh.

    The rotation itself succeeded server-side (the old refresh_token is
    burned), so the fresh triple must be persisted before the cookie
    failure is surfaced — otherwise a retry would rotate with a dead
    token. The reauth reports failure (False) to its caller.
    """
    provider = _make_provider(
        {
            CONF_MUSIC_TOKEN: "mt_stale",
            CONF_X_TOKEN: "xt_stale",
            CONF_REFRESH_TOKEN: "rt_good",
            CONF_REMEMBER_SESSION: True,
        }
    )
    provider._session = mock.MagicMock()
    # Cookie refresh fails even with freshly rotated x_token.
    provider._session.login_token = mock.AsyncMock(return_value=False)

    new_creds = mock.MagicMock()
    new_creds.x_token = SecretStr("xt_new")
    new_creds.music_token = SecretStr("mt_new")
    new_creds.refresh_token = SecretStr("rt_new")

    with (
        mock.patch(
            "ya_passport_auth.ma.cascade.refresh_music_token",
            side_effect=LoginFailed("x expired"),
        ),
        mock.patch("ya_passport_auth.ma.cascade.refresh_credentials", return_value=new_creds),
    ):
        ok = await provider._silent_reauth()

    assert ok is False
    # New creds were persisted before the cookie failure was surfaced.
    written = {k: v for (_inst, k, v, _enc) in _updates(provider)}
    assert written[CONF_X_TOKEN] == "xt_new"
    assert written[CONF_REFRESH_TOKEN] == "rt_new"


# ── Quasar 401/403 retry ─────────────────────────────────────────


async def test_get_speakers_retries_after_401(fake_session_cls: Any) -> None:  # noqa: ARG001
    """First Quasar call raises RuntimeError('…401'), silent reauth succeeds, retry returns list."""
    provider = _make_provider(
        {
            CONF_MUSIC_TOKEN: "mt",
            CONF_X_TOKEN: "xt",
            CONF_REFRESH_TOKEN: None,
            CONF_REMEMBER_SESSION: True,
        }
    )
    provider._session = mock.MagicMock()
    provider._session.login_token = mock.AsyncMock(return_value=True)

    quasar = mock.MagicMock()
    quasar.get_speakers = mock.AsyncMock(
        side_effect=[RuntimeError("https://… returned 401"), [{"id": 1}]]
    )
    provider._quasar = quasar

    with mock.patch("ya_passport_auth.ma.cascade.refresh_music_token") as rmt:
        rmt.return_value = SecretStr("mt_new")
        speakers = await provider._get_speakers_with_reauth()

    assert speakers == [{"id": 1}]
    assert quasar.get_speakers.await_count == 2


async def test_get_speakers_propagates_non_auth_error(
    fake_session_cls: Any,  # noqa: ARG001
) -> None:
    """Non-401/403 RuntimeError is not retried (e.g. 500)."""
    provider = _make_provider(
        {
            CONF_MUSIC_TOKEN: "mt",
            CONF_X_TOKEN: "xt",
            CONF_REFRESH_TOKEN: None,
            CONF_REMEMBER_SESSION: True,
        }
    )
    quasar = mock.MagicMock()
    quasar.get_speakers = mock.AsyncMock(side_effect=RuntimeError("returned 500"))
    provider._quasar = quasar

    with pytest.raises(RuntimeError, match="500"):
        await provider._get_speakers_with_reauth()
    assert quasar.get_speakers.await_count == 1


# ── discover_players: Glagol device_list fallback ─────────────────────


async def test_discover_falls_back_to_glagol_device_list_when_quasar_fails(
    fake_session_cls: Any,  # noqa: ARG001
) -> None:
    """
    When Quasar fails but Glagol works, register from the local list.

    Covers the case: cookies expired / x_token mishap, but music_token still
    valid.  Without the fallback, discovery would return empty and the user
    would be left with an integration that surfaces no players until the
    cloud auth recovers.
    """
    provider = _make_provider(
        {
            CONF_MUSIC_TOKEN: "mt",
            CONF_X_TOKEN: "xt",
            CONF_REFRESH_TOKEN: None,
            CONF_REMEMBER_SESSION: True,
        }
    )
    # setattr() rather than direct attribute assignment to dodge mypy strict
    # mode's [method-assign] complaint in upstream's lint job.
    setattr(provider, "_init_session", mock.AsyncMock(return_value=True))  # noqa: B010
    provider._session = mock.MagicMock()

    quasar = mock.MagicMock()
    # Cloud-side speakers fetch fails (cookie/CSRF auth path)
    quasar.get_speakers = mock.AsyncMock(side_effect=RuntimeError("returned 401"))
    quasar.load_device_config = mock.AsyncMock()
    # But Glagol device_list (music_token auth path) succeeds
    quasar.get_local_speakers = mock.AsyncMock(
        return_value=[
            {
                "device_id": "dev_a",
                "name": "Kitchen Mini",
                "platform": "yandexmini",
                "host": "192.168.1.10",
                "port": 1961,
                "glagol": {"security": {"server_certificate": "..."}},
            },
            {
                "device_id": "dev_b",
                "name": "Bedroom Mini",
                "platform": "yandexmini_2",
                "host": "192.168.1.11",
                "port": 1961,
                "glagol": {},
            },
        ]
    )
    # Patch silent_reauth so the 401 retry inside _get_speakers_with_reauth
    # also fails (otherwise the retry would synthesise a success path).
    setattr(provider, "_silent_reauth", mock.AsyncMock(return_value=False))  # noqa: B010
    setattr(provider, "_create_player", mock.AsyncMock())  # noqa: B010

    with mock.patch(f"{_MOD}.YandexQuasar", return_value=quasar):
        await provider.discover_players()

    # Both devices were registered using the synthetic quasar_info built
    # from the local list.
    create_player = cast("mock.AsyncMock", provider._create_player)
    assert create_player.await_count == 2
    registered_ids = {c.args[0] for c in create_player.await_args_list}
    assert registered_ids == {"ys_dev_a", "ys_dev_b"}
    # Speakers passed in carry the synthesised quasar_info.
    speakers_arg = [c.args[1] for c in create_player.await_args_list]
    for s in speakers_arg:
        qi = s["quasar_info"]
        assert qi["device_id"] in {"dev_a", "dev_b"}
        assert qi["platform"] in {"yandexmini", "yandexmini_2"}
        assert s["host"] in {"192.168.1.10", "192.168.1.11"}
    assert provider._discovery_done is True


async def test_discover_returns_when_both_quasar_and_glagol_fail(
    fake_session_cls: Any,  # noqa: ARG001
) -> None:
    """If both auth paths fail, leave _discovery_done=False so MA retries later."""
    provider = _make_provider(
        {
            CONF_MUSIC_TOKEN: "mt",
            CONF_X_TOKEN: "xt",
            CONF_REFRESH_TOKEN: None,
            CONF_REMEMBER_SESSION: True,
        }
    )
    setattr(provider, "_init_session", mock.AsyncMock(return_value=True))  # noqa: B010
    provider._session = mock.MagicMock()

    quasar = mock.MagicMock()
    quasar.get_speakers = mock.AsyncMock(side_effect=RuntimeError("returned 401"))
    # Glagol device_list returns empty (caught exception inside get_local_speakers)
    quasar.get_local_speakers = mock.AsyncMock(return_value=[])
    setattr(provider, "_silent_reauth", mock.AsyncMock(return_value=False))  # noqa: B010
    setattr(provider, "_create_player", mock.AsyncMock())  # noqa: B010

    with mock.patch(f"{_MOD}.YandexQuasar", return_value=quasar):
        await provider.discover_players()

    cast("mock.AsyncMock", provider._create_player).assert_not_awaited()
    assert provider._discovery_done is False  # retry-friendly
