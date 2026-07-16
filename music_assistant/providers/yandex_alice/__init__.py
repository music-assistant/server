"""
Yandex Alice (Dialogs custom skill) plugin provider for Music Assistant.

Exposes Music Assistant playback to a Yandex Dialogs custom skill — a Russian
NLU voice control surface invoked via *«Алиса, попроси Music Assistant …»*.

Setup paths:

1. **Auto** (since v1.1.0): the *Create skill* button kicks off a Yandex
   Passport Device Flow login and registers the skill in
   ``https://dialogs.yandex.ru/developer`` programmatically via
   ``ya-dialogs-api``. The skill ID is auto-populated on success.
2. **Manual** (still supported): create the skill yourself in the dev console,
   point its webhook URL at ``/api/yandex_dialogs/webhook/<your-secret>``,
   and paste the skill ID + token into the form.
"""

from __future__ import annotations

import dataclasses
import logging
import secrets
import time
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.constants import SECURE_STRING_SUBSTITUTE
from music_assistant_models.enums import ConfigEntryType, ProviderFeature
from music_assistant_models.errors import InvalidDataError, LoginFailed
from ya_dialogs_api import (
    SkillCreationArtifacts,
    SkillCreationState,
    dump_artifacts,
    load_artifacts,
)
from ya_passport_auth.ma import (
    BORROW_SOURCE_OWN,
    BorrowedCredentialSource,
    list_yandex_music_instances,
)

from .auth_page import perform_device_auth
from .auto_create import (
    AutoCreateOutcome,
    LocalAutoCreateStage,
    adopt_existing_skill,
    delete_existing_skill_then_recreate,
    run_create_skill,
)
from .auto_update import run_auto_update
from .constants import (
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
    CONF_ACTION_RENAME_DIALOG_SKILL,
    CONF_ACTION_RESET_MANIFEST,
    CONF_ACTION_REVERT_SKILL_NAME,
    CONF_ACTION_SIGN_IN,
    CONF_ACTION_TEST_WEBHOOK,
    CONF_ACTION_UPDATE_SKILL,
    CONF_ACTION_VALIDATE_MANIFEST,
    CONF_AUTH_USER_NAME,
    CONF_AUTH_X_TOKEN,
    CONF_DIALOG_ACTIVATION_PHRASE_2,
    CONF_DIALOG_ACTIVATION_PHRASE_3,
    CONF_DIALOG_ACTIVATION_PHRASE_4,
    CONF_DIALOG_AUTO_CREATE_ARTIFACTS,
    CONF_DIALOG_PUBLICATION_STATUS,
    CONF_DIALOG_SKILL_ID,
    CONF_DIALOG_SKILL_NAME,
    CONF_DIALOG_SKILL_OVERRIDE_PASTE,
    CONF_DIALOG_SKILL_TOKEN,
    CONF_DIALOG_SKILL_VOICE,
    CONF_DIALOG_WEBHOOK_SECRET,
    CONF_EDIT_MODE,
    CONF_EXTERNAL_BASE_URL,
    CONF_INSTANCE_NAME,
    CONF_PENDING_DUPLICATE_SKILL_ID,
    CONF_PENDING_DUPLICATE_SKILL_NAME,
    CONF_USE_DIFFERENT_INSTANCE_NAME,
    CONF_YM_INSTANCE,
    DIALOG_DEFAULT_NAME,
    DIALOG_VOICE_DEFAULT,
)
from .dialog_skill_meta import (
    build_activation_phrases,
    build_backend_uri,
    build_skill_description,
    build_structured_examples,
)
from .plugin import YandexAlicePlugin
from .publication_status import fetch_skill_publication_status
from .setup_view import build_form_entries
from .skill_manifest_provider import SkillManifestProvider
from .url_helpers import (
    is_public_https_url,
    try_detect_any_base_url,
    try_detect_public_https_url,
)
from .webhook_probe import probe_webhook_reachability

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


_LOGGER = logging.getLogger(__name__)

SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> ProviderInstanceType:
    """Initialise the provider instance with the given configuration."""
    return YandexAlicePlugin(mass, manifest, config, SUPPORTED_FEATURES)


def _generate_webhook_secret() -> str:
    """Return a fresh URL-safe random secret for the webhook path."""
    return secrets.token_urlsafe(24)


async def _delete_skill_in_yandex(x_token: str, skill_id: str) -> None:
    """
    Hard-delete a skill from the user's Yandex Dialogs account.

    Used by the *Delete skill* action button in Step 3 edit mode.
    Errors propagate to the caller (the dispatcher) which wraps them
    in a user-visible LABEL.
    """
    from ya_dialogs_api import DialogsSkillCreator  # noqa: PLC0415

    from .auth_session import cached_authenticated_session  # noqa: PLC0415
    from .constants import DIALOG_CHANNEL  # noqa: PLC0415

    async with cached_authenticated_session(x_token) as session:
        creator = DialogsSkillCreator(session, channel=DIALOG_CHANNEL)
        csrf = await creator.fetch_csrf()
        await creator.delete_skill(csrf, skill_id)


