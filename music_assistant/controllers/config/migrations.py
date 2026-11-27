"""Settings (settings.json) migration logic for the config controller."""

from __future__ import annotations

import logging
import re
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

from music_assistant_models.constants import PLAYER_CONTROL_NATIVE, PLAYER_CONTROL_NONE
from music_assistant_models.enums import CrossfadeMode, TaskScheduleType
from music_assistant_models.errors import InvalidDataError

from music_assistant.constants import (
    CONF_AI,
    CONF_AI_DEFAULT_ENGINES_MIGRATED,
    CONF_AIRPLAY_SYNC_ADJUST_SIGN_MIGRATED,
    CONF_ANNOUNCE_TTS_ENGINE,
    CONF_APPLE_MUSIC_SYNC_SCHEDULE_MIGRATED,
    CONF_CACHED_ARP_MAC,
    CONF_CORE,
    CONF_CROSSFADE_DURATION,
    CONF_CROSSFADE_MODE,
    CONF_DEFAULT_IMAGE_ENGINE,
    CONF_DEFAULT_SPEECH_ENGINE,
    CONF_DEFAULT_TEXT_ENGINE,
    CONF_EMBY_ALBUM_ARTISTS_HEAL_MIGRATED,
    CONF_EMBY_ALBUM_ARTISTS_HEAL_PENDING,
    CONF_ENABLE_ICY_METADATA,
    CONF_ENABLED,
    CONF_ICON,
    CONF_MUTE_CONTROL,
    CONF_PLAYBACK_QUALITY,
    CONF_PLAYER_DSP,
    CONF_PLAYER_PROTOCOLS,
    CONF_PLAYER_QUEUES,
    CONF_PLAYERS,
    CONF_PREFERRED_OUTPUT_PROTOCOL,
    CONF_PROTOCOL_EXPERIMENTAL_NOTE,
    CONF_PROTOCOL_IDENTIFIERS,
    CONF_PROTOCOL_KEY_SPLITTER,
    CONF_PROTOCOL_OFF_BY_DEFAULT,
    CONF_PROVIDERS,
    CONF_RAUMFELD_ICY_BASIC_MIGRATED,
    CONF_REPORTED_MAC,
    CONF_SENDSPIN_ONE_OUTPUT_MIGRATED,
    CONF_SMART_FADES_MODE,
    CONF_SYNC_ADJUST,
    CONF_UNDERLYING_PLAYER_ID,
    CONF_VOLUME_CONTROL,
    CONF_VOLUME_NORMALIZATION,
    NON_HTTP_PROVIDERS,
)
from music_assistant.controllers.config.player_split import (
    LEGACY_LINKED_PROTOCOL_IDS,
    LEGACY_PROTOCOL_PARENT_ID,
    migrate_player_split,
    remove_player_records_of_older_versions,
)
from music_assistant.controllers.player_queues.constants import CONF_DEFAULT_ENQUEUE_SELECT_ARTIST
from music_assistant.controllers.tasks.constants import TASK_STATE_CONFIG_KEY
from music_assistant.helpers.config_entries import CONF_CONNECTED_PLAYERS

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant_models.config_entries import ConfigValueType

LOGGER = logging.getLogger(__name__)

# removed automatic-player-selection sentinel of the connected-player plugins, only
# referenced by their migration
LEGACY_PLAYER_ID_AUTO = "__auto__"

# shared prefix of the removed per-player Bose SoundTouch preset keys
LEGACY_BOSE_PRESET_KEY_PREFIX = "preset_"

# removed MSX Bridge provider config keys, only referenced by their migration
LEGACY_MSX_BRIDGE_KEYS = ("enable_player_grouping", "enable_sendspin_bridge")

# player id prefix of the Sendspin clients the removed MSX Sendspin bridge registered
LEGACY_MSX_SENDSPIN_BRIDGE_PREFIX = "spb_msx_"

# removed player HTTP profile config key, only referenced by its migrations
LEGACY_HTTP_PROFILE_KEY = "http_profile"

# removed player fixed content length config key, only referenced by its migration
LEGACY_FIXED_CONTENT_LENGTH_KEY = "requires_fixed_content_length"

# removed player output codec config key, only referenced by its migration
LEGACY_OUTPUT_CODEC_KEY = "output_codec"

# the playback quality level each value of the removed output codec setting belongs to
LEGACY_OUTPUT_CODEC_PLAYBACK_QUALITY = {
    "wav": "lossless_uncompressed",
    "pcm": "lossless_uncompressed",
    "flac": "lossless_compressed",
    "mp3": "compressed",
    "aac": "compressed",
}

# domains of the removed builtin providers, whose stored configs nothing can load anymore
RETIRED_BUILTIN_PROVIDER_DOMAINS = ("builtin_player", "universal_player", "sendspin_source")

# the order in which the Sendspin bridges took a device while each kept an output of its
# own, only referenced by the migration that folds those outputs into one
LEGACY_SENDSPIN_BRIDGE_ORDER = ("airplay", "squeezelite", "chromecast")

# values of a Sendspin output that the migration to one output per player reads and writes
_SENDSPIN_STATIC_DELAY = "sendspin_static_delay"
_SENDSPIN_BRIDGE_STATIC_DELAYS = "sendspin_bridge_static_delays"
_SENDSPIN_CAST_OPT_IN = "sendspin_cast_opt_in"
# value of a Cast protocol whose bridge had yet to switch its new output off
_LEGACY_SENDSPIN_OPT_OUT_PENDING = "sendspin_opt_out_pending"

# values of a Sendspin output that describe that output only, which no other one inherits
_SENDSPIN_OUTPUT_OWN_KEYS = frozenset(
    {
        CONF_CACHED_ARP_MAC,
        CONF_PROTOCOL_EXPERIMENTAL_NOTE,
        CONF_PROTOCOL_IDENTIFIERS,
        CONF_PROTOCOL_OFF_BY_DEFAULT,
        CONF_REPORTED_MAC,
        CONF_UNDERLYING_PLAYER_ID,
        _SENDSPIN_BRIDGE_STATIC_DELAYS,
        _SENDSPIN_CAST_OPT_IN,
        _SENDSPIN_STATIC_DELAY,
    }
)

# the engine each feature picked before every kind of engine got a default, as (the key of the
# default it seeds, provider domain or None for the players core module, section, key). Picks of
# the same kind are listed in the order that breaks a tie between equally used engines.
# TODO: remove after 2.12 release
FEATURE_ENGINE_PICKS: tuple[tuple[str, str | None, str, str], ...] = (
    (CONF_DEFAULT_SPEECH_ENGINE, None, "values", CONF_ANNOUNCE_TTS_ENGINE),
    (CONF_DEFAULT_SPEECH_ENGINE, "ai_radio", "setup_data", "tts_engine"),
    (CONF_DEFAULT_TEXT_ENGINE, "ai_radio", "setup_data", "ai_engine"),
    (CONF_DEFAULT_TEXT_ENGINE, "smart_playlist", "values", "ai_engine"),
    (CONF_DEFAULT_TEXT_ENGINE, "music_quiz", "values", "ai_engine"),
    (CONF_DEFAULT_TEXT_ENGINE, "playlist_metadata", "values", "ai_text_engine"),
    (CONF_DEFAULT_IMAGE_ENGINE, "playlist_metadata", "values", "ai_image_engine"),
)


# Canonical ids of the shared icon set (music-assistant/shared-icons v0.3.0);
# stored icon values already in this set are never touched by the icon migration.
_CANONICAL_ICON_IDS: frozenset[str] = frozenset(
    (
        "homepod-mini",
        "sonos",
        "mac",
        "apple-tv",
        "google-nest",
        "voice-pe",
        "wiim",
        "speaker",
        "speakers",
        "soundbar",
        "radio",
        "tv",
        "monitor",
        "laptop",
        "smartphone",
        "tablet",
        "headphones",
        "bluetooth",
        "airplay",
        "cast",
        "car",
        "music",
        "vinyl",
        "mic",
        "volume",
        "living-room",
        "bedroom",
        "bathroom",
        "toilet",
        "kitchen",
        "office",
        "hallway",
        "garden",
        "outdoor",
        "sun",
        "home",
        "building",
    )
)

