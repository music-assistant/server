# ruff: noqa: PLC0415
"""
Integration tests for provider/__init__.get_config_entries — action dispatcher.

Mocks the orchestrator entry points (``run_auto_create_step``,
``run_auto_update``) so we test the dispatcher's ``values`` rehydration,
re-create / cancel reset semantics, and the entries it places into the
returned tuple — not the orchestrator internals (those are tested elsewhere).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from ya_dialogs_api import (
    SkillCreationArtifacts,
    SkillCreationState,
    dump_artifacts,
)

from music_assistant.providers import yandex_alice
from music_assistant.providers.yandex_alice import get_config_entries
from music_assistant.providers.yandex_alice.auto_create import (
    AutoCreateOutcome,
    LocalAutoCreateStage,
)
from music_assistant.providers.yandex_alice.auto_update import AutoUpdateOutcome
from music_assistant.providers.yandex_alice.constants import (
    CONF_ACTION_ADOPT_EXISTING,
    CONF_ACTION_AUTO_CREATE_DIALOG,
    CONF_ACTION_CANCEL_DIALOG_SKILL_FLOW,
    CONF_ACTION_CLEAR_AUTH,
    CONF_ACTION_DELETE_SKILL,
    CONF_ACTION_REFRESH_STATUS,
    CONF_ACTION_REGENERATE_WEBHOOK_SECRET,
    CONF_ACTION_RENAME_DIALOG_SKILL,
    CONF_ACTION_TEST_WEBHOOK,
    CONF_AUTH_USER_NAME,
    CONF_AUTH_X_TOKEN,
    CONF_DIALOG_AUTO_CREATE_ARTIFACTS,
    CONF_DIALOG_PUBLICATION_STATUS,
    CONF_DIALOG_SKILL_ID,
    CONF_DIALOG_SKILL_NAME,
    CONF_DIALOG_WEBHOOK_SECRET,
    CONF_EXTERNAL_BASE_URL,
    CONF_INSTANCE_NAME,
    CONF_PENDING_DUPLICATE_SKILL_ID,
    CONF_PENDING_DUPLICATE_SKILL_NAME,
)

from .localization import entry_text


def _make_mass() -> MagicMock:
    """
    Build a MagicMock MA with empty player + playlist enumeration.

    ``webserver`` is explicitly set to ``None`` so the device-code page
    helper short-circuits — unit tests that exercise form rendering
    don't need (or want) the dynamic-route side effect.
    """
    mass = MagicMock()
    mass.players.all_players = MagicMock(return_value=[])
    mass.webserver = None
    return mass


# v1.2.0: removed CONF_EXPOSED_PLAYLISTS — fetch_playlist_options no longer
# imported. The autouse fixture that used to stub it is gone too.


def _entries_by_key(entries: tuple[Any, ...]) -> dict[str, Any]:
    """Index entries by their ``key`` for easy lookup."""
    return {e.key: e for e in entries}


# ---------------------------------------------------------------------------
# action=None: default form
# ---------------------------------------------------------------------------


class TestDefaultForm:
    """No action: form has both auto-create button and (conditionally) rename."""

    @pytest.mark.asyncio
    async def test_no_action_renders_sign_in_button(self) -> None:
        """When no x_token cached → Authorization block shows Sign in button."""
        from music_assistant.providers.yandex_alice.constants import CONF_ACTION_SIGN_IN

        entries = await get_config_entries(_make_mass(), values={})
        keys = _entries_by_key(entries)
        # Sign in is the primary CTA in the Auth block; Create skill
        # appears only after auth (or when skill_id is set manually).
        assert CONF_ACTION_SIGN_IN in keys
        assert CONF_ACTION_AUTO_CREATE_DIALOG not in keys

    @pytest.mark.asyncio
    async def test_rename_hidden_without_skill_id_or_token(self) -> None:
        """Rename ACTION is suppressed when skill_id or x_token is missing."""
        entries = await get_config_entries(_make_mass(), values={})
        keys = _entries_by_key(entries)
        assert CONF_ACTION_RENAME_DIALOG_SKILL not in keys

    # v1.2.0 Phase F: rename / drift-cluster removed — Edit skill in
    # Step 3 covers the same use case with a richer set of fields.


# ---------------------------------------------------------------------------
# action = CONF_ACTION_AUTO_CREATE_DIALOG
# ---------------------------------------------------------------------------


class TestAutoCreateAction:
    """auto-create dispatch: invokes run_create_skill (Step 2) with derived inputs."""

    @pytest.mark.asyncio
    async def test_invokes_run_create_skill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Click with cached_x_token → run_create_skill is awaited with skill_name + backend_uri."""
        outcome = AutoCreateOutcome(
            artifacts=SkillCreationArtifacts(),
            x_token=None,
            user_message="started",
            stage=LocalAutoCreateStage.PIPELINE_RUNNING,
        )
        step_mock = AsyncMock(return_value=outcome)
        monkeypatch.setattr(yandex_alice, "run_create_skill", step_mock)

        values: dict[str, Any] = {
            CONF_INSTANCE_NAME: "Music Assistant",
            CONF_DIALOG_SKILL_NAME: "MA Test",
            CONF_EXTERNAL_BASE_URL: "https://ma.example.com",
            CONF_AUTH_X_TOKEN: "tok",
        }
        await get_config_entries(
            _make_mass(),
            action=CONF_ACTION_AUTO_CREATE_DIALOG,
            values=values,
        )

        step_mock.assert_awaited_once()
        assert step_mock.await_args is not None
        kwargs = step_mock.await_args.kwargs
        assert kwargs["skill_name"] == "MA Test"
        assert kwargs["backend_uri"].startswith(
            "https://ma.example.com/api/yandex_dialogs/webhook/"
        )

    @pytest.mark.asyncio
    async def test_https_required_short_circuits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """http:// base URL → FAILED before run_create_skill is called."""
        step_mock = AsyncMock()
        monkeypatch.setattr(yandex_alice, "run_create_skill", step_mock)

        values: dict[str, Any] = {
            CONF_INSTANCE_NAME: "MA",
            CONF_EXTERNAL_BASE_URL: "http://insecure.example.com",
            CONF_AUTH_X_TOKEN: "tok",
        }
        await get_config_entries(
            _make_mass(),
            action=CONF_ACTION_AUTO_CREATE_DIALOG,
            values=values,
        )

        step_mock.assert_not_awaited()
        # The dispatcher writes a FAILED artifacts blob into values
        from ya_dialogs_api import load_artifacts

        artifacts = load_artifacts(str(values.get(CONF_DIALOG_AUTO_CREATE_ARTIFACTS) or "") or None)
        assert artifacts.state == SkillCreationState.FAILED
        assert "HTTPS" in (artifacts.last_error or "")

    @pytest.mark.asyncio
    async def test_re_click_on_done_resets_artifacts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If artifacts.state was DONE, dispatcher resets to NONE before stepping."""
        captured_artifacts: list[SkillCreationArtifacts] = []

        async def _capture(**kwargs: Any) -> AutoCreateOutcome:
            captured_artifacts.append(kwargs["artifacts"])
            return AutoCreateOutcome(
                artifacts=SkillCreationArtifacts(),
                x_token=None,
                user_message="restart",
                stage=LocalAutoCreateStage.IDLE,
            )

        monkeypatch.setattr(yandex_alice, "run_create_skill", _capture)

        done = SkillCreationArtifacts(
            state=SkillCreationState.DONE,
            skill_id="sk-old",
            last_known_name="Old",
        )
        values: dict[str, Any] = {
            CONF_DIALOG_AUTO_CREATE_ARTIFACTS: dump_artifacts(done),
            CONF_EXTERNAL_BASE_URL: "https://ma.example.com",
            CONF_AUTH_X_TOKEN: "tok",
        }
        await get_config_entries(
            _make_mass(),
            action=CONF_ACTION_AUTO_CREATE_DIALOG,
            values=values,
        )
        assert len(captured_artifacts) == 1
        # The dispatcher reset before stepping — old skill_id is gone
        assert captured_artifacts[0].state == SkillCreationState.NONE
        assert captured_artifacts[0].skill_id is None

    @pytest.mark.asyncio
    async def test_writes_skill_id_on_done(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Successful pipeline → CONF_DIALOG_SKILL_ID auto-populated in values."""
        outcome = AutoCreateOutcome(
            artifacts=SkillCreationArtifacts(
                state=SkillCreationState.DONE,
                skill_id="sk-new-uuid",
                last_known_name="MA",
            ),
            x_token=None,
            user_message="✅",
            stage=LocalAutoCreateStage.DONE,
        )
        monkeypatch.setattr(yandex_alice, "run_create_skill", AsyncMock(return_value=outcome))

        values: dict[str, Any] = {
            CONF_EXTERNAL_BASE_URL: "https://ma.example.com",
            CONF_AUTH_X_TOKEN: "tok",
        }
        await get_config_entries(
            _make_mass(),
            action=CONF_ACTION_AUTO_CREATE_DIALOG,
            values=values,
        )
        assert values[CONF_DIALOG_SKILL_ID] == "sk-new-uuid"

    @pytest.mark.asyncio
    async def test_backup_restore_pre_sets_app_created(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """skill_id in values + artifacts NONE → pre-set to APP_CREATED to skip create_app."""
        captured_artifacts: list[SkillCreationArtifacts] = []

        async def _capture(**kwargs: Any) -> AutoCreateOutcome:
            captured_artifacts.append(kwargs["artifacts"])
            return AutoCreateOutcome(
                artifacts=kwargs["artifacts"],
                x_token=None,
                user_message="stub",
                stage=LocalAutoCreateStage.PIPELINE_RUNNING,
            )

        monkeypatch.setattr(yandex_alice, "run_create_skill", _capture)

        # Empty artifacts but skill_id present (config restored from backup)
        values: dict[str, Any] = {
            CONF_DIALOG_SKILL_ID: "sk-existing-uuid",
            CONF_EXTERNAL_BASE_URL: "https://ma.example.com",
            CONF_AUTH_X_TOKEN: "tok",
        }
        await get_config_entries(
            _make_mass(),
            action=CONF_ACTION_AUTO_CREATE_DIALOG,
            values=values,
        )

        assert captured_artifacts[0].state == SkillCreationState.APP_CREATED
        assert captured_artifacts[0].skill_id == "sk-existing-uuid"


# ---------------------------------------------------------------------------
# action = CONF_ACTION_RENAME_DIALOG_SKILL
# ---------------------------------------------------------------------------


class TestRenameAction:
    """Rename dispatch: invokes run_auto_update with skill_name + cached token."""

    @pytest.mark.asyncio
    async def test_invokes_run_auto_update(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Click → run_auto_update awaited with skill_name + backend_uri + cached_x_token."""
        result = AutoUpdateOutcome(
            artifacts=SkillCreationArtifacts(
                state=SkillCreationState.DONE,
                skill_id="sk-1",
                last_known_name="New Name",
            ),
            x_token=None,
            user_message="✅ обновлён",
        )
        update_mock = AsyncMock(return_value=result)
        monkeypatch.setattr(yandex_alice, "run_auto_update", update_mock)

        values: dict[str, Any] = {
            CONF_DIALOG_SKILL_NAME: "New Name",
            CONF_EXTERNAL_BASE_URL: "https://ma.example.com",
            CONF_AUTH_X_TOKEN: "tok",
            CONF_DIALOG_AUTO_CREATE_ARTIFACTS: dump_artifacts(
                SkillCreationArtifacts(
                    state=SkillCreationState.DONE,
                    skill_id="sk-1",
                    last_known_name="Old Name",
                )
            ),
        }
        await get_config_entries(
            _make_mass(),
            action=CONF_ACTION_RENAME_DIALOG_SKILL,
            values=values,
        )

        update_mock.assert_awaited_once()
        assert update_mock.await_args is not None
        kwargs = update_mock.await_args.kwargs
        assert kwargs["skill_name"] == "New Name"
        assert kwargs["cached_x_token"] == "tok"

    @pytest.mark.asyncio
    async def test_token_cleared_on_auth_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """run_auto_update returns x_token='' → values clears CONF_AUTH_X_TOKEN."""
        result = AutoUpdateOutcome(
            artifacts=SkillCreationArtifacts(
                state=SkillCreationState.FAILED,
                skill_id="sk-1",
                last_error="истёк",
            ),
            x_token="",
            user_message="auth expired",
        )
        monkeypatch.setattr(yandex_alice, "run_auto_update", AsyncMock(return_value=result))

        values: dict[str, Any] = {
            CONF_AUTH_X_TOKEN: "stale",
            CONF_EXTERNAL_BASE_URL: "https://ma.example.com",
            CONF_DIALOG_AUTO_CREATE_ARTIFACTS: dump_artifacts(
                SkillCreationArtifacts(state=SkillCreationState.DONE, skill_id="sk-1")
            ),
        }
        await get_config_entries(
            _make_mass(),
            action=CONF_ACTION_RENAME_DIALOG_SKILL,
            values=values,
        )
        assert values[CONF_AUTH_X_TOKEN] == ""


# ---------------------------------------------------------------------------
# action = CONF_ACTION_CANCEL_DIALOG_SKILL_FLOW
# ---------------------------------------------------------------------------


class TestCancelAction:
    """Cancel: reset artifacts; keep cached x_token (sign-in stays valid)."""

    @pytest.mark.asyncio
    async def test_resets_artifacts(self) -> None:
        """Cancel resets artifacts to NONE; cached x_token preserved."""
        values: dict[str, Any] = {
            CONF_DIALOG_AUTO_CREATE_ARTIFACTS: dump_artifacts(
                SkillCreationArtifacts(
                    state=SkillCreationState.APP_CREATED,
                    skill_id="sk-orphan",
                )
            ),
            CONF_AUTH_X_TOKEN: "preserve-me",
            CONF_EXTERNAL_BASE_URL: "https://ma.example.com",
        }
        await get_config_entries(
            _make_mass(),
            action=CONF_ACTION_CANCEL_DIALOG_SKILL_FLOW,
            values=values,
        )

        from ya_dialogs_api import load_artifacts

        # Artifacts reset to NONE
        rehydrated = load_artifacts(str(values[CONF_DIALOG_AUTO_CREATE_ARTIFACTS]))
        assert rehydrated.state == SkillCreationState.NONE
        assert rehydrated.skill_id is None
        # Token preserved
        assert values[CONF_AUTH_X_TOKEN] == "preserve-me"


class TestSideEffectingActions:
    """Dispatcher coverage for account, skill and network side effects."""

    @pytest.mark.asyncio
    async def test_clear_auth_resets_local_credentials_and_artifacts(self) -> None:
        """Sign-out clears tokens, identity and resumable setup state."""
        values: dict[str, Any] = {
            CONF_AUTH_X_TOKEN: "tok",
            CONF_AUTH_USER_NAME: "User",
            CONF_PENDING_DUPLICATE_SKILL_ID: "duplicate",
            CONF_PENDING_DUPLICATE_SKILL_NAME: "Duplicate Skill",
            CONF_DIALOG_AUTO_CREATE_ARTIFACTS: dump_artifacts(
                SkillCreationArtifacts(state=SkillCreationState.DONE, skill_id="sk-1")
            ),
        }

        await get_config_entries(_make_mass(), action=CONF_ACTION_CLEAR_AUTH, values=values)

        assert values[CONF_AUTH_X_TOKEN] == ""
        assert values[CONF_AUTH_USER_NAME] == ""
        assert values[CONF_PENDING_DUPLICATE_SKILL_ID] == ""
        assert values[CONF_PENDING_DUPLICATE_SKILL_NAME] == ""

    @pytest.mark.asyncio
    async def test_delete_skill_dispatches_and_clears_registration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Delete calls Yandex once and removes the persisted local identity."""
        delete_mock = AsyncMock()
        monkeypatch.setattr(yandex_alice, "_delete_skill_in_yandex", delete_mock)
        values: dict[str, Any] = {
            CONF_AUTH_X_TOKEN: "tok",
            CONF_DIALOG_SKILL_ID: "sk-1",
            CONF_DIALOG_PUBLICATION_STATUS: "on_air",
            CONF_DIALOG_AUTO_CREATE_ARTIFACTS: dump_artifacts(
                SkillCreationArtifacts(state=SkillCreationState.DONE, skill_id="sk-1")
            ),
        }

        await get_config_entries(_make_mass(), action=CONF_ACTION_DELETE_SKILL, values=values)

        delete_mock.assert_awaited_once_with("tok", "sk-1")
        assert values[CONF_DIALOG_SKILL_ID] == ""
        assert values[CONF_DIALOG_PUBLICATION_STATUS] == ""

    @pytest.mark.asyncio
    async def test_regenerate_secret_resets_skill_registration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Secret rotation invalidates the old skill registration locally."""
        monkeypatch.setattr(yandex_alice, "_generate_webhook_secret", lambda: "new-secret")
        values: dict[str, Any] = {
            CONF_AUTH_X_TOKEN: "tok",
            CONF_DIALOG_WEBHOOK_SECRET: "old-secret",
            CONF_DIALOG_SKILL_ID: "sk-1",
            CONF_DIALOG_AUTO_CREATE_ARTIFACTS: dump_artifacts(
                SkillCreationArtifacts(state=SkillCreationState.DONE, skill_id="sk-1")
            ),
        }

        await get_config_entries(
            _make_mass(), action=CONF_ACTION_REGENERATE_WEBHOOK_SECRET, values=values
        )

        assert values[CONF_DIALOG_WEBHOOK_SECRET] == "new-secret"
        assert values[CONF_DIALOG_SKILL_ID] == ""

    @pytest.mark.asyncio
    async def test_adopt_existing_dispatches_selected_duplicate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Adopt forwards the duplicate ID and persists the resulting skill."""
        outcome = AutoCreateOutcome(
            artifacts=SkillCreationArtifacts(
                state=SkillCreationState.DONE,
                skill_id="sk-existing",
                last_known_name="MA Test",
            ),
            x_token=None,
            user_message="adopted",
            stage=LocalAutoCreateStage.DONE,
        )
        adopt_mock = AsyncMock(return_value=outcome)
        monkeypatch.setattr(yandex_alice, "adopt_existing_skill", adopt_mock)
        monkeypatch.setattr(
            yandex_alice, "fetch_skill_publication_status", AsyncMock(return_value=None)
        )
        values: dict[str, Any] = {
            CONF_AUTH_X_TOKEN: "tok",
            CONF_DIALOG_SKILL_NAME: "MA Test",
            CONF_EXTERNAL_BASE_URL: "https://ma.example.com",
            CONF_PENDING_DUPLICATE_SKILL_ID: "sk-existing",
            CONF_PENDING_DUPLICATE_SKILL_NAME: "MA Test",
        }

        await get_config_entries(_make_mass(), action=CONF_ACTION_ADOPT_EXISTING, values=values)

        assert adopt_mock.await_args is not None
        assert adopt_mock.await_args.kwargs["existing_skill_id"] == "sk-existing"
        assert values[CONF_DIALOG_SKILL_ID] == "sk-existing"
        assert values[CONF_PENDING_DUPLICATE_SKILL_ID] == ""

    @pytest.mark.asyncio
    async def test_webhook_probe_action_dispatches_current_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The probe action uses the exact configured base URL and secret."""
        probe_mock = AsyncMock(return_value=(True, "reachable"))
        monkeypatch.setattr(yandex_alice, "probe_webhook_reachability", probe_mock)
        values: dict[str, Any] = {
            CONF_EXTERNAL_BASE_URL: "https://ma.example.com",
            CONF_DIALOG_WEBHOOK_SECRET: "secret",
        }

        await get_config_entries(_make_mass(), action=CONF_ACTION_TEST_WEBHOOK, values=values)

        probe_mock.assert_awaited_once_with("https://ma.example.com", "secret")

    @pytest.mark.asyncio
    async def test_refresh_status_persists_live_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Refresh fetches the registered skill and stores its live status."""
        status_mock = AsyncMock(return_value="on_air")
        monkeypatch.setattr(yandex_alice, "fetch_skill_publication_status", status_mock)
        values: dict[str, Any] = {
            CONF_AUTH_X_TOKEN: "tok",
            CONF_DIALOG_SKILL_ID: "sk-1",
            CONF_DIALOG_AUTO_CREATE_ARTIFACTS: dump_artifacts(
                SkillCreationArtifacts(state=SkillCreationState.DONE, skill_id="sk-1")
            ),
        }

        await get_config_entries(_make_mass(), action=CONF_ACTION_REFRESH_STATUS, values=values)

        status_mock.assert_awaited_once_with("tok", "sk-1")
        assert values[CONF_DIALOG_PUBLICATION_STATUS] == "on_air"


# ---------------------------------------------------------------------------
# Code-review fixes — targeted regression coverage
# ---------------------------------------------------------------------------


class TestStableWebhookSecret:
    """
    Webhook secret must NOT regenerate between action clicks.

    Otherwise auto-create would register a webhook URL containing a secret
    that the next render replaces with a different one — orphaning the
    Yandex-side webhook against MA's eventual saved secret.
    """

    @pytest.mark.asyncio
    async def test_secret_reused_across_action_clicks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two consecutive clicks see the same backend_uri/secret (no regen)."""
        captured_uris: list[str] = []

        async def _capture(**kwargs: Any) -> AutoCreateOutcome:
            captured_uris.append(kwargs["backend_uri"])
            return AutoCreateOutcome(
                artifacts=SkillCreationArtifacts(),
                x_token=None,
                user_message="ok",
                stage=LocalAutoCreateStage.IDLE,
            )

        monkeypatch.setattr(yandex_alice, "run_create_skill", _capture)

        # First click: no secret in values → dispatcher generates + writes back.
        values: dict[str, Any] = {
            CONF_EXTERNAL_BASE_URL: "https://ma.example.com",
            CONF_AUTH_X_TOKEN: "tok",
        }
        await get_config_entries(
            _make_mass(),
            action=CONF_ACTION_AUTO_CREATE_DIALOG,
            values=values,
        )

        # The dispatcher must have stabilised the secret in values
        # so subsequent renders see the same one.
        first_secret = str(values.get("dialog_webhook_secret") or "")
        assert first_secret

        # Second click — must reuse the same secret in backend_uri.
        await get_config_entries(
            _make_mass(),
            action=CONF_ACTION_AUTO_CREATE_DIALOG,
            values=values,
        )

        assert len(captured_uris) == 2
        assert captured_uris[0] == captured_uris[1]
        assert first_secret in captured_uris[0]


class TestDeriveStageRespectsCachedToken:
    """
    Intermediate artifact state without cached x_token → IDLE, not Resume.

    Otherwise the button label says "Resume" but the next click actually
    starts a fresh Device Flow — confusing UX.
    """

    @pytest.mark.asyncio
    async def test_intermediate_state_without_token_shows_sign_in(self) -> None:
        """
        artifacts=APP_CREATED + no x_token → Auth block shows Sign in.

        Skill block ALSO renders because skill_id is known (manual
        backup-restore path), but the primary CTA stays the Sign in
        button until auth is resolved.
        """
        from music_assistant.providers.yandex_alice.constants import CONF_ACTION_SIGN_IN

        artifacts = SkillCreationArtifacts(
            state=SkillCreationState.APP_CREATED,
            skill_id="sk-partial",
        )
        values: dict[str, Any] = {
            CONF_DIALOG_AUTO_CREATE_ARTIFACTS: dump_artifacts(artifacts),
            # No CONF_AUTH_X_TOKEN → next click will hit Device Flow
        }
        entries = await get_config_entries(_make_mass(), values=values)
        keys = _entries_by_key(entries)
        assert CONF_ACTION_SIGN_IN in keys
        assert keys[CONF_ACTION_SIGN_IN].action == CONF_ACTION_SIGN_IN
        assert entry_text(CONF_ACTION_SIGN_IN, "action_label") == "Sign in to Yandex Passport"

    @pytest.mark.asyncio
    async def test_intermediate_state_with_token_renders_resume_label(self) -> None:
        """artifacts=APP_CREATED + cached x_token → button says 'Resume'."""
        artifacts = SkillCreationArtifacts(
            state=SkillCreationState.APP_CREATED,
            skill_id="sk-partial",
        )
        values: dict[str, Any] = {
            CONF_DIALOG_AUTO_CREATE_ARTIFACTS: dump_artifacts(artifacts),
            CONF_AUTH_X_TOKEN: "tok",
        }
        entries = await get_config_entries(_make_mass(), values=values)
        keys = _entries_by_key(entries)
        # v1.2.0 #19: PIPELINE_RUNNING button = "Continue setup"
        assert keys[CONF_ACTION_AUTO_CREATE_DIALOG].translation_key == "auto_create_continue"
        assert entry_text("auto_create_continue", "action_label") == "Continue setup"


# v1.2.0 Phase C refactor: the self-resuming Device Flow is gone.
# Sign-in is a single blocking action that opens an AuthenticationHelper
# popup; there is no mid-flow form reload to re-render the user_code in.
# The dedicated test class for that case has been removed.
