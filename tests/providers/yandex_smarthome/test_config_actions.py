"""Tests for the auto-create action wired into get_config_entries.

Exercises _run_auto_create_action via the public _handle_config_actions
entry point so the integration between config values, artifacts, and
the orchestrator is covered end-to-end (with the orchestrator itself
mocked).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import aiohttp
import pytest

from music_assistant.providers import yandex_smarthome as provider_module
from music_assistant.providers.yandex_smarthome import _handle_config_actions
from music_assistant.providers.yandex_smarthome.auto_skill_state import (
    SkillCreationArtifacts,
    SkillCreationState,
)
from music_assistant.providers.yandex_smarthome.constants import (
    CONF_ACTION_AUTO_CREATE,
    CONF_AUTO_CREATE_ARTIFACTS,
    CONF_CLOUD_INSTANCE_ID,
    CONF_DIRECT_CLIENT_SECRET,
    CONF_INSTANCE_NAME,
    CONF_SKILL_ID,
    CONNECTION_TYPE_CLOUD_PLUS,
    CONNECTION_TYPE_DIRECT,
)


def _make_mass() -> MagicMock:
    mass = MagicMock()
    mass.get_provider.return_value = None
    mass.signal_event = MagicMock()
    return mass


@pytest.mark.asyncio
async def test_auto_create_done_populates_skill_id(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """On DONE, CONF_SKILL_ID is written and artifacts are persisted."""

    async def _fake_auto_create(**_kwargs: Any) -> SkillCreationArtifacts:
        return SkillCreationArtifacts(
            state=SkillCreationState.DONE,
            skill_id="new-skill-uuid",
            logo_id="l1",
            oauth_app_id="o1",
        )

    monkeypatch.setattr(
        provider_module,
        "auto_create_skill",
        _fake_auto_create,
    )

    mass = _make_mass()
    values: dict[str, Any] = {
        CONF_INSTANCE_NAME: "My Instance",
        CONF_CLOUD_INSTANCE_ID: "inst-1",
    }

    await _handle_config_actions(
        mass,
        CONF_ACTION_AUTO_CREATE,
        values,
        instance_id=None,
        is_cloud_plus=True,
        connection_type=CONNECTION_TYPE_CLOUD_PLUS,
    )

    assert values[CONF_SKILL_ID] == "new-skill-uuid"
    stored = json.loads(values[CONF_AUTO_CREATE_ARTIFACTS])
    assert stored["state"] == "done"
    assert stored["skill_id"] == "new-skill-uuid"


@pytest.mark.asyncio
async def test_auto_create_failed_preserves_artifacts(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """On FAILED, CONF_SKILL_ID is NOT set so runtime doesn't use a half-skill."""

    async def _fake_auto_create(**_kwargs: Any) -> SkillCreationArtifacts:
        return SkillCreationArtifacts(
            state=SkillCreationState.FAILED,
            skill_id="partial-skill-id",
            last_error="upload failed",
        )

    monkeypatch.setattr(
        provider_module,
        "auto_create_skill",
        _fake_auto_create,
    )

    mass = _make_mass()
    values: dict[str, Any] = {
        CONF_INSTANCE_NAME: "X",
        CONF_CLOUD_INSTANCE_ID: "inst-1",
    }

    await _handle_config_actions(
        mass,
        CONF_ACTION_AUTO_CREATE,
        values,
        instance_id=None,
        is_cloud_plus=True,
        connection_type=CONNECTION_TYPE_CLOUD_PLUS,
    )

    # CONF_SKILL_ID must remain unset — skill is incomplete
    assert CONF_SKILL_ID not in values
    stored = json.loads(values[CONF_AUTO_CREATE_ARTIFACTS])
    assert stored["state"] == "failed"
    assert stored["last_error"] == "upload failed"
    assert stored["skill_id"] == "partial-skill-id"


