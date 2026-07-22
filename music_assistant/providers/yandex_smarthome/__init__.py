"""
Yandex Smart Home Plugin Provider for Music Assistant.

Exposes Music Assistant players as Yandex Smart Home devices so Alice can
control playback (play / pause / volume / mute / source) via natural-language
commands. The voice-skill (custom dialog) functionality lives in the sister
provider `ma-provider-yandex-alice`.

Connection modes:
- ``cloud`` — public yaha-cloud.ru relay (zero setup, but the public skill can
  only be linked to one MA / Home Assistant instance per Yandex account).
- ``cloud_plus`` — private skill via the yaha-cloud relay (multiple instances
  per account, registered manually in the dev console).
- ``direct`` — Yandex calls the MA webserver directly (requires public HTTPS).

Reference: https://github.com/dext0r/yandex_smart_home
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import uuid
from typing import TYPE_CHECKING, cast

import aiohttp
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ProviderFeature
from ya_dialogs_api import (
    SMART_HOME_CHANNEL,
    SecretStr,
    SkillCreationArtifacts,
    SkillCreationState,
    auto_create_skill,
    dump_artifacts,
    load_artifacts,
    load_default_logo_bytes,
)
from ya_passport_auth.ma import (
    BORROW_SOURCE_OWN,
    BorrowedCredentialSource,
    list_yandex_music_instances,
)

from ._smarthome_auto_create import derive_smart_home_urls, resolve_base_url
from .cloud import get_cloud_otp, register_cloud_instance
from .constants import (
    CONF_ACTION_AUTO_CREATE,
    CONF_ACTION_GET_OTP,
    CONF_ACTION_REGISTER,
    CONF_AUTH_X_TOKEN,
    CONF_AUTO_CREATE_ARTIFACTS,
    CONF_AUTO_CREATE_SESSION_ID,
    CONF_CLOUD_CONNECTION_TOKEN,
    CONF_CLOUD_INSTANCE_ID,
    CONF_CLOUD_INSTANCE_PASSWORD,
    CONF_CONNECTION_TYPE,
    CONF_DIRECT_ACCESS_TOKEN,
    CONF_DIRECT_CLIENT_SECRET,
    CONF_EXPOSED_PLAYERS,
    CONF_EXPOSED_PLAYLISTS,
    CONF_EXTERNAL_BASE_URL,
    CONF_INSTANCE_NAME,
    CONF_SKILL_ID,
    CONF_SKILL_TOKEN,
    CONF_YM_INSTANCE,
    CONNECTION_TYPE_CLOUD,
    CONNECTION_TYPE_CLOUD_PLUS,
    CONNECTION_TYPE_DIRECT,
    YANDEX_OAUTH_URL,
)
from .ma_authenticator import make_authenticator
from .playlists import fetch_playlist_options
from .plugin import YandexSmartHomePlugin

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


_LOGGER = logging.getLogger(__name__)

SUPPORTED_FEATURES: set[ProviderFeature] = set()


def _build_status_label(otp_code: str | None, is_cloud_plus: bool, is_registered: bool) -> str:
    """Status banner text for cloud / cloud_plus modes."""
    if otp_code and is_cloud_plus:
        return (
            "✅ Cloud instance registered! "
            "Open Yandex app → Devices → Add device → Smart Home → "
            "find your private skill → enter OTP code below → "
            "then click Save to complete setup."
        )
    if otp_code:
        return (
            "✅ Cloud instance registered! "
            "Open Yandex app → Devices → Add device → Smart Home → "
            "find 'Yaha Cloud' skill → enter OTP code below → "
            "then click Save to complete setup."
        )
    if is_registered:
        return (
            "✅ Cloud instance is configured. "
            "Use 'Get OTP code' if you need to re-link with Yandex."
        )
    return (
        "Register a cloud instance to connect with Yandex Alice. "
        "This is free and uses the yaha-cloud.ru relay service (no public URL needed)."
    )


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return YandexSmartHomePlugin(mass, manifest, config, SUPPORTED_FEATURES)


def _is_skill_token_set(
    mass: MusicAssistant,
    instance_id: str | None,
    values: dict[str, ConfigValueType],
) -> bool:
    """
    Return True if a non-empty Skill OAuth token is persisted or in-flight.

    The token is a SECURE_STRING — the frontend doesn't echo it back into
    ``values`` on re-open. Prefer the persisted provider-config value; fall
    back to ``values`` for the very first save round-trip when nothing is
    persisted yet.
    """
    if instance_id:
        prov = mass.get_provider(instance_id)
        if prov and prov.config:
            saved = prov.config.get_value(CONF_SKILL_TOKEN)
            if saved:
                return True
    return bool(values.get(CONF_SKILL_TOKEN))


def _resolve_direct_client_secret(
    mass: MusicAssistant,
    instance_id: str | None,
    values: dict[str, ConfigValueType],
) -> str:
    """
    Return the direct-mode per-install OAuth client secret.

    The frontend does not echo SECURE_STRING fields back into ``values`` on
    re-open, so prefer the saved provider config; fall back to the in-flight
    form value (e.g. on a fresh-install round-trip before the first save).
    """
    if instance_id:
        prov = mass.get_provider(instance_id)
        if prov and prov.config:
            saved = prov.config.get_value(CONF_DIRECT_CLIENT_SECRET)
            if saved:
                return str(saved)
    return str(values.get(CONF_DIRECT_CLIENT_SECRET) or "")


def _resolve_cached_x_token(
    mass: MusicAssistant,
    instance_id: str | None,
    values: dict[str, ConfigValueType],
) -> str:
    """
    Return the cached Yandex Passport x_token, or empty string if absent.

    Like the other secret resolvers, prefers the persisted SECURE_STRING
    from saved config since the frontend does not echo secrets back into
    ``values`` on re-open.
    """
    if instance_id:
        prov = mass.get_provider(instance_id)
        if prov and prov.config:
            saved = prov.config.get_value(CONF_AUTH_X_TOKEN)
            if saved:
                return str(saved)
    return str(values.get(CONF_AUTH_X_TOKEN) or "")


async def _run_auto_create_action(
    mass: MusicAssistant,
    values: dict[str, ConfigValueType],
    connection_type: str,
    instance_id: str | None,
) -> None:
    """
    Run the smart-home auto-create skill pipeline.

    Never re-raises: failures are persisted in artifacts.last_error so the
    UI can render the message on the next form open.
    """
    artifacts_raw = values.get(CONF_AUTO_CREATE_ARTIFACTS)
    artifacts = load_artifacts(str(artifacts_raw) if artifacts_raw else None)

    # MA's frontend supplies values["session_id"] on every action invocation —
    # AuthenticationHelper listens on that exact id to open and later close
    # the popup. Generating our own UUID would tie the popup we open via
    # auth_helper.send_url(...) to a channel nothing is listening on, leaving
    # the user with a popup that doesn't appear or doesn't close. Fail loudly.
    session_id_raw = values.get("session_id")
    if not session_id_raw or not str(session_id_raw).strip():
        new_artifacts = dataclasses.replace(
            artifacts,
            state=SkillCreationState.FAILED,
            last_error=(
                "Missing session_id from the config-flow frontend. "
                "Auto-create needs a session id to open the Device Code "
                "popup; the action must be invoked through the MA UI, not "
                "programmatically."
            ),
        )
        values[CONF_AUTO_CREATE_ARTIFACTS] = dump_artifacts(new_artifacts)
        _LOGGER.warning("auto-create invoked without frontend session_id")
        return
    session_id = str(session_id_raw).strip()
    values[CONF_AUTO_CREATE_SESSION_ID] = session_id

    base_url = resolve_base_url(mass, str(values.get(CONF_EXTERNAL_BASE_URL) or "") or None)

    try:
        urls = derive_smart_home_urls(
            connection_type=connection_type,
            base_url=base_url,
            cloud_instance_id=str(values.get(CONF_CLOUD_INSTANCE_ID, "")),
            direct_client_secret=_resolve_direct_client_secret(mass, instance_id, values),
        )
    except ValueError as exc:
        new_artifacts = dataclasses.replace(
            artifacts, state=SkillCreationState.FAILED, last_error=str(exc)
        )
        values[CONF_AUTO_CREATE_ARTIFACTS] = dump_artifacts(new_artifacts)
        _LOGGER.warning("auto-create precondition failed: %s", exc)
        return

    def _cache_x_token(token: str) -> None:
        values[CONF_AUTH_X_TOKEN] = token

    async def _persist_artifacts(a: SkillCreationArtifacts) -> None:
        values[CONF_AUTO_CREATE_ARTIFACTS] = dump_artifacts(a)

    # Yandex account source: borrow the linked yandex_music instance's
    # x_token read-only (no Device Flow fallback, no persistence into this
    # provider's config), or run the own cached-token/Device Flow path.
    ym_selected = str(values.get(CONF_YM_INSTANCE) or BORROW_SOURCE_OWN)
    borrowing = ym_selected != BORROW_SOURCE_OWN
    if borrowing:
        try:
            _, borrowed_x = BorrowedCredentialSource(mass, ym_selected).read_tokens()
        except Exception as exc:
            new_artifacts = dataclasses.replace(
                artifacts, state=SkillCreationState.FAILED, last_error=str(exc)
            )
            values[CONF_AUTO_CREATE_ARTIFACTS] = dump_artifacts(new_artifacts)
            _LOGGER.warning("auto-create borrow source unavailable: %s", exc)
            return
        authenticator = make_authenticator(
            mass=mass,
            session_id=session_id,
            cached_x_token=borrowed_x.get_secret() if borrowed_x else None,
            on_token_obtained=None,
            allow_device_flow=False,
        )
    else:
        cached = _resolve_cached_x_token(mass, instance_id, values) or None
        authenticator = make_authenticator(
            mass=mass,
            session_id=session_id,
            cached_x_token=cached,
            on_token_obtained=_cache_x_token,
        )

    try:
        new_artifacts = await auto_create_skill(
            authenticator=authenticator,
            skill_name=str(values.get(CONF_INSTANCE_NAME) or "Music Assistant"),
            artifacts=artifacts,
            backend_uri=urls.backend_uri,
            oauth_authorize_url=urls.oauth_authorize_url,
            oauth_token_url=urls.oauth_token_url,
            oauth_client_id=urls.oauth_client_id,
            oauth_client_secret=urls.oauth_client_secret,
            logo_bytes=load_default_logo_bytes(),
            channel=SMART_HOME_CHANNEL,
            progress_cb=_persist_artifacts,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # defensive — never crash the config form
        # Use type-name + str(exc) instead of repr(exc): repr() of e.g.
        # aiohttp.ClientResponseError includes request_info (URL, headers)
        # which can leak into the UI-visible artifacts.last_error. The full
        # traceback is captured via _LOGGER.exception below.
        msg = str(exc).strip() or type(exc).__name__
        # Cap the surfaced message so a runaway exception body can't bloat
        # the round-tripped artifacts blob.
        if len(msg) > 500:
            msg = msg[:497] + "..."
        new_artifacts = dataclasses.replace(
            artifacts,
            state=SkillCreationState.FAILED,
            last_error=f"{type(exc).__name__}: {msg}",
        )
        _LOGGER.exception("auto-create hit unexpected error")

    values[CONF_AUTO_CREATE_ARTIFACTS] = dump_artifacts(new_artifacts)
    if new_artifacts.state == SkillCreationState.DONE and new_artifacts.skill_id:
        # Only set CONF_SKILL_ID on full success so the runtime doesn't
        # try to use a half-built skill mid-pipeline.
        values[CONF_SKILL_ID] = new_artifacts.skill_id


async def _handle_config_actions(
    mass: MusicAssistant,
    action: str | None,
    values: dict[str, ConfigValueType],
    instance_id: str | None,
    is_cloud_plus: bool,
    connection_type: str,
) -> str | None:
    """Execute config-flow actions; return OTP code if obtained, else None."""
    saved_config = None
    if instance_id:
        prov = mass.get_provider(instance_id)
        if prov:
            saved_config = prov.config

    if action == CONF_ACTION_REGISTER:
        try:
            platform = "yandex" if is_cloud_plus else None
            async with aiohttp.ClientSession() as session:
                data = await register_cloud_instance(session, platform=platform)
            values[CONF_CLOUD_INSTANCE_ID] = data["id"]
            values[CONF_CLOUD_INSTANCE_PASSWORD] = data["password"]
            values[CONF_CLOUD_CONNECTION_TOKEN] = data["connection_token"]
            _LOGGER.info("Auto-registered cloud instance: %s", data["id"])
        except Exception:
            _LOGGER.exception("Failed to register cloud instance")

    otp_code: str | None = None
    if action == CONF_ACTION_GET_OTP:
        cloud_id = str(values.get(CONF_CLOUD_INSTANCE_ID, ""))
        cloud_token = ""
        if saved_config:
            cloud_token = str(saved_config.get_value(CONF_CLOUD_CONNECTION_TOKEN) or "")
        if not cloud_token:
            cloud_token = str(values.get(CONF_CLOUD_CONNECTION_TOKEN, ""))
        if cloud_id and cloud_token:
            try:
                async with aiohttp.ClientSession() as session:
                    otp_code = await get_cloud_otp(session, cloud_id, SecretStr(cloud_token))
            except Exception:
                _LOGGER.exception("Failed to get OTP code")

    if action == CONF_ACTION_AUTO_CREATE:
        await _run_auto_create_action(mass, values, connection_type, instance_id)

    return otp_code


async def _list_player_options(mass: MusicAssistant) -> list[ConfigValueOption]:
    """Build the player-picker options list."""
    options: list[ConfigValueOption] = []
    try:
        for player in mass.players.all_players():
            state = player.state
            options.append(
                ConfigValueOption(title=state.name or state.player_id, value=state.player_id)
            )
    except Exception:
        _LOGGER.debug("could not enumerate players")
    return options


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Build the provider config-form entries."""
    if values is None:
        values = {}

    # Yandex account source: normalize a stale selection back to own so
    # the auto-create flow doesn't chase a removed instance.
    ym_instances = list_yandex_music_instances(mass)
    ym_selected = str(values.get(CONF_YM_INSTANCE) or BORROW_SOURCE_OWN)
    if ym_selected != BORROW_SOURCE_OWN and ym_selected not in {
        inst_id for inst_id, _ in ym_instances
    }:
        ym_selected = BORROW_SOURCE_OWN
        values[CONF_YM_INSTANCE] = BORROW_SOURCE_OWN

    connection_type = str(values.get(CONF_CONNECTION_TYPE, CONNECTION_TYPE_CLOUD))
    is_cloud = connection_type == CONNECTION_TYPE_CLOUD
    is_cloud_plus = connection_type == CONNECTION_TYPE_CLOUD_PLUS
    is_direct = connection_type == CONNECTION_TYPE_DIRECT

    otp_code = await _handle_config_actions(
        mass, action, values, instance_id, is_cloud_plus, connection_type
    )

    is_registered = bool(values.get(CONF_CLOUD_INSTANCE_ID)) and bool(
        values.get(CONF_CLOUD_CONNECTION_TOKEN)
    )
    label_text = _build_status_label(otp_code, is_cloud_plus, is_registered)

    # Load current auto-create artifacts for state-aware button labels.
    artifacts_raw = values.get(CONF_AUTO_CREATE_ARTIFACTS)
    artifacts_str = str(artifacts_raw) if artifacts_raw else None
    artifacts = load_artifacts(artifacts_str)

    player_options = await _list_player_options(mass)
    playlist_options: list[ConfigValueOption] = []
    try:
        playlist_options = await fetch_playlist_options(mass)
    except asyncio.CancelledError:
        raise
    except Exception:
        _LOGGER.debug("could not enumerate playlists")

    entries: list[ConfigEntry] = [
        ConfigEntry(
            key=CONF_INSTANCE_NAME,
            type=ConfigEntryType.STRING,
            required=False,
            default_value="Music Assistant",
        ),
        ConfigEntry(
            key="label_connection_type_notice",
            type=ConfigEntryType.LABEL,
        ),
        ConfigEntry(
            key=CONF_CONNECTION_TYPE,
            type=ConfigEntryType.STRING,
            required=False,
            default_value=CONNECTION_TYPE_CLOUD,
            options=[
                ConfigValueOption("cloud"),
                ConfigValueOption("cloud_plus"),
                ConfigValueOption("direct"),
            ],
        ),
    ]

    skill_token_set = _is_skill_token_set(mass, instance_id, values)

    if is_cloud:
        entries.extend(_cloud_mode_entries(label_text, otp_code, is_registered))
    elif is_cloud_plus:
        entries.extend(
            _cloud_plus_mode_entries(
                label_text, otp_code, is_registered, values, artifacts, skill_token_set
            )
        )
    elif is_direct:
        entries.extend(_direct_mode_entries(mass, instance_id, values, artifacts, skill_token_set))

    entries.extend(_common_tail_entries(player_options, playlist_options, values, ym_instances))
    return tuple(entries)


