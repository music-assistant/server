"""Yandex Music provider support for Music Assistant."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, ProviderFeature
from music_assistant_models.errors import InvalidDataError

from .auth import perform_device_auth, perform_qr_auth
from .constants import (
    CONF_ACTION_AUTH_DEVICE,
    CONF_ACTION_AUTH_QR,
    CONF_ACTION_CLEAR_AUTH,
    CONF_ACTION_DELETE_WAVE_PRESET,
    CONF_ACTION_SAVE_WAVE_PRESET,
    CONF_BASE_URL,
    CONF_LIKED_TRACKS_MAX_TRACKS,
    CONF_MY_WAVE_MAX_TRACKS,
    CONF_QUALITY,
    CONF_REFRESH_TOKEN,
    CONF_REMEMBER_SESSION,
    CONF_RESTRICTIVE_RATE_LIMITS,
    CONF_SESSION_ID,
    CONF_TOKEN,
    CONF_WAVE_PRESET_DRAFT_DIVERSITY,
    CONF_WAVE_PRESET_DRAFT_LANGUAGE,
    CONF_WAVE_PRESET_DRAFT_MOOD,
    CONF_WAVE_PRESET_DRAFT_NAME,
    CONF_WAVE_PRESET_TO_DELETE,
    CONF_WAVE_PRESETS_DATA,
    CONF_X_TOKEN,
    DEFAULT_BASE_URL,
    QUALITY_BALANCED,
    QUALITY_EFFICIENT,
    QUALITY_HIGH,
    QUALITY_SUPERB,
    WAVE_PRESET_DIVERSITY_VALUES,
    WAVE_PRESET_LANGUAGE_VALUES,
    WAVE_PRESET_MOOD_VALUES,
)
from .presets import parse_stored_presets as _parse_stored_presets
from .provider import YandexMusicProvider


def _save_wave_preset_action(values: dict[str, ConfigValueType]) -> None:
    """Merge the current draft fields into the stored preset list.

    Overwrites an existing preset with the same name instead of creating a
    duplicate. Clears draft fields after persisting so the UI returns to a
    blank state. Raises ``InvalidDataError`` when the name is blank.
    """
    name_raw = values.get(CONF_WAVE_PRESET_DRAFT_NAME)
    name = name_raw.strip() if isinstance(name_raw, str) else ""
    if not name:
        raise InvalidDataError("Please fill the preset name before saving.")
    presets = _parse_stored_presets(values.get(CONF_WAVE_PRESETS_DATA))
    presets = [p for p in presets if p["name"] != name]
    new_preset: dict[str, str] = {"name": name}
    for conf_key, api_key in (
        (CONF_WAVE_PRESET_DRAFT_DIVERSITY, "diversity"),
        (CONF_WAVE_PRESET_DRAFT_MOOD, "moodEnergy"),
        (CONF_WAVE_PRESET_DRAFT_LANGUAGE, "language"),
    ):
        val = values.get(conf_key)
        if isinstance(val, str) and val:
            new_preset[api_key] = val
    presets.append(new_preset)
    values[CONF_WAVE_PRESETS_DATA] = json.dumps(presets, ensure_ascii=False)
    # Clear draft so the UI is ready for the next preset
    values[CONF_WAVE_PRESET_DRAFT_NAME] = None
    values[CONF_WAVE_PRESET_DRAFT_DIVERSITY] = ""
    values[CONF_WAVE_PRESET_DRAFT_MOOD] = ""
    values[CONF_WAVE_PRESET_DRAFT_LANGUAGE] = ""


def _delete_wave_preset_action(values: dict[str, ConfigValueType]) -> None:
    """Remove the preset named by CONF_WAVE_PRESET_TO_DELETE from the store.

    Raises ``InvalidDataError`` when no name is selected. Idempotent — absent
    names simply rewrite an unchanged list.
    """
    target_raw = values.get(CONF_WAVE_PRESET_TO_DELETE)
    target = target_raw.strip() if isinstance(target_raw, str) else ""
    if not target:
        raise InvalidDataError("Please select a preset to delete.")
    presets = _parse_stored_presets(values.get(CONF_WAVE_PRESETS_DATA))
    presets = [p for p in presets if p["name"] != target]
    values[CONF_WAVE_PRESETS_DATA] = json.dumps(presets, ensure_ascii=False)
    values[CONF_WAVE_PRESET_TO_DELETE] = ""


def _wave_preset_config_entries(values: dict[str, ConfigValueType]) -> list[ConfigEntry]:
    """Return the wave-preset builder UI (all advanced settings).

    Layout:
      - Section label showing how many presets are saved.
      - Four "draft" fields (name + three dropdowns) the user fills in.
      - "Save preset" action → copies draft into the JSON store.
      - "Delete preset" dropdown + action (hidden when no presets exist).
      - Hidden STRING carrying the JSON store itself.

    Number of presets is unbounded; the user never edits JSON directly.
    """
    empty_title = "— Default —"
    diversity_options = [
        ConfigValueOption(title=empty_title if not v else v.title(), value=v)
        for v in WAVE_PRESET_DIVERSITY_VALUES
    ]
    mood_options = [
        ConfigValueOption(title=empty_title if not v else v.title(), value=v)
        for v in WAVE_PRESET_MOOD_VALUES
    ]
    language_options = [
        ConfigValueOption(title=empty_title if not v else v.replace("-", " ").title(), value=v)
        for v in WAVE_PRESET_LANGUAGE_VALUES
    ]

    presets = _parse_stored_presets(values.get(CONF_WAVE_PRESETS_DATA))
    has_presets = bool(presets)
    delete_options = [ConfigValueOption(title=p["name"], value=p["name"]) for p in presets]
    if not delete_options:
        # Empty options can break some frontends; supply a no-op placeholder.
        delete_options = [ConfigValueOption(title="(no presets saved)", value="")]

    def _str_value(key: str) -> str | None:
        v = values.get(key)
        return v if isinstance(v, str) else None

    return [
        ConfigEntry(
            key="wave_preset_section_label",
            type=ConfigEntryType.LABEL,
            label=(f"My Wave presets ({len(presets)} saved)" if has_presets else "My Wave presets"),
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_WAVE_PRESET_DRAFT_NAME,
            type=ConfigEntryType.STRING,
            label="New preset name",
            description=(
                "Give the preset a short name, pick up to three dropdowns "
                "below and click Save. Saving the same name again overwrites."
            ),
            default_value=None,
            required=False,
            advanced=True,
            value=_str_value(CONF_WAVE_PRESET_DRAFT_NAME),
        ),
        ConfigEntry(
            key=CONF_WAVE_PRESET_DRAFT_DIVERSITY,
            type=ConfigEntryType.STRING,
            label="New preset: diversity",
            description="How broadly the wave explores.",
            options=diversity_options,
            default_value="",
            required=False,
            advanced=True,
            value=_str_value(CONF_WAVE_PRESET_DRAFT_DIVERSITY),
        ),
        ConfigEntry(
            key=CONF_WAVE_PRESET_DRAFT_MOOD,
            type=ConfigEntryType.STRING,
            label="New preset: mood",
            description="Energy and mood of the tracks.",
            options=mood_options,
            default_value="",
            required=False,
            advanced=True,
            value=_str_value(CONF_WAVE_PRESET_DRAFT_MOOD),
        ),
        ConfigEntry(
            key=CONF_WAVE_PRESET_DRAFT_LANGUAGE,
            type=ConfigEntryType.STRING,
            label="New preset: language",
            description="Lyrics language filter.",
            options=language_options,
            default_value="",
            required=False,
            advanced=True,
            value=_str_value(CONF_WAVE_PRESET_DRAFT_LANGUAGE),
        ),
        ConfigEntry(
            key=CONF_ACTION_SAVE_WAVE_PRESET,
            type=ConfigEntryType.ACTION,
            label="Save preset",
            description=(
                "Adds the values above to Saved presets. The list is shown "
                "under Radio then My Presets in Browse."
            ),
            action=CONF_ACTION_SAVE_WAVE_PRESET,
            action_label="Save preset",
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_WAVE_PRESET_TO_DELETE,
            type=ConfigEntryType.STRING,
            label="Select preset to delete",
            options=delete_options,
            default_value="",
            required=False,
            advanced=True,
            hidden=not has_presets,
            value=_str_value(CONF_WAVE_PRESET_TO_DELETE),
        ),
        ConfigEntry(
            key=CONF_ACTION_DELETE_WAVE_PRESET,
            type=ConfigEntryType.ACTION,
            label="Delete selected preset",
            description="Removes the selected preset from the Saved presets list.",
            action=CONF_ACTION_DELETE_WAVE_PRESET,
            action_label="Delete",
            advanced=True,
            hidden=not has_presets,
        ),
        ConfigEntry(
            key=CONF_WAVE_PRESETS_DATA,
            type=ConfigEntryType.STRING,
            label="Saved presets (internal)",
            default_value="",
            required=False,
            advanced=True,
            hidden=True,
            value=_str_value(CONF_WAVE_PRESETS_DATA) or "",
        ),
    ]


if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.LIBRARY_AUDIOBOOKS,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_ARTISTS_EDIT,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
    ProviderFeature.LIBRARY_TRACKS_EDIT,
    ProviderFeature.LIBRARY_PODCASTS_EDIT,
    ProviderFeature.LIBRARY_AUDIOBOOKS_EDIT,
    ProviderFeature.BROWSE,
    ProviderFeature.SIMILAR_TRACKS,
    ProviderFeature.SIMILAR_ARTISTS,
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.LYRICS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return YandexMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    if values is None:
        values = {}

    # Handle QR auth action
    if action == CONF_ACTION_AUTH_QR:
        session_id = values.get(CONF_SESSION_ID)
        if not session_id:
            raise InvalidDataError("Missing session_id for QR authentication")
        x_token, music_token = await perform_qr_auth(mass, str(session_id))
        values[CONF_TOKEN] = music_token
        if values.get(CONF_REMEMBER_SESSION, True):
            values[CONF_X_TOKEN] = x_token
        else:
            values[CONF_X_TOKEN] = None
        # QR flow never yields a refresh_token — clear any stale one from a
        # prior device-flow login so we don't leave dead credentials behind
        values[CONF_REFRESH_TOKEN] = None

    # Handle Device Flow auth action (yields x_token + refresh_token,
    # so we get silent auto-refresh on music-token AND x_token expiry)
    if action == CONF_ACTION_AUTH_DEVICE:
        session_id = values.get(CONF_SESSION_ID)
        if not session_id:
            raise InvalidDataError("Missing session_id for device authentication")
        x_token, music_token, refresh_token = await perform_device_auth(mass, str(session_id))
        values[CONF_TOKEN] = music_token
        if values.get(CONF_REMEMBER_SESSION, True):
            values[CONF_X_TOKEN] = x_token
            values[CONF_REFRESH_TOKEN] = refresh_token
        else:
            values[CONF_X_TOKEN] = None
            values[CONF_REFRESH_TOKEN] = None

    # Handle clear auth action
    if action == CONF_ACTION_CLEAR_AUTH:
        values[CONF_TOKEN] = None
        values[CONF_X_TOKEN] = None
        values[CONF_REFRESH_TOKEN] = None

    # Wave-preset save/delete actions mutate the hidden JSON store and clear
    # the draft / selection fields so the UI re-renders in a clean state.
    if action == CONF_ACTION_SAVE_WAVE_PRESET:
        _save_wave_preset_action(values)
    if action == CONF_ACTION_DELETE_WAVE_PRESET:
        _delete_wave_preset_action(values)

    # Check if user is authenticated
    is_authenticated = bool(values.get(CONF_TOKEN))

    # Dynamic label text
    if not is_authenticated:
        label_text = (
            "Open a verification URL on any device and enter the short code, "
            "or scan a QR code with the Yandex app on your phone.\n\n"
            "Alternatively, you can enter a music token manually in the advanced settings."
        )
    elif action in (CONF_ACTION_AUTH_QR, CONF_ACTION_AUTH_DEVICE):
        label_text = "Authenticated to Yandex Music. Don't forget to save to complete setup."
    else:
        label_text = "Authenticated to Yandex Music."

    return (
        # Status label
        ConfigEntry(
            key="label_text",
            type=ConfigEntryType.LABEL,
            label=label_text,
        ),
        # Device Flow authentication (primary)
        ConfigEntry(
            key=CONF_ACTION_AUTH_DEVICE,
            type=ConfigEntryType.ACTION,
            label="Login with device code",
            description=("Open a verification URL on any device and enter the short code."),
            action=CONF_ACTION_AUTH_DEVICE,
            action_label="Login with device code",
            hidden=is_authenticated,
        ),
        # QR authentication (alternative)
        ConfigEntry(
            key=CONF_ACTION_AUTH_QR,
            type=ConfigEntryType.ACTION,
            label="Login with QR code",
            description="Opens a QR code page — scan it with the Yandex app on your phone.",
            action=CONF_ACTION_AUTH_QR,
            action_label="Login with QR code",
            hidden=is_authenticated,
        ),
        # Remember session toggle
        ConfigEntry(
            key=CONF_REMEMBER_SESSION,
            type=ConfigEntryType.BOOLEAN,
            label="Remember session (auto-refresh token)",
            description="When enabled, stores a long-lived session token to automatically "
            "refresh your music token when it expires. When disabled, you must "
            "re-authenticate manually when the token expires.",
            default_value=True,
            hidden=is_authenticated,
        ),
        # Clear auth
        ConfigEntry(
            key=CONF_ACTION_CLEAR_AUTH,
            type=ConfigEntryType.ACTION,
            label="Reset authentication",
            description="Clear the current authentication details.",
            action=CONF_ACTION_CLEAR_AUTH,
            action_label="Reset authentication",
            hidden=not is_authenticated,
        ),
        # Token storage (populated by QR action or manual entry)
        ConfigEntry(
            key=CONF_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Yandex Music Token (manual)",
            description="Advanced: manually enter a music token. "
            "See the documentation for how to obtain it.",
            required=True,
            hidden=is_authenticated,
            advanced=True,
            value=cast("str", values.get(CONF_TOKEN)) if values else None,
        ),
        # x_token (internal storage, always hidden)
        ConfigEntry(
            key=CONF_X_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Session token",
            hidden=True,
            required=False,
            value=cast("str", values.get(CONF_X_TOKEN)) if values else None,
        ),
        # refresh_token (internal storage, always hidden — device flow only)
        ConfigEntry(
            key=CONF_REFRESH_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Refresh token",
            hidden=True,
            required=False,
            value=cast("str", values.get(CONF_REFRESH_TOKEN)) if values else None,
        ),
        # Quality
        ConfigEntry(
            key=CONF_QUALITY,
            type=ConfigEntryType.STRING,
            label="Audio quality",
            description="Select preferred audio quality.",
            options=[
                ConfigValueOption("Efficient (AAC ~64kbps)", QUALITY_EFFICIENT),
                ConfigValueOption("Balanced (AAC ~192kbps)", QUALITY_BALANCED),
                ConfigValueOption("High (MP3 ~320kbps)", QUALITY_HIGH),
                ConfigValueOption("Superb (FLAC Lossless)", QUALITY_SUPERB),
            ],
            default_value=QUALITY_BALANCED,
        ),
        # My Wave maximum tracks (advanced)
        ConfigEntry(
            key=CONF_MY_WAVE_MAX_TRACKS,
            type=ConfigEntryType.INTEGER,
            label="My Wave maximum tracks",
            description="Maximum number of tracks to fetch for My Wave playlist. "
            "Lower values load faster but provide fewer tracks. Default: 150.",
            range=(10, 1000),
            default_value=150,
            required=False,
            advanced=True,
        ),
        # User-defined wave presets: builder + save/delete actions (dynamic list)
        *_wave_preset_config_entries(values),
        # Liked Tracks maximum tracks (advanced)
        ConfigEntry(
            key=CONF_LIKED_TRACKS_MAX_TRACKS,
            type=ConfigEntryType.INTEGER,
            label="Liked Tracks maximum tracks",
            description="Maximum number of tracks to show in Liked Tracks virtual playlist. "
            "Higher values may significantly increase load time and risk "
            "triggering Yandex smart-captcha. Lower values load faster. "
            "Default: 200.",
            range=(50, 2000),
            default_value=200,
            required=False,
            advanced=True,
        ),
        # API Base URL (advanced)
        ConfigEntry(
            key=CONF_BASE_URL,
            type=ConfigEntryType.STRING,
            label="API Base URL",
            description="API endpoint base URL. "
            "Only change if Yandex Music changes their API endpoint. "
            f"Default: {DEFAULT_BASE_URL}",
            default_value=DEFAULT_BASE_URL,
            required=False,
            advanced=True,
        ),
        # Restrictive rate limits (advanced)
        ConfigEntry(
            key=CONF_RESTRICTIVE_RATE_LIMITS,
            type=ConfigEntryType.BOOLEAN,
            label="Restrictive rate limits (datacenter / VPN safe mode)",
            description=(
                "Enable when Music Assistant runs on a VPS, NAS-behind-VPN, "
                "or any datacenter IP. Caps total in-flight requests to "
                "Yandex below the edge concurrency limit observed for those "
                "IPs (~6 simultaneous), trading some browse / recommendations "
                "responsiveness for fewer captcha trips. Residential users "
                "do not need this — Yandex tolerates much higher concurrency "
                "from regular ISPs."
            ),
            default_value=False,
            required=False,
            advanced=True,
        ),
    )