@pytest.mark.asyncio
async def test_auto_create_precondition_valueerror_becomes_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ValueError from preconditions is caught and stored as a FAILED artifact."""

    async def _raises_value_error(**_kwargs: Any) -> SkillCreationArtifacts:
        msg = "direct mode requires HTTPS"
        raise ValueError(msg)

    monkeypatch.setattr(
        provider_module,
        "auto_create_skill",
        _raises_value_error,
    )

    mass = _make_mass()
    values: dict[str, Any] = {
        CONF_INSTANCE_NAME: "X",
        CONF_DIRECT_CLIENT_SECRET: "secret",
    }

    await _handle_config_actions(
        mass,
        CONF_ACTION_AUTO_CREATE,
        values,
        instance_id=None,
        is_cloud_plus=False,
        connection_type=CONNECTION_TYPE_DIRECT,
    )

    stored = json.loads(values[CONF_AUTO_CREATE_ARTIFACTS])
    assert stored["state"] == "failed"
    assert "HTTPS" in stored["last_error"]
    assert CONF_SKILL_ID not in values


@pytest.mark.asyncio
async def test_auto_create_unexpected_error_caught(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Any other exception from orchestrator also surfaces as FAILED, not a crash."""

    async def _raises_runtime(**_kwargs: Any) -> SkillCreationArtifacts:
        msg = "network unreachable"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        provider_module,
        "auto_create_skill",
        _raises_runtime,
    )

    mass = _make_mass()
    values: dict[str, Any] = {
        CONF_INSTANCE_NAME: "X",
        CONF_CLOUD_INSTANCE_ID: "inst-1",
    }

    await _handle_config_actions(
        mass,
        CONF_ACTION_AUTO_CREATE,
        values,
        instance_id=None,
        is_cloud_plus=True,
        connection_type=CONNECTION_TYPE_CLOUD_PLUS,
    )

    stored = json.loads(values[CONF_AUTO_CREATE_ARTIFACTS])
    assert stored["state"] == "failed"
    assert "network" in stored["last_error"]