# Legacy stored player icon values (mdi-* names and pre-1.0 picker names) mapped to
# the closest canonical id of the shared icon set. Sourced from
# https://github.com/music-assistant/shared-icons/blob/main/migration/legacy-map.json
_LEGACY_ICON_MAP: dict[str, str] = {
    "apple-homepod-mini": "homepod-mini",
    "appletv": "apple-tv",
    "armchair": "living-room",
    "audio-lines": "volume",
    "bath": "bathroom",
    "bed": "bedroom",
    "bed-double": "bedroom",
    "bed-single": "bedroom",
    "bluetooth-speaker": "bluetooth",
    "boom-box": "radio",
    "boombox": "radio",
    "briefcase": "office",
    "building-2": "building",
    "cassette-tape": "music",
    "chef-hat": "kitchen",
    "cooking-pot": "kitchen",
    "disc": "vinyl",
    "disc-2": "vinyl",
    "disc-3": "vinyl",
    "disc-album": "vinyl",
    "door-closed": "hallway",
    "door-open": "hallway",
    "drum": "music",
    "flower": "garden",
    "flower-2": "garden",
    "guitar": "music",
    "headset": "headphones",
    "homepod": "homepod-mini",
    "hotel": "building",
    "house": "home",
    "lamp-desk": "office",
    "lamp-floor": "living-room",
    "laptop-2": "laptop",
    "laptop-minimal": "laptop",
    "leaf": "garden",
    "mdi-airplay": "airplay",
    "mdi-album": "vinyl",
    "mdi-amplifier": "speaker",
    "mdi-antenna": "radio",
    "mdi-apple": "apple-tv",
    "mdi-apple-airplay": "airplay",
    "mdi-audio-video": "speaker",
    "mdi-audio-video-remote": "speaker",
    "mdi-balcony": "outdoor",
    "mdi-bathtub": "bathroom",
    "mdi-bathtub-outline": "bathroom",
    "mdi-bed": "bedroom",
    "mdi-bed-empty": "bedroom",
    "mdi-bed-king": "bedroom",
    "mdi-bed-queen": "bedroom",
    "mdi-bluetooth": "bluetooth",
    "mdi-bluetooth-audio": "bluetooth",
    "mdi-bookshelf": "office",
    "mdi-boombox": "radio",
    "mdi-briefcase": "office",
    "mdi-bullhorn": "volume",
    "mdi-bunk-bed": "bedroom",
    "mdi-car": "car",
    "mdi-car-estate": "car",
    "mdi-car-hatchback": "car",
    "mdi-car-side": "car",
    "mdi-cast": "cast",
    "mdi-cast-audio": "cast",
    "mdi-cast-connected": "cast",
    "mdi-cast-variant": "airplay",
    "mdi-cellphone": "smartphone",
    "mdi-cellphone-sound": "smartphone",
    "mdi-cellphone-wireless": "smartphone",
    "mdi-chair-rolling": "office",
    "mdi-chef-hat": "kitchen",
    "mdi-city": "building",
    "mdi-coat-rack": "hallway",
    "mdi-coffee": "kitchen",
    "mdi-coffee-maker": "kitchen",
    "mdi-countertop": "kitchen",
    "mdi-desk": "office",
    "mdi-desk-lamp": "office",
    "mdi-desktop-classic": "monitor",
    "mdi-desktop-mac": "mac",
    "mdi-desktop-tower": "monitor",
    "mdi-desktop-tower-monitor": "monitor",
    "mdi-disc": "vinyl",
    "mdi-disc-player": "vinyl",
    "mdi-domain": "building",
    "mdi-door": "hallway",
    "mdi-door-closed": "hallway",
    "mdi-door-open": "hallway",
    "mdi-earbuds": "headphones",
    "mdi-earbuds-outline": "headphones",
    "mdi-flower": "garden",
    "mdi-flower-outline": "garden",
    "mdi-flower-tulip": "garden",
    "mdi-forest": "outdoor",
    "mdi-fridge": "kitchen",
    "mdi-fridge-outline": "kitchen",
    "mdi-garage": "car",
    "mdi-garage-variant": "car",
    "mdi-google-assistant": "google-nest",
    "mdi-google-home": "google-nest",
    "mdi-grass": "garden",
    "mdi-grill": "outdoor",
    "mdi-guitar-acoustic": "music",
    "mdi-guitar-electric": "music",
    "mdi-headphones": "headphones",
    "mdi-headset": "headphones",
    "mdi-home": "home",
    "mdi-home-city": "building",
    "mdi-home-modern": "home",
    "mdi-home-outline": "home",
    "mdi-home-variant": "home",
    "mdi-hot-tub": "bathroom",
    "mdi-karaoke": "mic",
    "mdi-laptop": "laptop",
    "mdi-laptop-mac": "mac",
    "mdi-microphone": "mic",
    "mdi-microphone-variant": "mic",
    "mdi-monitor": "monitor",
    "mdi-monitor-speaker": "speaker",
    "mdi-music": "music",
    "mdi-music-box": "music",
    "mdi-music-circle": "music",
    "mdi-music-clef-treble": "music",
    "mdi-music-note": "music",
    "mdi-nature": "outdoor",
    "mdi-office-building": "building",
    "mdi-office-building-outline": "building",
    "mdi-palm-tree": "outdoor",
    "mdi-patio-heater": "outdoor",
    "mdi-piano": "music",
    "mdi-pine-tree": "outdoor",
    "mdi-podcast": "mic",
    "mdi-pool": "outdoor",
    "mdi-pot-steam": "kitchen",
    "mdi-projector": "tv",
    "mdi-projector-screen": "tv",
    "mdi-radio": "radio",
    "mdi-radio-tower": "radio",
    "mdi-record": "vinyl",
    "mdi-record-player": "vinyl",
    "mdi-saxophone": "music",
    "mdi-shower": "bathroom",
    "mdi-shower-head": "bathroom",
    "mdi-silverware": "kitchen",
    "mdi-silverware-fork": "kitchen",
    "mdi-silverware-fork-knife": "kitchen",
    "mdi-silverware-variant": "kitchen",
    "mdi-sofa": "living-room",
    "mdi-sofa-outline": "living-room",
    "mdi-sofa-single": "living-room",
    "mdi-soundbar": "soundbar",
    "mdi-speaker": "speaker",
    "mdi-speaker-bluetooth": "bluetooth",
    "mdi-speaker-multiple": "speakers",
    "mdi-speaker-wireless": "speaker",
    "mdi-sprout": "garden",
    "mdi-stairs": "hallway",
    "mdi-stove": "kitchen",
    "mdi-surround-sound": "speakers",
    "mdi-tablet": "tablet",
    "mdi-television": "tv",
    "mdi-television-box": "tv",
    "mdi-television-classic": "tv",
    "mdi-television-guide": "tv",
    "mdi-theater": "tv",
    "mdi-toilet": "toilet",
    "mdi-tree": "outdoor",
    "mdi-tree-outline": "outdoor",
    "mdi-truck": "car",
    "mdi-trumpet": "music",
    "mdi-turntable": "vinyl",
    "mdi-violin": "music",
    "mdi-volume-high": "volume",
    "mdi-volume-low": "volume",
    "mdi-volume-medium": "volume",
    "mdi-watering-can": "garden",
    "mdi-weather-sunny": "sun",
    "mdi-white-balance-sunny": "sun",
    "megaphone": "volume",
    "mic-vocal": "mic",
    "microphone": "mic",
    "microwave": "kitchen",
    "monitor-speaker": "speaker",
    "music-2": "music",
    "music-3": "music",
    "music-4": "music",
    "piano": "music",
    "podcast": "mic",
    "projector": "tv",
    "radio-receiver": "radio",
    "radio-tower": "radio",
    "refrigerator": "kitchen",
    "screen-share": "cast",
    "shower-head": "bathroom",
    "sofa": "living-room",
    "speaker-group": "speakers",
    "speaker-loud": "volume",
    "speaker-multiple": "speakers",
    "sprout": "garden",
    "television": "tv",
    "tent-tree": "outdoor",
    "toilet": "toilet",
    "tree": "outdoor",
    "tree-deciduous": "outdoor",
    "tree-pine": "outdoor",
    "trees": "outdoor",
    "tv-2": "tv",
    "tv-minimal": "tv",
    "tv-minimal-play": "tv",
    "utensils": "kitchen",
    "utensils-crossed": "kitchen",
    "volume-1": "volume",
    "volume-2": "volume",
}

# Config keys each provider's setup flow owns: the keys it reads back with
# get_setup_value / get_provider_setup_value (and rotates via _update_setup_data) from
# `setup_data` rather than `values`. The one-off migrate_provider_setup_data step below
# moves these keys from the raw `values` dict into `setup_data` (encrypting string values
# at rest) for installs that were configured before setup flows existed. Keys that stayed
# regular options (quality, sync toggles, output settings, ...) are intentionally absent:
# they keep being read via get_config_value from `values`.
# The literal strings are the persisted config keys, not the CONF_* symbol names; some
# providers redefine common names (e.g. the Hue bridge stores its user under
# "hue_username", the scrobblers under "_username", Open Subsonic its url under "baseURL").
# Notes on the non-obvious entries:
# - the filesystem providers' "content_type" is also surfaced by get_config_entries, but
#   only as a read-only mirror that carries the setup value as its default (so it is never
#   persisted back to values), so moving it to setup_data is safe and required (it is read
#   via get_provider_setup_value).
# - hass "url"/"token"/"verify_ssl": on a Home Assistant add-on these come from fixed
#   (hidden) config entries whose values equal what a stored copy would hold, so moving a
#   stored copy is a harmless no-op there while restoring normal installs.
# - plex_connect's "plex_provider_id"/"mass_player_id" are not secrets, but its setup flow
#   collects them (the options are only known from live server state), so they live in
#   setup_data like any other flow-collected value.
# - spotify's deprecated "refresh_token" stays in `values`: _migrate_legacy_token reads it
#   from there to split it into the global/dev keys, and clears it once it has. That read
#   runs in setup(), where seed_stored_config_values still exposes the stored values, so
#   moving this one key here would leave the split with nothing to read.
# TODO: remove after 2.13 release
PROVIDER_SETUP_FLOW_KEYS: dict[str, tuple[str, ...]] = {
    "alexa": ("url", "username", "password", "api_url", "api_username", "api_password"),
    "amplipi": ("host",),
    "apple_music": ("music_app_token", "music_user_token", "music_user_manual_token"),
    "airplay_receiver": ("mass_player_id", "airplay_name"),
    "ard_audiothek": ("email", "password", "token", "user_id", "token_expiry", "display_name"),
    "ariacast_receiver": ("mass_player_id",),
    "audible": ("auth_file", "locale"),
    "audiobookshelf": ("url", "username", "password", "token", "api_token", "verify_ssl"),
    "bandcamp": ("identity",),
    "bbc_sounds": ("username", "password"),
    "bose_soundtouch": ("app_key",),
    "deezer": ("arl_token",),
    "digitally_incorporated": ("listen_key",),
    "emby": ("ip_address", "username", "password"),
    "filesystem_google_drive": (
        "content_type",
        "client_id",
        "client_secret",
        "folder_id",
        "refresh_token",
    ),
    "filesystem_local": ("content_type", "path"),
    "filesystem_nfs": ("content_type", "host", "export_path", "subfolder", "nfs_version"),
    "filesystem_onedrive": (
        "content_type",
        "client_id",
        "client_secret",
        "folder_id",
        "refresh_token",
    ),
    "filesystem_smb": (
        "content_type",
        "host",
        "share",
        "username",
        "password",
        "subfolder",
        "smb_version",
    ),
    "gpodder": ("url", "username", "password", "device_id", "token", "url_nc", "verify_ssl"),
    "hass": ("url", "token", "verify_ssl"),
    "hue_entertainment": ("bridge_host", "hue_username", "hue_clientkey", "bridge_id"),
    "ibroadcast": ("username", "password"),
    "jellyfin": ("url", "username", "password", "verify_ssl"),
    "kion_music": ("token",),
    "lastfm_scrobble": ("_provider", "_api_session_key", "_username", "_api_key", "_api_secret"),
    "listenbrainz_scrobble": ("_user_token", "api_base_url"),
    "musicme": ("username", "password"),
    "neteasecloudmusic": ("api_base_url", "cookie", "uid"),
    "nicovideo": ("user_session",),
    "nugs": ("username", "password"),
    "opensubsonic": ("username", "password", "baseURL", "port", "path", "enable_legacy_auth"),
    "pandora": ("username", "password"),
    "plex": (
        "token",
        "local_server_ip",
        "local_server_port",
        "local_server_ssl",
        "local_server_verify_cert",
        "library_id",
        "library_type",
    ),
    "plex_connect": ("plex_provider_id", "mass_player_id"),
    "pocketcasts": ("username", "password"),
    "podcast_index": ("api_key", "api_secret"),
    "podcastfeed": ("feed_url",),
    "qobuz": ("username", "password"),
    "qqmusic": ("uin", "musicid", "musickey", "login_type", "credential_json"),
    "siriusxm": ("sxm_email_address", "sxm_password", "sxm_region"),
    "soundcloud": ("client_id", "authorization"),
    "spotify": (
        "refresh_token_global",
        "refresh_token_dev",
        "librespot_credentials",
        "client_id",
        "account_id",
    ),
    "spotify_connect": ("mass_player_id", "publish_name"),
    "teddycloud": ("url",),
    "tidal": ("auth_token", "refresh_token", "expiry_time", "user_id"),
    "tunein": ("username",),
    "vban_receiver": (
        "bind_ip",
        "bind_port",
        "sender_host",
        "vban_stream_name",
        "audio_format",
        "sample_rate",
        "audio_channels",
    ),
    "webdav": ("content_type", "url", "username", "password", "verify_ssl"),
    "yandex_music": ("token", "x_token", "refresh_token"),
    "yandex_smarthome": (
        "connection_type",
        "cloud_instance_id",
        "cloud_instance_password",
        "cloud_connection_token",
        "skill_id",
        "skill_token",
        "direct_access_token",
        "direct_client_secret",
    ),
    "yandex_station": (
        "ym_instance",
        "music_token",
        "x_token",
        "refresh_token",
        "remember_session",
    ),
    "yandex_ynison": ("ym_instance", "token", "x_token", "mass_player_id", "publish_name"),
    "yousee": ("username", "password"),
    "ytmusic": ("username", "cookie", "po_token_server_url"),
    "zvuk_music": ("token",),
}