def _build_auto_create_status(
    artifacts: SkillCreationArtifacts,
    skill_id_already_set: bool,
    *,
    skill_token_set: bool = True,
) -> tuple[str, str]:
    """
    Return (status_label, action_button_label) based on artifacts state.

    The action button label flips based on what makes sense to do next:
    fresh attempt → 'Create…', resumable failure → 'Retry…',
    in-progress → 'Continue…', success → 'Re-create…'.

    ``skill_token_set`` lets the success-state label nudge the user to paste
    the OAuth token next — the auto-create pipeline only fills Skill ID; the
    token comes from a separate OAuth flow (oauth.yandex.ru/authorize) that
    the user does themselves via the help-link icon on the Skill OAuth Token
    field.
    """
    state = artifacts.state
    if state == SkillCreationState.DONE and (artifacts.skill_id or skill_id_already_set):
        skill_id = artifacts.skill_id or "<saved>"
        if not skill_token_set:
            return (
                f"✅ Smart Home skill registered (skill_id={skill_id}). "
                "**Next step:** click the link icon next to *Skill OAuth "
                "Token* below to open the Yandex OAuth page, approve access, "
                "and paste the access_token value from the resulting URL "
                "fragment into the field. State callbacks won't work until "
                "this is done.",
                "Re-create skill",
            )
        return (
            f"✅ Smart Home skill registered (skill_id={skill_id}). "
            "Click 'Re-create' below to provision a fresh skill in your "
            "Yandex account.",
            "Re-create skill",
        )
    if state == SkillCreationState.FAILED:
        err = (artifacts.last_error or "").strip()
        if err:
            return (
                f"❌ Last attempt failed: {err}\n\n"
                "Click 'Retry' to resume from the last completed step.",
                "Retry",
            )
        return (
            "Click 'Create Smart Home skill' to register a private skill in "
            "your Yandex account programmatically (Device Flow OAuth login, "
            "then automated skill provisioning).",
            "Create Smart Home skill",
        )
    if state in (
        SkillCreationState.APP_CREATED,
        SkillCreationState.DRAFT_UPDATED,
        SkillCreationState.OAUTH_CREATED,
        SkillCreationState.OAUTH_ATTACHED,
        SkillCreationState.DEPLOY_REQUESTED,
    ):
        return (
            f"🔄 Pipeline in progress (state: {state.value}). "
            "Click 'Continue' to resume from this step.",
            "Continue",
        )
    # NONE or any other unexpected state: fresh start.
    return (
        "Click 'Create Smart Home skill' to register a private skill in "
        "your Yandex account programmatically (Device Flow OAuth login, "
        "then automated skill provisioning).",
        "Create Smart Home skill",
    )


