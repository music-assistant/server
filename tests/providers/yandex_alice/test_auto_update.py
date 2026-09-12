"""
Tests for provider/auto_update.py — rename + drift-sync via cached x_token.

Mocks ``provider.auto_update.auto_update_skill`` (the ya-dialogs-api
orchestrator) so we don't talk to ``dialogs.yandex.ru``. The auth path is
exercised separately in test_auth_session.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from ya_dialogs_api import SkillCreationArtifacts, SkillCreationState
from ya_passport_auth.exceptions import InvalidCredentialsError

from music_assistant.providers.yandex_alice import auto_update
from music_assistant.providers.yandex_alice.auto_update import run_auto_update


def _patch_auto_update_skill(
    monkeypatch: pytest.MonkeyPatch,
    *,
    return_artifacts: SkillCreationArtifacts,
    side_effect: Exception | None = None,
) -> AsyncMock:
    """Replace auto_update.auto_update_skill with an AsyncMock."""
    mock = AsyncMock(return_value=return_artifacts, side_effect=side_effect)
    monkeypatch.setattr(auto_update, "auto_update_skill", mock)
    return mock


class TestRunAutoUpdatePreconditions:
    """Pre-network checks that fail fast with a clear last_error."""

    @pytest.mark.asyncio
    async def test_no_cached_x_token_returns_failed(self) -> None:
        """Empty cached_x_token → FAILED before any library call."""
        result = await run_auto_update(
            cached_x_token=None,
            skill_name="X",
            backend_uri="https://example.test/x",
            description="d",
            structured_examples=None,
            activation_phrases=None,
            intents=[],
            entities=[],
            artifacts=SkillCreationArtifacts(skill_id="sk-1"),
        )
        assert result.artifacts.state == SkillCreationState.FAILED
        assert result.x_token is None
        assert "No cached authentication" in result.user_message

    @pytest.mark.asyncio
    async def test_empty_cached_x_token_returns_failed(self) -> None:
        """Empty string cached_x_token → same FAILED outcome as None."""
        result = await run_auto_update(
            cached_x_token="",
            skill_name="X",
            backend_uri="https://example.test/x",
            description="d",
            structured_examples=None,
            activation_phrases=None,
            intents=[],
            entities=[],
            artifacts=SkillCreationArtifacts(skill_id="sk-1"),
        )
        assert result.artifacts.state == SkillCreationState.FAILED
        assert "No cached" in result.user_message

    @pytest.mark.asyncio
    async def test_no_skill_id_returns_failed(self) -> None:
        """Missing skill_id → FAILED with clear "create first" message."""
        result = await run_auto_update(
            cached_x_token="token",
            skill_name="X",
            backend_uri="https://example.test/x",
            description="d",
            structured_examples=None,
            activation_phrases=None,
            intents=[],
            entities=[],
            artifacts=SkillCreationArtifacts(),  # no skill_id
        )
        assert result.artifacts.state == SkillCreationState.FAILED
        assert "skill_id" in result.user_message


class TestRunAutoUpdateHappyPath:
    """auto_update_skill returns DONE → user-facing success message."""

    @pytest.mark.asyncio
    async def test_returns_done_with_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DONE outcome surfaces a confirmation message."""
        done = SkillCreationArtifacts(
            state=SkillCreationState.DONE,
            skill_id="sk-1",
            last_known_name="New Name",
            logo_id="lg-1",
        )
        skill_mock = _patch_auto_update_skill(monkeypatch, return_artifacts=done)

        result = await run_auto_update(
            cached_x_token="token",
            skill_name="New Name",
            backend_uri="https://example.test/x",
            description="Description",
            structured_examples=None,
            activation_phrases=None,
            intents=[],
            entities=[],
            artifacts=SkillCreationArtifacts(state=SkillCreationState.DONE, skill_id="sk-1"),
        )

        assert result.artifacts.state == SkillCreationState.DONE
        assert result.artifacts.last_known_name == "New Name"
        assert "updated" in result.user_message
        assert "New Name" in result.user_message
        skill_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_passes_dialog_channel_and_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Library is called with channel='aliceSkill' + the supplied metadata."""
        done = SkillCreationArtifacts(state=SkillCreationState.DONE, skill_id="sk-1")
        skill_mock = _patch_auto_update_skill(monkeypatch, return_artifacts=done)

        await run_auto_update(
            cached_x_token="token",
            skill_name="My Skill",
            backend_uri="https://example.test/api/yandex_dialogs/webhook/sec",
            description="Hello world",
            structured_examples=[{"marker": "попроси"}],
            activation_phrases=["My Skill"],
            intents=[],
            entities=[],
            artifacts=SkillCreationArtifacts(state=SkillCreationState.DONE, skill_id="sk-1"),
        )

        assert skill_mock.await_args is not None
        kwargs = skill_mock.await_args.kwargs
        assert kwargs["channel"] == "aliceSkill"
        assert kwargs["skill_name"] == "My Skill"
        assert kwargs["description"] == "Hello world"
        assert kwargs["structured_examples"] == [{"marker": "попроси"}]
        assert kwargs["activation_phrases"] == ["My Skill"]


class TestRunAutoUpdateFailures:
    """ya-dialogs-api / Passport failures are surfaced via FAILED artifacts."""

    @pytest.mark.asyncio
    async def test_library_failed_artifacts_surface(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Library returns FAILED → result.user_message uses last_error."""
        failed = SkillCreationArtifacts(
            state=SkillCreationState.FAILED,
            skill_id="sk-1",
            last_error="Yandex rejected: validation failed",
        )
        _patch_auto_update_skill(monkeypatch, return_artifacts=failed)

        result = await run_auto_update(
            cached_x_token="token",
            skill_name="X",
            backend_uri="https://example.test/x",
            description="d",
            structured_examples=None,
            activation_phrases=None,
            intents=[],
            entities=[],
            artifacts=SkillCreationArtifacts(state=SkillCreationState.DONE, skill_id="sk-1"),
        )

        assert result.artifacts.state == SkillCreationState.FAILED
        assert "validation failed" in result.user_message
        assert result.x_token is None  # No token clear unless 401

    @pytest.mark.asyncio
    async def test_passport_invalid_credentials_clears_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """InvalidCredentialsError from refresh_passport_cookies → x_token=''."""
        _patch_auto_update_skill(
            monkeypatch,
            return_artifacts=SkillCreationArtifacts(),
            side_effect=InvalidCredentialsError("expired"),
        )

        result = await run_auto_update(
            cached_x_token="stale-token",
            skill_name="X",
            backend_uri="https://example.test/x",
            description="d",
            structured_examples=None,
            activation_phrases=None,
            intents=[],
            entities=[],
            artifacts=SkillCreationArtifacts(state=SkillCreationState.DONE, skill_id="sk-1"),
        )

        assert result.artifacts.state == SkillCreationState.FAILED
        assert result.x_token == ""  # Signal to dispatcher: clear cache
        assert "expired" in result.user_message
