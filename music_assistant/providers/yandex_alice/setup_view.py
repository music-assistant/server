r"""
Provider settings form — clean rewrite with three top-level categories.

The form is grouped by *what the entry semantically belongs to*, not
by linear setup steps:

* **Authorization** — Yandex Passport sign-in / sign-out. Always
  visible. Primary CTA when no token is cached, signed-in banner +
  secondary "Sign out" otherwise.
* **Skill** — everything that touches the Yandex Dialogs skill
  itself: skill name, the public webhook URL, Create / Edit /
  Update / Delete buttons, the duplicate-name resolution prompt,
  the dev-console link, and (under *Show advanced*) the manual
  fields (skill_id, OAuth token, webhook secret) plus diagnostics.
  Visible whenever auth is done OR a manual ``skill_id`` is set.
* **Settings** — provider-wide tuning that does not relate to the
  skill object: voice-controllable players + the (advanced)
  "different MA-side instance name" toggle.

Each block is a small builder. The top-level
:func:`build_form_entries` composes the three of them, stamps every
entry with its category, and returns the flat tuple MA's frontend
expects.

Frontend sections render directly off ``ConfigEntry.category``: any
slug that is not ``generic`` / ``advanced`` / ``protocol_general``
becomes its own visual section, with the header text resolved from
``config_categories.{slug}`` in ``strings.json`` (the slug itself is
the fallback, hence TitleCase slugs).

All user-facing entry texts live in ``strings.json`` under
``config_entries.<key>.<field>`` and are resolved by MA at
serialization; dynamic fragments flow through ``translation_params``
(``{0}``/``{1}`` placeholders). Entries whose text is composed at
runtime by the dispatcher (outcome / update messages) keep passing
``label=`` directly — their keys are deliberately absent from
``strings.json`` so the in-code text survives serialization.
"""

from __future__ import annotations

import contextlib
import dataclasses
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from ya_dialogs_api import SkillCreationArtifacts, SkillCreationState
from ya_passport_auth.ma import BORROW_SOURCE_OWN

from .auto_create import AutoCreateOutcome, LocalAutoCreateStage
from .constants import (
    CATEGORY_AUTHORIZATION,
    CATEGORY_SETTINGS,
    CATEGORY_SKILL,
    CONF_ACTION_ADOPT_EXISTING,
    CONF_ACTION_AUTO_CREATE_DIALOG,
    CONF_ACTION_CANCEL_DIALOG_SKILL_FLOW,
    CONF_ACTION_CANCEL_EDIT,
    CONF_ACTION_CLEAR_AUTH,
    CONF_ACTION_DELETE_SKILL,
    CONF_ACTION_EDIT_SKILL,
    CONF_ACTION_EXPORT_MANIFEST,
    CONF_ACTION_IMPORT_MANIFEST,
    CONF_ACTION_RECREATE_DUPLICATE,
    CONF_ACTION_REFRESH_STATUS,
    CONF_ACTION_REGENERATE_WEBHOOK_SECRET,
    CONF_ACTION_RESET_MANIFEST,
    CONF_ACTION_SIGN_IN,
    CONF_ACTION_TEST_WEBHOOK,
    CONF_ACTION_UPDATE_SKILL,
    CONF_ACTION_VALIDATE_MANIFEST,
    CONF_DIALOG_ACTIVATION_PHRASE_2,
    CONF_DIALOG_ACTIVATION_PHRASE_3,
    CONF_DIALOG_ACTIVATION_PHRASE_4,
    CONF_DIALOG_SKILL_ID,
    CONF_DIALOG_SKILL_NAME,
    CONF_DIALOG_SKILL_OVERRIDE_PASTE,
    CONF_DIALOG_SKILL_TOKEN,
    CONF_DIALOG_SKILL_VOICE,
    CONF_DIALOG_WEBHOOK_SECRET,
    CONF_EXPOSED_PLAYERS,
    CONF_EXTERNAL_BASE_URL,
    CONF_INSTANCE_NAME,
    CONF_USE_DIFFERENT_INSTANCE_NAME,
    CONF_YM_INSTANCE,
    DIALOG_DEFAULT_NAME,
    DIALOG_VOICE_DEFAULT,
    DIALOG_VOICE_OPTIONS,
    DIALOG_WEBHOOK_BASE_PATH,
)
from .dialog_skill_meta import validate_activation_phrase, validate_skill_name
from .publication_status import (
    STATUS_DRAFT,
    STATUS_IN_MODERATION,
    STATUS_ON_AIR,
    STATUS_REJECTED,
)
from .skill_manifest_provider import ManifestActionResult, ManifestStatus
from .url_helpers import validate_external_base_url