@pytest.mark.asyncio
async def test_auto_create_non_action_is_noop(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Any action other than AUTO_CREATE must not touch auto-create values."""
    # Spy: if auto_create_skill is somehow called, this fails the test.
    called = []

    async def _fake(**_kwargs: Any) -> SkillCreationArtifacts:
        called.append(True)
        return SkillCreationArtifacts()

    monkeypatch.setattr(
        provider_module,
        "auto_create_skill",
        _fake,
    )

    mass = _make_mass()
    values: dict[str, Any] = {}

    # A different action — should not trigger auto-create
    await _handle_config_actions(
        mass,
        "some_other_action",
        values,
        instance_id=None,
        is_cloud_plus=True,
        connection_type=CONNECTION_TYPE_CLOUD_PLUS,
    )

    assert not called
    assert CONF_AUTO_CREATE_ARTIFACTS not in values


@pytest.mark.asyncio
async def test_session_id_forwarded_from_frontend(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Frontend-supplied ``values['session_id']`` is forwarded to the orchestrator.

    The session_id is what AuthenticationHelper listens on — if the
    wiring drops it and generates a local uuid instead, MA's popup
    never opens (silent failure the user won't see).
    """
    captured: dict[str, Any] = {}

    async def _capture(**kwargs: Any) -> SkillCreationArtifacts:
        captured["session_id"] = kwargs.get("session_id")
        return SkillCreationArtifacts(state=SkillCreationState.DONE, skill_id="s")

    monkeypatch.setattr(provider_module, "auto_create_skill", _capture)

    mass = _make_mass()
    values: dict[str, Any] = {
        CONF_INSTANCE_NAME: "X",
        CONF_CLOUD_INSTANCE_ID: "inst-1",
        "session_id": "frontend-supplied-id-123",
    }

    await _handle_config_actions(
        mass,
        CONF_ACTION_AUTO_CREATE,
        values,
        instance_id=None,
        is_cloud_plus=True,
        connection_type=CONNECTION_TYPE_CLOUD_PLUS,
    )

    assert captured["session_id"] == "frontend-supplied-id-123"


@pytest.mark.asyncio
async def test_auto_create_cancelled_error_propagates(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """CancelledError must not be absorbed into a FAILED artifact.

    Config-flow shutdown and HA stop rely on cooperative cancellation;
    converting it into state=FAILED would both leak the error into the
    UI and break clean task teardown.
    """
    import asyncio  # noqa: PLC0415

    async def _raises_cancelled(**_kwargs: Any) -> SkillCreationArtifacts:
        raise asyncio.CancelledError

    monkeypatch.setattr(provider_module, "auto_create_skill", _raises_cancelled)

    mass = _make_mass()
    values: dict[str, Any] = {
        CONF_INSTANCE_NAME: "X",
        CONF_CLOUD_INSTANCE_ID: "inst-1",
    }

    with pytest.raises(asyncio.CancelledError):
        await _handle_config_actions(
            mass,
            CONF_ACTION_AUTO_CREATE,
            values,
            instance_id=None,
            is_cloud_plus=True,
            connection_type=CONNECTION_TYPE_CLOUD_PLUS,
        )

    # The FAILED artifact must NOT have been written on cancellation.
    assert CONF_AUTO_CREATE_ARTIFACTS not in values


@pytest.mark.asyncio
async def test_auto_create_prefers_saved_direct_secret(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """SECURE_STRING client secret: saved_config beats empty ``values`` on re-open.

    On re-open MA does not echo SECURE_STRING values back into ``values``,
    so reading from ``values`` alone would pass an empty secret into the
    orchestrator. The helper must pull it from the persisted provider
    config instead.
    """
    captured: dict[str, Any] = {}

    async def _capture(**kwargs: Any) -> SkillCreationArtifacts:
        captured["direct_client_secret"] = kwargs.get("direct_client_secret")
        return SkillCreationArtifacts(state=SkillCreationState.DONE, skill_id="s")

    monkeypatch.setattr(provider_module, "auto_create_skill", _capture)

    mass = _make_mass()
    saved_cfg = MagicMock()
    saved_cfg.get_value.side_effect = lambda key: (
        "persisted-secret" if key == CONF_DIRECT_CLIENT_SECRET else None
    )
    prov = MagicMock()
    prov.config = saved_cfg
    mass.get_provider.return_value = prov

    # Frontend re-open: SECURE_STRING not echoed back -> values has no secret.
    values: dict[str, Any] = {
        CONF_INSTANCE_NAME: "X",
    }

    await _handle_config_actions(
        mass,
        CONF_ACTION_AUTO_CREATE,
        values,
        instance_id="inst-42",
        is_cloud_plus=False,
        connection_type=CONNECTION_TYPE_DIRECT,
    )

    assert captured["direct_client_secret"] == "persisted-secret"


@pytest.mark.asyncio
async def test_auto_create_falls_back_to_values_for_first_setup(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """First-time setup: no instance yet, secret is read from ``values``."""
    captured: dict[str, Any] = {}

    async def _capture(**kwargs: Any) -> SkillCreationArtifacts:
        captured["direct_client_secret"] = kwargs.get("direct_client_secret")
        return SkillCreationArtifacts(state=SkillCreationState.DONE, skill_id="s")

    monkeypatch.setattr(provider_module, "auto_create_skill", _capture)

    mass = _make_mass()  # get_provider returns None
    values: dict[str, Any] = {
        CONF_INSTANCE_NAME: "X",
        CONF_DIRECT_CLIENT_SECRET: "fresh-secret",
    }

    await _handle_config_actions(
        mass,
        CONF_ACTION_AUTO_CREATE,
        values,
        instance_id=None,
        is_cloud_plus=False,
        connection_type=CONNECTION_TYPE_DIRECT,
    )

    assert captured["direct_client_secret"] == "fresh-secret"


@pytest.mark.asyncio
async def test_existing_action_register_still_works(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Gap-fill: pre-existing CONF_ACTION_REGISTER path is still covered."""

    async def _fake_register(
        _session: Any,
        platform: str | None = None,  # noqa: ARG001
    ) -> dict[str, str]:
        return {"id": "inst-new", "password": "p", "connection_token": "t"}

    async def _fake_otp(_session: Any, _id: str, _tok: Any) -> str:
        return "111111"

    class _NoopSession:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *_a):  # type: ignore[no-untyped-def]
            return False

    monkeypatch.setattr(provider_module, "register_cloud_instance", _fake_register)
    monkeypatch.setattr(provider_module, "get_cloud_otp", _fake_otp)
    monkeypatch.setattr(aiohttp, "ClientSession", _NoopSession)

    mass = _make_mass()
    values: dict[str, Any] = {}

    otp = await _handle_config_actions(
        mass,
        "register_cloud",
        values,
        instance_id=None,
        is_cloud_plus=True,
        connection_type=CONNECTION_TYPE_CLOUD_PLUS,
    )

    assert values[CONF_CLOUD_INSTANCE_ID] == "inst-new"
    # Register no longer auto-fetches OTP — that's a separate Step 3 action.
    # The handler returns None because no OTP was requested.
    assert otp is None
