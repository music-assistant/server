"""
Yandex Smart Home Plugin Provider for Music Assistant.

Exposes Music Assistant players to Yandex Alice via the Yandex Smart Home API.
Allows voice control of MA players through Alice commands like
"Алиса, включи музыку на [имя плеера]".

Architecture:
  Alice voice command → Yandex Cloud → Smart Home API callback → this plugin → MA Player

The plugin registers MA players as media_device in Yandex Smart Home,
mapping capabilities (on_off, volume, pause) to MA player controls.

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

from ._compat import SecretStr
from .auto_skill import (
    auto_create_skill,
    load_default_logo_bytes,
)
from .auto_skill_state import (
    SkillCreationState,
    dump_artifacts,
    load_artifacts,
)
from .auto_skill_ui import build_cloud_plus_entries, build_direct_entries
from .cloud import get_cloud_otp, register_cloud_instance
from .constants import (
    CONF_ACTION_AUTO_CREATE,
    CONF_ACTION_GET_OTP,
    CONF_ACTION_REGISTER,
    CONF_AUTO_CREATE_ARTIFACTS,
    CONF_AUTO_CREATE_SESSION_ID,
    CONF_CLOUD_CONNECTION_TOKEN,
    CONF_CLOUD_INSTANCE_ID,
    CONF_CLOUD_INSTANCE_PASSWORD,
    CONF_CONNECTION_TYPE,
    CONF_DIRECT_ACCESS_TOKEN,
    CONF_DIRECT_CLIENT_SECRET,
    CONF_EXPOSED_PLAYERS,
    CONF_INSTANCE_NAME,
    CONF_SKILL_ID,
    CONF_SKILL_TOKEN,
    CONNECTION_TYPE_CLOUD,
    CONNECTION_TYPE_CLOUD_PLUS,
    CONNECTION_TYPE_DIRECT,
)
from .plugin import YandexSmartHomePlugin

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

_LOGGER = logging.getLogger(__name__)

SUPPORTED_FEATURES: set[ProviderFeature] = set()


def _build_status_label(otp_code: str | None, is_cloud_plus: bool, is_registered: bool) -> str:
    """Build the status label text based on registration state."""
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


def _resolve_direct_client_secret(
    mass: MusicAssistant,
    instance_id: str | None,
    values: dict[str, ConfigValueType],
) -> str:
    """Return the direct-mode OAuth client secret for the current install.

    `CONF_DIRECT_CLIENT_SECRET` is a SECURE_STRING: MA's frontend does
    not echo saved secrets back into ``values`` on re-open, so reading
    from ``values`` alone returns an empty string for existing instances.
    Prefer the persisted value from saved config and fall back to
    ``values`` only for first-time setup before any save.
    """
    if instance_id:
        prov = mass.get_provider(instance_id)
        if prov and prov.config:
            saved = prov.config.get_value(CONF_DIRECT_CLIENT_SECRET)
            if saved:
                return str(saved)
    return str(values.get(CONF_DIRECT_CLIENT_SECRET) or "")


async def _handle_config_actions(
    mass: MusicAssistant,
    action: str | None,
    values: dict[str, ConfigValueType],
    instance_id: str | None,
    is_cloud_plus: bool,
    connection_type: str,
) -> str | None:
    """Execute config-flow actions and return OTP code if obtained."""
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

    # NOTE: the old flow used to auto-fetch OTP right after Register so
    # the user saw the code immediately. In the 3-step cloud_plus flow
    # (Register → Create skill → Get OTP), that leaks the OTP into Step 1.
    # OTP is now fetched only when the user explicitly presses Get OTP
    # in Step 3.

    if action == CONF_ACTION_AUTO_CREATE:
        await _run_auto_create_action(mass, values, connection_type, instance_id)

    return otp_code


async def _run_auto_create_action(
    mass: MusicAssistant,
    values: dict[str, ConfigValueType],
    connection_type: str,
    instance_id: str | None,
) -> None:
    """Execute the experimental auto-create-skill action.

    Never re-raises: all errors are persisted into the artifacts blob so
    the UI can show a FAILED state on the next render rather than
    crashing the config form.
    """
    # MA's frontend supplies ``values["session_id"]`` when it triggers an
    # action — AuthenticationHelper listens on that exact id to open
    # and later close the popup. If we roll our own id nothing listens
    # and the popup never appears. Fall back to a local uuid only if the
    # frontend happened not to pass one (shouldn't happen in practice).
    session_id = str(values.get("session_id") or uuid.uuid4().hex)
    values[CONF_AUTO_CREATE_SESSION_ID] = session_id
    artifacts_raw = values.get(CONF_AUTO_CREATE_ARTIFACTS)
    artifacts = load_artifacts(str(artifacts_raw) if artifacts_raw else None)

    try:
        new_artifacts = await auto_create_skill(
            mass=mass,
            connection_type=connection_type,
            skill_name=str(values.get(CONF_INSTANCE_NAME) or "Music Assistant"),
            artifacts=artifacts,
            cloud_instance_id=str(values.get(CONF_CLOUD_INSTANCE_ID, "")),
            direct_client_secret=_resolve_direct_client_secret(mass, instance_id, values),
            logo_bytes=load_default_logo_bytes(),
            session_id=session_id,
        )
    except asyncio.CancelledError:
        # Preserve cooperative cancellation so config-flow shutdown
        # doesn't get converted into a FAILED artifact.
        raise
    except ValueError as exc:
        # Precondition failures come back here — surface as FAILED.
        new_artifacts = dataclasses.replace(
            artifacts,
            state=SkillCreationState.FAILED,
            last_error=str(exc),
        )
        _LOGGER.warning("auto-create precondition failed: %s", exc)
    except Exception as exc:  # defensive — never crash the config form
        new_artifacts = dataclasses.replace(
            artifacts,
            state=SkillCreationState.FAILED,
            last_error=repr(exc),
        )
        _LOGGER.exception("auto-create hit unexpected error")

    values[CONF_AUTO_CREATE_ARTIFACTS] = dump_artifacts(new_artifacts)
    if new_artifacts.state == SkillCreationState.DONE and new_artifacts.skill_id:
        # Only set CONF_SKILL_ID on full success so the runtime doesn't
        # try to use a half-built skill mid-pipeline.
        values[CONF_SKILL_ID] = new_artifacts.skill_id


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    if values is None:
        values = {}

    connection_type = str(values.get(CONF_CONNECTION_TYPE, CONNECTION_TYPE_CLOUD))
    is_cloud = connection_type == CONNECTION_TYPE_CLOUD
    is_cloud_plus = connection_type == CONNECTION_TYPE_CLOUD_PLUS
    is_direct = connection_type == CONNECTION_TYPE_DIRECT

    otp_code = await _handle_config_actions(
        mass, action, values, instance_id, is_cloud_plus, connection_type
    )

    # Auto-create-skill state — loaded once and threaded through the
    # per-mode builders below.
    artifacts_raw = values.get(CONF_AUTO_CREATE_ARTIFACTS)
    artifacts_str = str(artifacts_raw) if artifacts_raw else None
    artifacts = load_artifacts(artifacts_str)
    session_id_val = values.get(CONF_AUTO_CREATE_SESSION_ID)
    session_id_str = str(session_id_val) if session_id_val else None
    ma_base_url_for_ui = ""
    with contextlib.suppress(Exception):
        ma_base_url_for_ui = str(mass.webserver.base_url)

    is_registered = bool(values.get(CONF_CLOUD_INSTANCE_ID)) and bool(
        values.get(CONF_CLOUD_CONNECTION_TOKEN)
    )
    cloud_instance_id = str(values.get(CONF_CLOUD_INSTANCE_ID, ""))

    label_text = _build_status_label(otp_code, is_cloud_plus, is_registered)

    # Build player options for exposed players filter
    player_options: list[ConfigValueOption] = []
    try:
        for player in mass.players.all_players():
            state = player.state
            player_options.append(
                ConfigValueOption(title=state.name or state.player_id, value=state.player_id)
            )
    except Exception:  # noqa: S110
        pass

    entries: list[ConfigEntry] = [
        # Instance name
        ConfigEntry(
            key=CONF_INSTANCE_NAME,
            type=ConfigEntryType.STRING,
            label="Instance Name",
            description=(
                "Name of this MA instance as it will appear in Yandex Smart Home. "
                "Alice will use this name for voice commands, e.g. "
                '"Алиса, включи музыку на [имя]".'
            ),
            required=False,
            default_value="Music Assistant",
        ),
        # Save-and-reopen notice — the form doesn't re-render on
        # dropdown change, so the user has to Save + reopen to see
        # the next mode's fields.
        ConfigEntry(
            key="label_connection_type_notice",
            type=ConfigEntryType.LABEL,
            label=(
                "💡 After changing Connection Type below, click Save and "
                "reopen this settings page to see the fields for the new mode."
            ),
        ),
        # Connection type selector
        ConfigEntry(
            key=CONF_CONNECTION_TYPE,
            type=ConfigEntryType.STRING,
            label="Connection Type",
            description=(
                '"cloud" — public Yaha Cloud skill (simple setup). '
                '"cloud_plus" — private skill via cloud relay (for multi-platform setups). '
                '"direct" — Yandex calls your MA server directly (requires public HTTPS URL).'
            ),
            required=False,
            default_value=CONNECTION_TYPE_CLOUD,
            options=[
                ConfigValueOption(title="Cloud (public Yaha Cloud skill)", value="cloud"),
                ConfigValueOption(title="Cloud Plus (private skill)", value="cloud_plus"),
                ConfigValueOption(title="Direct (no relay, requires public URL)", value="direct"),
            ],
            # NOTE: immediate_apply produced glitchy mixed-mode renders
            # (entries from old mode stayed on screen next to new ones),
            # so users need Save + reopen after changing Connection
            # Type. Kept here to stop someone re-adding it.
        ),
    ]

    # -- Per-mode sections (each builder returns only the fields for its mode)
    if is_cloud:
        entries.extend(_cloud_mode_entries(label_text, otp_code, is_registered))
    elif is_cloud_plus:
        entries.extend(
            build_cloud_plus_entries(
                otp_code=otp_code,
                is_registered=is_registered,
                cloud_instance_id=cloud_instance_id,
                artifacts=artifacts,
                session_id=session_id_str,
                user_code=None,  # popup URL carries the code
                verification_url=None,
                existing_artifacts_raw=artifacts_str,
                base_url=ma_base_url_for_ui,
                skill_id=str(values.get(CONF_SKILL_ID) or ""),
                skill_token_set=bool(values.get(CONF_SKILL_TOKEN)),
            )
        )
    elif is_direct:
        # Pre-generate the per-install direct client secret once so it
        # survives round-trips (auto-skill pipeline reads it later).
        # SECURE_STRING is not echoed back into ``values`` on re-open,
        # so prefer the persisted value from saved config first and
        # only mint a fresh UUID on true first-time setup.
        direct_secret = _resolve_direct_client_secret(mass, instance_id, values)
        if not direct_secret:
            direct_secret = uuid.uuid4().hex
            values[CONF_DIRECT_CLIENT_SECRET] = direct_secret
        entries.extend(
            build_direct_entries(
                artifacts=artifacts,
                session_id=session_id_str,
                user_code=None,
                verification_url=None,
                existing_artifacts_raw=artifacts_str,
                base_url=ma_base_url_for_ui,
                direct_client_secret=direct_secret,
                skill_id=str(values.get(CONF_SKILL_ID) or ""),
                skill_token_set=bool(values.get(CONF_SKILL_TOKEN)),
            )
        )
        # NB: CONF_DIRECT_CLIENT_SECRET is now emitted by the manual
        # fallback block (advanced/hidden per state), so we don't add a
        # duplicate hidden round-trip entry here.

    # -- Tail: player filter + hidden round-trip fields (all modes) --
    entries.extend(_common_tail_entries(player_options, values))
    return tuple(entries)


def _cloud_mode_entries(
    label_text: str, otp_code: str | None, is_registered: bool
) -> list[ConfigEntry]:
    """Public-cloud mode: simple register + get-OTP flow."""
    return [
        # Advisory — the public Yaha Cloud skill can only be linked to one
        # instance per Yandex account, so users who already set up Yaha
        # Cloud in Home Assistant (or another MA install) need Cloud Plus.
        # There's no pre-flight API to detect this, so the warning is
        # static — cheaper than a failed OTP attempt.
        ConfigEntry(
            key="label_cloud_conflict_warning",
            type=ConfigEntryType.LABEL,
            label=(
                "⚠️ If this Yandex account already uses the Yaha Cloud skill "
                "via Home Assistant or another Music Assistant install, "
                "pick 'Cloud Plus' above instead — the public skill can "
                "only be linked to one instance per account."
            ),
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
            label="OTP Code",
            description="Copy this code and enter it in the Yandex app.",
            required=False,
            value=otp_code,
            hidden=not otp_code,
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value=CONNECTION_TYPE_CLOUD,
        ),
        ConfigEntry(
            key=CONF_ACTION_REGISTER,
            type=ConfigEntryType.ACTION,
            label="Register cloud instance",
            description="Register a new instance on yaha-cloud.ru relay service.",
            action=CONF_ACTION_REGISTER,
            action_label="Register with cloud",
            hidden=is_registered,
            # No depends_on — MA disables actions with an unsaved
            # dependency value until the user clicks Save, which breaks
            # the flow right after picking a connection type.
        ),
        ConfigEntry(
            key=CONF_ACTION_GET_OTP,
            type=ConfigEntryType.ACTION,
            label="Get OTP code",
            description="Get a fresh one-time password to link with Yandex Smart Home app.",
            action=CONF_ACTION_GET_OTP,
            action_label="Get OTP code",
            hidden=not is_registered,
        ),
    ]


def _common_tail_entries(
    player_options: list[ConfigValueOption], values: dict[str, ConfigValueType]
) -> list[ConfigEntry]:
    """Player filter + hidden round-trip fields shared by every mode."""
    return [
        ConfigEntry(
            key=CONF_EXPOSED_PLAYERS,
            type=ConfigEntryType.STRING,
            label="Exposed Players",
            description=(
                "Select which MA players to expose to Yandex Smart Home. "
                "Leave empty to expose all players."
            ),
            required=False,
            multi_value=True,
            default_value=[],
            options=list(player_options) if player_options else [],
        ),
        ConfigEntry(
            key=CONF_CLOUD_INSTANCE_ID,
            type=ConfigEntryType.STRING,
            label="Cloud Instance ID",
            hidden=True,
            required=False,
            value=cast("str", values.get(CONF_CLOUD_INSTANCE_ID)) if values else None,
        ),
        ConfigEntry(
            key=CONF_CLOUD_INSTANCE_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Cloud Instance Password",
            hidden=True,
            required=False,
            value=(cast("str", values.get(CONF_CLOUD_INSTANCE_PASSWORD)) if values else None),
        ),
        ConfigEntry(
            key=CONF_CLOUD_CONNECTION_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Cloud Connection Token",
            hidden=True,
            required=False,
            value=(cast("str", values.get(CONF_CLOUD_CONNECTION_TOKEN)) if values else None),
        ),
        ConfigEntry(
            key=CONF_DIRECT_ACCESS_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Direct Access Token",
            hidden=True,
            required=False,
            value=(cast("str", values.get(CONF_DIRECT_ACCESS_TOKEN)) if values else None),
        ),
    ]