def _auto_create_entries(
    artifacts: SkillCreationArtifacts,
    *,
    skill_id_already_set: bool,
    skill_token_set: bool,
    depends_on_value: str,
) -> list[ConfigEntry]:
    """Auto-create button + state-aware status label, gated by connection_type."""
    status_text, button_label = _build_auto_create_status(
        artifacts, skill_id_already_set, skill_token_set=skill_token_set
    )
    return [
        ConfigEntry(
            key=f"label_auto_create_status_{depends_on_value}",
            type=ConfigEntryType.LABEL,
            label=status_text,
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value=depends_on_value,
        ),
        ConfigEntry(
            key=CONF_ACTION_AUTO_CREATE,
            type=ConfigEntryType.ACTION,
            action=CONF_ACTION_AUTO_CREATE,
            action_label=button_label,
        ),
    ]


def _cloud_mode_entries(
    label_text: str, otp_code: str | None, is_registered: bool
) -> list[ConfigEntry]:
    """Public-cloud mode: simple register + get-OTP flow."""
    return [
        ConfigEntry(
            key="label_cloud_conflict_warning",
            type=ConfigEntryType.LABEL,
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value=CONNECTION_TYPE_CLOUD,
        ),
        ConfigEntry(
            key="label_status",
            type=ConfigEntryType.LABEL,
            label=label_text,
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value=CONNECTION_TYPE_CLOUD,
        ),
        ConfigEntry(
            key="otp_code",
            type=ConfigEntryType.STRING,
            required=False,
            value=otp_code,
            hidden=not otp_code,
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value=CONNECTION_TYPE_CLOUD,
        ),
        ConfigEntry(
            key=CONF_ACTION_REGISTER,
            type=ConfigEntryType.ACTION,
            action=CONF_ACTION_REGISTER,
            hidden=is_registered,
        ),
        ConfigEntry(
            key=CONF_ACTION_GET_OTP,
            type=ConfigEntryType.ACTION,
            action=CONF_ACTION_GET_OTP,
            hidden=not is_registered,
        ),
    ]


