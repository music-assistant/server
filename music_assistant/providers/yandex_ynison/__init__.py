"""Yandex Music Connect (Ynison) plugin for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ProviderFeature
from music_assistant_models.errors import LoginFailed

from .auth import perform_qr_auth
from .config_helpers import list_yandex_music_instances
from .constants import (
    CONF_ACCOUNT_LOGIN,
    CONF_ACTION_AUTH_QR,
    CONF_ACTION_CLEAR_AUTH,
    CONF_ALLOW_PLAYER_SWITCH,
    CONF_DEVICE_ID,
    CONF_ENABLE_UI_INTEGRATION,
    CONF_HANDOFF_HEARTBEAT_INTERVAL,
    CONF_MASS_PLAYER_ID,
    CONF_OUTPUT_BIT_DEPTH,
    CONF_OUTPUT_SAMPLE_RATE,
    CONF_PLAYBACK_MODE,
    CONF_PUBLISH_NAME,
    CONF_REMEMBER_SESSION,
    CONF_TOKEN,
    CONF_X_TOKEN,
    CONF_YM_INSTANCE,
    DEFAULT_DISPLAY_NAME,
    HANDOFF_HEARTBEAT_DEFAULT,
    OUTPUT_AUTO,
    PLAYBACK_MODE_HANDOFF,
    PLAYBACK_MODE_STREAM,
    PLAYER_ID_AUTO,
    YM_INSTANCE_OWN,
)
from .provider import YandexYnisonProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {ProviderFeature.AUDIO_SOURCE}


def _features_for_mode(mode: str) -> set[ProviderFeature]:
    """Return the supported features set for the given playback mode.

    Stream mode (default) advertises AUDIO_SOURCE so the plugin appears as a
    selectable audio source on players. Handoff mode hands playback off to
    MA's player_queue + yandex_music MusicProvider, so the plugin must NOT
    own the audio source — features set is empty.
    """
    if mode == PLAYBACK_MODE_HANDOFF:
        return set()
    return {ProviderFeature.AUDIO_SOURCE}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    mode = cast("str | None", config.get_value(CONF_PLAYBACK_MODE)) or PLAYBACK_MODE_STREAM
    features = _features_for_mode(mode)
    return YandexYnisonProvider(mass, manifest, config, features)


async def get_config_entries(  # noqa: PLR0915 — flow naturally returns ~12 ConfigEntry objects
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001 — required by MA callback signature
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    if values is None:
        values = {}

    # Migrate legacy config keys (renamed in v1.5.0)
    if "player" in values and CONF_MASS_PLAYER_ID not in values:
        values[CONF_MASS_PLAYER_ID] = values.pop("player")
    if "display_name" in values and CONF_PUBLISH_NAME not in values:
        values[CONF_PUBLISH_NAME] = values.pop("display_name")

    # Discover available yandex_music instances for borrow-mode dropdown
    ym_instances = list_yandex_music_instances(mass)
    ym_instance_ids = {inst_id for inst_id, _ in ym_instances}

    # Determine the currently selected source (borrow vs own)
    selected = cast("str | None", values.get(CONF_YM_INSTANCE))
    if selected is None:
        # Preserve existing own-token configs on upgrade (CONF_TOKEN already set
        # but CONF_YM_INSTANCE absent). Only auto-select borrowing for truly
        # fresh installs with no stored token and exactly one YM instance.
        has_manual_token = bool(values.get(CONF_TOKEN))
        if has_manual_token:
            selected = YM_INSTANCE_OWN
        else:
            selected = ym_instances[0][0] if len(ym_instances) == 1 else YM_INSTANCE_OWN
    # Normalize a stale selection (referenced YM instance was removed) up front
    # so borrowing/label/default downstream read consistent values, and a Save
    # without touching the dropdown persists the corrected id.
    if selected != YM_INSTANCE_OWN and selected not in ym_instance_ids:
        selected = YM_INSTANCE_OWN
        values[CONF_YM_INSTANCE] = YM_INSTANCE_OWN
    borrowing = selected != YM_INSTANCE_OWN

    # ------------------------------------------------------------------
    # Own-mode action handling: QR login / reset auth
    # ------------------------------------------------------------------
    # The buttons are only surfaced in own mode, but the action callback is
    # invoked with whatever `values` the frontend has cached — guard against
    # a stale-state save that fires the action while the dropdown points at
    # a (possibly missing) yandex_music instance.  Otherwise we'd overwrite
    # token/x_token in a config that won't even use them.
    remember_session = bool(values.get(CONF_REMEMBER_SESSION, True))
    if action in (CONF_ACTION_AUTH_QR, CONF_ACTION_CLEAR_AUTH) and selected != YM_INSTANCE_OWN:
        raise LoginFailed(
            f"Cannot run own-mode action '{action}' while the source is set to "
            f"'{selected}'. Switch the dropdown to 'Use own credentials' first."
        )
    if action == CONF_ACTION_AUTH_QR:
        session_id = values.get("session_id")
        if not session_id:
            raise LoginFailed("Missing session_id for QR authentication")
        x_token, music_token, display_login = await perform_qr_auth(mass, str(session_id))
        values[CONF_TOKEN] = music_token
        values[CONF_X_TOKEN] = x_token if remember_session else None
        values[CONF_ACCOUNT_LOGIN] = display_login
    elif action == CONF_ACTION_CLEAR_AUTH:
        values[CONF_TOKEN] = None
        values[CONF_X_TOKEN] = None
        values[CONF_ACCOUNT_LOGIN] = None

    # In own mode, treat presence of a music token OR a stored x_token as
    # "authenticated" — both can drive the connection (token directly, or
    # x_token via in-memory refresh).
    own_authenticated = bool(values.get(CONF_TOKEN) or values.get(CONF_X_TOKEN))
    account_login = cast("str | None", values.get(CONF_ACCOUNT_LOGIN))

    # ------------------------------------------------------------------
    # Status label
    # ------------------------------------------------------------------
    if borrowing:
        ym_name = next((name for inst_id, name in ym_instances if inst_id == selected), selected)
        label_text = f"Borrowing credentials from Yandex Music instance '{ym_name}'."
    elif action == CONF_ACTION_AUTH_QR:
        who = f" as {account_login}" if account_login else ""
        label_text = f"Authenticated to Yandex Music{who}. Don't forget to save to complete setup."
    elif own_authenticated:
        who = f" as {account_login}" if account_login else ""
        label_text = f"Authenticated to Yandex Music{who}."
    else:
        label_text = (
            "Not authenticated. Click 'Login with QR code' to scan with the "
            "Yandex app, or paste a music token manually below."
        )

    # Build dropdown options: one per YM instance + "Use own credentials" sentinel
    source_options = [
        ConfigValueOption(f"Yandex Music: {name}", inst_id) for inst_id, name in ym_instances
    ]
    source_options.append(ConfigValueOption("Use own credentials (QR or token)", YM_INSTANCE_OWN))

    # `selected` is normalized above, so it is always either a known instance
    # id (borrowing) or YM_INSTANCE_OWN — safe to use directly as the default.
    dropdown_default = selected

    # Own-mode-only entries are hidden when borrowing.
    own_hidden = borrowing
    # Stream-only UI integration toggle: hide in handoff (it would have no
    # effect there). The form re-renders when CONF_PLAYBACK_MODE changes,
    # so reading values[CONF_PLAYBACK_MODE] gives the live selection.
    selected_mode = cast("str | None", values.get(CONF_PLAYBACK_MODE)) or PLAYBACK_MODE_STREAM
    ui_integration_hidden = selected_mode == PLAYBACK_MODE_HANDOFF
    # Token field requirement: in own mode it's only required when there's no
    # alternative path (no stored x_token to refresh from).
    token_required = not borrowing and not bool(values.get(CONF_X_TOKEN))

    return (
        ConfigEntry(
            key="label_text",
            type=ConfigEntryType.LABEL,
            label=label_text,
        ),
        ConfigEntry(
            key=CONF_YM_INSTANCE,
            type=ConfigEntryType.STRING,
            label="Yandex Music source",
            description="Borrow OAuth credentials from a linked Yandex Music provider "
            "instance, or use your own credentials (QR-scan login or manual token paste). "
            "Per-instance own credentials let you bind separate players to separate "
            "Yandex accounts without sharing tokens with a Yandex Music provider.",
            options=source_options,
            default_value=dropdown_default,
            required=True,
        ),
        # Own-mode: QR login button
        ConfigEntry(
            key=CONF_ACTION_AUTH_QR,
            type=ConfigEntryType.ACTION,
            label="Login with QR code",
            description="Open a QR code in a popup and scan it with the Yandex app on "
            "your phone. Populates the token automatically — no manual paste needed.",
            action=CONF_ACTION_AUTH_QR,
            action_label="Login with QR code",
            hidden=own_hidden or own_authenticated,
        ),
        # Own-mode: remember-session toggle
        ConfigEntry(
            key=CONF_REMEMBER_SESSION,
            type=ConfigEntryType.BOOLEAN,
            label="Remember session (auto-refresh token)",
            description="Store a long-lived session token (x_token) alongside the music "
            "token so this plugin can refresh on its own when the token expires. "
            "Disable to keep only the short-lived music token (re-QR required on expiry).",
            default_value=True,
            hidden=own_hidden or own_authenticated,
        ),
        # Own-mode: reset authentication
        ConfigEntry(
            key=CONF_ACTION_CLEAR_AUTH,
            type=ConfigEntryType.ACTION,
            label="Reset authentication",
            description="Clear the current authentication details "
            "(music token, session token, and stored login).",
            action=CONF_ACTION_CLEAR_AUTH,
            action_label="Reset authentication",
            hidden=own_hidden or not own_authenticated,
        ),
        ConfigEntry(
            key=CONF_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Yandex Music Token",
            description="Manually pasted Yandex Music OAuth token. Populated "
            "automatically after a successful QR login; only fill in by hand if "
            "you can't use QR (e.g. headless setup).",
            required=token_required,
            hidden=borrowing,
            value=cast("str", values.get(CONF_TOKEN)) if values else None,
        ),
        # Hidden: long-lived session token used for reactive 401 refresh
        ConfigEntry(
            key=CONF_X_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Session token (x_token)",
            hidden=True,
            required=False,
            value=cast("str", values.get(CONF_X_TOKEN)) if values else None,
        ),
        # Hidden: cached display login for the status label
        ConfigEntry(
            key=CONF_ACCOUNT_LOGIN,
            type=ConfigEntryType.STRING,
            label="Account login",
            hidden=True,
            required=False,
            value=cast("str", values.get(CONF_ACCOUNT_LOGIN)) if values else None,
        ),
        ConfigEntry(
            key=CONF_MASS_PLAYER_ID,
            type=ConfigEntryType.STRING,
            label="Connected Music Assistant Player",
            description="The Music Assistant player connected to this Ynison plugin. "
            "When playback is directed to this device in the Yandex Music app, "
            "the audio will play on the selected player. "
            "Set to 'Auto' to automatically select a currently playing player.",
            default_value=PLAYER_ID_AUTO,
            options=[
                ConfigValueOption("Auto (prefer playing player)", PLAYER_ID_AUTO),
                *(
                    ConfigValueOption(x.display_name, x.player_id)
                    for x in sorted(
                        mass.players.all_players(False, False),
                        key=lambda p: p.display_name.lower(),
                    )
                ),
            ],
            required=True,
        ),
        ConfigEntry(
            key=CONF_ALLOW_PLAYER_SWITCH,
            type=ConfigEntryType.BOOLEAN,
            label="Allow manual player switching",
            description="When enabled, you can select this plugin as a source on any player "
            "to switch playback to that player. When disabled, playback is fixed to the "
            "configured default player.",
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_OUTPUT_SAMPLE_RATE,
            type=ConfigEntryType.STRING,
            label="Output sample rate",
            description="Sample rate for PCM output to the player. "
            "'Auto' selects 44.1 kHz for lossy or 48 kHz for lossless sources.",
            default_value=OUTPUT_AUTO,
            options=[
                ConfigValueOption("Auto (from source quality)", OUTPUT_AUTO),
                ConfigValueOption("44100 Hz (CD)", "44100"),
                ConfigValueOption("48000 Hz", "48000"),
                ConfigValueOption("96000 Hz (Hi-Res)", "96000"),
            ],
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_OUTPUT_BIT_DEPTH,
            type=ConfigEntryType.STRING,
            label="Output bit depth",
            description="Bit depth for PCM output to the player. "
            "'Auto' selects 16-bit for lossy or 24-bit for lossless sources.",
            default_value=OUTPUT_AUTO,
            options=[
                ConfigValueOption("Auto (from source quality)", OUTPUT_AUTO),
                ConfigValueOption("16-bit", "16"),
                ConfigValueOption("24-bit", "24"),
            ],
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_PUBLISH_NAME,
            type=ConfigEntryType.STRING,
            label="Device name in Yandex Music",
            description="How this device appears in the Yandex Music app.",
            default_value=DEFAULT_DISPLAY_NAME,
        ),
        ConfigEntry(
            key=CONF_PLAYBACK_MODE,
            type=ConfigEntryType.STRING,
            label="Playback mode (experimental)",
            description=(
                "How audio reaches the player when a track is selected from the "
                "Yandex Music app.\n\n"
                "Stream (default): the plugin acts as an audio source — it streams "
                "PCM into Music Assistant, which then forwards to the player. "
                "Stable, but adds an extra ffmpeg in the pipeline.\n\n"
                "Handoff (experimental): the plugin pushes the chosen track into "
                "Music Assistant's player queue and lets MA stream it natively "
                "through the linked Yandex Music provider — no extra ffmpeg, no "
                "PCM resampling. Spotify Connect intentionally avoids this mode "
                "(see CONF_HANDOFF_MODE in their provider) for the looser sync "
                "trade-off described below.\n\n"
                "Handoff requires a working `yandex_music` music provider in MA — "
                "without it, play_media() will fail to resolve track URIs.\n\n"
                "In handoff, the MA player queue is owned by Ynison: starting "
                "playback from the Yandex Music app will REPLACE any queue you "
                "built manually in the MA UI, without warning.\n\n"
                "Audio quality (Hi-Res / lossless) in handoff depends on the "
                "linked yandex_music provider's quality setting, not on this "
                "plugin's output_sample_rate / output_bit_depth (those apply "
                "only to stream mode)."
            ),
            default_value=PLAYBACK_MODE_STREAM,
            options=[
                ConfigValueOption("Stream (recommended)", PLAYBACK_MODE_STREAM),
                ConfigValueOption("Handoff (experimental)", PLAYBACK_MODE_HANDOFF),
            ],
        ),
        ConfigEntry(
            key=CONF_ENABLE_UI_INTEGRATION,
            type=ConfigEntryType.BOOLEAN,
            label="Show full player card in MA UI (experimental)",
            description=(
                "Stream mode only. When enabled, the plugin publishes a "
                "frontend-only fake queue under its own id and stamps the "
                "player's output_format so MA's UI renders the seek bar, "
                "signal-chain panel, and quality indicator — the same player "
                "card you get when MA streams its own queue.\n\n"
                "Off by default because the integration relies on private "
                "frontend behaviours that may break across MA versions, can "
                "interfere with 'Play Now' on local content while this source "
                "is active (the click is routed to a queue id the backend "
                "doesn't own, and errors), and may cause brief signal-chain "
                "flicker at track start before the source format is known.\n\n"
                "Multi-user limitation: for users with a restricted "
                "`player_filter` on their websocket session, MA's event gate "
                "drops QUEUE_* events whose `object_id` is not one of the "
                "allowed player ids. Our fake-queue events use the plugin "
                "instance id as `object_id`, so the player card may render "
                "empty for restricted users. Admin / unrestricted users are "
                "unaffected.\n\n"
                "Ignored in handoff mode — MA already owns a real queue there."
            ),
            default_value=False,
            advanced=True,
            hidden=ui_integration_hidden,
        ),
        ConfigEntry(
            key=CONF_HANDOFF_HEARTBEAT_INTERVAL,
            type=ConfigEntryType.STRING,
            label="Handoff progress heartbeat (seconds)",
            description=(
                "How often (in seconds) the plugin pushes a fresh `update_playing_status` "
                "to Ynison while in handoff mode, regardless of MA queue events. Guards "
                "against the Ynison server re-balancing the active device away from us "
                "when the player generates QUEUE_TIME_UPDATED events sparsely (typical "
                "for DLNA/UPnP renderers). Ignored in stream mode."
            ),
            default_value=str(int(HANDOFF_HEARTBEAT_DEFAULT)),
            options=[
                ConfigValueOption("3 seconds (aggressive)", "3"),
                ConfigValueOption("5 seconds (default)", "5"),
                ConfigValueOption("7 seconds", "7"),
                ConfigValueOption("10 seconds (conservative)", "10"),
            ],
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_DEVICE_ID,
            type=ConfigEntryType.STRING,
            label="Device ID",
            hidden=True,
            required=False,
        ),
    )
