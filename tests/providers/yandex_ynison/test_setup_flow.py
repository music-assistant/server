"""Tests for the linked-only Ynison setup flow."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlayerType

from music_assistant.models.setup_flow import (
    AbortFlow,
    SetupFlowContext,
    SetupFlowError,
    SetupSession,
)
from music_assistant.providers.yandex_ynison.constants import (
    CONF_MASS_PLAYER_ID,
    CONF_YM_INSTANCE,
    LEGACY_AUTH_KEYS,
)
from music_assistant.providers.yandex_ynison.setup_flow import run_setup

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType


def _player(player_id: str = "living-room", name: str = "Living room") -> MagicMock:
    """Return a real-player-shaped setup option."""
    player = MagicMock()
    player.player_id = player_id
    player.display_name = name
    player.type = PlayerType.PLAYER
    return player


class _SetupSession(SetupSession):
    """Small setup-session fake retaining the form and persisted result."""

    def __init__(
        self,
        providers: dict[str, dict[str, Any]],
        submitted: dict[str, ConfigValueType],
        *,
        players: list[Any] | None = None,
        values: dict[str, ConfigValueType] | None = None,
        setup_data: dict[str, ConfigValueType] | None = None,
    ) -> None:
        self._mass_mock = MagicMock()
        self.mass = self._mass_mock
        self._mass_mock.config.get.return_value = providers
        self._mass_mock.players.all_players.return_value = (
            [_player()] if players is None else players
        )
        self.context = SetupFlowContext(
            kind="setup",
            reason="user",
            domain="yandex_ynison",
            values=values or {},
            setup_data=setup_data or {},
        )
        self._submitted = submitted
        self.entries: list[ConfigEntry] = []
        self.form_kwargs: dict[str, Any] = {}
        self.shown_errors: list[dict[str, str] | None] = []
        self.finished_values: dict[str, ConfigValueType] | None = None

    async def form(
        self,
        entries: list[ConfigEntry],
        step_id: str = "user",
        errors: dict[str, str] | None = None,
        last_step: bool | None = None,
        expires_in: float | None = None,
        translation_params: list[str] | None = None,
    ) -> dict[str, ConfigValueType]:
        """Capture the presented form and return the configured submission."""
        self.entries = entries
        self.form_kwargs = {
            "step_id": step_id,
            "errors": errors,
            "last_step": last_step,
            "expires_in": expires_in,
            "translation_params": translation_params,
        }
        self.shown_errors.append(errors)
        return self._submitted

    async def finish(self, values: dict[str, ConfigValueType]) -> dict[str, str]:
        """Capture setup data as Music Assistant would persist it."""
        self.finished_values = values
        return {"instance_id": "ynison-test"}


def _entry(session: _SetupSession, key: str) -> Any:
    return next(entry for entry in session.entries if entry.key == key)


async def test_setup_collects_only_linked_account_and_concrete_player() -> None:
    """The final form must not expose Auto, a free-form name, or own authentication."""
    session = _SetupSession(
        {"ym-main": {"domain": "yandex_music", "name": "Primary"}},
        {CONF_YM_INSTANCE: "ym-main", CONF_MASS_PLAYER_ID: "living-room"},
    )

    await run_setup(session)

    assert {entry.key for entry in session.entries} == {
        CONF_YM_INSTANCE,
        CONF_MASS_PLAYER_ID,
    }
    selector = _entry(session, CONF_MASS_PLAYER_ID)
    assert selector.required is True
    assert [option.value for option in selector.options] == ["living-room"]
    assert selector.value is None
    assert selector.value != "__auto__"
    assert session.form_kwargs["last_step"] is True
    assert session.finished_values == {
        CONF_YM_INSTANCE: "ym-main",
        CONF_MASS_PLAYER_ID: "living-room",
    }


async def test_no_players_aborts_before_rendering_setup() -> None:
    """A provider instance cannot be created without a concrete target player."""
    session = _SetupSession(
        {"ym-main": {"domain": "yandex_music", "name": "Primary"}},
        {},
        players=[],
    )

    with pytest.raises(AbortFlow) as err:
        await run_setup(session)

    assert err.value.reason == "no_players"


async def test_no_yandex_music_instance_aborts_as_missing_dependency() -> None:
    """An authenticated linked account remains a hard setup dependency."""
    session = _SetupSession({}, {})

    with pytest.raises(AbortFlow) as err:
        await run_setup(session)

    assert err.value.reason == "missing_dependency"


async def test_single_yandex_music_instance_is_preselected() -> None:
    """A sole linked account is selected without introducing another auth path."""
    session = _SetupSession(
        {"ym-main": {"domain": "yandex_music", "name": "Primary"}},
        {CONF_YM_INSTANCE: "ym-main", CONF_MASS_PLAYER_ID: "living-room"},
    )

    await run_setup(session)

    source = _entry(session, CONF_YM_INSTANCE)
    assert source.default_value == "ym-main"
    assert source.value == "ym-main"
    assert [option.value for option in source.options] == ["ym-main"]


async def test_multiple_accounts_require_an_explicit_valid_selection() -> None:
    """A stale legacy source must not silently select the first linked account."""
    session = _SetupSession(
        {
            "ym-a": {"domain": "yandex_music", "name": "A"},
            "ym-b": {"domain": "yandex_music", "name": "B"},
        },
        {CONF_YM_INSTANCE: "ym-b", CONF_MASS_PLAYER_ID: "living-room"},
        setup_data={CONF_YM_INSTANCE: "__own__"},
    )

    await run_setup(session)

    source = _entry(session, CONF_YM_INSTANCE)
    assert source.default_value is None
    assert source.value is None
    assert [option.value for option in source.options] == ["ym-a", "ym-b"]
    assert session.finished_values is not None
    assert session.finished_values[CONF_YM_INSTANCE] == "ym-b"


async def test_reconfigure_clears_legacy_auth_and_drops_legacy_identity() -> None:
    """Reconfigure must leave exactly one credential owner and player-derived identity."""
    setup_data: dict[str, ConfigValueType] = {
        CONF_YM_INSTANCE: "ym-main",
        CONF_MASS_PLAYER_ID: "living-room",
        "publish_name": "Old free-form name",
        "token": "old-music-token",
        "x_token": "old-x-token",
        "account_login": "alice",
        "remember_session": True,
    }
    session = _SetupSession(
        {"ym-main": {"domain": "yandex_music", "name": "Primary"}},
        {CONF_YM_INSTANCE: "ym-main", CONF_MASS_PLAYER_ID: "living-room"},
        setup_data=setup_data,
    )

    await run_setup(session)

    assert _entry(session, CONF_YM_INSTANCE).value == "ym-main"
    assert _entry(session, CONF_MASS_PLAYER_ID).value == "living-room"
    assert session.finished_values == {
        CONF_YM_INSTANCE: "ym-main",
        CONF_MASS_PLAYER_ID: "living-room",
        **dict.fromkeys(LEGACY_AUTH_KEYS),
    }


async def test_new_setup_does_not_persist_legacy_auth_keys() -> None:
    """New instances must persist only the linked account and concrete player."""
    session = _SetupSession(
        {"ym-main": {"domain": "yandex_music", "name": "Primary"}},
        {CONF_YM_INSTANCE: "ym-main", CONF_MASS_PLAYER_ID: "living-room"},
    )

    await run_setup(session)

    assert session.finished_values == {
        CONF_YM_INSTANCE: "ym-main",
        CONF_MASS_PLAYER_ID: "living-room",
    }


async def test_disabled_yandex_music_instances_are_not_linkable() -> None:
    """A disabled credential owner cannot satisfy the runtime dependency."""
    session = _SetupSession(
        {
            "ym-disabled": {
                "domain": "yandex_music",
                "name": "Disabled",
                "enabled": False,
            },
            "ym-enabled": {
                "domain": "yandex_music",
                "name": "Enabled",
                "enabled": True,
            },
        },
        {CONF_YM_INSTANCE: "ym-enabled", CONF_MASS_PLAYER_ID: "living-room"},
    )

    await run_setup(session)

    source = _entry(session, CONF_YM_INSTANCE)
    assert [option.value for option in source.options] == ["ym-enabled"]
    assert source.value == "ym-enabled"


async def test_finish_error_reopens_form_with_preserved_values() -> None:
    """A load failure must not discard the linked-account or player selection."""

    class RetrySession(_SetupSession):
        attempts = 0

        async def finish(self, values: dict[str, ConfigValueType]) -> dict[str, str]:
            self.attempts += 1
            if self.attempts == 1:
                raise SetupFlowError("provider rejected setup", translation_key="invalid_auth")
            return await super().finish(values)

    session = RetrySession(
        {"ym-main": {"domain": "yandex_music", "name": "Primary"}},
        {CONF_YM_INSTANCE: "ym-main", CONF_MASS_PLAYER_ID: "living-room"},
    )

    await run_setup(session)

    assert session.attempts == 2
    assert session.shown_errors == [None, {"base": "invalid_auth"}]
    assert _entry(session, CONF_YM_INSTANCE).value == "ym-main"
    assert _entry(session, CONF_MASS_PLAYER_ID).value == "living-room"