def _cloud_plus_mode_entries(
    label_text: str,
    otp_code: str | None,
    is_registered: bool,
    values: dict[str, ConfigValueType],
    artifacts: SkillCreationArtifacts,
    skill_token_set: bool,
) -> list[ConfigEntry]:
    """Cloud Plus: register + auto-create or manual skill_id/skill_token."""
    skill_id_set = bool(values.get(CONF_SKILL_ID))
    return [
        ConfigEntry(
            key="label_status_cp",
            type=ConfigEntryType.LABEL,
            label=label_text,
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value=CONNECTION_TYPE_CLOUD_PLUS,
        ),
        ConfigEntry(
            key="label_cloud_plus_help",
            type=ConfigEntryType.LABEL,
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value=CONNECTION_TYPE_CLOUD_PLUS,
        ),
        ConfigEntry(
            key="otp_code_cp",
            type=ConfigEntryType.STRING,
            required=False,
            value=otp_code,
            hidden=not otp_code,
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value=CONNECTION_TYPE_CLOUD_PLUS,
        ),
        ConfigEntry(
            key=CONF_ACTION_REGISTER,
            type=ConfigEntryType.ACTION,
            action=CONF_ACTION_REGISTER,
            hidden=is_registered,
        ),
        *_auto_create_entries(
            artifacts,
            skill_id_already_set=skill_id_set,
            skill_token_set=skill_token_set,
            depends_on_value=CONNECTION_TYPE_CLOUD_PLUS,
        ),
        ConfigEntry(
            key=CONF_SKILL_ID,
            type=ConfigEntryType.STRING,
            required=False,
            value=cast("str", values.get(CONF_SKILL_ID)) if values else None,
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value=CONNECTION_TYPE_CLOUD_PLUS,
        ),
        ConfigEntry(
            key=CONF_SKILL_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            help_link=YANDEX_OAUTH_URL,
            required=False,
            value=cast("str", values.get(CONF_SKILL_TOKEN)) if values else None,
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value=CONNECTION_TYPE_CLOUD_PLUS,
        ),
        ConfigEntry(
            key=CONF_ACTION_GET_OTP,
            type=ConfigEntryType.ACTION,
            action=CONF_ACTION_GET_OTP,
            hidden=not is_registered,
        ),
    ]