# Fallback defaults for setup-flow keys that were never stored in the first place.
# A config value that matches its entry default is not persisted, so a key left at its
# default had nothing in `values` for the migration below to move and now reads back as
# None. These are the defaults those keys carried as config entries before the setup
# flows landed. Only keys whose read site has no fallback of its own are listed; the
# others already resolve their default at runtime. This runs on every startup, so only
# keys their setup flow persists unconditionally belong here - a key a flow may
# legitimately omit would be re-injected forever.
# TODO: remove after 2.13 release
PROVIDER_SETUP_FLOW_DEFAULTS: dict[str, dict[str, ConfigValueType]] = {
    "alexa": {"url": "amazon.com", "api_url": "http://localhost:5000"},
    "audiobookshelf": {"verify_ssl": True},
    "filesystem_local": {"path": "/media"},
    "filesystem_smb": {"subfolder": "", "smb_version": "3.0"},
    "jellyfin": {"verify_ssl": True},
    "lastfm_scrobble": {"_provider": "lastfm"},
    "plex": {
        "local_server_port": 32400,
        "local_server_ssl": False,
        "local_server_verify_cert": True,
    },
    "siriusxm": {"sxm_region": "US"},
}


async def migrate(data: dict[str, Any]) -> bool:  # noqa: C901, PLR0912, PLR0915
    """Migrate the persistent settings data in-place; return True if anything changed."""
    changed = False

    # Drop orphaned provider config stubs: a load failure could write last_error back to a
    # provider key whose config had already been removed (e.g. removing an unsupported provider
    # while a load/retry was still in flight), leaving an entry with only a last_error and no
    # 'domain'. Such stubs are dead data and crash get_provider_configs on startup.
    # TODO: remove after 2.11 release
    all_provider_configs = data.get(CONF_PROVIDERS, {})
    if isinstance(all_provider_configs, dict):
        orphaned = [
            instance_id
            for instance_id, cfg in all_provider_configs.items()
            if isinstance(cfg, dict) and "domain" not in cfg
        ]
        for instance_id in orphaned:
            del all_provider_configs[instance_id]
            LOGGER.warning("Removed orphaned provider config stub %s", instance_id)
            changed = True

    # Move queue-scoped settings (crossfade duration, volume normalization) from the per-player
    # config to the new per-queue config (queue_id == player_id, so the id maps 1:1).
    # TODO: remove after 2.11 release
    if _migrate_player_queue_settings(data):
        changed = True

    # Promote local_audio attribution stubs to regular players and fold the settings of
    # their (now obsolete) universal player wrappers back onto them.
    # TODO: remove after 2.11 release
    if _migrate_local_audio_attribution_stubs(data):
        changed = True

    # Give Apple TVs paired before native power control existed the current
    # default ("native") instead of the stale "none" that hid their power button.
    # TODO: remove after 2.11 release
    if _migrate_airplay_apple_power_control(data):
        changed = True

        # Move player-owned credential/pairing keys (AirPlay creds, Fully Kiosk / MPD password)
    # from the player config `values` into the player's encrypted `setup_data`, so those reads
    # switch to Player.get_setup_value now that pairing/credentials are owned by the setup flows.
    # TODO: remove after 2.11 release
    if _migrate_player_setup_data(data):
        changed = True

    # Drop the per-player Bose SoundTouch preset mappings; presets are now mapped once on
    # the provider config and shared by all its speakers.
    # TODO: remove after 2.11 release
    if _migrate_bose_soundtouch_presets(data):
        changed = True

    # Rewrite stored player icons from legacy values (mdi-* names and pre-1.0 picker
    # names) to canonical ids of the shared icon set; unmappable mdi-* picks drop
    # back to the player-type default.
    # TODO: remove after 2.12 release
    if _migrate_player_icons(data):
        changed = True

    # Drop the stored HTTP profile of Bluesound players; the setting is no longer offered
    # because BluOS only plays back correctly on the forced content length profile.
    # TODO: remove after 2.12 release
    if _migrate_bluesound_http_profile(data):
        changed = True

    # Clear the stored name of players that were never renamed, so an updated default
    # name is no longer shadowed by the auto-generated name stored at creation time.
    # TODO: remove after 2.12 release
    if _migrate_unrenamed_player_names(data):
        changed = True

    # Drop the retired MSX Bridge grouping and Sendspin bridge settings, move the removed
    # shared group stream mode to independent and remove the players of the Sendspin bridge.
    # TODO: remove after 2.12 release
    if _migrate_msx_bridge_settings(data):
        changed = True

    # Move Apple Music library sync tasks still on the former 12 hour default to 24 hours.
    # TODO: remove after 2.13 release
    if _migrate_apple_music_sync_schedule(data):
        changed = True

    # Mark Emby instances to unlink the featured track artists earlier syncs stored as album
    # artists; the provider heals and clears the marker on its next album sync.
    # TODO: remove after 2.13 release
    if _migrate_emby_album_artists_heal(data):
        changed = True

    # Drop the nicovideo mail/password/MFA credentials, which are no longer used since the
    # provider only supports logging in with a user_session cookie.
    # TODO: remove after 2.13 release
    if _migrate_nicovideo_drop_mail_login(data):
        changed = True

    # Drop the stored value of the removed MilkDrop visualizer color tint setting.
    # TODO: remove after 2.13 release
    if _migrate_milkdrop_drop_color_tint(data):
        changed = True

    # Move Raumfeld players still on the former "full" ICY default to "basic".
    # TODO: remove after 2.13 release
    if _migrate_raumfeld_icy_basic(data):
        changed = True

    # Move an artist selection that listed the artist's whole catalogue to the tracks of the
    # artist's albums in the library.
    # TODO: remove after 2.13 release
    if _migrate_artist_catalogue_selection(data):
        changed = True

    # Split the player configs into one record per player and one per protocol. This runs
    # after the steps above, which work on the records as they were before the split.
    if migrate_player_split(data):
        changed = True

    # Remove the player records an older version stored in the old shape after the split.
    if remove_player_records_of_older_versions(data):
        changed = True

    # Drop the stored value of the removed HTTP profile setting from every player and protocol.
    # It reads both trees, so it also reaches a config that was split before this step existed.
    # TODO: remove after 2.13 release
    if _migrate_drop_http_profile(data):
        changed = True

    # Move the stored value of the removed output codec setting to the playback quality level
    # of that codec. It reads both trees, so it also reaches a config that was split before.
    # TODO: remove after 2.13 release
    if _migrate_output_codec_to_playback_quality(data):
        changed = True

    # Negate the stored sync adjust of every AirPlay player, whose positive value now plays
    # earlier like it does on every other protocol. It reads the split trees, so it runs
    # after the split.
    # TODO: remove after 2.13 release
    if _migrate_airplay_sync_adjust_sign(data):
        changed = True

    # Remove the stored configs of the retired builtin providers, which the UI would
    # otherwise list as unavailable providers forever.
    # TODO: remove after 2.13 release
    if _migrate_remove_retired_builtin_providers(data):
        changed = True

    # Store every player that is stored as a stereo pair as a multi-channel player, the type
    # a set of speakers has. It reads the split tree, so it runs after the split.
    # TODO: remove after 2.13 release
    if _migrate_stereo_pair_player_type(data):
        changed = True

    # Give every player one Sendspin output for all its bridges: mark what a Cast bridge
    # left switched off as its default, and fold the outputs of a player that has several
    # into the one in use. It reads the split tree, so it runs after the split.
    # TODO: remove after 2.13 release
    if _migrate_sendspin_one_output(data):
        changed = True

    # Drop the stored value of the removed fixed content length setting from every player
    # and protocol.
    # TODO: remove after 2.13 release
    if _migrate_drop_fixed_content_length(data):
        changed = True

    return changed


def migrate_provider_setup_data(data: dict[str, Any], encrypt: Callable[[str], str]) -> bool:
    """
    Move each provider's setup-flow-owned keys from `values` to `setup_data` in-place.

    Also restores the keys listed in PROVIDER_SETUP_FLOW_DEFAULTS that are absent from
    `setup_data`, which covers the installs whose values were already moved by an
    earlier run of this step.

    Runs after encryption is initialized (unlike migrate()), so string values are
    encrypted at rest with the given callback - matching how the setup flows persist
    collected values. Returns True if anything changed.

    :param data: The persistent settings data to migrate in-place.
    :param encrypt: Callback that encrypts a string value (idempotent for already
        encrypted values), used to encrypt migrated string values at rest.
    """
    all_provider_configs = data.get(CONF_PROVIDERS, {})
    if not isinstance(all_provider_configs, dict):
        return False
    changed = False
    for provider_cfg in all_provider_configs.values():
        if not isinstance(provider_cfg, dict):
            continue
        domain = provider_cfg.get("domain", "")
        owned_keys = PROVIDER_SETUP_FLOW_KEYS.get(domain)
        if not owned_keys:
            continue
        values = provider_cfg.get("values")
        if not isinstance(values, dict):
            # a config without stored values has nothing to move, but may still
            # be missing a default
            values = {}
        movable_keys = [key for key in owned_keys if key in values]
        setup_data = provider_cfg.get("setup_data")
        if not isinstance(setup_data, dict):
            setup_data = {}
        # a key that is about to be moved carries the user's own value and is left alone
        missing_defaults = {
            key: value
            for key, value in PROVIDER_SETUP_FLOW_DEFAULTS.get(domain, {}).items()
            if key not in setup_data and key not in movable_keys
        }
        if not movable_keys and not missing_defaults:
            continue
        provider_cfg["setup_data"] = setup_data
        for key in movable_keys:
            # a value already collected into setup_data wins; only drop the stale copy
            if key not in setup_data:
                value = values[key]
                setup_data[key] = encrypt(value) if isinstance(value, str) else value
            del values[key]
        for key, value in missing_defaults.items():
            setup_data[key] = encrypt(value) if isinstance(value, str) else value
        changed = True
    if changed:
        LOGGER.info("Migrated provider setup values into setup_data")
    return changed


