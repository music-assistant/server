"""
Tests for provider/auto_create.py — blocking single-click pipeline.

The self-resuming Device Flow state machine is gone (see Phase C
refactor) — sign-in now lives in :mod:`provider.auth_page` as a
single blocking call. This module covers what's left: the duplicate
pre-check + the pipeline runner + the Recreate / Adopt resolution
helpers.
"""

# ruff: noqa: D102  # tests don't need per-method docstrings.

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from ya_dialogs_api import SkillCreationArtifacts, SkillCreationState
from ya_passport_auth.exceptions import InvalidCredentialsError

from music_assistant.providers.yandex_alice import auto_create
from music_assistant.providers.yandex_alice.auto_create import (
    LocalAutoCreateStage,
    adopt_existing_skill,
    delete_existing_skill_then_recreate,
    run_create_skill,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _fake_session_cm(
    fake_session: Any,
) -> AsyncIterator[Any]:
    """Async context manager wrapping a fake aiohttp.ClientSession."""
    yield fake_session


def _make_session_factory(fake_session: Any) -> Any:
    def _factory(_x_token: str) -> Any:
        return _fake_session_cm(fake_session)

    return _factory


def _patch_cached_session(monkeypatch: pytest.MonkeyPatch, fake_session: Any) -> None:
    """Replace ``cached_authenticated_session`` in auto_create."""
    monkeypatch.setattr(
        auto_create, "cached_authenticated_session", _make_session_factory(fake_session)
    )


def _patch_creator(
    monkeypatch: pytest.MonkeyPatch,
    *,
    list_existing_skills: list[dict[str, Any]] | None = None,
    csrf: str = "csrf-tok",
    delete_skill: AsyncMock | None = None,
) -> MagicMock:
    """Stub DialogsSkillCreator constructor in auto_create."""
    creator = MagicMock()
    creator.fetch_csrf = AsyncMock(return_value=csrf)
    creator.list_existing_skills = AsyncMock(return_value=list_existing_skills or [])
    if delete_skill is not None:
        creator.delete_skill = delete_skill
    monkeypatch.setattr(
        auto_create,
        "DialogsSkillCreator",
        lambda *a, **kw: creator,  # noqa: ARG005
    )
    return creator


# ---------------------------------------------------------------------------
# Duplicate pre-check
# ---------------------------------------------------------------------------


class TestPreCheckDuplicate:
    """``_pre_check_duplicate`` must be best-effort and case-insensitive."""

    @pytest.mark.asyncio
    async def test_no_match_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_cached_session(monkeypatch, MagicMock())
        _patch_creator(
            monkeypatch,
            list_existing_skills=[
                {"id": "x", "appName": "Other"},
                {"id": "y", "name": "Yet Another"},
            ],
        )
        result = await auto_create._pre_check_duplicate("tok", "Music Assistant")
        assert result is None

    @pytest.mark.asyncio
    async def test_case_insensitive_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_cached_session(monkeypatch, MagicMock())
        _patch_creator(
            monkeypatch,
            list_existing_skills=[
                {"id": "sk-1", "appName": "music ASSISTANT"},
            ],
        )
        result = await auto_create._pre_check_duplicate("tok", "Music Assistant")
        assert result == ("sk-1", "music ASSISTANT")

    @pytest.mark.asyncio
    async def test_blank_target_returns_none(self) -> None:
        # No session/creator interaction needed — early return.
        result = await auto_create._pre_check_duplicate("tok", "   ")
        assert result is None

    @pytest.mark.asyncio
    async def test_swallows_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        @asynccontextmanager
        async def _raising_factory(_x_token: str) -> AsyncIterator[Any]:
            raise RuntimeError("network blip")
            yield  # type: ignore[unreachable]  # pragma: no cover

        monkeypatch.setattr(auto_create, "cached_authenticated_session", _raising_factory)
        result = await auto_create._pre_check_duplicate("tok", "Music Assistant")
        assert result is None


# ---------------------------------------------------------------------------
# run_create_skill — happy path + duplicate + invalid token
# ---------------------------------------------------------------------------


class TestRunCreateSkill:
    """End-to-end pipeline through ``run_create_skill``."""

    @pytest.mark.asyncio
    async def test_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_cached_session(monkeypatch, MagicMock())
        _patch_creator(monkeypatch, list_existing_skills=[])

        async def _fake_auto_create_skill(**kwargs: Any) -> SkillCreationArtifacts:
            return SkillCreationArtifacts(
                state=SkillCreationState.DONE,
                skill_id="sk-new",
                last_known_name=str(kwargs["skill_name"]),
            )

        monkeypatch.setattr(auto_create, "auto_create_skill", _fake_auto_create_skill)

        outcome = await run_create_skill(
            cached_x_token="tok",
            skill_name="Music Assistant",
            backend_uri="https://ma.example.com/api/yandex_dialogs/webhook/sec",
            description="Voice control for Music Assistant",
            structured_examples=None,
            activation_phrases=None,
            intents=[],
            entities=[],
            artifacts=SkillCreationArtifacts(),
        )
        assert outcome.stage == LocalAutoCreateStage.DONE
        assert outcome.artifacts.skill_id == "sk-new"

    @pytest.mark.asyncio
    async def test_duplicate_detected_short_circuits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_cached_session(monkeypatch, MagicMock())
        _patch_creator(
            monkeypatch,
            list_existing_skills=[{"id": "sk-existing", "appName": "Music Assistant"}],
        )

        async def _should_not_run(**_kwargs: Any) -> SkillCreationArtifacts:
            raise AssertionError("auto_create_skill should not run when duplicate detected")

        monkeypatch.setattr(auto_create, "auto_create_skill", _should_not_run)

        outcome = await run_create_skill(
            cached_x_token="tok",
            skill_name="Music Assistant",
            backend_uri="https://ma.example.com/api/yandex_dialogs/webhook/sec",
            description="d",
            structured_examples=None,
            activation_phrases=None,
            intents=[],
            entities=[],
            artifacts=SkillCreationArtifacts(),
        )
        assert outcome.stage == LocalAutoCreateStage.DUPLICATE_DETECTED
        assert outcome.pending_duplicate_skill_id == "sk-existing"
        assert outcome.pending_duplicate_skill_name == "Music Assistant"

    @pytest.mark.asyncio
    async def test_invalid_credentials_clears_x_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_cached_session(monkeypatch, MagicMock())
        _patch_creator(monkeypatch, list_existing_skills=[])

        async def _raise_invalid(**_kwargs: Any) -> SkillCreationArtifacts:
            raise InvalidCredentialsError("expired")

        monkeypatch.setattr(auto_create, "auto_create_skill", _raise_invalid)

        outcome = await run_create_skill(
            cached_x_token="tok",
            skill_name="Music Assistant",
            backend_uri="https://ma.example.com/api/yandex_dialogs/webhook/sec",
            description="d",
            structured_examples=None,
            activation_phrases=None,
            intents=[],
            entities=[],
            artifacts=SkillCreationArtifacts(),
        )
        assert outcome.stage == LocalAutoCreateStage.FAILED
        assert outcome.x_token == ""

    @pytest.mark.asyncio
    async def test_failed_artifacts_become_failed_outcome(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_cached_session(monkeypatch, MagicMock())
        _patch_creator(monkeypatch, list_existing_skills=[])

        async def _fail(**_kwargs: Any) -> SkillCreationArtifacts:
            return SkillCreationArtifacts(
                state=SkillCreationState.FAILED,
                last_error="upload_logo: 502 Bad Gateway",
            )

        monkeypatch.setattr(auto_create, "auto_create_skill", _fail)

        outcome = await run_create_skill(
            cached_x_token="tok",
            skill_name="Music Assistant",
            backend_uri="https://ma.example.com/api/yandex_dialogs/webhook/sec",
            description="d",
            structured_examples=None,
            activation_phrases=None,
            intents=[],
            entities=[],
            artifacts=SkillCreationArtifacts(),
        )
        assert outcome.stage == LocalAutoCreateStage.FAILED
        assert "Bad Gateway" in outcome.user_message


# ---------------------------------------------------------------------------
# Adopt / Recreate
# ---------------------------------------------------------------------------


class TestAdoptExisting:
    """``adopt_existing_skill`` skips duplicate-check and create_app."""

    @pytest.mark.asyncio
    async def test_pre_positions_app_created(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_cached_session(monkeypatch, MagicMock())
        captured: dict[str, Any] = {}

        async def _fake_pipeline(**kwargs: Any) -> SkillCreationArtifacts:
            captured.update(kwargs)
            return SkillCreationArtifacts(
                state=SkillCreationState.DONE,
                skill_id=kwargs["artifacts"].skill_id,
                last_known_name=str(kwargs["skill_name"]),
            )

        monkeypatch.setattr(auto_create, "auto_create_skill", _fake_pipeline)

        outcome = await adopt_existing_skill(
            cached_x_token="tok",
            skill_name="Music Assistant",
            backend_uri="https://ma.example.com/api/yandex_dialogs/webhook/sec",
            description="d",
            structured_examples=None,
            activation_phrases=None,
            intents=[],
            entities=[],
            existing_skill_id="sk-existing",
        )
        assert outcome.stage == LocalAutoCreateStage.DONE
        assert outcome.artifacts.skill_id == "sk-existing"
        # Pipeline was invoked with state APP_CREATED, NOT NONE
        assert captured["artifacts"].state == SkillCreationState.APP_CREATED


class TestDeleteThenRecreate:
    """``delete_existing_skill_then_recreate`` deletes first, then runs fresh."""

    @pytest.mark.asyncio
    async def test_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_cached_session(monkeypatch, MagicMock())
        delete_mock = AsyncMock()
        _patch_creator(monkeypatch, delete_skill=delete_mock)

        async def _fake_pipeline(**_kwargs: Any) -> SkillCreationArtifacts:
            return SkillCreationArtifacts(
                state=SkillCreationState.DONE,
                skill_id="sk-fresh",
            )

        monkeypatch.setattr(auto_create, "auto_create_skill", _fake_pipeline)

        outcome = await delete_existing_skill_then_recreate(
            cached_x_token="tok",
            skill_name="Music Assistant",
            backend_uri="https://ma.example.com/api/yandex_dialogs/webhook/sec",
            description="d",
            structured_examples=None,
            activation_phrases=None,
            intents=[],
            entities=[],
            existing_skill_id="sk-old",
        )
        assert outcome.stage == LocalAutoCreateStage.DONE
        assert outcome.artifacts.skill_id == "sk-fresh"
        delete_mock.assert_awaited_once_with("csrf-tok", "sk-old")

    @pytest.mark.asyncio
    async def test_delete_failure_short_circuits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_cached_session(monkeypatch, MagicMock())
        delete_mock = AsyncMock(side_effect=RuntimeError("boom"))
        _patch_creator(monkeypatch, delete_skill=delete_mock)

        async def _should_not_run(**_kwargs: Any) -> SkillCreationArtifacts:
            raise AssertionError("pipeline should not run after delete failure")

        monkeypatch.setattr(auto_create, "auto_create_skill", _should_not_run)

        outcome = await delete_existing_skill_then_recreate(
            cached_x_token="tok",
            skill_name="Music Assistant",
            backend_uri="https://ma.example.com/api/yandex_dialogs/webhook/sec",
            description="d",
            structured_examples=None,
            activation_phrases=None,
            intents=[],
            entities=[],
            existing_skill_id="sk-old",
        )
        assert outcome.stage == LocalAutoCreateStage.FAILED
        assert "boom" in outcome.user_message