def _direct_mode_entries(
    mass: MusicAssistant,
    instance_id: str | None,
    values: dict[str, ConfigValueType],
    artifacts: SkillCreationArtifacts,
    skill_token_set: bool,
) -> list[ConfigEntry]:
    """Direct mode: HTTPS callback URL + auto-create or manual skill_id/skill_token."""
    direct_secret = _resolve_direct_client_secret(mass, instance_id, values)
    if not direct_secret:
        direct_secret = uuid.uuid4().hex
        values[CONF_DIRECT_CLIENT_SECRET] = direct_secret

    ma_global_base_url = ""
    with contextlib.suppress(Exception):
        ma_global_base_url = str(mass.webserver.base_url)
    external_url_description = (
        "Public HTTPS URL of this MA instance (e.g. https://ma.example.com), "
        "used for Yandex callbacks and webhooks. Set this if you don't want "
        "to change MA's global Base URL — e.g. you reach MA via Home "
        "Assistant Ingress and exposing a public URL globally would break "
        f"local access. Leave empty to use MA's Base URL ({ma_global_base_url or '<unset>'})."
    )
    skill_id_set = bool(values.get(CONF_SKILL_ID))

    return [
        ConfigEntry(
            key="label_direct_help",
            type=ConfigEntryType.LABEL,
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value=CONNECTION_TYPE_DIRECT,
        ),
        ConfigEntry(
            key=CONF_EXTERNAL_BASE_URL,
            type=ConfigEntryType.STRING,
            description=external_url_description,
            required=False,
            default_value="",
            value=str(values.get(CONF_EXTERNAL_BASE_URL) or ""),
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value=CONNECTION_TYPE_DIRECT,
        ),
        *_auto_create_entries(
            artifacts,
            skill_id_already_set=skill_id_set,
            skill_token_set=skill_token_set,
            depends_on_value=CONNECTION_TYPE_DIRECT,
        ),
        ConfigEntry(
            key=CONF_SKILL_ID,
            type=ConfigEntryType.STRING,
            required=False,
            value=cast("str", values.get(CONF_SKILL_ID)) if values else None,
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value=CONNECTION_TYPE_DIRECT,
        ),
        ConfigEntry(
            key=CONF_SKILL_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            help_link=YANDEX_OAUTH_URL,
            required=False,
            value=cast("str", values.get(CONF_SKILL_TOKEN)) if values else None,
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value=CONNECTION_TYPE_DIRECT,
        ),
        # Per-install OAuth client secret (kept hidden, round-tripped).
        ConfigEntry(
            key=CONF_DIRECT_CLIENT_SECRET,
            type=ConfigEntryType.SECURE_STRING,
            hidden=True,
            required=False,
            value=direct_secret,
        ),
    ]