def _publication_status_banner(
    status: str | None,
) -> tuple[str, ConfigEntryType]:
    """
    Map a classified publication status to a Step 3 banner.

    Returns ``(translation_key, entry_type)`` — the key selects the
    banner text from ``strings.json``. Negative / actionable states
    use :attr:`ConfigEntryType.ALERT` — the MA frontend renders these
    as a tonal amber ``v-alert`` to draw the eye. Neutral / success
    states use :attr:`ConfigEntryType.LABEL` (plain text) so the form
    doesn't scream at a user whose skill is fine.

    Color emoji prefixes carry the semantic differentiation since
    ALERT's color is hard-coded amber by the frontend:

    * ✅ on-air (success) — LABEL
    * ⏳ in-moderation (waiting) — ALERT
    * ❌ rejected (error) — ALERT
    * ⚠️ draft (needs user action) — ALERT
    * info icon: unknown / not yet fetched — LABEL
    """
    if status == STATUS_ON_AIR:
        return ("pub_status_on_air", ConfigEntryType.LABEL)
    if status == STATUS_IN_MODERATION:
        return ("pub_status_in_moderation", ConfigEntryType.ALERT)
    if status == STATUS_REJECTED:
        return ("pub_status_rejected", ConfigEntryType.ALERT)
    if status == STATUS_DRAFT:
        return ("pub_status_draft", ConfigEntryType.ALERT)
    return ("pub_status_unknown", ConfigEntryType.LABEL)