async def _list_player_options(mass: MusicAssistant) -> list[ConfigValueOption]:
    """List MA players the user can expose to voice control."""
    options: list[ConfigValueOption] = []
    try:
        for player in mass.players.all_players():
            options.append(
                ConfigValueOption(
                    title=player.display_name or player.name or player.player_id,
                    value=player.player_id,
                )
            )
    except Exception as exc:
        _LOGGER.debug("could not enumerate players: %s", exc)
    return options


def _build_diagnostics_entries(
    mass: MusicAssistant, instance_id: str | None
) -> tuple[ConfigEntry, ...]:
    """
    Render runtime stats from the loaded plugin instance (#17).

    Reads counters off the running ``YandexAlicePlugin`` (set in
    ``handle_async_init`` / updated in the webhook handler). When the
    provider is not loaded yet (config-edit before first save) we render
    a single placeholder LABEL so users know diagnostics is available.
    """
    if not instance_id:
        return ()
    try:
        plugin = mass.get_provider(instance_id)
    except Exception:
        return ()
    if plugin is None or not isinstance(plugin, YandexAlicePlugin):
        return ()

    stats = plugin.diagnostics_snapshot()
    handler_active = bool(stats.get("handler_active"))
    if not handler_active:
        return (
            ConfigEntry(
                key="label_diagnostics_inactive",
                type=ConfigEntryType.LABEL,
                advanced=True,
            ),
        )

    webhook_calls = int(stats.get("webhook_calls_total") or 0)
    authenticated_calls = int(stats.get("authenticated_calls_total") or 0)
    last_ts_raw = stats.get("last_webhook_ts")
    if isinstance(last_ts_raw, (int, float)) and last_ts_raw > 0:
        delta = max(0, int(time.time() - last_ts_raw))
        if delta < 60:
            last_ago = f"{delta} sec ago"
        elif delta < 3600:
            last_ago = f"{delta // 60} min ago"
        else:
            last_ago = f"{delta // 3600} h ago"
    else:
        last_ago = "never"

    summary = (
        f"Diagnostics: {webhook_calls} webhook hits "
        f"({authenticated_calls} past auth) · last webhook {last_ago}."
    )
    return (
        ConfigEntry(
            key="label_diagnostics_summary",
            type=ConfigEntryType.LABEL,
            label=summary,
            advanced=True,
        ),
    )


def _resolve_saved_value(
    values: dict[str, ConfigValueType],
    key: str,
) -> str:
    """Read a plain config value from form ``values`` (string-coerced)."""
    return str(values.get(key) or "")


def _saved_provider_config(mass: MusicAssistant, instance_id: str | None) -> object | None:
    """
    Cache helper: return the running provider's ``.config`` once per render.

    SECURE_STRING fallback (see :func:`_resolve_secure_string_from`)
    has to look up the persisted value for *every* token field on
    every dispatcher invocation. Calling ``mass.get_provider`` 3-4
    times per render is harmless but redundant; this helper resolves
    it once and reuses the same object for the lifetime of the call.
    """
    if not instance_id:
        return None
    try:
        prov = mass.get_provider(instance_id)
    except Exception as exc:
        _LOGGER.debug("saved_provider_config lookup failed: %r", exc)
        return None
    return getattr(prov, "config", None) if prov is not None else None


def _resolve_secure_string_from(
    saved_config: object | None,
    values: dict[str, ConfigValueType],
    key: str,
) -> str:
    """
    Read a SECURE_STRING value, resolving the FE substitute.

    MA's frontend never echoes the actual SECURE_STRING value back to
    the backend — instead it sends ``SECURE_STRING_SUBSTITUTE``
    ("this_value_is_encrypted") whenever the user hasn't edited the
    field. Reading ``values[key]`` raw would therefore hand us the
    substitute marker, not the real token, and any downstream call
    would fail with an opaque auth error.

    Behaviour, in order:

    1. Use the user-supplied value from ``values`` only when it's
       non-empty AND not the substitute marker (user just typed in a
       fresh secret).
    2. Otherwise fall back to the persisted value via
       ``saved_config.get_value(key)`` — same pattern as
       ``yandex_smarthome._resolve_direct_client_secret``.
    3. Empty string if neither path yields a value.

    The ``saved_config`` argument is the running provider's
    ``ProviderConfig`` (resolved once per render via
    :func:`_saved_provider_config`) — this avoids repeatedly calling
    ``mass.get_provider`` for every secure field on every dispatch.
    """
    raw = str(values.get(key) or "")
    if raw and raw != SECURE_STRING_SUBSTITUTE:
        return raw
    if saved_config is None:
        return ""
    try:
        saved = saved_config.get_value(key)  # type: ignore[attr-defined]
    except Exception as exc:
        _LOGGER.debug("secure-string fallback failed for %s: %r", key, exc)
        return ""
    return str(saved or "")