# TODO: remove after 2.12 release
def migrate_connected_player_plugins(
    data: dict[str, Any],
    decrypt: Callable[[str], str],
    storage_path: str,
) -> bool:
    """
    Move the connected-player plugins to the player-bound configuration model, once.

    spotify_connect and airplay_receiver are single-instance providers now, driven by a
    connected-players multi-select: existing instances collapse into one keyed by the
    domain, the explicitly configured players carry over into the multi-select and the
    per-instance device names are dropped (the advertised name follows the player now).
    For ariacast_receiver and yandex_ynison the connected player became mandatory: the
    removed automatic selection is cleared (so the provider fails into reconfigure) and
    their free-form device name keys are dropped. A configured player is kept whether
    a config is stored for it or not: a player that only exists while its device is
    connected has none.

    Runs after encryption is initialized, and after migrate_provider_setup_data so the
    pre-setup-flow values have landed in setup_data by now.

    :param data: The persistent settings data to migrate in-place.
    :param decrypt: Callback that decrypts a stored string value (a no-op for plain values).
    :param storage_path: The server storage path holding per-instance provider data dirs.
    """
    all_provider_configs = data.get(CONF_PROVIDERS, {})
    if not isinstance(all_provider_configs, dict):
        return False
    changed = False
    for domain in ("spotify_connect", "airplay_receiver"):
        if _collapse_connected_player_instances(
            all_provider_configs, domain, decrypt, storage_path
        ):
            changed = True
    if _clear_invalid_connected_players(all_provider_configs, decrypt):
        changed = True
    return changed


# TODO: remove after 2.12 release
def migrate_ai_default_engines(data: dict[str, Any], decrypt: Callable[[str], str]) -> bool:
    """
    Seed the default AI engines from the engines the features picked themselves, once.

    Each kind of engine (text, speech, image) has a default now, which a feature follows unless
    it picks an engine of its own. Every default that is not set yet is seeded with the engine
    most features of its kind picked, as long as the plugin providing it is still configured and
    enabled. A pick equal to the default of its kind is removed, so that feature follows the
    default; any other pick stays the feature's own choice. Guarded by
    CONF_AI_DEFAULT_ENGINES_MIGRATED, which is claimed even when nothing moved, so a pick equal
    to the default that is made afterwards is kept. Returns True when the settings were
    modified, including the first run's marker.

    Runs after encryption is initialized, since the AI Radio picks sit in its encrypted
    setup_data.

    :param data: The persistent settings data to migrate in-place.
    :param decrypt: Callback that decrypts a stored string value (a no-op for plain values).
    """
    if data.get(CONF_AI_DEFAULT_ENGINES_MIGRATED):
        return False
    picks = _read_feature_engine_picks(data, decrypt)
    defaults = _seed_default_engines(data, picks)
    followed = 0
    for default_key, section, key, uid in picks:
        if uid == defaults.get(default_key):
            del section[key]
            followed += 1
    if defaults:
        LOGGER.info(
            "Default AI engines: %s; %d feature(s) follow them now",
            ", ".join(f"{key}={uid}" for key, uid in defaults.items()),
            followed,
        )
    data[CONF_AI_DEFAULT_ENGINES_MIGRATED] = True
    return True


def _collapse_connected_player_instances(
    all_provider_configs: dict[str, Any],
    domain: str,
    decrypt: Callable[[str], str],
    storage_path: str,
) -> bool:
    """
    Collapse the instances of one per-player plugin domain into a single instance.

    :param all_provider_configs: The stored provider configurations, modified in-place.
    :param domain: The plugin domain to collapse (spotify_connect or airplay_receiver).
    :param decrypt: Callback that decrypts a stored string value.
    :param storage_path: The server storage path holding per-instance provider data dirs.
    """
    instances = {
        instance_id: provider_cfg
        for instance_id, provider_cfg in all_provider_configs.items()
        if isinstance(provider_cfg, dict) and provider_cfg.get("domain") == domain
    }
    if not instances:
        return False
    # already collapsed (or created post-change): a single instance keyed by the bare
    # domain can only originate from the new single-instance model — the legacy
    # multi-instance era always minted suffixed ids. This check is durable, unlike the
    # connected_players marker below, which the config store drops again when the
    # stored value equals the entry default (an empty selection).
    if len(instances) == 1 and domain in instances:
        return False
    if any(
        isinstance(cfg.get("values"), dict) and CONF_CONNECTED_PLAYERS in cfg["values"]
        for cfg in instances.values()
    ):
        # already collapsed by an earlier run
        return False
    # decrypt each instance's stored setup so the configured player and (for spotify)
    # the backend can be read; an unreadable instance contributes nothing. Ordering
    # follows the stored configs, so "first instance" ties resolve deterministically.
    decrypted: dict[str, dict[str, Any]] = {}
    for instance_id, provider_cfg in instances.items():
        setup_data = provider_cfg.get("setup_data")
        if not isinstance(setup_data, dict):
            decrypted[instance_id] = {}
            continue
        try:
            decrypted[instance_id] = {
                key: decrypt(value) if isinstance(value, str) else value
                for key, value in setup_data.items()
            }
        except InvalidDataError:
            LOGGER.warning(
                "Could not read the stored setup of %s; its configured player is not carried over",
                instance_id,
            )
    # a disabled instance must not decide the surviving config or contribute players:
    # its enabled flag becomes the whole provider's after the collapse
    enabled_ids = [iid for iid, cfg in instances.items() if cfg.get("enabled", True)]
    survivor_pool = enabled_ids or list(instances)
    # the soloist instance carries the API key and ToS consent, so it must be the one
    # that survives the collapse
    survivor_id = survivor_pool[0]
    if domain == "spotify_connect":
        survivor_id = next(
            (iid for iid in survivor_pool if decrypted.get(iid, {}).get("backend") == "soloist"),
            survivor_id,
        )
    # ordered de-duped carry-over of the explicitly configured players; the removed
    # automatic selection contributes nothing
    connected_players: list[str] = []
    soloist_player_ids: dict[str, str] = {}
    for instance_id in enabled_ids:
        player_id = decrypted.get(instance_id, {}).get("mass_player_id")
        if not isinstance(player_id, str) or player_id == LEGACY_PLAYER_ID_AUTO:
            continue
        if player_id not in connected_players:
            connected_players.append(player_id)
        if decrypted[instance_id].get("backend") == "soloist":
            soloist_player_ids[instance_id] = player_id
    survivor = instances[survivor_id]
    setup_data = survivor.get("setup_data")
    setup_data = setup_data if isinstance(setup_data, dict) else {}
    dropped_keys = [
        key
        for key in ("mass_player_id", "publish_name", "airplay_name")
        if setup_data.pop(key, None) is not None
    ]
    survivor["setup_data"] = setup_data
    values = survivor.get("values")
    values = values if isinstance(values, dict) else {}
    # always stored (even empty): doubles as this migration's idempotency marker
    values[CONF_CONNECTED_PLAYERS] = connected_players
    survivor["values"] = values
    survivor["instance_id"] = domain
    for instance_id in instances:
        del all_provider_configs[instance_id]
    all_provider_configs[domain] = survivor
    if len(instances) > 1 or connected_players or dropped_keys:
        LOGGER.warning(
            "Migrated %d %s configuration(s) into a single instance connected to %d "
            "player(s). The advertised device name now follows the connected player's name.",
            len(instances),
            domain,
            len(connected_players),
        )
    if domain == "spotify_connect":
        _move_soloist_data_dirs(storage_path, soloist_player_ids)
    return True


def _move_soloist_data_dirs(storage_path: str, soloist_player_ids: dict[str, str]) -> None:
    """
    Move per-instance soloist data dirs to their per-player location, best effort.

    A moved dir keeps the Spotify pairing of that player's device; a failed move only
    costs the user a re-pair in the Spotify app and never fails startup.

    :param storage_path: The server storage path.
    :param soloist_player_ids: Old soloist instance id mapped to its carried-over player id.
    """
    base_path = Path(storage_path) / "spotify_connect"
    for old_instance_id, player_id in soloist_player_ids.items():
        src = base_path / old_instance_id / "soloist-data"
        # matches the per-player identity key the provider derives its data dir from
        safe_player_id = re.sub(r"[^A-Za-z0-9_.-]", "_", player_id)
        dst = base_path / f"spotify_connect_{safe_player_id}" / "soloist-data"
        if not src.is_dir() or dst.exists():
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
        except OSError as err:
            LOGGER.warning(
                "Could not move the Spotify Connect (soloist) data of %s to %s: %s",
                old_instance_id,
                dst,
                err,
            )


def _clear_invalid_connected_players(
    all_provider_configs: dict[str, Any],
    decrypt: Callable[[str], str],
) -> bool:
    """
    Enforce the now-mandatory connected player on the single-player plugins.

    :param all_provider_configs: The stored provider configurations, modified in-place.
    :param decrypt: Callback that decrypts a stored string value.
    """
    changed = False
    for instance_id, provider_cfg in all_provider_configs.items():
        if not isinstance(provider_cfg, dict) or provider_cfg.get("domain") not in (
            "ariacast_receiver",
            "yandex_ynison",
        ):
            continue
        setup_data = provider_cfg.get("setup_data")
        if not isinstance(setup_data, dict):
            continue
        # the free-form device names are gone; the advertised name follows the player now
        for key in ("ariacast_name", "publish_name"):
            if key in setup_data:
                del setup_data[key]
                changed = True
        stored_player_id = setup_data.get("mass_player_id")
        if not isinstance(stored_player_id, str):
            continue
        try:
            player_id = decrypt(stored_player_id)
        except InvalidDataError:
            LOGGER.warning(
                "Could not read the configured player of %s; leaving it in place", instance_id
            )
            continue
        if player_id == LEGACY_PLAYER_ID_AUTO:
            del setup_data["mass_player_id"]
            changed = True
            LOGGER.warning(
                "The connected player of %s is no longer valid; open its settings and run "
                "the setup again to select a player",
                instance_id,
            )
    return changed