def _common_tail_entries(
    player_options: list[ConfigValueOption],
    playlist_options: list[ConfigValueOption],
    values: dict[str, ConfigValueType],
    ym_instances: list[tuple[str, str]],
) -> list[ConfigEntry]:
    """Player filter + hidden round-trip fields shared by every mode."""
    return [
        ConfigEntry(
            key=CONF_EXPOSED_PLAYERS,
            type=ConfigEntryType.STRING,
            required=False,
            multi_value=True,
            default_value=[],
            options=list(player_options) if player_options else [],
        ),
        ConfigEntry(
            key=CONF_EXPOSED_PLAYLISTS,
            type=ConfigEntryType.STRING,
            required=False,
            multi_value=True,
            default_value=[],
            options=list(playlist_options) if playlist_options else [],
        ),
        ConfigEntry(
            key=CONF_CLOUD_INSTANCE_ID,
            type=ConfigEntryType.STRING,
            hidden=True,
            required=False,
            value=cast("str", values.get(CONF_CLOUD_INSTANCE_ID)) if values else None,
        ),
        ConfigEntry(
            key=CONF_CLOUD_INSTANCE_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            hidden=True,
            required=False,
            value=(cast("str", values.get(CONF_CLOUD_INSTANCE_PASSWORD)) if values else None),
        ),
        ConfigEntry(
            key=CONF_CLOUD_CONNECTION_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            hidden=True,
            required=False,
            value=(cast("str", values.get(CONF_CLOUD_CONNECTION_TOKEN)) if values else None),
        ),
        ConfigEntry(
            key=CONF_DIRECT_ACCESS_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            hidden=True,
            required=False,
            value=(cast("str", values.get(CONF_DIRECT_ACCESS_TOKEN)) if values else None),
        ),
        # Auto-create-skill state — JSON-serialised SkillCreationArtifacts.
        # Round-tripped through every config-flow render so the state machine
        # survives popup cycles and partial failures.
        ConfigEntry(
            key=CONF_AUTO_CREATE_ARTIFACTS,
            type=ConfigEntryType.STRING,
            hidden=True,
            required=False,
            value=(cast("str", values.get(CONF_AUTO_CREATE_ARTIFACTS)) if values else None),
        ),
        ConfigEntry(
            key=CONF_AUTO_CREATE_SESSION_ID,
            type=ConfigEntryType.STRING,
            hidden=True,
            required=False,
            value=(cast("str", values.get(CONF_AUTO_CREATE_SESSION_ID)) if values else None),
        ),
        ConfigEntry(
            key=CONF_YM_INSTANCE,
            type=ConfigEntryType.STRING,
            options=[
                *(
                    ConfigValueOption(inst_id, f"Yandex Music: {name}")
                    for inst_id, name in ym_instances
                ),
                ConfigValueOption(BORROW_SOURCE_OWN, "Use own credentials (default)"),
            ],
            default_value=BORROW_SOURCE_OWN,
            required=False,
        ),
        # Cached Yandex Passport x_token — populated after the first
        # successful auto-create Device Flow and reused on subsequent
        # auto-create runs to skip the device-code prompt.
        ConfigEntry(
            key=CONF_AUTH_X_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            hidden=True,
            required=False,
            value=(cast("str", values.get(CONF_AUTH_X_TOKEN)) if values else None),
        ),
    ]
