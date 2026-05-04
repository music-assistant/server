"""Tests for auto_skill_ui — ConfigEntry visibility and action label logic."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.providers.yandex_smarthome.auto_skill_state import (
    SkillCreationArtifacts,
    SkillCreationState,
)
from music_assistant.providers.yandex_smarthome.auto_skill_ui import (
    auto_create_entries,
    build_cloud_plus_entries,
    build_direct_entries,
    should_show_button,
)
from music_assistant.providers.yandex_smarthome.constants import (
    CONF_ACTION_AUTO_CREATE,
    CONF_ACTION_GET_OTP,
    CONF_ACTION_REGISTER,
    CONF_AUTO_CREATE_ARTIFACTS,
    CONF_SKILL_ID,
    CONF_SKILL_TOKEN,
    CONNECTION_TYPE_CLOUD,
    CONNECTION_TYPE_CLOUD_PLUS,
    CONNECTION_TYPE_DIRECT,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from music_assistant_models.config_entries import ConfigEntry


def _find(entries: Iterable[ConfigEntry], key: str) -> ConfigEntry | None:
    for e in entries:
        if e.key == key:
            return e
    return None


# ---------------------------------------------------------------------------
# should_show_button
# ---------------------------------------------------------------------------


class TestShouldShowButton:
    """should_show_button captures the full visibility truth table."""

    def test_hidden_in_cloud_mode(self) -> None:
        """Plain cloud has no custom skill — button hidden."""
        assert not should_show_button(
            connection_type=CONNECTION_TYPE_CLOUD,
            state=SkillCreationState.NONE,
            cloud_instance_id="abc",
            base_url="https://x",
        )

    def test_hidden_when_state_done(self) -> None:
        """DONE means skill already created — don't offer re-creation."""
        assert not should_show_button(
            connection_type=CONNECTION_TYPE_CLOUD_PLUS,
            state=SkillCreationState.DONE,
            cloud_instance_id="abc",
            base_url="https://x",
        )

    def test_hidden_cloud_plus_no_instance(self) -> None:
        """cloud_plus requires a registered cloud instance first."""
        assert not should_show_button(
            connection_type=CONNECTION_TYPE_CLOUD_PLUS,
            state=SkillCreationState.NONE,
            cloud_instance_id="",
            base_url="https://x",
        )

    def test_hidden_direct_non_https(self) -> None:
        """Direct needs an HTTPS base URL or Yandex will reject the skill."""
        assert not should_show_button(
            connection_type=CONNECTION_TYPE_DIRECT,
            state=SkillCreationState.NONE,
            cloud_instance_id="",
            base_url="http://localhost:8095",
        )

    def test_shown_cloud_plus_ready(self) -> None:
        """All gates satisfied for cloud_plus → button visible."""
        assert should_show_button(
            connection_type=CONNECTION_TYPE_CLOUD_PLUS,
            state=SkillCreationState.NONE,
            cloud_instance_id="abc",
            base_url="https://x",
        )

    def test_shown_direct_ready(self) -> None:
        """All gates satisfied for direct → button visible."""
        assert should_show_button(
            connection_type=CONNECTION_TYPE_DIRECT,
            state=SkillCreationState.NONE,
            cloud_instance_id="",
            base_url="https://ma.example.com",
        )

    def test_shown_after_failed(self) -> None:
        """FAILED state keeps the button visible for retry."""
        assert should_show_button(
            connection_type=CONNECTION_TYPE_CLOUD_PLUS,
            state=SkillCreationState.FAILED,
            cloud_instance_id="abc",
            base_url="https://x",
        )


# ---------------------------------------------------------------------------
# auto_create_entries
# ---------------------------------------------------------------------------