def _migrate_player_queue_settings(data: dict[str, Any]) -> bool:
    """Move queue-scoped settings from the per-player config to the per-queue config."""
    moved_keys = (
        CONF_CROSSFADE_DURATION,
        CONF_VOLUME_NORMALIZATION,
    )
    all_player_configs = data.get(CONF_PLAYERS, {})
    if not isinstance(all_player_configs, dict):
        return False
    changed = False
    for player_id, player_cfg in all_player_configs.items():
        if not isinstance(player_cfg, dict):
            continue
        player_values = player_cfg.get("values")
        if not isinstance(player_values, dict):
            continue
        to_move = {key: player_values[key] for key in moved_keys if key in player_values}
        # the legacy smart_fades_mode encoded both on/off and standard-vs-smart; the on/off is
        # now a runtime queue toggle, and standard/smart carries over to the crossfade_mode
        # select ("disabled" just means crossfade is off -> nothing to carry). Consume the key.
        legacy_mode = player_values.pop(CONF_SMART_FADES_MODE, None)
        migrated_mode = (
            legacy_mode
            if legacy_mode in (CrossfadeMode.STANDARD_CROSSFADE, CrossfadeMode.SMART_CROSSFADE)
            else None
        )
        if not to_move and legacy_mode is None:
            continue
        if to_move or migrated_mode is not None:
            queue_cfg = data.setdefault(CONF_PLAYER_QUEUES, {}).setdefault(
                player_id, {"queue_id": player_id}
            )
            queue_values = queue_cfg.setdefault("values", {})
            for key, value in to_move.items():
                # don't clobber an existing queue value if one was already stored
                queue_values.setdefault(key, value)
                del player_values[key]
            if migrated_mode is not None:
                queue_values.setdefault(CONF_CROSSFADE_MODE, migrated_mode)
        LOGGER.info("Migrated queue settings for %s", player_id)
        changed = True
    return changed


def _migrate_local_audio_attribution_stubs(data: dict[str, Any]) -> bool:
    """
    Promote local_audio attribution-stub players to regular players.

    The local_audio provider used to register a hidden PROTOCOL "attribution stub"
    per audio device, which got wrapped (together with the Sendspin bridge player)
    in an auto-created universal player. The stub is now a regular, visible player
    that parents the Sendspin bridge directly, making the universal player wrapper
    obsolete: its user settings move onto the stub's config (same bare device-uuid
    player_id) and the wrapper is removed.
    """
    all_player_configs = data.get(CONF_PLAYERS, {})
    if not isinstance(all_player_configs, dict):
        return False
    changed = False
    for player_id, player_cfg in list(all_player_configs.items()):
        if not isinstance(player_cfg, dict):
            continue
        if player_cfg.get("provider") != "local_audio":
            continue
        if player_cfg.get("player_type") != "protocol":
            continue
        player_cfg["player_type"] = "player"
        values = player_cfg.setdefault("values", {})
        values.pop(LEGACY_PROTOCOL_PARENT_ID, None)
        changed = True

        # the universal player wrapper was keyed on the stub's player_id
        # (the stub had no device identifiers to derive a device key from)
        universal_id = f"up{player_id.replace('-', '').lower()}"
        universal_cfg = all_player_configs.get(universal_id)
        if isinstance(universal_cfg, dict) and universal_cfg.get("provider") == "universal_player":
            del all_player_configs[universal_id]
            _absorb_universal_player_config(
                data, player_id, player_cfg, universal_id, universal_cfg
            )
            LOGGER.info(
                "Migrated universal player %s settings to local_audio player %s",
                universal_id,
                player_id,
            )
        LOGGER.info("Promoted local_audio player %s to a regular player", player_id)
    return changed


def _absorb_universal_player_config(
    data: dict[str, Any],
    player_id: str,
    player_cfg: dict[str, Any],
    universal_id: str,
    universal_cfg: dict[str, Any],
) -> None:
    """
    Fold the user settings of an obsolete universal player onto its replacement.

    The universal player was the visible device the user configured, so its
    settings win over anything stored on the (hidden) stub. Everything keyed on
    the old universal player_id (protocol parent links, queue settings, DSP
    config, group memberships) is re-pointed to the new player_id.
    """
    player_cfg["enabled"] = universal_cfg.get("enabled", True)
    # only carry an actual user rename, not the auto-generated default name
    if universal_cfg.get("name") and universal_cfg.get("name") != universal_cfg.get("default_name"):
        player_cfg["name"] = universal_cfg["name"]

    values = player_cfg.setdefault("values", {})
    universal_values = universal_cfg.get("values")
    universal_values = universal_values if isinstance(universal_values, dict) else {}
    # bookkeeping only relevant to the universal player wrapper itself
    internal_keys = (
        LEGACY_LINKED_PROTOCOL_IDS,
        LEGACY_PROTOCOL_PARENT_ID,
        "device_identifiers",
        "device_info",
    )
    for key, value in universal_values.items():
        if key in internal_keys:
            continue
        values[key] = value

    # carry the linked protocols (minus the stub itself, it is the parent now)
    # and re-point their cached parent so they restore fast on the next start
    linked_ids = [
        pid for pid in (universal_values.get(LEGACY_LINKED_PROTOCOL_IDS) or []) if pid != player_id
    ]
    if linked_ids:
        existing_ids = list(values.get(LEGACY_LINKED_PROTOCOL_IDS) or [])
        values[LEGACY_LINKED_PROTOCOL_IDS] = existing_ids + [
            pid for pid in linked_ids if pid not in existing_ids
        ]
    all_player_configs = data.get(CONF_PLAYERS, {})
    for protocol_id in linked_ids:
        protocol_cfg = all_player_configs.get(protocol_id)
        if not isinstance(protocol_cfg, dict):
            continue
        protocol_values = protocol_cfg.setdefault("values", {})
        if protocol_values.get(LEGACY_PROTOCOL_PARENT_ID) == universal_id:
            protocol_values[LEGACY_PROTOCOL_PARENT_ID] = player_id

    # move per-queue settings and DSP configuration to the new player_id
    for tree_key in (CONF_PLAYER_QUEUES, CONF_PLAYER_DSP):
        tree = data.get(tree_key)
        if isinstance(tree, dict) and universal_id in tree and player_id not in tree:
            tree[player_id] = tree.pop(universal_id)
            if tree_key == CONF_PLAYER_QUEUES and isinstance(tree[player_id], dict):
                tree[player_id]["queue_id"] = player_id

    # re-point group memberships that referenced the universal player
    for other_cfg in all_player_configs.values():
        if not isinstance(other_cfg, dict):
            continue
        other_values = other_cfg.get("values")
        if not isinstance(other_values, dict):
            continue
        for key in ("group_members", "allowed_members"):
            members = other_values.get(key)
            if isinstance(members, list) and universal_id in members:
                other_values[key] = [player_id if pid == universal_id else pid for pid in members]


def _migrate_airplay_apple_power_control(data: dict[str, Any]) -> bool:
    """
    Enable native power control for Apple TVs paired before the feature existed.

    Native on/off (Companion) power control was added to Apple TVs later, but
    players configured earlier kept the power_control default from that time
    ("none"), so the power button stayed hidden. Flip that stale default to
    "native" for paired Apple devices (those with Companion credentials, i.e.
    the ones that actually gained the feature); a device that turns out not to
    support power degrades back to "none" at runtime.
    """
    all_player_configs = data.get(CONF_PLAYERS, {})
    if not isinstance(all_player_configs, dict):
        return False
    changed = False
    for player_id, player_cfg in all_player_configs.items():
        if not isinstance(player_cfg, dict):
            continue
        if not str(player_cfg.get("provider", "")).startswith("airplay"):
            continue
        values = player_cfg.get("values")
        if not isinstance(values, dict) or not values.get("companion_credentials"):
            continue
        if values.get("power_control") != PLAYER_CONTROL_NONE:
            continue
        values["power_control"] = PLAYER_CONTROL_NATIVE
        LOGGER.info("Enabled native power control for paired Apple device %s", player_id)
        changed = True
    return changed


# the only HTTP profile BluOS devices play back correctly on
FORCED_HTTP_PROFILE = "forced_content_length"


def _migrate_bluesound_http_profile(data: dict[str, Any]) -> bool:
    """
    Drop a stored HTTP profile that Bluesound players can no longer select.

    BluOS keeps looping the audio on any profile other than the forced content length one,
    so the setting is no longer offered. A player left on another profile would stay broken
    with no way back, so that pick is removed.
    """
    all_player_configs = data.get(CONF_PLAYERS, {})
    if not isinstance(all_player_configs, dict):
        return False
    changed = False
    for player_cfg in all_player_configs.values():
        if not isinstance(player_cfg, dict):
            continue
        if not str(player_cfg.get("provider", "")).startswith("bluesound"):
            continue
        player_values = player_cfg.get("values")
        if not isinstance(player_values, dict):
            continue
        if player_values.get(LEGACY_HTTP_PROFILE_KEY, FORCED_HTTP_PROFILE) != FORCED_HTTP_PROFILE:
            del player_values[LEGACY_HTTP_PROFILE_KEY]
            changed = True
    if changed:
        LOGGER.info("Restored the required HTTP profile on the Bluesound player configuration(s)")
    return changed


def _migrate_drop_http_profile(data: dict[str, Any]) -> bool:
    """Drop the stored value of the removed HTTP profile setting from every player and protocol."""
    changed = _drop_player_setting(data, LEGACY_HTTP_PROFILE_KEY)
    if changed:
        LOGGER.info("Dropped the removed HTTP profile from the player configuration(s)")
    return changed


def _migrate_drop_fixed_content_length(data: dict[str, Any]) -> bool:
    """Drop the stored value of the removed fixed content length setting from every player."""
    changed = _drop_player_setting(data, LEGACY_FIXED_CONTENT_LENGTH_KEY)
    if changed:
        LOGGER.info(
            "Dropped the removed fixed content length setting from the player configuration(s)"
        )
    return changed


def _drop_player_setting(data: dict[str, Any], setting_key: str) -> bool:
    """
    Drop the stored value of a removed setting from every player and protocol record.

    :param data: The persistent settings data to migrate in-place.
    :param setting_key: The config key of the removed setting.
    :return: Whether a value was dropped.
    """
    # a player record can hold a copy of a protocol's value under a prefixed key: a parent
    # player stored one for a linked protocol player, and the split keeps the values of a
    # universal player on its player record
    protocol_key_suffix = f"{CONF_PROTOCOL_KEY_SPLITTER}{setting_key}"
    changed = False
    for tree in (CONF_PLAYERS, CONF_PLAYER_PROTOCOLS):
        records = data.get(tree)
        if not isinstance(records, dict):
            continue
        for record in records.values():
            if not isinstance(record, dict):
                continue
            values = record.get("values")
            if not isinstance(values, dict):
                continue
            for key in [
                key for key in values if key == setting_key or key.endswith(protocol_key_suffix)
            ]:
                del values[key]
                changed = True
    return changed


