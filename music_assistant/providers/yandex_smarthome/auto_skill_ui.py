"""ConfigEntry builders for the Yandex Smart Home provider.

Builds the numbered-step config form per connection type:

* ``cloud_plus`` → 3 steps: Register cloud → Create skill → Link via OTP.
* ``direct``     → 1 step:  Create skill  (skill is linked by Yandex Dialogs
  account-linking UI, no OTP).
* ``cloud``      → 2 steps: Register → Link via OTP  (unchanged).

Each step hides until the previous completes, so the user always sees
the single next action they need to take.

Kept separate from ``__init__.py`` so the long field list doesn't bloat
``get_config_entries`` and so it can be unit-tested in isolation from
the network-facing parts of the feature.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from .auto_skill_state import SkillCreationArtifacts, SkillCreationState
from .constants import (
    CLOUD_OAUTH_AUTHORIZE_URL,
    CLOUD_OAUTH_TOKEN_URL,
    CLOUD_SKILL_CLIENT_ID_TEMPLATE,
    CLOUD_SKILL_CLIENT_SECRET,
    CLOUD_SKILL_WEBHOOK_TEMPLATE,
    CONF_ACTION_AUTO_CREATE,
    CONF_ACTION_GET_OTP,
    CONF_ACTION_REGISTER,
    CONF_AUTO_CREATE_ARTIFACTS,
    CONF_AUTO_CREATE_SESSION_ID,
    CONF_CONNECTION_TYPE,
    CONF_DIRECT_CLIENT_SECRET,
    CONF_SKILL_ID,
    CONF_SKILL_TOKEN,
    CONNECTION_TYPE_CLOUD,
    CONNECTION_TYPE_CLOUD_PLUS,
    CONNECTION_TYPE_DIRECT,
    DIRECT_API_BASE_PATH,
    DIRECT_AUTH_BASE_PATH,
    DIRECT_OAUTH_CLIENT_ID,
    YANDEX_DIALOGS_DEVELOPER_URL,
    YANDEX_OAUTH_URL,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "AUTO_CREATE_CATEGORY",
    "auto_create_entries",
    "build_cloud_plus_entries",
    "build_direct_entries",
    "should_show_button",
]

AUTO_CREATE_CATEGORY = "Auto-create skill"

# Category names are used as visual group headers in the MA UI. Numbered
# so they render in order and users see the flow as a sequence.
_CAT_STEP_1_REGISTER = "Step 1 — Register cloud instance"
_CAT_STEP_2_CREATE = "Step 2 — Create Smart Home skill"
_CAT_STEP_3_LINK = "Step 3 — Link skill to Yandex"
# Direct mode has only one step — no cloud registration, no OTP linking.
_CAT_STEP_DIRECT_CREATE = "Create Smart Home skill"


def _status_label(state: SkillCreationState, last_error: str | None) -> str:
    """Human-readable status line shown in the UI."""
    if state == SkillCreationState.DONE:
        return (
            "✅ Skill created and published. Now get the OAuth token "
            "(link below) and paste it into 'Skill OAuth Token'."
        )
    if state == SkillCreationState.FAILED:
        err = last_error or "unknown error"
        return (
            f"❌ Creation failed: {err}\n"
            f"Press '{_action_label(state)}' to try again, or fill in "
            "Skill ID / Skill OAuth Token manually below."
        )
    if state == SkillCreationState.NONE:
        return "Ready to create skill. Press the button below to start."
    # Any partial state — resume is possible.
    return (
        f"Partial progress saved ({state.value}). "
        f"Press '{_action_label(state)}' to finish, or fill Skill ID manually."
    )


def _action_label(state: SkillCreationState) -> str:
    if state == SkillCreationState.NONE:
        return "Create skill automatically"
    if state == SkillCreationState.FAILED:
        return "Retry"
    return "Retry from last step"


def should_show_button(
    *,
    connection_type: str,
    state: SkillCreationState,
    cloud_instance_id: str,
    base_url: str,
) -> bool:
    """Return True iff the auto-create action button is actionable now.

    Hides the button when:
    - Mode is plain ``cloud`` (no custom skill exists there).
    - Skill creation already reached DONE.
    - cloud_plus is selected but no cloud instance has been registered.
    - direct is selected but MA base_url is not HTTPS.
    """
    if connection_type == CONNECTION_TYPE_CLOUD:
        return False
    if state == SkillCreationState.DONE:
        return False
    if connection_type == CONNECTION_TYPE_CLOUD_PLUS and not cloud_instance_id:
        return False
    return not (connection_type == CONNECTION_TYPE_DIRECT and not base_url.startswith("https://"))


def auto_create_entries(
    *,
    connection_type: str,
    artifacts: SkillCreationArtifacts,
    cloud_instance_id: str,
    base_url: str,
    session_id: str | None,
    user_code: str | None,
    verification_url: str | None,
    existing_artifacts_raw: str | None,
) -> Sequence[ConfigEntry]:
    """Build the auto-create section of the config form.

    Empty list for ``cloud`` mode — the feature is meaningless without
    a custom skill.
    """
    if connection_type == CONNECTION_TYPE_CLOUD:
        return ()

    entries: list[ConfigEntry] = []

    # Device-flow user code — shown when the flow obtained one this round.
    if user_code:
        entries.append(
            ConfigEntry(
                key="auto_create_user_code",
                type=ConfigEntryType.STRING,
                label="Device code for ya.ru/device",
                description=(
                    "Open the URL below in your browser, log in to your "
                    "Yandex account, and enter this code."
                ),
                value=user_code,
                required=False,
                help_link=verification_url or "https://ya.ru/device",
                depends_on=CONF_CONNECTION_TYPE,
                depends_on_value_not=CONNECTION_TYPE_CLOUD,
                category=AUTO_CREATE_CATEGORY,
            )
        )

    # Status label — dynamic based on current artifact state.
    entries.append(
        ConfigEntry(
            key="label_auto_create_status",
            type=ConfigEntryType.LABEL,
            label=_status_label(artifacts.state, artifacts.last_error),
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value_not=CONNECTION_TYPE_CLOUD,
            category=AUTO_CREATE_CATEGORY,
        )
    )

    # The action button — hidden in states where it can't run.
    show_button = should_show_button(
        connection_type=connection_type,
        state=artifacts.state,
        cloud_instance_id=cloud_instance_id,
        base_url=base_url,
    )
    entries.append(
        ConfigEntry(
            key=CONF_ACTION_AUTO_CREATE,
            type=ConfigEntryType.ACTION,
            label=_action_label(artifacts.state),
            description=(
                "Runs the Yandex Device Flow login, then creates and "
                "publishes the private Smart Home skill. Takes ~30 seconds "
                "after you enter the code."
            ),
            action=CONF_ACTION_AUTO_CREATE,
            action_label=_action_label(artifacts.state),
            hidden=not show_button,
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value_not=CONNECTION_TYPE_CLOUD,
            category=AUTO_CREATE_CATEGORY,
        )
    )

    entries.extend(_hidden_state_entries(existing_artifacts_raw, session_id))
    return entries


def _hidden_state_entries(
    existing_artifacts_raw: str | None, session_id: str | None
) -> list[ConfigEntry]:
    """Round-trip the artifact blob and session id through the config form."""
    return [
        ConfigEntry(
            key=CONF_AUTO_CREATE_ARTIFACTS,
            type=ConfigEntryType.STRING,
            label="Auto-create artifacts (internal)",
            hidden=True,
            required=False,
            value=existing_artifacts_raw,
        ),
        ConfigEntry(
            key=CONF_AUTO_CREATE_SESSION_ID,
            type=ConfigEntryType.STRING,
            label="Auto-create session id (internal)",
            hidden=True,
            required=False,
            value=session_id,
        ),
    ]


# ---------------------------------------------------------------------------
# Cloud Plus step-flow
# ---------------------------------------------------------------------------


def build_cloud_plus_entries(  # noqa: PLR0913
    *,
    otp_code: str | None,
    is_registered: bool,
    cloud_instance_id: str,
    artifacts: SkillCreationArtifacts,
    session_id: str | None,
    user_code: str | None,
    verification_url: str | None,
    existing_artifacts_raw: str | None,
    base_url: str,
    skill_id: str = "",
    skill_token_set: bool = False,
) -> list[ConfigEntry]:
    """Return the cloud_plus-mode config entries as three visible steps.

    Step 1 (Register)       — always visible.
    Step 2 (Create skill)   — visible once cloud instance is registered.
    Step 3 (Link via OTP)   — visible once the skill (id + token) is set.
    """
    skill_id_set = bool(skill_id)
    fully_configured = skill_id_set and skill_token_set

    entries: list[ConfigEntry] = []
    entries.extend(_step1_register_entries(is_registered, cloud_instance_id))
    entries.extend(
        _step2_create_skill_entries(
            is_registered=is_registered,
            cloud_instance_id=cloud_instance_id,
            artifacts=artifacts,
            user_code=user_code,
            verification_url=verification_url,
            base_url=base_url,
            skill_id=skill_id,
            fully_configured=fully_configured,
        )
    )
    entries.extend(
        _step3_link_entries(
            is_registered=is_registered,
            skill_id_set=skill_id_set,
            otp_code=otp_code,
        )
    )
    entries.extend(_hidden_state_entries(existing_artifacts_raw, session_id))
    return entries


def _step1_register_entries(is_registered: bool, cloud_instance_id: str) -> list[ConfigEntry]:
    """Step 1 — yaha-cloud.ru instance registration."""
    status_text = (
        f"✅ Cloud instance registered (id: {cloud_instance_id})."
        if is_registered
        else (
            "Click 'Register with cloud' to create a yaha-cloud.ru relay "
            "instance. This is free and takes a second."
        )
    )
    return [
        ConfigEntry(
            key="label_step1_status",
            type=ConfigEntryType.LABEL,
            label=status_text,
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value=CONNECTION_TYPE_CLOUD_PLUS,
            category=_CAT_STEP_1_REGISTER,
        ),
        ConfigEntry(
            key=CONF_ACTION_REGISTER,
            type=ConfigEntryType.ACTION,
            label="Register cloud instance",
            description="Registers a new instance on yaha-cloud.ru relay.",
            action=CONF_ACTION_REGISTER,
            action_label="Register with cloud",
            hidden=is_registered,
            # No depends_on — see note above action_auto_create.
            category=_CAT_STEP_1_REGISTER,
        ),
    ]


def _create_skill_step_entries(
    *,
    connection_type: str,
    category: str,
    cloud_instance_id: str,
    artifacts: SkillCreationArtifacts,
    user_code: str | None,
    verification_url: str | None,
    base_url: str,
    direct_client_secret: str = "",
    skill_id: str = "",
    fully_configured: bool = False,
) -> list[ConfigEntry]:
    """Shared builder for the Create-Skill step.

    Used by both cloud_plus Step 2 and direct single-step mode.

    Skill ID / Skill OAuth Token are shown after DONE (happy path) or
    FAILED (so the user can finish manually). FAILED additionally shows
    manual copy-paste fields (Backend URL / Client ID / Secret / Auth
    URLs / Dialogs console link) so the user can create the skill by
    hand in Yandex.Dialogs without leaving the form.
    """
    entries: list[ConfigEntry] = []

    if user_code:
        entries.append(
            ConfigEntry(
                key="auto_create_user_code",
                type=ConfigEntryType.STRING,
                label="Device code for ya.ru/device",
                description=(
                    "Open the URL below, log in to your Yandex account, and enter this code."
                ),
                value=user_code,
                required=False,
                help_link=verification_url or "https://ya.ru/device",
                depends_on=CONF_CONNECTION_TYPE,
                depends_on_value=connection_type,
                category=category,
            )
        )

    entries.append(
        ConfigEntry(
            key="label_create_skill_status",
            type=ConfigEntryType.LABEL,
            label=_status_label(artifacts.state, artifacts.last_error),
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value=connection_type,
            category=category,
        )
    )

    # direct mode prerequisite: Yandex Dialogs only accepts HTTPS
    # backends. If MA's Base URL is not HTTPS, show the user what's
    # wrong (and what URL we'd have used) so they can fix it in
    # Settings → Core → Webserver → Base URL.
    direct_https_missing = connection_type == CONNECTION_TYPE_DIRECT and not base_url.startswith(
        "https://"
    )
    if direct_https_missing:
        entries.append(
            ConfigEntry(
                key="label_direct_https_warning",
                type=ConfigEntryType.LABEL,
                label=(
                    f"⚠️ MA's Base URL is {base_url or '<unset>'}. "
                    "Direct mode requires a **publicly reachable HTTPS URL** — "
                    "Yandex refuses to talk to a non-HTTPS backend. "
                    "Set a reverse proxy with a real certificate and "
                    "update Settings → Core → Webserver → Base URL, then "
                    "reopen these settings."
                ),
                depends_on=CONF_CONNECTION_TYPE,
                depends_on_value=connection_type,
                category=category,
            )
        )
        entries.append(
            ConfigEntry(
                key="current_ma_base_url",
                type=ConfigEntryType.STRING,
                label="Current MA Base URL (read-only)",
                description=(
                    "For reference. Change it in Settings → Core → Webserver "
                    "→ Base URL; provider doesn't own this setting."
                ),
                required=False,
                default_value=base_url or "",
                depends_on=CONF_CONNECTION_TYPE,
                depends_on_value=connection_type,
                category=category,
            )
        )

    show_button = should_show_button(
        connection_type=connection_type,
        state=artifacts.state,
        cloud_instance_id=cloud_instance_id,
        base_url=base_url,
    )
    entries.append(
        ConfigEntry(
            key=CONF_ACTION_AUTO_CREATE,
            type=ConfigEntryType.ACTION,
            label=_action_label(artifacts.state),
            description=(
                "Runs the Yandex Device Flow login, then creates and "
                "publishes the private Smart Home skill."
            ),
            action=CONF_ACTION_AUTO_CREATE,
            action_label=_action_label(artifacts.state),
            hidden=not show_button,
            # No depends_on — MA disables actions with unsaved-dependency
            # fields until the user clicks Save, which breaks the flow.
            # Visibility is already correctly gated via `hidden`.
            category=category,
        )
    )

    # Manual-fallback copy-paste fields — always emitted so advanced
    # users can edit them, but on any state other than FAILED they're
    # marked ``advanced=True`` so default view stays clean. On FAILED
    # they show up unconditionally (auto-fallback UX).
    entries.extend(
        _manual_fallback_entries(
            connection_type=connection_type,
            category=category,
            cloud_instance_id=cloud_instance_id,
            base_url=base_url,
            direct_client_secret=direct_client_secret,
            advanced=artifacts.state != SkillCreationState.FAILED,
        )
    )

    # Skill ID / Skill OAuth Token / OAuth URL — the actual fields the
    # provider needs at runtime. Auto-shown once auto-create reached
    # DONE or FAILED; hidden under Advanced once the user has fully
    # configured them (so a clean default view stays clean after setup).
    token_fields_advanced = fully_configured or artifacts.state not in (
        SkillCreationState.DONE,
        SkillCreationState.FAILED,
    )
    entries.extend(
        [
            ConfigEntry(
                key="oauth_url",
                type=ConfigEntryType.STRING,
                label="OAuth URL (open to get token)",
                description=(
                    "Open this URL in your browser, approve, and copy the "
                    "access_token from the resulting URL into the field below."
                ),
                required=False,
                default_value=YANDEX_OAUTH_URL,
                help_link=YANDEX_OAUTH_URL,
                advanced=token_fields_advanced,
                depends_on=CONF_CONNECTION_TYPE,
                depends_on_value=connection_type,
                category=category,
            ),
            ConfigEntry(
                key=CONF_SKILL_TOKEN,
                type=ConfigEntryType.SECURE_STRING,
                label="Skill OAuth Token",
                description="Paste the OAuth token obtained from the URL above.",
                required=False,
                advanced=token_fields_advanced,
                depends_on=CONF_CONNECTION_TYPE,
                depends_on_value=connection_type,
                category=category,
            ),
            ConfigEntry(
                key=CONF_SKILL_ID,
                type=ConfigEntryType.STRING,
                label="Skill ID",
                description=(
                    "UUID of your private Smart Home skill. Set automatically "
                    "when auto-create succeeds; you can paste it manually if "
                    "you created the skill by hand."
                ),
                required=False,
                advanced=token_fields_advanced,
                depends_on=CONF_CONNECTION_TYPE,
                depends_on_value=connection_type,
                category=category,
            ),
        ]
    )

    # Once setup is complete, replace the cluster of edit fields with
    # a single non-editable link to the skill in Yandex.Dialogs so the
    # user can quickly open it (the fields are still available under
    # Advanced if they need to re-edit).
    if fully_configured and skill_id:
        skill_url = f"https://dialogs.yandex.ru/developer/skills/{skill_id}/"
        entries.append(
            ConfigEntry(
                key="skill_dialogs_link",
                type=ConfigEntryType.STRING,
                label="Open skill in Yandex.Dialogs",
                description="Click the link to open the skill's page.",
                required=False,
                default_value=skill_url,
                help_link=skill_url,
                depends_on=CONF_CONNECTION_TYPE,
                depends_on_value=connection_type,
                category=category,
            )
        )

    return entries


def _manual_fallback_entries(
    *,
    connection_type: str,
    category: str,
    cloud_instance_id: str,
    base_url: str,
    direct_client_secret: str,
    advanced: bool,
) -> list[ConfigEntry]:
    """Copy-paste fields for creating the skill by hand.

    ``advanced=True``  → hidden from the default view, shown when the
    user toggles Advanced. Used when auto-create is expected to work
    but power users still want to see/edit everything.

    ``advanced=False`` → visible unconditionally. Used on FAILED state
    so the user has everything they need to finish manually without
    needing to click Advanced.
    """
    if connection_type == CONNECTION_TYPE_CLOUD_PLUS:
        # The cloud_plus Client ID embeds the yaha-cloud instance UUID,
        # which doesn't exist until the user registers. Suppress the
        # manual block entirely before registration rather than render
        # an invalid ``yandex_smart_home:`` Client ID that would lead
        # someone to create a broken skill.
        if not cloud_instance_id:
            return []
        backend_uri = CLOUD_SKILL_WEBHOOK_TEMPLATE
        client_id = CLOUD_SKILL_CLIENT_ID_TEMPLATE.format(instance_id=cloud_instance_id)
        client_secret = CLOUD_SKILL_CLIENT_SECRET
        auth_url = CLOUD_OAUTH_AUTHORIZE_URL
        token_url = CLOUD_OAUTH_TOKEN_URL
    elif connection_type == CONNECTION_TYPE_DIRECT:
        base = base_url.rstrip("/") or "https://<YOUR_MA_HOST>"
        backend_uri = f"{base}{DIRECT_API_BASE_PATH}"
        client_id = DIRECT_OAUTH_CLIENT_ID
        client_secret = direct_client_secret or "(auto-generated on save)"
        auth_url = f"{base}{DIRECT_AUTH_BASE_PATH}/authorize"
        token_url = f"{base}{DIRECT_AUTH_BASE_PATH}/token"
    else:
        return []

    label_text = (
        "Auto-create failed — you can create the skill by hand instead. "
        "Open Yandex.Dialogs (link below), create a private Smart Home "
        "skill, paste the values below into the skill's Basic info and "
        "Account linking tabs, then put the skill UUID in the Skill ID "
        "field below."
        if not advanced
        else (
            "Manual setup values — copy these into Yandex.Dialogs if you "
            "prefer to create the skill by hand, or want to verify what "
            "auto-create used."
        )
    )

    # For direct mode, also surface the hidden generated secret as an
    # editable field so user can copy it.
    extra: list[ConfigEntry] = []
    if connection_type == CONNECTION_TYPE_DIRECT and direct_client_secret:
        extra.append(
            ConfigEntry(
                key=CONF_DIRECT_CLIENT_SECRET,
                type=ConfigEntryType.SECURE_STRING,
                label="Client Secret (→ Account linking)",
                description="Copy to 'Account linking' → 'Client secret' field.",
                required=False,
                default_value=direct_client_secret,
                advanced=advanced,
                depends_on=CONF_CONNECTION_TYPE,
                depends_on_value=connection_type,
                category=category,
            )
        )

    return [
        ConfigEntry(
            key="manual_fallback_label",
            type=ConfigEntryType.LABEL,
            label=label_text,
            advanced=advanced,
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value=connection_type,
            category=category,
        ),
        ConfigEntry(
            key="manual_dialogs_url",
            type=ConfigEntryType.STRING,
            label="Yandex.Dialogs Console",
            required=False,
            default_value=YANDEX_DIALOGS_DEVELOPER_URL,
            help_link=YANDEX_DIALOGS_DEVELOPER_URL,
            advanced=advanced,
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value=connection_type,
            category=category,
        ),
        ConfigEntry(
            key="manual_backend_url",
            type=ConfigEntryType.STRING,
            label="Backend URL (→ Basic info)",
            description="Copy to 'Basic info' → 'Backend URL' in your skill.",
            required=False,
            # Reference-only fields use default_value so MA UI renders
            # the text without storing it as a mutable config value
            # (``value=`` was returning empty on first render before the
            # user clicked Save).
            default_value=backend_uri,
            advanced=advanced,
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value=connection_type,
            category=category,
        ),
        ConfigEntry(
            key="manual_client_id",
            type=ConfigEntryType.STRING,
            label="Client ID (→ Account linking)",
            description="Copy to 'Account linking' → 'Client identifier' field.",
            required=False,
            default_value=client_id,
            advanced=advanced,
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value=connection_type,
            category=category,
        ),
        *extra,
        # manual_client_secret is a plain STRING so it's readable for
        # copy-paste in cloud_plus (where the value is the literal
        # "secret"). For direct mode we skip it: the real per-install
        # UUID is surfaced via the SECURE_STRING CONF_DIRECT_CLIENT_SECRET
        # entry in ``extra`` above, and showing the same value in a
        # second unmasked STRING would leak it.
        *(
            [
                ConfigEntry(
                    key="manual_client_secret",
                    type=ConfigEntryType.STRING,
                    label="Client Secret value (for reference)",
                    description=("Copy this string into 'Account linking' → 'Client secret'."),
                    required=False,
                    default_value=client_secret,
                    advanced=advanced,
                    depends_on=CONF_CONNECTION_TYPE,
                    depends_on_value=connection_type,
                    category=category,
                )
            ]
            if connection_type != CONNECTION_TYPE_DIRECT
            else []
        ),
        ConfigEntry(
            key="manual_auth_url",
            type=ConfigEntryType.STRING,
            label="Authorization URL (→ Account linking)",
            required=False,
            default_value=auth_url,
            advanced=advanced,
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value=connection_type,
            category=category,
        ),
        ConfigEntry(
            key="manual_token_url",
            type=ConfigEntryType.STRING,
            label="Token URL (→ Account linking, both fields)",
            description="Paste into BOTH 'Token endpoint' and 'Refresh token URL'.",
            required=False,
            default_value=token_url,
            advanced=advanced,
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value=connection_type,
            category=category,
        ),
    ]


def _step2_create_skill_entries(
    *,
    is_registered: bool,  # noqa: ARG001 — kept for call-site symmetry
    cloud_instance_id: str,
    artifacts: SkillCreationArtifacts,
    user_code: str | None,
    verification_url: str | None,
    base_url: str,
    skill_id: str = "",
    fully_configured: bool = False,
) -> list[ConfigEntry]:
    """cloud_plus Step 2 — always emitted.

    The Create-Skill action button self-hides via ``should_show_button``
    when no cloud instance exists yet, but the section's other fields
    (status label + advanced manual-setup references) stay visible so
    power users can see everything under Advanced without first going
    through the register step.
    """
    return _create_skill_step_entries(
        connection_type=CONNECTION_TYPE_CLOUD_PLUS,
        category=_CAT_STEP_2_CREATE,
        cloud_instance_id=cloud_instance_id,
        artifacts=artifacts,
        user_code=user_code,
        verification_url=verification_url,
        base_url=base_url,
        skill_id=skill_id,
        fully_configured=fully_configured,
    )


def _step3_link_entries(
    *, is_registered: bool, skill_id_set: bool, otp_code: str | None
) -> list[ConfigEntry]:
    """Step 3 — get an OTP from the cloud and enter it in the Yandex app.

    Hidden until both Step 1 (cloud registration) and Step 2 (skill
    created — ``skill_id_set``) are done: OTP linking only makes sense
    once the private skill exists in Yandex.Dialogs for the user to
    link against in the Yandex app.
    """
    if not is_registered or not skill_id_set:
        return []

    # Banner priority: fresh OTP > linked (skill configured).
    if otp_code:
        banner_text = f"Enter this OTP in the Yandex app: {otp_code}"
    else:
        banner_text = (
            "✅ Skill configured. Press 'Get OTP code' to link it with your "
            "Yandex account (or re-link if you ever unlinked it)."
        )

    entries: list[ConfigEntry] = [
        ConfigEntry(
            key="label_step3_status",
            type=ConfigEntryType.LABEL,
            label=banner_text,
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value=CONNECTION_TYPE_CLOUD_PLUS,
            category=_CAT_STEP_3_LINK,
        ),
        ConfigEntry(
            key="otp_code",
            type=ConfigEntryType.STRING,
            label="OTP Code",
            description="Copy this code and enter it in the Yandex app.",
            required=False,
            value=otp_code,
            hidden=not otp_code,
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value=CONNECTION_TYPE_CLOUD_PLUS,
            category=_CAT_STEP_3_LINK,
        ),
        ConfigEntry(
            key=CONF_ACTION_GET_OTP,
            type=ConfigEntryType.ACTION,
            label="Get OTP code",
            description="Get a fresh one-time password to link with Yandex.",
            action=CONF_ACTION_GET_OTP,
            action_label="Get OTP code",
            # No depends_on — see note above action_auto_create.
            category=_CAT_STEP_3_LINK,
        ),
    ]
    return entries


# ---------------------------------------------------------------------------
# Direct-mode flow (auto-create only; no cloud registration, no OTP)
# ---------------------------------------------------------------------------


def build_direct_entries(
    *,
    artifacts: SkillCreationArtifacts,
    session_id: str | None,
    user_code: str | None,
    verification_url: str | None,
    existing_artifacts_raw: str | None,
    base_url: str,
    direct_client_secret: str = "",
    skill_id: str = "",
    skill_token_set: bool = False,
) -> list[ConfigEntry]:
    """Return the direct-mode config entries as a single Create-Skill step.

    direct mode has no yaha-cloud registration (Step 1) and no OTP
    linking (Step 3) — Yandex Dialogs' account-linking UI handles that
    once the skill exists.
    """
    fully_configured = bool(skill_id) and skill_token_set
    entries = _create_skill_step_entries(
        connection_type=CONNECTION_TYPE_DIRECT,
        category=_CAT_STEP_DIRECT_CREATE,
        cloud_instance_id="",
        artifacts=artifacts,
        user_code=user_code,
        verification_url=verification_url,
        base_url=base_url,
        direct_client_secret=direct_client_secret,
        skill_id=skill_id,
        fully_configured=fully_configured,
    )
    entries.extend(_hidden_state_entries(existing_artifacts_raw, session_id))
    return entries