class TestAutoCreateEntries:
    """auto_create_entries renders a list of ConfigEntries matching state."""

    def _entries(
        self,
        *,
        connection_type: str = CONNECTION_TYPE_CLOUD_PLUS,
        state: SkillCreationState = SkillCreationState.NONE,
        cloud_instance_id: str = "abc",
        base_url: str = "https://ma.example.com",
    ) -> Sequence[ConfigEntry]:
        return auto_create_entries(
            connection_type=connection_type,
            artifacts=SkillCreationArtifacts(state=state),
            cloud_instance_id=cloud_instance_id,
            base_url=base_url,
            session_id=None,
            user_code=None,
            verification_url=None,
            existing_artifacts_raw=None,
        )

    def test_empty_for_cloud_mode(self) -> None:
        """Plain cloud returns no entries — the section is meaningless."""
        assert self._entries(connection_type=CONNECTION_TYPE_CLOUD) == ()

    def test_action_shown_and_enabled_when_ready(self) -> None:
        """In the ready-to-create state, action is present and not hidden."""
        entries = list(self._entries())
        action = _find(entries, CONF_ACTION_AUTO_CREATE)
        assert action is not None
        assert action.hidden is False

    def test_action_hidden_when_state_done(self) -> None:
        """state=DONE renders the action with hidden=True."""
        entries = list(self._entries(state=SkillCreationState.DONE))
        action = _find(entries, CONF_ACTION_AUTO_CREATE)
        assert action is not None
        assert action.hidden is True

    def test_action_label_changes_on_failed(self) -> None:
        """After a failure the button label switches to 'Retry'."""
        entries = list(self._entries(state=SkillCreationState.FAILED))
        action = _find(entries, CONF_ACTION_AUTO_CREATE)
        assert action is not None
        assert action.action_label == "Retry"

    def test_action_label_says_retry_on_partial(self) -> None:
        """Partial (non-FAILED) progress state uses the 'Retry from last step' label."""
        entries = list(self._entries(state=SkillCreationState.OAUTH_CREATED))
        action = _find(entries, CONF_ACTION_AUTO_CREATE)
        assert action is not None
        assert action.action_label is not None
        assert "Retry" in action.action_label

    def test_hidden_artifacts_always_round_tripped(self) -> None:
        """Artifacts blob is included even when the section is mostly empty."""
        entries = list(self._entries())
        artifacts_entry = _find(entries, CONF_AUTO_CREATE_ARTIFACTS)
        assert artifacts_entry is not None
        assert artifacts_entry.hidden is True

    def test_status_label_reflects_failed_error(self) -> None:
        """The status LABEL carries the last_error text for user visibility."""
        entries = auto_create_entries(
            connection_type=CONNECTION_TYPE_CLOUD_PLUS,
            artifacts=SkillCreationArtifacts(
                state=SkillCreationState.FAILED,
                last_error="HTTP 401: session expired",
            ),
            cloud_instance_id="abc",
            base_url="https://x",
            session_id=None,
            user_code=None,
            verification_url=None,
            existing_artifacts_raw=None,
        )
        status = _find(list(entries), "label_auto_create_status")
        assert status is not None
        assert "401" in status.label


# ---------------------------------------------------------------------------
# build_cloud_plus_entries — 3-step structure
# ---------------------------------------------------------------------------


class TestBuildCloudPlusEntries:
    """cloud_plus entries render as numbered steps with proper gating."""

    def _call(
        self,
        *,
        is_registered: bool = False,
        state: SkillCreationState = SkillCreationState.NONE,
        otp_code: str | None = None,
        skill_id: str = "",
    ) -> list[ConfigEntry]:
        return build_cloud_plus_entries(
            otp_code=otp_code,
            is_registered=is_registered,
            cloud_instance_id="inst-1" if is_registered else "",
            artifacts=SkillCreationArtifacts(state=state),
            session_id=None,
            user_code=None,
            verification_url=None,
            existing_artifacts_raw=None,
            base_url="https://ma.example.com",
            skill_id=skill_id,
        )

    def test_step1_register_always_visible(self) -> None:
        """Step 1 (Register) is always rendered — even before first registration."""
        entries = self._call(is_registered=False)
        keys = [e.key for e in entries]
        assert CONF_ACTION_REGISTER in keys

    def test_step2_button_inactive_until_registered(self) -> None:
        """Step 2 fields are emitted, but the Create-Skill button is hidden."""
        entries = self._call(is_registered=False)
        action = _find(list(entries), CONF_ACTION_AUTO_CREATE)
        # Entry exists (so advanced users can see other Step 2 fields),
        # but the action button self-hides without a cloud instance.
        assert action is not None
        assert action.hidden is True

    def test_step2_visible_after_register(self) -> None:
        """After register, Step 2 renders the auto-create action."""
        entries = self._call(is_registered=True)
        keys = [e.key for e in entries]
        assert CONF_ACTION_AUTO_CREATE in keys

    def test_step3_hidden_until_registered(self) -> None:
        """Step 3 (Get OTP) requires Step 1 done (cloud registration)."""
        entries = self._call(is_registered=False, skill_id="s1")
        keys = [e.key for e in entries]
        assert CONF_ACTION_GET_OTP not in keys

    def test_step3_hidden_until_skill_created(self) -> None:
        """Step 3 also requires Step 2 done — OTP linking needs an existing skill."""
        entries = self._call(is_registered=True, skill_id="")
        keys = [e.key for e in entries]
        assert CONF_ACTION_GET_OTP not in keys

    def test_step3_visible_after_register_and_skill(self) -> None:
        """After register AND skill created, Step 3 renders the Get-OTP action."""
        entries = self._call(is_registered=True, skill_id="s1")
        keys = [e.key for e in entries]
        assert CONF_ACTION_GET_OTP in keys

    def test_register_action_hidden_once_registered(self) -> None:
        """The Register button disappears after a cloud instance exists."""
        entries = self._call(is_registered=True)
        register = _find(list(entries), CONF_ACTION_REGISTER)
        assert register is not None
        assert register.hidden is True

    def test_skill_token_shown_on_done(self) -> None:
        """Skill OAuth Token input is surfaced (non-advanced) on DONE."""
        entries = self._call(is_registered=True, state=SkillCreationState.DONE)
        token = _find(list(entries), CONF_SKILL_TOKEN)
        assert token is not None
        assert getattr(token, "advanced", False) is False

    def test_skill_token_shown_on_failed(self) -> None:
        """Skill OAuth Token input is also surfaced on FAILED for manual entry."""
        entries = self._call(is_registered=True, state=SkillCreationState.FAILED)
        token = _find(list(entries), CONF_SKILL_TOKEN)
        assert token is not None
        assert getattr(token, "advanced", False) is False

    def test_skill_token_advanced_on_none(self) -> None:
        """Before user interacts, the token field is hidden behind Advanced."""
        entries = self._call(is_registered=True, state=SkillCreationState.NONE)
        token = _find(list(entries), CONF_SKILL_TOKEN)
        assert token is not None
        assert getattr(token, "advanced", False) is True

    def test_manual_fallback_appears_on_failed(self) -> None:
        """FAILED renders manual copy-paste fields inline (non-advanced)."""
        entries = self._call(is_registered=True, state=SkillCreationState.FAILED)
        keys = [e.key for e in entries]
        assert "manual_backend_url" in keys
        assert "manual_client_id" in keys
        assert "manual_auth_url" in keys
        assert "manual_token_url" in keys
        # On FAILED the block is NOT advanced — auto-visible
        backend = _find(list(entries), "manual_backend_url")
        assert getattr(backend, "advanced", False) is False

    def test_manual_fallback_shown_under_advanced_on_done(self) -> None:
        """Happy path keeps manual fields under Advanced (power-user visibility)."""
        entries = self._call(is_registered=True, state=SkillCreationState.DONE)
        keys = [e.key for e in entries]
        # Still emitted — but advanced so the default view stays clean
        assert "manual_backend_url" in keys
        backend = _find(list(entries), "manual_backend_url")
        assert getattr(backend, "advanced", False) is True

    def test_manual_fallback_suppressed_before_cloud_register(self) -> None:
        """Don't render manual fields with an invalid `yandex_smart_home:` Client ID.

        cloud_plus Client ID embeds the yaha-cloud instance UUID; before
        the user clicks Register there's no UUID, and emitting a
        half-formed ``yandex_smart_home:`` value would lead power users
        (Advanced view) to create a skill with broken account-linking.
        """
        entries = self._call(is_registered=False, state=SkillCreationState.NONE)
        keys = [e.key for e in entries]
        assert "manual_backend_url" not in keys
        assert "manual_client_id" not in keys
        assert "manual_fallback_label" not in keys

    def test_otp_code_appears_when_present(self) -> None:
        """OTP code field is visible once an OTP has been fetched."""
        entries = self._call(is_registered=True, skill_id="s1", otp_code="ABC123")
        otp = _find(list(entries), "otp_code")
        assert otp is not None
        assert otp.hidden is False