async def get_config_entries(  # noqa: PLR0915
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Build the provider config-form entries with auto-create / rename actions.

    Action handling:

    - ``CONF_ACTION_AUTO_CREATE_DIALOG`` — advance the Device Flow + skill
      creation state machine by one external-IO step. Re-click drives further
      stages (see :mod:`provider.auto_create`).
    - ``CONF_ACTION_RENAME_DIALOG_SKILL`` — patch the existing skill draft
      via cached x_token; no Device Flow.
    - ``CONF_ACTION_CANCEL_DIALOG_SKILL_FLOW`` — drop pending session +
      reset artifacts; preserve cached x_token.

    Auto-create / rename state lives in two hidden config entries
    (``CONF_AUTH_X_TOKEN``, ``CONF_DIALOG_AUTO_CREATE_ARTIFACTS``)
    that round-trip through the form on every save.
    """
    values = values or {}

    # Generate a webhook secret on first open if the user hasn't set one yet.
    # Read through saved provider config too: the frontend may not echo
    # SECURE_STRING fields between action clicks, and regenerating the
    # secret per call would orphan webhooks already registered with Yandex
    # against an earlier (now-discarded) secret.
    # Resolve the running provider config once (#9) — sibling lookups
    # for SECURE_STRING substitute fallback all share this handle, so
    # we don't call ``mass.get_provider`` redundantly per render.
    saved_provider = _saved_provider_config(mass, instance_id)

    existing_secret = _resolve_secure_string_from(
        saved_provider, values, CONF_DIALOG_WEBHOOK_SECRET
    ).strip()
    default_secret = existing_secret or _generate_webhook_secret()
    # Stabilise inside this dispatch: any backend_uri assembled below uses
    # the same secret as the form will save on user click.
    values[CONF_DIALOG_WEBHOOK_SECRET] = default_secret

    instance_name = str(values.get(CONF_INSTANCE_NAME) or DIALOG_DEFAULT_NAME)

    # ---- Pull persistent auto-create / auth state ----
    artifacts = load_artifacts(
        _resolve_saved_value(values, CONF_DIALOG_AUTO_CREATE_ARTIFACTS) or None
    )
    cached_x_token = _resolve_secure_string_from(saved_provider, values, CONF_AUTH_X_TOKEN)

    # ---- Yandex account source (borrow from a linked yandex_music) ----
    # A stale selection (instance removed) normalizes back to own so the
    # sign-in block reappears. While borrowing, the linked instance's
    # x_token feeds the pipeline read-only: it is never written into this
    # plugin's own auth_x_token storage and never rotated here.
    ym_instances = list_yandex_music_instances(mass)
    borrow_selected = str(values.get(CONF_YM_INSTANCE) or BORROW_SOURCE_OWN)
    if borrow_selected != BORROW_SOURCE_OWN and borrow_selected not in {
        inst_id for inst_id, _ in ym_instances
    }:
        borrow_selected = BORROW_SOURCE_OWN
        values[CONF_YM_INSTANCE] = BORROW_SOURCE_OWN
    borrowing = borrow_selected != BORROW_SOURCE_OWN
    borrow_error: str | None = None
    if borrowing:
        cached_x_token = ""
        try:
            _, borrowed_x = BorrowedCredentialSource(mass, borrow_selected).read_tokens()
        except Exception as exc:
            borrow_error = str(exc)
        else:
            if borrowed_x is None:
                borrow_error = (
                    "The linked Yandex Music instance has no session token. "
                    "Authenticate it (with Remember session enabled) first."
                )
            else:
                cached_x_token = borrowed_x.get_secret()
    # The static "own credentials" option title is authored in strings.json
    # (config_entries.ym_instance.options.__own__); instance titles are
    # data-driven and stay code-composed.
    borrow_options = [
        *(ConfigValueOption(inst_id, f"Yandex Music: {name}") for inst_id, name in ym_instances),
        ConfigValueOption(BORROW_SOURCE_OWN),
    ]
    skill_token_value = _resolve_secure_string_from(saved_provider, values, CONF_DIALOG_SKILL_TOKEN)
    # Carried across renders unless a deploy-related action below
    # overrides it via a snapshot fetch (or DELETE_SKILL clears it).
    publication_status = _resolve_saved_value(values, CONF_DIALOG_PUBLICATION_STATUS)

    # Skill name priority: explicit dialog skill name → instance name → default.
    skill_name = (
        str(values.get(CONF_DIALOG_SKILL_NAME) or "").strip()
        or str(values.get(CONF_INSTANCE_NAME) or "").strip()
        or DIALOG_DEFAULT_NAME
    )

    external_base_url = str(values.get(CONF_EXTERNAL_BASE_URL) or "").strip().rstrip("/")
    webhook_secret = default_secret

    action_outcome: AutoCreateOutcome | None = None
    update_message: str | None = None
    # Surfaced in the manifest banner block, separate from update_message
    # so manifest actions don't bleed into the skill-block status line.
    manifest_message: str | None = None

    # Effective skill manifest — bundled default unless user wrote an
    # override file. Cheap to construct (no I/O until .grammar() / .entities()
    # are called). Reused across action branches that ship intents to Yandex.
    manifest_provider = SkillManifestProvider(mass)

    # ---- Action dispatcher ----
    if action == CONF_ACTION_SIGN_IN and borrowing:
        action = None  # sign-in is managed by the linked Yandex Music instance
    if action == CONF_ACTION_SIGN_IN:
        # Authorization block: blocking Device Flow with popup.
        # session_id MUST come from values["session_id"] — that's the
        # channel the MA frontend listens on for the AUTH_SESSION
        # popup signal.
        session_id_raw = values.get("session_id")
        session_id = str(session_id_raw or "").strip()
        if not session_id:
            msg = "Missing session_id for device authentication"
            raise InvalidDataError(msg)
        try:
            cached_x_token, display_login = await perform_device_auth(
                mass, session_id, skill_name=skill_name
            )
            values[CONF_AUTH_USER_NAME] = display_login
        except LoginFailed as exc:
            update_message = str(exc)
        except Exception as exc:
            _LOGGER.exception("yandex-alice: sign-in raised unexpectedly")
            update_message = f"Sign-in error: {exc!r}"

    elif action == CONF_ACTION_CLEAR_AUTH:
        # Sign out — drop the cached x_token + cached display name.
        # Skill artifacts are reset too so the form snaps back to a
        # clean "needs sign-in" state. The skill itself stays in
        # Yandex; user can re-sign-in to resume managing it.
        cached_x_token = ""
        values[CONF_AUTH_USER_NAME] = ""
        artifacts = SkillCreationArtifacts()
        values[CONF_PENDING_DUPLICATE_SKILL_ID] = ""
        values[CONF_PENDING_DUPLICATE_SKILL_NAME] = ""

    elif action == CONF_ACTION_AUTO_CREATE_DIALOG:
        # Skill block: Create skill (blocking pipeline).
        # Re-click on DONE → reset artifacts so we run a fresh
        # create_app (after Delete skill). Backup-restore safety —
        # if a skill_id is in config but artifacts are NONE, pre-set
        # APP_CREATED so the library skips create_app.
        if artifacts.state == SkillCreationState.DONE:
            artifacts = SkillCreationArtifacts()
        saved_skill_id = str(values.get(CONF_DIALOG_SKILL_ID) or "").strip()
        if saved_skill_id and artifacts.state == SkillCreationState.NONE and not artifacts.skill_id:
            artifacts = dataclasses.replace(
                artifacts,
                state=SkillCreationState.APP_CREATED,
                skill_id=saved_skill_id,
            )

        try:
            backend_uri = build_backend_uri(external_base_url, webhook_secret)
        except ValueError as exc:
            action_outcome = AutoCreateOutcome(
                artifacts=dataclasses.replace(
                    artifacts,
                    state=SkillCreationState.FAILED,
                    last_error=str(exc),
                ),
                x_token=None,
                user_message=str(exc),
                stage=LocalAutoCreateStage.FAILED,
            )
        else:
            action_outcome = await run_create_skill(
                cached_x_token=cached_x_token,
                skill_name=skill_name,
                backend_uri=backend_uri,
                description=build_skill_description(skill_name),
                structured_examples=build_structured_examples(skill_name),
                activation_phrases=build_activation_phrases(skill_name),
                intents=manifest_provider.grammar(),
                entities=manifest_provider.entities(),
                artifacts=artifacts,
            )

    elif action == CONF_ACTION_DELETE_SKILL:
        # Hard-delete the registered skill from Yandex and reset
        # artifacts so the Skill block flips back to its "create"
        # variant. Cached Passport sign-in is kept.
        target_skill_id = artifacts.skill_id or str(values.get(CONF_DIALOG_SKILL_ID) or "").strip()
        if not target_skill_id or not cached_x_token:
            update_message = "Nothing to delete — no skill_id on record."
        else:
            try:
                await _delete_skill_in_yandex(cached_x_token, target_skill_id)
                update_message = "Skill deleted from Yandex Dialogs."
                artifacts = SkillCreationArtifacts()
                values[CONF_DIALOG_SKILL_ID] = ""
                publication_status = ""
            except Exception as exc:
                _LOGGER.exception("yandex-alice: delete_skill failed")
                update_message = f"Failed to delete skill: {exc!r}"

    elif action == CONF_ACTION_RECREATE_DUPLICATE:
        existing_id = _resolve_saved_value(values, CONF_PENDING_DUPLICATE_SKILL_ID).strip()
        try:
            backend_uri = build_backend_uri(external_base_url, webhook_secret)
        except ValueError as exc:
            update_message = str(exc)
        else:
            if not existing_id or not cached_x_token:
                update_message = (
                    "Recreate is only available when an existing skill has "
                    "been detected. Click 'Create skill' first."
                )
            else:
                action_outcome = await delete_existing_skill_then_recreate(
                    cached_x_token=cached_x_token,
                    skill_name=skill_name,
                    backend_uri=backend_uri,
                    description=build_skill_description(skill_name),
                    structured_examples=build_structured_examples(skill_name),
                    activation_phrases=build_activation_phrases(skill_name),
                    intents=manifest_provider.grammar(),
                    entities=manifest_provider.entities(),
                    existing_skill_id=existing_id,
                )
                values[CONF_PENDING_DUPLICATE_SKILL_ID] = ""
                values[CONF_PENDING_DUPLICATE_SKILL_NAME] = ""

    elif action == CONF_ACTION_ADOPT_EXISTING:
        existing_id = _resolve_saved_value(values, CONF_PENDING_DUPLICATE_SKILL_ID).strip()
        try:
            backend_uri = build_backend_uri(external_base_url, webhook_secret)
        except ValueError as exc:
            update_message = str(exc)
        else:
            if not existing_id or not cached_x_token:
                update_message = (
                    "Adopt is only available when an existing skill has been "
                    "detected. Click 'Create skill' first."
                )
            else:
                action_outcome = await adopt_existing_skill(
                    cached_x_token=cached_x_token,
                    skill_name=skill_name,
                    backend_uri=backend_uri,
                    description=build_skill_description(skill_name),
                    structured_examples=build_structured_examples(skill_name),
                    activation_phrases=build_activation_phrases(skill_name),
                    intents=manifest_provider.grammar(),
                    entities=manifest_provider.entities(),
                    existing_skill_id=existing_id,
                )
                values[CONF_PENDING_DUPLICATE_SKILL_ID] = ""
                values[CONF_PENDING_DUPLICATE_SKILL_NAME] = ""

    elif action == CONF_ACTION_EDIT_SKILL:
        # Toggle edit mode on; render path picks it up via CONF_EDIT_MODE.
        values[CONF_EDIT_MODE] = True

    elif action == CONF_ACTION_CANCEL_EDIT:
        # Drop edit mode; user-edited values for activation_phrases/voice
        # are kept in the form but not pushed to Yandex until Update.
        values[CONF_EDIT_MODE] = False

    elif action == CONF_ACTION_UPDATE_SKILL:
        # Edit-mode commit — pushes the edited skill_name + up to 3
        # alternative activation phrases + voice to Yandex via
        # auto_update_skill. The skill_name itself is the first
        # activation phrase; empty alt slots are skipped.
        edited_phrases: list[str] = [skill_name.strip()] if skill_name.strip() else []
        for key in (
            CONF_DIALOG_ACTIVATION_PHRASE_2,
            CONF_DIALOG_ACTIVATION_PHRASE_3,
            CONF_DIALOG_ACTIVATION_PHRASE_4,
        ):
            extra = str(values.get(key) or "").strip()
            if extra:
                edited_phrases.append(extra)
        if not edited_phrases:
            edited_phrases = build_activation_phrases(skill_name)
        edited_voice = (
            str(values.get(CONF_DIALOG_SKILL_VOICE) or "").strip() or DIALOG_VOICE_DEFAULT
        )

        try:
            backend_uri = build_backend_uri(external_base_url, webhook_secret)
        except ValueError as exc:
            update_message = str(exc)
        else:
            update_outcome = await run_auto_update(
                cached_x_token=cached_x_token or None,
                skill_name=skill_name,
                backend_uri=backend_uri,
                description=build_skill_description(skill_name),
                structured_examples=build_structured_examples(skill_name),
                activation_phrases=edited_phrases,
                voice=edited_voice,
                intents=manifest_provider.grammar(),
                entities=manifest_provider.entities(),
                artifacts=artifacts,
            )
            update_message = update_outcome.user_message
            if update_outcome.x_token == "":
                cached_x_token = ""
            if update_outcome.artifacts.state == SkillCreationState.DONE:
                # Successful update — pick up the refreshed snapshot
                # (e.g. last_known_name advanced) and exit edit mode.
                artifacts = update_outcome.artifacts
                values[CONF_EDIT_MODE] = False
            # Else: keep the existing DONE artifacts so the form stays
            # in Step 3 edit mode with the error LABEL on top — flipping
            # to artifacts.state=FAILED would route us back to Step 2.

    elif action == CONF_ACTION_RENAME_DIALOG_SKILL:
        try:
            backend_uri = build_backend_uri(external_base_url, webhook_secret)
        except ValueError as exc:
            update_message = str(exc)
            artifacts = dataclasses.replace(
                artifacts,
                state=SkillCreationState.FAILED,
                last_error=str(exc),
            )
        else:
            update_outcome = await run_auto_update(
                cached_x_token=cached_x_token or None,
                skill_name=skill_name,
                backend_uri=backend_uri,
                description=build_skill_description(skill_name),
                structured_examples=build_structured_examples(skill_name),
                activation_phrases=build_activation_phrases(skill_name),
                intents=manifest_provider.grammar(),
                entities=manifest_provider.entities(),
                artifacts=artifacts,
            )
            artifacts = update_outcome.artifacts
            update_message = update_outcome.user_message
            if update_outcome.x_token == "":
                cached_x_token = ""

    elif action == CONF_ACTION_CANCEL_DIALOG_SKILL_FLOW:
        # Reset artifacts; keep cached x_token (sign-in stays valid).
        artifacts = SkillCreationArtifacts()
        values[CONF_PENDING_DUPLICATE_SKILL_ID] = ""
        values[CONF_PENDING_DUPLICATE_SKILL_NAME] = ""

    elif action == CONF_ACTION_REGENERATE_WEBHOOK_SECRET:
        # Webhook secret rotation: invalidates the URL Yandex was registered
        # against, so the existing skill's webhook would 404. Reset everything
        # and force the user back through auto-create with a fresh secret —
        # cached x_token is preserved so the second pass skips Passport login.
        default_secret = _generate_webhook_secret()
        values[CONF_DIALOG_WEBHOOK_SECRET] = default_secret
        webhook_secret = default_secret
        artifacts = SkillCreationArtifacts()
        values[CONF_DIALOG_SKILL_ID] = ""
        publication_status = ""
        if cached_x_token:
            update_message = (
                "Webhook secret regenerated. Click 'Create skill' to register "
                "a fresh skill against the new URL."
            )
        else:
            update_message = (
                "Webhook secret regenerated. Click 'Sign in to Yandex Passport' "
                "to register a fresh skill against the new URL."
            )

    elif action == CONF_ACTION_TEST_WEBHOOK:
        # Reachability probe — does Yandex's traffic actually land in our
        # handler? Returns ``(ok, message)`` ready for an inline LABEL.
        reachable, msg = await probe_webhook_reachability(external_base_url, webhook_secret)
        update_message = ("✅ " if reachable else "❌ ") + msg

    elif action == CONF_ACTION_REVERT_SKILL_NAME:
        # Drift undo (#13) — copy artifacts.last_known_name back into the
        # form field so the user can abandon a half-typed rename and go
        # back to whatever Yandex currently has.
        if artifacts.last_known_name:
            values[CONF_DIALOG_SKILL_NAME] = artifacts.last_known_name
            update_message = (
                f"Skill name reverted to «{artifacts.last_known_name}» "
                "(matches the value currently registered with Yandex)."
            )
        else:
            update_message = "Nothing to revert — no last-known name on record yet."

    elif action == CONF_ACTION_REFRESH_STATUS:
        # Manual snapshot fetch — single HTTP call, updates the cached
        # publication_status field. Used to track Yandex moderation
        # transitions (in_moderation → on_air) without re-deploying.
        target_skill_id = artifacts.skill_id or str(values.get(CONF_DIALOG_SKILL_ID) or "").strip()
        if not target_skill_id or not cached_x_token:
            update_message = "Refresh status is only available after a skill has been registered."
        else:
            fetched = await fetch_skill_publication_status(cached_x_token, target_skill_id)
            if fetched is None:
                update_message = (
                    "Could not fetch publication status — Yandex Dialogs is "
                    "not reachable, or the skill no longer exists in your account."
                )
            else:
                publication_status = fetched
                update_message = "Publication status refreshed."

    elif action == CONF_ACTION_EXPORT_MANIFEST:
        manifest_message = manifest_provider.export_to_override()

    elif action == CONF_ACTION_IMPORT_MANIFEST:
        paste = str(values.get(CONF_DIALOG_SKILL_OVERRIDE_PASTE) or "")
        manifest_message = manifest_provider.import_from_paste(paste)
        if manifest_provider.last_import_success:
            # Successful import — clear the paste field so the form
            # doesn't redisplay the (now stale) raw TOML.
            values[CONF_DIALOG_SKILL_OVERRIDE_PASTE] = ""

    elif action == CONF_ACTION_RESET_MANIFEST:
        manifest_message = manifest_provider.reset_override()

    elif action == CONF_ACTION_VALIDATE_MANIFEST:
        manifest_message = manifest_provider.validate_override_message()

    # ---- Reflect outcome into values so the next form save persists state ----
    if action_outcome is not None:
        artifacts = action_outcome.artifacts
        if action_outcome.x_token is not None:
            cached_x_token = action_outcome.x_token
        # Surface duplicate-name pre-check result into hidden form values
        # so the next render shows the Recreate / Adopt resolution UI.
        if action_outcome.stage == LocalAutoCreateStage.DUPLICATE_DETECTED:
            values[CONF_PENDING_DUPLICATE_SKILL_ID] = (
                action_outcome.pending_duplicate_skill_id or ""
            )
            values[CONF_PENDING_DUPLICATE_SKILL_NAME] = (
                action_outcome.pending_duplicate_skill_name or ""
            )
        elif action_outcome.stage in (
            LocalAutoCreateStage.DONE,
            LocalAutoCreateStage.FAILED,
        ):
            values[CONF_PENDING_DUPLICATE_SKILL_ID] = ""
            values[CONF_PENDING_DUPLICATE_SKILL_NAME] = ""

    values[CONF_DIALOG_AUTO_CREATE_ARTIFACTS] = dump_artifacts(artifacts)
    # While borrowing, this plugin's own token storage stays empty — the
    # borrowed x_token must never round-trip into config on Save.
    own_x_token_value = "" if borrowing else cached_x_token
    values[CONF_AUTH_X_TOKEN] = own_x_token_value
    if artifacts.state == SkillCreationState.DONE and artifacts.skill_id:
        values[CONF_DIALOG_SKILL_ID] = artifacts.skill_id

    # ---- Post-deploy publication-status snapshot ----
    # One HTTP call right after a deploy-related action so the Step 3
    # banner reflects Yandex's view as of the moment the user clicked.
    # CONF_ACTION_REFRESH_STATUS already fetched inside its handler;
    # other actions either don't change publication state (sign-in,
    # cancel, edit-mode toggles) or run their own snapshot inline.
    deploy_actions_for_status_fetch = (
        CONF_ACTION_AUTO_CREATE_DIALOG,
        CONF_ACTION_RECREATE_DUPLICATE,
        CONF_ACTION_ADOPT_EXISTING,
        CONF_ACTION_UPDATE_SKILL,
        CONF_ACTION_RENAME_DIALOG_SKILL,
    )
    if (
        action in deploy_actions_for_status_fetch
        and artifacts.state == SkillCreationState.DONE
        and artifacts.skill_id
        and cached_x_token
    ):
        fetched_status = await fetch_skill_publication_status(cached_x_token, artifacts.skill_id)
        if fetched_status is not None:
            publication_status = fetched_status

    values[CONF_DIALOG_PUBLICATION_STATUS] = publication_status

    # ---- Player options for voice exposure ----
    player_options = await _list_player_options(mass)

    # ---- External base URL: autodetect (#8) + inline HTTPS warning ----
    user_supplied_base_url = str(values.get(CONF_EXTERNAL_BASE_URL) or "").strip()
    if not user_supplied_base_url:
        # First preference: a public HTTPS URL (ready to use). Second:
        # any base URL MA knows about — typically the internal docker /
        # LAN URL. We pre-fill it so the user has a starting point to
        # edit (e.g. swap the host for their reverse-proxy domain).
        detected_public = try_detect_public_https_url(mass)
        if detected_public:
            values[CONF_EXTERNAL_BASE_URL] = detected_public
            external_base_url = detected_public
            base_url_description = (
                f"Auto-detected public HTTPS URL: {detected_public}. "
                "Edit if you use a different reverse-proxy URL."
            )
        else:
            detected_any = try_detect_any_base_url(mass)
            if detected_any:
                values[CONF_EXTERNAL_BASE_URL] = detected_any
                external_base_url = detected_any
                base_url_description = (
                    f"Pre-filled from MA's webserver settings: {detected_any}. "
                    "Yandex needs a *public HTTPS* URL — replace this with "
                    "your reverse-proxy / DDNS hostname (e.g. "
                    "https://ma.example.com) before creating the skill."
                )
            else:
                base_url_description = (
                    "Public HTTPS URL of this Music Assistant instance — the "
                    "address Yandex will use to reach the webhook. "
                    "Examples: https://ma.example.com, https://ha.example.com. "
                    "Required for auto-create."
                )
    elif not is_public_https_url(user_supplied_base_url):
        base_url_description = (
            "❌ This URL is not a public HTTPS endpoint — Yandex requires "
            "https:// and a non-private host. Auto-create will refuse this. "
            f"Got: {user_supplied_base_url!r}"
        )
    else:
        base_url_description = (
            f"Public HTTPS URL: {user_supplied_base_url}. "
            "Click 'Test webhook' below to verify Yandex can reach it."
        )

    # ---- Auto-create cluster: Step 1 / 2 / 3 dispatcher ----
    duplicate_skill_id = _resolve_saved_value(values, CONF_PENDING_DUPLICATE_SKILL_ID).strip()
    duplicate_skill_name = _resolve_saved_value(values, CONF_PENDING_DUPLICATE_SKILL_NAME).strip()
    edit_mode = bool(values.get(CONF_EDIT_MODE, False))
    activation_phrase_2_value = _resolve_saved_value(values, CONF_DIALOG_ACTIVATION_PHRASE_2)
    activation_phrase_3_value = _resolve_saved_value(values, CONF_DIALOG_ACTIVATION_PHRASE_3)
    activation_phrase_4_value = _resolve_saved_value(values, CONF_DIALOG_ACTIVATION_PHRASE_4)
    voice_value = _resolve_saved_value(values, CONF_DIALOG_SKILL_VOICE) or DIALOG_VOICE_DEFAULT

    # Surface a sign-in error in the Authorization block as ✗ LABEL.
    sign_in_error: str | None = update_message if update_message and not cached_x_token else None
    user_name = _resolve_saved_value(values, CONF_AUTH_USER_NAME)
    if not user_name and saved_provider is not None:
        try:
            user_name = str(saved_provider.get_value(CONF_AUTH_USER_NAME) or "")  # type: ignore[attr-defined]
        except Exception:
            user_name = ""

    # ---- Hidden state-carrier entries (round-trip persistence) ----
    # Use ``value=`` (not ``default_value=``) so MA frontend round-trips
    # the actual current state on form Save. hidden=True entries never
    # render, so they carry no label/description (what each key holds is
    # documented on its CONF_* constant).
    hidden_state_entries = (
        ConfigEntry(
            key=CONF_AUTH_X_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            required=False,
            value=own_x_token_value,
            hidden=True,
        ),
        ConfigEntry(
            key=CONF_AUTH_USER_NAME,
            type=ConfigEntryType.STRING,
            required=False,
            value=user_name,
            hidden=True,
        ),
        ConfigEntry(
            key=CONF_DIALOG_AUTO_CREATE_ARTIFACTS,
            type=ConfigEntryType.STRING,
            required=False,
            value=dump_artifacts(artifacts),
            hidden=True,
        ),
        ConfigEntry(
            key=CONF_PENDING_DUPLICATE_SKILL_ID,
            type=ConfigEntryType.STRING,
            required=False,
            value=duplicate_skill_id,
            hidden=True,
        ),
        ConfigEntry(
            key=CONF_PENDING_DUPLICATE_SKILL_NAME,
            type=ConfigEntryType.STRING,
            required=False,
            value=duplicate_skill_name,
            hidden=True,
        ),
        ConfigEntry(
            key=CONF_EDIT_MODE,
            type=ConfigEntryType.BOOLEAN,
            required=False,
            value=edit_mode,
            hidden=True,
        ),
        ConfigEntry(
            key=CONF_DIALOG_PUBLICATION_STATUS,
            type=ConfigEntryType.STRING,
            required=False,
            value=publication_status,
            hidden=True,
        ),
    )

    diagnostics_entries = _build_diagnostics_entries(mass, instance_id)
    use_different_instance_name = bool(values.get(CONF_USE_DIFFERENT_INSTANCE_NAME, False))

    manifest_status = manifest_provider.status()
    manifest_paste = str(values.get(CONF_DIALOG_SKILL_OVERRIDE_PASTE) or "")

    return build_form_entries(
        artifacts=artifacts,
        cached_x_token_present=bool(cached_x_token),
        user_name=user_name,
        skill_id_value=str(values.get(CONF_DIALOG_SKILL_ID) or "").strip(),
        skill_token_value=skill_token_value,
        webhook_secret=default_secret,
        last_error=sign_in_error,
        action_outcome=action_outcome,
        duplicate_skill_id=duplicate_skill_id or None,
        duplicate_skill_name=duplicate_skill_name or None,
        edit_mode=edit_mode,
        skill_name=skill_name,
        activation_phrase_2=activation_phrase_2_value,
        activation_phrase_3=activation_phrase_3_value,
        activation_phrase_4=activation_phrase_4_value,
        voice=voice_value,
        update_message=update_message,
        external_base_url=external_base_url,
        base_url_description=base_url_description,
        base_url_valid=bool(external_base_url) and is_public_https_url(external_base_url),
        player_options=player_options,
        instance_name=instance_name,
        use_different_instance_name=use_different_instance_name,
        publication_status=publication_status or None,
        diagnostics=diagnostics_entries,
        manifest_status=manifest_status,
        borrow_options=borrow_options,
        borrow_selected=borrow_selected,
        borrow_error=borrow_error,
        manifest_paste=manifest_paste,
        manifest_message=manifest_message,
        hidden_state=hidden_state_entries,
    )