def _migrate_output_codec_to_playback_quality(data: dict[str, Any]) -> bool:
    """Move the stored output codec of every player and protocol to its playback quality level."""
    # a player record can hold a copy of a protocol's value under a prefixed key, like the
    # removed HTTP profile above
    protocol_key_suffix = f"{CONF_PROTOCOL_KEY_SPLITTER}{LEGACY_OUTPUT_CODEC_KEY}"
    changed = False
    for tree in (CONF_PLAYERS, CONF_PLAYER_PROTOCOLS):
        records = data.get(tree)
        if not isinstance(records, dict):
            continue
        for record in records.values():
            if not isinstance(record, dict):
                continue
            values = record.get("values")
            if not isinstance(values, dict):
                continue
            # the protocols of these providers never used their own output codec value
            skip_own_value = _provider_domain(data, record) in NON_HTTP_PROVIDERS
            for key in [
                key
                for key in values
                if key == LEGACY_OUTPUT_CODEC_KEY or key.endswith(protocol_key_suffix)
            ]:
                codec = values.pop(key)
                changed = True
                if skip_own_value and key == LEGACY_OUTPUT_CODEC_KEY:
                    continue
                if not isinstance(codec, str):
                    continue
                if (quality := LEGACY_OUTPUT_CODEC_PLAYBACK_QUALITY.get(codec)) is None:
                    continue
                quality_key = key.removesuffix(LEGACY_OUTPUT_CODEC_KEY) + CONF_PLAYBACK_QUALITY
                if values.get(quality_key) is None:
                    values[quality_key] = quality
    if changed:
        LOGGER.info("Moved the output codec to the playback quality in the player configuration(s)")
    return changed


def _migrate_unrenamed_player_names(data: dict[str, Any]) -> bool:
    """
    Clear the stored name of player configs that hold the default name verbatim.

    Player configs used to store the name a player was created with as both the custom
    and the default name, which makes a never-renamed player indistinguishable from a
    renamed one and lets the creation-time name shadow every later default name.
    """
    all_player_configs = data.get(CONF_PLAYERS, {})
    if not isinstance(all_player_configs, dict):
        return False
    changed = False
    for player_cfg in all_player_configs.values():
        if not isinstance(player_cfg, dict):
            continue
        # a config without a default name would be left without any name at all
        if not (default_name := player_cfg.get("default_name")):
            continue
        if player_cfg.get("name") != default_name:
            continue
        player_cfg["name"] = None
        changed = True
    return changed


def _migrate_msx_bridge_settings(data: dict[str, Any]) -> bool:
    """
    Migrate MSX Bridge configs to the settings and players of the current provider.

    The provider no longer offers player grouping, the Sendspin bridge or the shared group
    stream mode. Their stored values are dropped (shared moves to independent, so the
    setting holds a choice that can still be selected) and the Sendspin players the bridge
    registered per TV are removed, as nothing registers them anymore.
    """
    changed = False
    all_provider_configs = data.get(CONF_PROVIDERS)
    if isinstance(all_provider_configs, dict):
        for provider_cfg in all_provider_configs.values():
            if not isinstance(provider_cfg, dict) or provider_cfg.get("domain") != "msx_bridge":
                continue
            provider_values = provider_cfg.get("values")
            if not isinstance(provider_values, dict):
                continue
            for key in LEGACY_MSX_BRIDGE_KEYS:
                if key in provider_values:
                    del provider_values[key]
                    changed = True
            if provider_values.get("group_stream_mode") == "shared":
                provider_values["group_stream_mode"] = "independent"
                changed = True
    if changed:
        LOGGER.info("Removed the retired settings from the MSX Bridge provider configuration(s)")
    all_player_configs = data.get(CONF_PLAYERS)
    if not isinstance(all_player_configs, dict):
        return changed
    bridge_ids = [
        player_id
        for player_id in all_player_configs
        if isinstance(player_id, str) and player_id.startswith(LEGACY_MSX_SENDSPIN_BRIDGE_PREFIX)
    ]
    dsp_configs = data.get(CONF_PLAYER_DSP)
    for player_id in bridge_ids:
        del all_player_configs[player_id]
        if isinstance(dsp_configs, dict):
            dsp_configs.pop(player_id, None)
        LOGGER.info("Removed obsolete MSX Sendspin bridge player configuration %s", player_id)
        changed = True
    for player_cfg in all_player_configs.values():
        if not isinstance(player_cfg, dict):
            continue
        player_values = player_cfg.get("values")
        if not isinstance(player_values, dict):
            continue
        cached_ids = player_values.get(LEGACY_LINKED_PROTOCOL_IDS)
        if not isinstance(cached_ids, list):
            continue
        kept_ids = [
            pid
            for pid in cached_ids
            if not (isinstance(pid, str) and pid.startswith(LEGACY_MSX_SENDSPIN_BRIDGE_PREFIX))
        ]
        if len(kept_ids) != len(cached_ids):
            player_values[LEGACY_LINKED_PROTOCOL_IDS] = kept_ids
            changed = True
    return changed


def _migrate_bose_soundtouch_presets(data: dict[str, Any]) -> bool:
    """
    Remove the per-player Bose SoundTouch preset mappings.

    The physical preset buttons are now mapped once on the provider config, so the same
    button plays the same content on every speaker. The old per-player values are dropped
    rather than promoted: several speakers can hold conflicting mappings and there is no
    correct winner, so the user maps the buttons once more on the provider.
    """
    all_player_configs = data.get(CONF_PLAYERS, {})
    if not isinstance(all_player_configs, dict):
        return False
    changed = False
    for player_id, player_cfg in all_player_configs.items():
        if not isinstance(player_cfg, dict):
            continue
        if str(player_cfg.get("provider", "")).split("--", 1)[0] != "bose_soundtouch":
            continue
        values = player_cfg.get("values")
        if not isinstance(values, dict):
            continue
        preset_keys = [key for key in values if key.startswith(LEGACY_BOSE_PRESET_KEY_PREFIX)]
        if not preset_keys:
            continue
        for key in preset_keys:
            del values[key]
        LOGGER.info(
            "Removed the per-player preset mappings for Bose SoundTouch player %s; "
            "map the preset buttons on the provider settings instead",
            player_id,
        )
        changed = True
    return changed


_PLAYER_SETUP_DATA_KEYS: dict[str, tuple[str, ...]] = {
    "airplay": (
        "raop_credentials",
        "airplay_credentials",
        "companion_credentials",
        "mrp_credentials",
        "native_mrp_credentials",
    ),
    "fully_kiosk": ("password",),
    "mpd": ("password",),
}


_PLAYER_DEAD_SETUP_KEYS: dict[str, tuple[str, ...]] = {
    "airplay": ("ap2password",),
    "sendspin": ("source_approval_dismissed",),
}


def _migrate_player_setup_data(data: dict[str, Any]) -> bool:
    """
    Move player-owned credential/pairing keys from player `values` into `setup_data`.

    Idempotent (only moves a key still present in `values` and absent from `setup_data`)
    and multi-instance safe (matches on the player provider domain). Values are moved
    as-is: they are already encrypted SECURE_STRINGs, which is exactly the at-rest form
    setup_data expects. Also drops keys that are dead now (never read at runtime).
    """
    all_player_configs = data.get(CONF_PLAYERS, {})
    if not isinstance(all_player_configs, dict):
        return False
    changed = False
    for player_id, player_cfg in all_player_configs.items():
        if not isinstance(player_cfg, dict):
            continue
        domain = str(player_cfg.get("provider", "")).split("--", 1)[0]
        move_keys = _PLAYER_SETUP_DATA_KEYS.get(domain, ())
        dead_keys = _PLAYER_DEAD_SETUP_KEYS.get(domain, ())
        if not move_keys and not dead_keys:
            continue
        values = player_cfg.get("values")
        if not isinstance(values, dict):
            continue
        setup_data = player_cfg.get("setup_data")
        if not isinstance(setup_data, dict):
            setup_data = {}
        moved = False
        for key in move_keys:
            if key not in values:
                continue
            value = values.pop(key)
            moved = True
            # a stored null is just dropped; only real values move across
            if value is not None and key not in setup_data:
                setup_data[key] = value
        for key in dead_keys:
            if key in values:
                del values[key]
                moved = True
        if moved:
            if setup_data:
                player_cfg["setup_data"] = setup_data
            LOGGER.info(
                "Migrated credential/pairing values into setup_data for player %s", player_id
            )
            changed = True
    return changed


def _migrate_player_icons(data: dict[str, Any]) -> bool:
    """Rewrite legacy stored player icon values to canonical shared-icon-set ids."""
    all_player_configs = data.get(CONF_PLAYERS, {})
    if not isinstance(all_player_configs, dict):
        return False
    changed = False
    for player_id, player_cfg in all_player_configs.items():
        if not isinstance(player_cfg, dict):
            continue
        values = player_cfg.get("values")
        if not isinstance(values, dict):
            continue
        icon = values.get(CONF_ICON)
        if not isinstance(icon, str) or icon in _CANONICAL_ICON_IDS:
            continue
        if (replacement := _LEGACY_ICON_MAP.get(icon)) is not None:
            values[CONF_ICON] = replacement
            LOGGER.info("Migrated icon %s to %s for player %s", icon, replacement, player_id)
            changed = True
        elif icon.startswith("mdi-"):
            # no close equivalent in the shared icon set: drop the stored value
            # so the player-type default applies
            del values[CONF_ICON]
            LOGGER.info("Dropped legacy icon %s for player %s", icon, player_id)
            changed = True
        # any other unknown value is left in place: clients render the fallback icon
        # for unknown ids and the value may become a valid id in a future icon set
    return changed


def _migrate_apple_music_sync_schedule(data: dict[str, Any]) -> bool:
    """
    Move Apple Music library sync tasks still on the former 12 hour default to 24 hours, once.

    The tasks controller stores the full schedule of every scheduled task, so a changed default
    never reaches existing installs. Guarded by CONF_APPLE_MUSIC_SYNC_SCHEDULE_MIGRATED so a
    12 hour schedule the user picks afterwards is kept. Returns True when the settings were
    modified, including the first run's marker.
    """
    if data.get(CONF_APPLE_MUSIC_SYNC_SCHEDULE_MIGRATED):
        return False
    all_provider_configs = data.get(CONF_PROVIDERS, {})
    if not isinstance(all_provider_configs, dict):
        return False
    apple_sync_prefixes = tuple(
        f"music_sync_{instance_id}_"
        for instance_id, provider_cfg in all_provider_configs.items()
        if isinstance(provider_cfg, dict) and provider_cfg.get("domain") == "apple_music"
    )
    if not apple_sync_prefixes:
        return False
    core_cfg = data.get(CONF_CORE)
    tasks_cfg = core_cfg.get("tasks") if isinstance(core_cfg, dict) else None
    task_states = tasks_cfg.get(TASK_STATE_CONFIG_KEY) if isinstance(tasks_cfg, dict) else None
    if not isinstance(task_states, dict):
        task_states = {}
    migrated = 0
    for task_id, state in task_states.items():
        schedule = state.get("schedule") if isinstance(state, dict) else None
        if (
            task_id.startswith(apple_sync_prefixes)
            and isinstance(schedule, dict)
            and schedule.get("type") == TaskScheduleType.HOURLY.value
            and schedule.get("every") == 12
        ):
            schedule["every"] = 24
            migrated += 1
    if migrated:
        LOGGER.info("Moved %d Apple Music library sync task(s) to a daily schedule", migrated)
    # claim the marker even when nothing moved, so a 12 hour schedule picked later is kept
    data[CONF_APPLE_MUSIC_SYNC_SCHEDULE_MIGRATED] = True
    return True