# ---------------------------------------------------------------------------
# build_direct_entries — 1-step structure (no register, no OTP)
# ---------------------------------------------------------------------------


class TestBuildDirectEntries:
    """direct mode renders a single Create-Skill step (no yaha, no OTP)."""

    def _call(
        self,
        *,
        state: SkillCreationState = SkillCreationState.NONE,
        base_url: str = "https://ma.example.com",
        direct_client_secret: str = "secret-123",  # noqa: S107
    ) -> list[ConfigEntry]:
        return build_direct_entries(
            artifacts=SkillCreationArtifacts(state=state),
            session_id=None,
            user_code=None,
            verification_url=None,
            existing_artifacts_raw=None,
            base_url=base_url,
            direct_client_secret=direct_client_secret,
        )

    def test_no_register_action(self) -> None:
        """Direct mode has no yaha registration step."""
        entries = self._call()
        keys = [e.key for e in entries]
        assert CONF_ACTION_REGISTER not in keys

    def test_no_get_otp_action(self) -> None:
        """Direct mode has no OTP linking step."""
        entries = self._call()
        keys = [e.key for e in entries]
        assert CONF_ACTION_GET_OTP not in keys

    def test_has_create_skill_action(self) -> None:
        """Direct mode renders the Create-Skill action."""
        entries = self._call()
        keys = [e.key for e in entries]
        assert CONF_ACTION_AUTO_CREATE in keys

    def test_create_hidden_when_base_url_not_https(self) -> None:
        """Non-HTTPS base URL disables the create button (Yandex rejects)."""
        entries = self._call(base_url="http://ma.local:8095")
        action = _find(list(entries), CONF_ACTION_AUTO_CREATE)
        assert action is not None
        assert action.hidden is True

    def test_manual_fallback_includes_per_install_secret(self) -> None:
        """FAILED fallback for direct shows the per-install client_secret."""
        entries = self._call(state=SkillCreationState.FAILED)
        # The fallback block surfaces the generated secret in a visible
        # field, plus the Backend URL with the MA base URL.
        backend = _find(list(entries), "manual_backend_url")
        assert backend is not None
        assert "ma.example.com" in str(backend.default_value)
        assert "/api/yandex_smarthome/v1.0" in str(backend.default_value)

    def test_skill_id_field_shown_on_done(self) -> None:
        """Skill ID input field is surfaced (non-advanced) on DONE."""
        entries = self._call(state=SkillCreationState.DONE)
        skill_id = _find(list(entries), CONF_SKILL_ID)
        assert skill_id is not None
        assert getattr(skill_id, "advanced", False) is False