if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["build_form_entries"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stamp(entries: Iterable[ConfigEntry], category: str) -> tuple[ConfigEntry, ...]:
    """
    Set ``category`` on every entry that hasn't picked one explicitly.

    Default for ``ConfigEntry.category`` is ``"generic"``; treat that
    as "needs a category" and stamp it with *category*. The section
    header text resolves from ``config_categories.<slug>`` in
    ``strings.json`` (slug itself as fallback).
    """
    out: list[ConfigEntry] = []
    for e in entries:
        current = getattr(e, "category", "") or ""
        if current and current != "generic":
            out.append(e)
            continue
        # dataclasses.replace fails on non-dataclass ConfigEntry stand-ins
        # (the test suite's conftest stub); fall back to attribute set.
        try:
            out.append(dataclasses.replace(e, category=category))
        except TypeError, ValueError:
            with contextlib.suppress(Exception):
                e.category = category
            out.append(e)
    return tuple(out)


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


def _authorization_block(
    *,
    signed_in: bool,
    user_name: str,
    last_error: str | None,
    borrow_options: list[ConfigValueOption],
    borrow_selected: str,
    borrow_error: str | None,
) -> tuple[ConfigEntry, ...]:
    """Account-source dropdown plus sign-in CTA / signed-in banner / borrow label."""
    source_entry = ConfigEntry(
        key=CONF_YM_INSTANCE,
        type=ConfigEntryType.STRING,
        options=borrow_options,
        default_value=borrow_selected,
        required=False,
    )
    if borrow_selected and borrow_selected != BORROW_SOURCE_OWN:
        borrowed: list[ConfigEntry] = [source_entry]
        if borrow_error:
            borrowed.append(
                ConfigEntry(
                    key="label_auth_borrow_error",
                    type=ConfigEntryType.ALERT,
                    translation_key="alert_error",
                    translation_params=[borrow_error],
                )
            )
        borrowed.append(
            ConfigEntry(
                key="label_auth_borrowed",
                type=ConfigEntryType.LABEL,
            )
        )
        return tuple(borrowed)
    if signed_in:
        # Anonymous variant: a params-injected English fallback would
        # stay untranslated inside a localized sentence, so the no-name
        # case gets its own authored text instead.
        display_name = user_name.strip()
        status_entry = (
            ConfigEntry(
                key="label_auth_status",
                type=ConfigEntryType.LABEL,
                translation_params=[display_name],
            )
            if display_name
            else ConfigEntry(
                key="label_auth_status",
                type=ConfigEntryType.LABEL,
                translation_key="label_auth_status_anonymous",
            )
        )
        return (
            source_entry,
            status_entry,
            ConfigEntry(
                key=CONF_ACTION_CLEAR_AUTH,
                type=ConfigEntryType.ACTION,
                action=CONF_ACTION_CLEAR_AUTH,
                required=False,
                default_value="",
            ),
        )

    entries: list[ConfigEntry] = [source_entry]
    if last_error:
        entries.append(
            ConfigEntry(
                key="label_auth_last_error",
                type=ConfigEntryType.ALERT,
                translation_key="alert_error",
                translation_params=[last_error],
            )
        )
    entries.append(
        ConfigEntry(
            key="label_auth_intro",
            type=ConfigEntryType.LABEL,
        )
    )
    entries.append(
        ConfigEntry(
            key=CONF_ACTION_SIGN_IN,
            type=ConfigEntryType.ACTION,
            action=CONF_ACTION_SIGN_IN,
            required=False,
            default_value="",
        )
    )
    return tuple(entries)


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------


def _skill_create_subblock(
    *,
    artifacts: SkillCreationArtifacts,
    skill_name: str,
    external_base_url: str,
    base_url_description: str,
    base_url_valid: bool,
    update_message: str | None,
    stage: LocalAutoCreateStage,
) -> tuple[ConfigEntry, ...]:
    """Pre-DONE state: Skill name + URL + Create / Continue / Try-again."""
    entries: list[ConfigEntry] = [
        ConfigEntry(
            key=CONF_DIALOG_SKILL_NAME,
            type=ConfigEntryType.STRING,
            required=False,
            value=skill_name,
            default_value="",
            validate=validate_skill_name,
        ),
        # base_url_description is composed at runtime (probe results) —
        # strings.json deliberately authors no description for this key
        # so the dynamic text survives serialization.
        ConfigEntry(
            key=CONF_EXTERNAL_BASE_URL,
            type=ConfigEntryType.STRING,
            description=base_url_description,
            required=False,
            value=external_base_url,
            default_value="",
            validate=validate_external_base_url,
        ),
    ]
    if base_url_valid:
        entries.append(
            ConfigEntry(
                key=CONF_ACTION_TEST_WEBHOOK,
                type=ConfigEntryType.ACTION,
                action=CONF_ACTION_TEST_WEBHOOK,
                required=False,
                default_value="",
            )
        )

    if update_message and stage not in (
        LocalAutoCreateStage.FAILED,
        LocalAutoCreateStage.DUPLICATE_DETECTED,
    ):
        entries.append(
            ConfigEntry(
                key="label_skill_msg",
                type=ConfigEntryType.LABEL,
                label=update_message,
            )
        )

    if stage == LocalAutoCreateStage.PIPELINE_RUNNING:
        entries.append(
            ConfigEntry(
                key="label_skill_resume",
                type=ConfigEntryType.LABEL,
                translation_params=[artifacts.state.value],
            )
        )
        # The auto-create button keeps one key (the dispatcher matches
        # on it) but re-keys its texts per stage via translation_key.
        entries.append(
            ConfigEntry(
                key=CONF_ACTION_AUTO_CREATE_DIALOG,
                type=ConfigEntryType.ACTION,
                translation_key="auto_create_continue",
                action=CONF_ACTION_AUTO_CREATE_DIALOG,
                required=False,
                default_value="",
            )
        )
        return tuple(entries)

    if stage == LocalAutoCreateStage.FAILED:
        # No-detail variant gets its own authored text — an English
        # "Unknown error." injected via params would stay untranslated.
        err = (artifacts.last_error or "").strip()
        entries.append(
            ConfigEntry(
                key="label_skill_failed",
                type=ConfigEntryType.ALERT,
                translation_key="alert_error",
                translation_params=[err],
            )
            if err
            else ConfigEntry(
                key="label_skill_failed",
                type=ConfigEntryType.ALERT,
                translation_key="label_skill_failed_unknown",
            )
        )
        if external_base_url:
            entries.append(
                ConfigEntry(
                    key="label_skill_failed_manual",
                    type=ConfigEntryType.LABEL,
                    translation_params=[
                        external_base_url.rstrip("/"),
                        DIALOG_WEBHOOK_BASE_PATH,
                    ],
                )
            )
        entries.append(
            ConfigEntry(
                key=CONF_ACTION_AUTO_CREATE_DIALOG,
                type=ConfigEntryType.ACTION,
                translation_key="auto_create_retry",
                action=CONF_ACTION_AUTO_CREATE_DIALOG,
                required=False,
                default_value="",
            )
        )
        entries.append(
            ConfigEntry(
                key=CONF_ACTION_CANCEL_DIALOG_SKILL_FLOW,
                type=ConfigEntryType.ACTION,
                action=CONF_ACTION_CANCEL_DIALOG_SKILL_FLOW,
                required=False,
                default_value="",
            )
        )
        return tuple(entries)

    # IDLE — ready to create.
    entries.append(
        ConfigEntry(
            key="label_skill_intro",
            type=ConfigEntryType.LABEL,
        )
    )
    entries.append(
        ConfigEntry(
            key=CONF_ACTION_AUTO_CREATE_DIALOG,
            type=ConfigEntryType.ACTION,
            translation_key="auto_create_start",
            action=CONF_ACTION_AUTO_CREATE_DIALOG,
            required=False,
            default_value="",
        )
    )
    return tuple(entries)


def _skill_duplicate_subblock(
    *,
    duplicate_skill_name: str,
    duplicate_skill_id: str,
    action_outcome: AutoCreateOutcome | None,
) -> tuple[ConfigEntry, ...]:
    """Same-name conflict: Recreate / Adopt resolution prompt."""
    entries: list[ConfigEntry] = []
    if action_outcome and action_outcome.user_message:
        entries.append(
            ConfigEntry(
                key="label_skill_outcome",
                type=ConfigEntryType.LABEL,
                label=action_outcome.user_message,
            )
        )
    entries.append(
        ConfigEntry(
            key="label_skill_dup_intro",
            type=ConfigEntryType.LABEL,
            translation_params=[duplicate_skill_name],
        )
    )
    entries.append(
        ConfigEntry(
            key="label_skill_dup_recreate_hint",
            type=ConfigEntryType.LABEL,
        )
    )
    entries.append(
        ConfigEntry(
            key="label_skill_dup_adopt_hint",
            type=ConfigEntryType.LABEL,
        )
    )
    if duplicate_skill_id:
        url = f"https://dialogs.yandex.ru/developer/skills/{duplicate_skill_id}"
        entries.append(
            ConfigEntry(
                key="label_skill_dup_console",
                type=ConfigEntryType.LABEL,
                translation_params=[url],
                help_link=url,
            )
        )
    entries.append(
        ConfigEntry(
            key=CONF_ACTION_RECREATE_DUPLICATE,
            type=ConfigEntryType.ACTION,
            action=CONF_ACTION_RECREATE_DUPLICATE,
            required=False,
            default_value="",
        )
    )
    entries.append(
        ConfigEntry(
            key=CONF_ACTION_ADOPT_EXISTING,
            type=ConfigEntryType.ACTION,
            action=CONF_ACTION_ADOPT_EXISTING,
            required=False,
            default_value="",
        )
    )
    return tuple(entries)


def _skill_registered_subblock(
    *,
    artifacts: SkillCreationArtifacts,
    edit_mode: bool,
    skill_name: str,
    activation_phrase_2: str,
    activation_phrase_3: str,
    activation_phrase_4: str,
    voice: str,
    update_message: str | None,
    publication_status: str | None,
) -> tuple[ConfigEntry, ...]:
    """DONE state: identity card + status banner + Edit / Refresh / Delete."""
    name = artifacts.last_known_name or skill_name or "Music Assistant"
    skill_id = artifacts.skill_id or ""
    dev_console_url = f"https://dialogs.yandex.ru/developer/skills/{skill_id}" if skill_id else ""

    status_translation_key, status_entry_type = _publication_status_banner(publication_status)
    entries: list[ConfigEntry] = [
        ConfigEntry(
            key="label_skill_registered",
            type=ConfigEntryType.LABEL,
            translation_params=[name, skill_id],
        ),
        ConfigEntry(
            key="label_skill_publication_status",
            type=status_entry_type,
            translation_key=status_translation_key,
        ),
    ]
    if dev_console_url:
        entries.append(
            ConfigEntry(
                key="label_skill_dev_console_url",
                type=ConfigEntryType.STRING,
                required=False,
                value=dev_console_url,
                default_value="",
            )
        )
    if update_message:
        entries.append(
            ConfigEntry(
                key="label_skill_update_msg",
                type=ConfigEntryType.LABEL,
                label=update_message,
            )
        )
    if not edit_mode:
        entries.append(
            ConfigEntry(
                key=CONF_ACTION_EDIT_SKILL,
                type=ConfigEntryType.ACTION,
                action=CONF_ACTION_EDIT_SKILL,
                required=False,
                default_value="",
            )
        )
        entries.append(
            ConfigEntry(
                key=CONF_ACTION_REFRESH_STATUS,
                type=ConfigEntryType.ACTION,
                action=CONF_ACTION_REFRESH_STATUS,
                required=False,
                default_value="",
            )
        )
        return tuple(entries)

    # Edit mode
    entries.append(
        ConfigEntry(
            key="label_skill_edit_intro",
            type=ConfigEntryType.LABEL,
        )
    )
    entries.append(
        ConfigEntry(
            key=CONF_DIALOG_SKILL_NAME,
            type=ConfigEntryType.STRING,
            translation_key="dialog_skill_name_edit",
            required=False,
            value=skill_name,
            default_value="",
            validate=validate_skill_name,
        )
    )
    for key, phrase in (
        (CONF_DIALOG_ACTIVATION_PHRASE_2, activation_phrase_2),
        (CONF_DIALOG_ACTIVATION_PHRASE_3, activation_phrase_3),
        (CONF_DIALOG_ACTIVATION_PHRASE_4, activation_phrase_4),
    ):
        entries.append(
            ConfigEntry(
                key=key,
                type=ConfigEntryType.STRING,
                required=False,
                value=phrase,
                default_value="",
                validate=validate_activation_phrase,
            )
        )
    voice_options = [
        ConfigValueOption(title=label, value=value) for value, label in DIALOG_VOICE_OPTIONS
    ]
    entries.append(
        ConfigEntry(
            key=CONF_DIALOG_SKILL_VOICE,
            type=ConfigEntryType.STRING,
            required=False,
            value=voice or DIALOG_VOICE_DEFAULT,
            default_value=DIALOG_VOICE_DEFAULT,
            options=voice_options,
        )
    )
    entries.append(
        ConfigEntry(
            key=CONF_ACTION_UPDATE_SKILL,
            type=ConfigEntryType.ACTION,
            action=CONF_ACTION_UPDATE_SKILL,
            required=False,
            default_value="",
        )
    )
    entries.append(
        ConfigEntry(
            key=CONF_ACTION_DELETE_SKILL,
            type=ConfigEntryType.ACTION,
            action=CONF_ACTION_DELETE_SKILL,
            required=False,
            default_value="",
        )
    )
    entries.append(
        ConfigEntry(
            key=CONF_ACTION_CANCEL_EDIT,
            type=ConfigEntryType.ACTION,
            action=CONF_ACTION_CANCEL_EDIT,
            required=False,
            default_value="",
        )
    )
    return tuple(entries)


def _skill_advanced_subblock(  # noqa: PLR0913
    *,
    is_configured: bool,
    skill_id_value: str,
    skill_token_value: str,
    webhook_secret: str,
    activation_phrase_2: str,
    activation_phrase_3: str,
    activation_phrase_4: str,
    voice: str,
    suppress_phrase_voice_mirror: bool,
    suppress_skill_name_mirror: bool,
    suppress_external_base_url_mirror: bool,
    skill_name: str,
    external_base_url: str,
    diagnostics: tuple[ConfigEntry, ...],
) -> tuple[ConfigEntry, ...]:
    """
    Skill-related Advanced fields (visible behind ``Show advanced`` toggle).

    The various ``suppress_*_mirror`` flags avoid duplicate-key
    collisions: when a key is already rendered *visible* by
    ``_skill_create_subblock`` / ``_skill_registered_subblock`` in
    edit mode, it must NOT also appear as a hidden Advanced mirror.
    The dispatcher passes ``True`` for whichever keys are visible in
    the current state and ``False`` for the rest; the latter need a
    mirror so their values still round-trip on form Save (FE only
    persists visible field values).
    """
    entries: list[ConfigEntry] = []
    if not suppress_skill_name_mirror:
        entries.append(
            ConfigEntry(
                key=CONF_DIALOG_SKILL_NAME,
                type=ConfigEntryType.STRING,
                translation_key="dialog_skill_name_advanced",
                required=False,
                value=skill_name,
                default_value="",
                advanced=True,
            )
        )
    if not suppress_external_base_url_mirror:
        # Re-keyed texts: the visible create-block entry pairs this key
        # with a dynamic probe description, the Advanced mirror needs a
        # static one.
        entries.append(
            ConfigEntry(
                key=CONF_EXTERNAL_BASE_URL,
                type=ConfigEntryType.STRING,
                translation_key="external_base_url_advanced",
                required=False,
                value=external_base_url,
                default_value="",
                advanced=True,
                validate=validate_external_base_url,
            )
        )
    entries.append(
        ConfigEntry(
            key=CONF_DIALOG_SKILL_ID,
            type=ConfigEntryType.STRING,
            required=False,
            value=skill_id_value,
            default_value="",
            read_only=is_configured,
            advanced=True,
        )
    )
    entries.append(
        ConfigEntry(
            key=CONF_DIALOG_SKILL_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            help_link=(
                "https://oauth.yandex.ru/authorize?response_type=token"
                "&client_id=c473ca268cd749d3a8371351a8f2bcbd"
            ),
            required=False,
            value=skill_token_value,
            advanced=True,
        )
    )
    entries.append(
        ConfigEntry(
            key=CONF_DIALOG_WEBHOOK_SECRET,
            type=ConfigEntryType.SECURE_STRING,
            translation_params=[DIALOG_WEBHOOK_BASE_PATH],
            required=False,
            value=webhook_secret,
            advanced=True,
        )
    )
    if is_configured:
        entries.append(
            ConfigEntry(
                key=CONF_ACTION_REGENERATE_WEBHOOK_SECRET,
                type=ConfigEntryType.ACTION,
                action=CONF_ACTION_REGENERATE_WEBHOOK_SECRET,
                required=False,
                default_value="",
                advanced=True,
            )
        )
    if not suppress_phrase_voice_mirror:
        for key, value in (
            (CONF_DIALOG_ACTIVATION_PHRASE_2, activation_phrase_2),
            (CONF_DIALOG_ACTIVATION_PHRASE_3, activation_phrase_3),
            (CONF_DIALOG_ACTIVATION_PHRASE_4, activation_phrase_4),
        ):
            entries.append(
                ConfigEntry(
                    key=key,
                    type=ConfigEntryType.STRING,
                    required=False,
                    value=value,
                    default_value="",
                    advanced=True,
                )
            )
        entries.append(
            ConfigEntry(
                key=CONF_DIALOG_SKILL_VOICE,
                type=ConfigEntryType.STRING,
                translation_key="dialog_skill_voice_advanced",
                required=False,
                value=voice or DIALOG_VOICE_DEFAULT,
                default_value=DIALOG_VOICE_DEFAULT,
                advanced=True,
            )
        )
    entries.extend(diagnostics)
    return tuple(entries)


def _skill_block(  # noqa: PLR0913
    *,
    artifacts: SkillCreationArtifacts,
    cached_x_token_present: bool,
    skill_id_value: str,
    skill_token_value: str,
    webhook_secret: str,
    edit_mode: bool,
    skill_name: str,
    activation_phrase_2: str,
    activation_phrase_3: str,
    activation_phrase_4: str,
    voice: str,
    update_message: str | None,
    external_base_url: str,
    base_url_description: str,
    base_url_valid: bool,
    duplicate_skill_id: str | None,
    duplicate_skill_name: str | None,
    action_outcome: AutoCreateOutcome | None,
    publication_status: str | None,
    diagnostics: tuple[ConfigEntry, ...],
) -> tuple[ConfigEntry, ...]:
    """Skill section visible when authed OR a manual skill_id is set."""
    is_done = artifacts.state == SkillCreationState.DONE
    skill_known = is_done or bool(skill_id_value)
    visible = cached_x_token_present or skill_known
    if not visible:
        return ()

    duplicate_pending = bool(duplicate_skill_id)
    if duplicate_pending:
        stage = LocalAutoCreateStage.DUPLICATE_DETECTED
    elif is_done:
        stage = LocalAutoCreateStage.DONE
    elif artifacts.state == SkillCreationState.FAILED:
        stage = LocalAutoCreateStage.FAILED
    elif cached_x_token_present and artifacts.state in (
        SkillCreationState.APP_CREATED,
        SkillCreationState.DRAFT_UPDATED,
        SkillCreationState.OAUTH_CREATED,
        SkillCreationState.OAUTH_ATTACHED,
        SkillCreationState.DEPLOY_REQUESTED,
    ):
        stage = LocalAutoCreateStage.PIPELINE_RUNNING
    else:
        stage = LocalAutoCreateStage.IDLE

    visible_entries: tuple[ConfigEntry, ...]
    suppress_external_base_url_mirror = False
    if duplicate_pending:
        visible_entries = _skill_duplicate_subblock(
            duplicate_skill_name=duplicate_skill_name or "",
            duplicate_skill_id=duplicate_skill_id or "",
            action_outcome=action_outcome,
        )
        suppress_phrase_voice_mirror = False
        suppress_skill_name_mirror = False
    elif is_done:
        visible_entries = _skill_registered_subblock(
            artifacts=artifacts,
            edit_mode=edit_mode,
            skill_name=skill_name,
            activation_phrase_2=activation_phrase_2,
            activation_phrase_3=activation_phrase_3,
            activation_phrase_4=activation_phrase_4,
            voice=voice,
            update_message=update_message,
            publication_status=publication_status,
        )
        suppress_phrase_voice_mirror = edit_mode
        suppress_skill_name_mirror = edit_mode
        # external_base_url is NOT visible in DONE / edit-mode → keep
        # the Advanced mirror so its value still round-trips on Save.
    else:
        visible_entries = _skill_create_subblock(
            artifacts=artifacts,
            skill_name=skill_name,
            external_base_url=external_base_url,
            base_url_description=base_url_description,
            base_url_valid=base_url_valid,
            update_message=update_message,
            stage=stage,
        )
        suppress_phrase_voice_mirror = False
        suppress_skill_name_mirror = True  # rendered visible above
        suppress_external_base_url_mirror = True  # rendered visible above

    advanced_entries = _skill_advanced_subblock(
        is_configured=is_done and bool(artifacts.skill_id),
        skill_id_value=skill_id_value,
        skill_token_value=skill_token_value,
        webhook_secret=webhook_secret,
        activation_phrase_2=activation_phrase_2,
        activation_phrase_3=activation_phrase_3,
        activation_phrase_4=activation_phrase_4,
        voice=voice,
        suppress_phrase_voice_mirror=suppress_phrase_voice_mirror,
        suppress_skill_name_mirror=suppress_skill_name_mirror,
        suppress_external_base_url_mirror=suppress_external_base_url_mirror,
        skill_name=skill_name,
        external_base_url=external_base_url,
        diagnostics=diagnostics,
    )
    return (*visible_entries, *advanced_entries)


# ---------------------------------------------------------------------------
# Settings (provider-wide)
# ---------------------------------------------------------------------------


def _manifest_block(
    *,
    status: ManifestStatus,
    paste_value: str,
    update_message: ManifestActionResult | None,
) -> tuple[ConfigEntry, ...]:
    """
    Skill manifest banner + Export / Import / Reset / Validate actions.

    The manifest controls intents + entities deployed to Yandex (skill
    grammar) and the runtime mapping that turns NLU matches into
    player actions. Bundled by default; users override by writing
    ``<storage>/yandex_alice/skill.toml`` (manually or via Import).
    """
    counts = [str(status.intent_count), str(status.entity_count)]
    if status.source == "bundled":
        banner_key = "manifest_banner_bundled"
        banner_params = [*counts, str(status.override_path)]
    elif status.source == "override_valid":
        banner_key = "manifest_banner_override"
        banner_params = [str(status.override_path), *counts]
    else:  # override_invalid
        banner_key = "manifest_banner_invalid"
        banner_params = [str(status.override_path), *counts, status.error or "unknown parse error"]

    entries: list[ConfigEntry] = [
        ConfigEntry(
            key="label_manifest_banner",
            type=ConfigEntryType.LABEL,
            translation_key=banner_key,
            translation_params=banner_params,
        ),
    ]
    if update_message:
        entries.append(
            ConfigEntry(
                key="label_manifest_message",
                type=ConfigEntryType.LABEL,
                translation_key=update_message.translation_key,
                translation_params=list(update_message.translation_params),
            )
        )
    entries.extend(
        (
            ConfigEntry(
                key=CONF_ACTION_EXPORT_MANIFEST,
                type=ConfigEntryType.ACTION,
                translation_params=[str(status.override_path)],
                action=CONF_ACTION_EXPORT_MANIFEST,
                required=False,
                default_value="",
            ),
            ConfigEntry(
                key=CONF_DIALOG_SKILL_OVERRIDE_PASTE,
                type=ConfigEntryType.STRING,
                required=False,
                value=paste_value,
                default_value="",
            ),
            ConfigEntry(
                key=CONF_ACTION_IMPORT_MANIFEST,
                type=ConfigEntryType.ACTION,
                translation_params=[str(status.override_path)],
                action=CONF_ACTION_IMPORT_MANIFEST,
                required=False,
                default_value="",
            ),
            ConfigEntry(
                key=CONF_ACTION_VALIDATE_MANIFEST,
                type=ConfigEntryType.ACTION,
                action=CONF_ACTION_VALIDATE_MANIFEST,
                required=False,
                default_value="",
            ),
            ConfigEntry(
                key=CONF_ACTION_RESET_MANIFEST,
                type=ConfigEntryType.ACTION,
                action=CONF_ACTION_RESET_MANIFEST,
                required=False,
                default_value="",
            ),
        )
    )
    return tuple(entries)


def _settings_block(
    *,
    player_options: list[ConfigValueOption],
    instance_name: str,
    skill_name: str,
    use_different_instance_name: bool,
) -> tuple[ConfigEntry, ...]:
    """Voice-controllable players + (advanced) MA-side instance name override."""
    entries: list[ConfigEntry] = [
        ConfigEntry(
            key=CONF_EXPOSED_PLAYERS,
            type=ConfigEntryType.STRING,
            multi_value=True,
            options=player_options,
            required=False,
            default_value=[],
        ),
        ConfigEntry(
            key=CONF_USE_DIFFERENT_INSTANCE_NAME,
            type=ConfigEntryType.BOOLEAN,
            required=False,
            default_value=False,
            advanced=True,
        ),
    ]
    if use_different_instance_name:
        entries.append(
            ConfigEntry(
                key=CONF_INSTANCE_NAME,
                type=ConfigEntryType.STRING,
                required=False,
                default_value=instance_name or DIALOG_DEFAULT_NAME,
                advanced=True,
                depends_on=CONF_USE_DIFFERENT_INSTANCE_NAME,
                depends_on_value=True,
            )
        )
    else:
        # Hidden tracker: keep instance_name in sync with skill_name
        # while the toggle is off, so MA's display label looks right.
        entries.append(
            ConfigEntry(
                key=CONF_INSTANCE_NAME,
                type=ConfigEntryType.STRING,
                required=False,
                default_value=skill_name or DIALOG_DEFAULT_NAME,
                hidden=True,
            )
        )
    return tuple(entries)


# ---------------------------------------------------------------------------
# Top-level composer
# ---------------------------------------------------------------------------


def build_form_entries(  # noqa: PLR0913
    *,
    artifacts: SkillCreationArtifacts,
    cached_x_token_present: bool,
    user_name: str,
    skill_id_value: str,
    skill_token_value: str,
    webhook_secret: str,
    last_error: str | None,
    action_outcome: AutoCreateOutcome | None,
    duplicate_skill_id: str | None,
    duplicate_skill_name: str | None,
    edit_mode: bool,
    skill_name: str,
    activation_phrase_2: str,
    activation_phrase_3: str,
    activation_phrase_4: str,
    voice: str,
    update_message: str | None,
    external_base_url: str,
    base_url_description: str,
    base_url_valid: bool,
    player_options: list[ConfigValueOption],
    instance_name: str,
    use_different_instance_name: bool,
    publication_status: str | None,
    diagnostics: tuple[ConfigEntry, ...],
    manifest_status: ManifestStatus,
    manifest_paste: str,
    manifest_message: ManifestActionResult | None,
    hidden_state: tuple[ConfigEntry, ...],
    borrow_options: list[ConfigValueOption],
    borrow_selected: str,
    borrow_error: str | None,
) -> tuple[ConfigEntry, ...]:
    """Compose Authorization + Skill + Settings + hidden state."""
    auth = _stamp(
        _authorization_block(
            signed_in=cached_x_token_present,
            user_name=user_name,
            last_error=last_error,
            borrow_options=borrow_options,
            borrow_selected=borrow_selected,
            borrow_error=borrow_error,
        ),
        CATEGORY_AUTHORIZATION,
    )
    skill = _stamp(
        _skill_block(
            artifacts=artifacts,
            cached_x_token_present=cached_x_token_present,
            skill_id_value=skill_id_value,
            skill_token_value=skill_token_value,
            webhook_secret=webhook_secret,
            edit_mode=edit_mode,
            skill_name=skill_name,
            activation_phrase_2=activation_phrase_2,
            activation_phrase_3=activation_phrase_3,
            activation_phrase_4=activation_phrase_4,
            voice=voice,
            update_message=update_message,
            external_base_url=external_base_url,
            base_url_description=base_url_description,
            base_url_valid=base_url_valid,
            duplicate_skill_id=duplicate_skill_id,
            duplicate_skill_name=duplicate_skill_name,
            action_outcome=action_outcome,
            publication_status=publication_status,
            diagnostics=diagnostics,
        ),
        CATEGORY_SKILL,
    )
    settings = _stamp(
        (
            *_settings_block(
                player_options=player_options,
                instance_name=instance_name,
                skill_name=skill_name,
                use_different_instance_name=use_different_instance_name,
            ),
            *_manifest_block(
                status=manifest_status,
                paste_value=manifest_paste,
                update_message=manifest_message,
            ),
        ),
        CATEGORY_SETTINGS,
    )
    # Hidden state-carriers stay in the default "generic" category —
    # they're hidden=True so they never render as a section anyway.
    return (*auth, *skill, *settings, *hidden_state)