def _migrate_emby_album_artists_heal(data: dict[str, Any]) -> bool:
    """Mark each Emby provider instance for a one-time album artist heal, once."""
    if data.get(CONF_EMBY_ALBUM_ARTISTS_HEAL_MIGRATED):
        return False
    all_provider_configs = data.get(CONF_PROVIDERS, {})
    if not isinstance(all_provider_configs, dict):
        return False
    marked = 0
    for provider_cfg in all_provider_configs.values():
        if not isinstance(provider_cfg, dict) or provider_cfg.get("domain") != "emby":
            continue
        values = provider_cfg.setdefault("values", {})
        if not isinstance(values, dict):
            continue
        values[CONF_EMBY_ALBUM_ARTISTS_HEAL_PENDING] = True
        marked += 1
    if not marked:
        return False
    LOGGER.info("Scheduled an album artist heal for %d Emby provider(s)", marked)
    data[CONF_EMBY_ALBUM_ARTISTS_HEAL_MIGRATED] = True
    return True


def _migrate_nicovideo_drop_mail_login(data: dict[str, Any]) -> bool:
    """Remove the stored mail, password and MFA code from every nicovideo provider instance."""
    all_provider_configs = data.get(CONF_PROVIDERS, {})
    if not isinstance(all_provider_configs, dict):
        return False
    dropped = 0
    for provider_cfg in all_provider_configs.values():
        if not isinstance(provider_cfg, dict) or provider_cfg.get("domain") != "nicovideo":
            continue
        for section in ("values", "setup_data"):
            stored = provider_cfg.get(section)
            if not isinstance(stored, dict):
                continue
            for key in ("mail", "password", "mfa"):
                if key in stored:
                    del stored[key]
                    dropped += 1
    if not dropped:
        return False
    LOGGER.info("Removed %d unused nicovideo mail login value(s)", dropped)
    return True


def _migrate_milkdrop_drop_color_tint(data: dict[str, Any]) -> bool:
    """Remove the stored color tint value from every milkdrop_visualizer provider instance."""
    all_provider_configs = data.get(CONF_PROVIDERS, {})
    if not isinstance(all_provider_configs, dict):
        return False
    dropped = 0
    for provider_cfg in all_provider_configs.values():
        if (
            not isinstance(provider_cfg, dict)
            or provider_cfg.get("domain") != "milkdrop_visualizer"
        ):
            continue
        stored = provider_cfg.get("values")
        if isinstance(stored, dict) and "color_tint" in stored:
            del stored["color_tint"]
            dropped += 1
    if not dropped:
        return False
    LOGGER.info("Removed %d unused milkdrop color tint value(s)", dropped)
    return True


def _migrate_raumfeld_icy_basic(data: dict[str, Any]) -> bool:
    """
    Move Raumfeld players still on the former "full" ICY default to "basic", once.

    The Raumfeld provider stored "full" for every room, so it cannot be told apart from a
    user's pick; the Raumfeld app does not use the cover link "full" adds. Guarded by
    CONF_RAUMFELD_ICY_BASIC_MIGRATED so a "full" the user picks afterwards is kept. Returns
    True when the settings were modified, including the first run's marker.
    """
    if data.get(CONF_RAUMFELD_ICY_BASIC_MIGRATED):
        return False
    all_player_configs = data.get(CONF_PLAYERS, {})
    if not isinstance(all_player_configs, dict):
        return False
    raumfeld_configs = [
        player_cfg
        for player_cfg in all_player_configs.values()
        if isinstance(player_cfg, dict) and player_cfg.get("provider") == "raumfeld"
    ]
    if not raumfeld_configs:
        return False
    migrated = 0
    for player_cfg in raumfeld_configs:
        player_values = player_cfg.get("values")
        if (
            isinstance(player_values, dict)
            and player_values.get(CONF_ENABLE_ICY_METADATA) == "full"
        ):
            player_values[CONF_ENABLE_ICY_METADATA] = "basic"
            migrated += 1
    if migrated:
        LOGGER.info("Moved %d Raumfeld player(s) from ICY metadata full to basic", migrated)
    # claim the marker even when nothing moved, so a "full" picked later is kept
    data[CONF_RAUMFELD_ICY_BASIC_MIGRATED] = True
    return True


def _migrate_airplay_sync_adjust_sign(data: dict[str, Any]) -> bool:
    """
    Negate the stored sync adjust of every AirPlay player and protocol, once.

    AirPlay played later for a positive value, unlike every other protocol. Guarded by
    CONF_AIRPLAY_SYNC_ADJUST_SIGN_MIGRATED, which is claimed even when nothing moved, so a
    value stored afterwards keeps its sign. Returns True when the settings were modified,
    including the first run's marker.
    """
    if data.get(CONF_AIRPLAY_SYNC_ADJUST_SIGN_MIGRATED):
        return False
    migrated: list[str] = []
    for tree in (CONF_PLAYERS, CONF_PLAYER_PROTOCOLS):
        records = data.get(tree)
        if not isinstance(records, dict):
            continue
        for record_id, record in records.items():
            if not isinstance(record, dict):
                continue
            if str(record.get("provider") or "").split("--", maxsplit=1)[0] != "airplay":
                continue
            values = record.get("values")
            if not isinstance(values, dict):
                continue
            sync_adjust = values.get(CONF_SYNC_ADJUST)
            if isinstance(sync_adjust, int) and sync_adjust:
                values[CONF_SYNC_ADJUST] = -sync_adjust
                migrated.append(f"{record_id} ({sync_adjust} -> {-sync_adjust})")
    if migrated:
        LOGGER.info(
            "Negated the AirPlay sync adjust, a positive value now plays earlier: %s",
            ", ".join(migrated),
        )
    data[CONF_AIRPLAY_SYNC_ADJUST_SIGN_MIGRATED] = True
    return True


def _migrate_stereo_pair_player_type(data: dict[str, Any]) -> bool:
    """
    Store every player that is stored as a stereo pair as a multi-channel player.

    Returns True when the settings were modified.
    """
    all_player_configs = data.get(CONF_PLAYERS)
    if not isinstance(all_player_configs, dict):
        return False
    changed = False
    for player_cfg in all_player_configs.values():
        if isinstance(player_cfg, dict) and player_cfg.get("player_type") == "stereo_pair":
            player_cfg["player_type"] = "multi_channel"
            changed = True
    return changed


def _migrate_artist_catalogue_selection(data: dict[str, Any]) -> bool:
    """Move a stored artist selection that is no longer offered to the library album tracks."""
    core_cfg = data.get(CONF_CORE)
    queues_cfg = core_cfg.get("player_queues") if isinstance(core_cfg, dict) else None
    queue_values = queues_cfg.get("values") if isinstance(queues_cfg, dict) else None
    if not isinstance(queue_values, dict):
        return False
    if queue_values.get(CONF_DEFAULT_ENQUEUE_SELECT_ARTIST) not in (
        "all_tracks",
        "all_album_tracks",
    ):
        return False
    queue_values[CONF_DEFAULT_ENQUEUE_SELECT_ARTIST] = "library_album_tracks"
    LOGGER.info("Moved the artist playback selection to the tracks of the library albums")
    return True


def _migrate_remove_retired_builtin_providers(data: dict[str, Any]) -> bool:
    """Remove the stored configs of the retired builtin providers."""
    all_provider_configs = data.get(CONF_PROVIDERS)
    if not isinstance(all_provider_configs, dict):
        return False
    retired = [
        instance_id
        for instance_id, provider_cfg in all_provider_configs.items()
        if isinstance(provider_cfg, dict)
        and provider_cfg.get("domain") in RETIRED_BUILTIN_PROVIDER_DOMAINS
    ]
    for instance_id in retired:
        del all_provider_configs[instance_id]
        LOGGER.info("Removed the config of the retired provider %s", instance_id)
    return bool(retired)


def _migrate_sendspin_one_output(data: dict[str, Any]) -> bool:
    """
    Give every player one Sendspin output for all its bridges, once.

    A Cast bridge left its output switched off until the user opted in, which the stored
    output did not tell apart from a user who switched it off: every output a Cast protocol
    carries is marked, as that default when it is off or was never offered to the user,
    and as opted in when it is on. A player whose bridges each stored an output keeps the
    one in use, with the settings of the others. Guarded by
    CONF_SENDSPIN_ONE_OUTPUT_MIGRATED, which is claimed even when nothing changed, so an
    output the user switches off afterwards is not read as a default.
    Returns True when the settings were modified, including the first run's marker.
    """
    if data.get(CONF_SENDSPIN_ONE_OUTPUT_MIGRATED):
        return False
    try:
        _mark_cast_sendspin_outputs(data)
        for player_id, output_ids in _sendspin_outputs_by_player(data).items():
            if len(output_ids) < 2:
                continue
            try:
                _fold_sendspin_outputs(data, player_id, output_ids)
            except Exception:
                LOGGER.exception(
                    "Could not fold the Sendspin outputs of player %s, skipping it", player_id
                )
    except Exception:
        LOGGER.exception("Could not migrate the Sendspin outputs to one per player")
    data[CONF_SENDSPIN_ONE_OUTPUT_MIGRATED] = True
    return True


def _sendspin_outputs(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the stored Sendspin outputs that ride on another protocol, by their id."""
    protocols = data.get(CONF_PLAYER_PROTOCOLS)
    if not isinstance(protocols, dict):
        return {}
    return {
        protocol_id: record
        for protocol_id, record in protocols.items()
        if isinstance(protocol_id, str)
        and isinstance(record, dict)
        and _provider_domain(data, record) == "sendspin"
        and isinstance(values := record.get("values"), dict)
        and isinstance(values.get(CONF_UNDERLYING_PLAYER_ID), str)
    }


def _sendspin_outputs_by_player(data: dict[str, Any]) -> dict[str, list[str]]:
    """Return the ids of the stored Sendspin outputs of each stored player, by its id."""
    players = data.get(CONF_PLAYERS)
    by_player: dict[str, list[str]] = {}
    for output_id, record in _sendspin_outputs(data).items():
        player_id = record.get("player_id")
        if (
            isinstance(player_id, str)
            and isinstance(players, dict)
            and isinstance(players.get(player_id), dict)
        ):
            by_player.setdefault(player_id, []).append(output_id)
    return by_player


def _stored_provider(data: dict[str, Any], record: Any) -> tuple[str, dict[str, Any] | None]:
    """
    Return the instance id of the provider of a stored protocol, and its stored config.

    :param data: The settings.
    :param record: The stored record of the protocol, if there is one.
    :return: The instance id, empty when the record names none, and the config of that
        provider when it is stored.
    """
    instance_id = record.get("provider") if isinstance(record, dict) else None
    if not isinstance(instance_id, str):
        return "", None
    providers = data.get(CONF_PROVIDERS)
    provider = providers.get(instance_id) if isinstance(providers, dict) else None
    return instance_id, provider if isinstance(provider, dict) else None


def _provider_domain(data: dict[str, Any], record: Any) -> str:
    """
    Return the domain of the provider of a stored protocol, empty when it is not known.

    :param data: The settings.
    :param record: The stored record of the protocol, if there is one.
    """
    instance_id, provider = _stored_provider(data, record)
    domain = provider.get("domain") if provider else None
    return domain if isinstance(domain, str) else instance_id.split("--", maxsplit=1)[0]


def _sendspin_carrier(data: dict[str, Any], output: dict[str, Any]) -> tuple[str, bool]:
    """
    Return the domain of the protocol a stored Sendspin output rides on, and whether it is on.

    :param data: The settings.
    :param output: The stored record of the output.
    :return: The domain, empty when that protocol is not stored anymore, and whether the
        user has that protocol and its provider switched on.
    """
    carrier = data[CONF_PLAYER_PROTOCOLS].get(output["values"][CONF_UNDERLYING_PLAYER_ID])
    if not isinstance(carrier, dict):
        return "", False
    _, provider = _stored_provider(data, carrier)
    switched_on = (
        bool(carrier.get(CONF_ENABLED, True))
        and provider is not None
        and bool(provider.get(CONF_ENABLED, True))
    )
    return _provider_domain(data, carrier), switched_on


def _mark_cast_sendspin_outputs(data: dict[str, Any]) -> None:
    """Mark every Sendspin output a Cast protocol carries as left off by default, or opted in."""
    for output in _sendspin_outputs(data).values():
        values = output["values"]
        domain, _ = _sendspin_carrier(data, output)
        # an output whose carrier is gone still tells by the warning Cast stored with it
        if domain != "chromecast" and (domain or not values.get(CONF_PROTOCOL_EXPERIMENTAL_NOTE)):
            continue
        carrier = data[CONF_PLAYER_PROTOCOLS].get(values[CONF_UNDERLYING_PLAYER_ID])
        carrier_values = carrier.get("values") if isinstance(carrier, dict) else None
        # the warning is stored once the output is switched off, so an output without it
        # whose carrier still has the marker was never offered to the user
        never_offered = (
            isinstance(carrier_values, dict)
            and bool(carrier_values.get(_LEGACY_SENDSPIN_OPT_OUT_PENDING))
            and not values.get(CONF_PROTOCOL_EXPERIMENTAL_NOTE)
        )
        if output.get(CONF_ENABLED, True) and not never_offered:
            values[_SENDSPIN_CAST_OPT_IN] = True
        else:
            output[CONF_ENABLED] = False
            values[CONF_PROTOCOL_OFF_BY_DEFAULT] = True
    protocols = data.get(CONF_PLAYER_PROTOCOLS)
    for record in protocols.values() if isinstance(protocols, dict) else ():
        if isinstance(record, dict) and isinstance(values := record.get("values"), dict):
            values.pop(_LEGACY_SENDSPIN_OPT_OUT_PENDING, None)


def _fold_sendspin_outputs(data: dict[str, Any], player_id: str, output_ids: list[str]) -> None:
    """
    Fold the stored Sendspin outputs of a player into the one that is in use.

    That is the output whose carrier comes first in LEGACY_SENDSPIN_BRIDGE_ORDER among the
    protocols the user has switched on, so a player keeps the Sendspin output it has, or
    has switched off. An output a Cast bridge left off by default only stays when the
    player has no other. The one that stays takes the static delays of the others, by
    their carrier, and the settings it has none of itself; a choice of the player that
    names another one names it from then on.

    :param data: The settings.
    :param player_id: Id of the player.
    :param output_ids: The ids of its stored Sendspin outputs.
    """
    protocols = data[CONF_PLAYER_PROTOCOLS]

    def _rank(output_id: str) -> tuple[bool, bool, int, bool, str]:
        output = protocols[output_id]
        domain, switched_on = _sendspin_carrier(data, output)
        return (
            bool(output["values"].get(CONF_PROTOCOL_OFF_BY_DEFAULT)),
            not switched_on,
            # the order only applies among the carriers that are switched on
            LEGACY_SENDSPIN_BRIDGE_ORDER.index(domain)
            if switched_on and domain in LEGACY_SENDSPIN_BRIDGE_ORDER
            else len(LEGACY_SENDSPIN_BRIDGE_ORDER),
            not output.get(CONF_ENABLED, True),
            output_id,
        )

    kept_id = min(output_ids, key=_rank)
    kept_values = protocols[kept_id]["values"]
    carrier_id = kept_values[CONF_UNDERLYING_PLAYER_ID]
    stored_delays = kept_values.get(_SENDSPIN_BRIDGE_STATIC_DELAYS)
    delays = dict(stored_delays) if isinstance(stored_delays, dict) else {}
    player_values = data[CONF_PLAYERS][player_id].get("values")
    for output_id in sorted(output_ids):
        if output_id == kept_id:
            continue
        removed = protocols.pop(output_id)
        values = removed["values"]
        set_aside = values.get(_SENDSPIN_BRIDGE_STATIC_DELAYS)
        if (delay := values.get(_SENDSPIN_STATIC_DELAY)) is not None:
            delays.setdefault(values[CONF_UNDERLYING_PLAYER_ID], delay)
        for other_carrier_id, delay in (set_aside if isinstance(set_aside, dict) else {}).items():
            delays.setdefault(other_carrier_id, delay)
        if values.get(_SENDSPIN_CAST_OPT_IN):
            kept_values[_SENDSPIN_CAST_OPT_IN] = True
        for key, value in values.items():
            if key not in _SENDSPIN_OUTPUT_OWN_KEYS:
                kept_values.setdefault(key, value)
        if isinstance(player_values, dict):
            for key in (CONF_PREFERRED_OUTPUT_PROTOCOL, CONF_VOLUME_CONTROL, CONF_MUTE_CONTROL):
                if player_values.get(key) == output_id:
                    player_values[key] = kept_id
        LOGGER.info(
            "Player %s keeps the Sendspin output %s, removed its other one %s: %s",
            player_id,
            kept_id,
            output_id,
            removed,
        )
    if (delay := delays.pop(carrier_id, None)) is not None:
        kept_values.setdefault(_SENDSPIN_STATIC_DELAY, delay)
    if delays:
        kept_values[_SENDSPIN_BRIDGE_STATIC_DELAYS] = delays
    else:
        kept_values.pop(_SENDSPIN_BRIDGE_STATIC_DELAYS, None)
    if _sendspin_carrier(data, protocols[kept_id])[0] not in ("chromecast", ""):
        kept_values.pop(CONF_PROTOCOL_EXPERIMENTAL_NOTE, None)


def _read_feature_engine_picks(
    data: dict[str, Any], decrypt: Callable[[str], str]
) -> list[tuple[str, dict[str, Any], str, str]]:
    """
    Return the engines the features picked, as (default key, section, key, engine uid).

    :param data: The persistent settings data to read.
    :param decrypt: Callback that decrypts a stored string value.
    """
    all_provider_configs = data.get(CONF_PROVIDERS)
    if not isinstance(all_provider_configs, dict):
        all_provider_configs = {}
    core_cfg = data.get(CONF_CORE)
    players_cfg = core_cfg.get("players") if isinstance(core_cfg, dict) else None
    picks: list[tuple[str, dict[str, Any], str, str]] = []
    for default_key, domain, section_name, key in FEATURE_ENGINE_PICKS:
        owners = (
            [players_cfg]
            if domain is None
            else [
                provider_cfg
                for provider_cfg in all_provider_configs.values()
                if isinstance(provider_cfg, dict) and provider_cfg.get("domain") == domain
            ]
        )
        for owner in owners:
            section = owner.get(section_name) if isinstance(owner, dict) else None
            if not isinstance(section, dict) or not isinstance(value := section.get(key), str):
                continue
            try:
                uid = decrypt(value)
            except InvalidDataError:
                LOGGER.warning("Could not read a stored %s; it is left in place", key)
                continue
            picks.append((default_key, section, key, uid))
    return picks


def _seed_default_engines(
    data: dict[str, Any], picks: list[tuple[str, dict[str, Any], str, str]]
) -> dict[str, str]:
    """
    Seed every default engine that is not set yet and return all defaults that are set.

    :param data: The persistent settings data to seed the defaults in.
    :param picks: The engines the features picked, see _read_feature_engine_picks.
    """
    core_cfg = data.get(CONF_CORE, {})
    if not isinstance(core_cfg, dict):
        return {}
    ai_cfg = core_cfg.get(CONF_AI)
    stored = ai_cfg.get("values") if isinstance(ai_cfg, dict) else None
    if not isinstance(stored, dict):
        stored = {}
    all_provider_configs = data.get(CONF_PROVIDERS)
    if not isinstance(all_provider_configs, dict):
        all_provider_configs = {}
    defaults: dict[str, str] = {}
    seeds: dict[str, str] = {}
    for default_key in (
        CONF_DEFAULT_TEXT_ENGINE,
        CONF_DEFAULT_SPEECH_ENGINE,
        CONF_DEFAULT_IMAGE_ENGINE,
    ):
        if isinstance(value := stored.get(default_key), str) and value:
            defaults[default_key] = value
            continue
        usable = [
            uid
            for pick_default_key, _section, _key, uid in picks
            if pick_default_key == default_key
            and _engine_provider_enabled(uid, all_provider_configs)
        ]
        if usable:
            # an engine picked as often as another loses to the one picked first
            seeds[default_key] = Counter(usable).most_common(1)[0][0]
    if not seeds:
        return defaults
    data[CONF_CORE] = core_cfg
    if not isinstance(ai_cfg, dict):
        ai_cfg = core_cfg[CONF_AI] = {}
    # a core block without its domain can not be parsed
    ai_cfg.setdefault("domain", CONF_AI)
    if not isinstance(ai_cfg.get("values"), dict):
        ai_cfg["values"] = {}
    ai_cfg["values"].update(seeds)
    return {**defaults, **seeds}


def _engine_provider_enabled(uid: str, all_provider_configs: dict[str, Any]) -> bool:
    """Return whether the plugin providing an engine is configured and enabled."""
    # an engine uid is the instance id of the providing plugin, a slash and the engine id
    instance_id, separator, _engine_id = uid.partition("/")
    provider_cfg = all_provider_configs.get(instance_id) if separator else None
    return isinstance(provider_cfg, dict) and bool(provider_cfg.get("enabled", True))
