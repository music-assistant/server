"""Settings (settings.json) migration logic for the config controller."""

from __future__ import annotations

import logging
import re
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from music_assistant_models.constants import PLAYER_CONTROL_NATIVE, PLAYER_CONTROL_NONE
from music_assistant_models.enums import CrossfadeMode
from music_assistant_models.errors import InvalidDataError

from music_assistant.constants import (
    CONF_CORE,
    CONF_CROSSFADE_DURATION,
    CONF_CROSSFADE_MODE,
    CONF_HTTP_PROFILE,
    CONF_ICON,
    CONF_LINKED_PROTOCOL_IDS,
    CONF_NFS_SUBFOLDER_MIGRATED,
    CONF_PLAYER_DSP,
    CONF_PLAYER_QUEUES,
    CONF_PLAYERS,
    CONF_PROTOCOL_PARENT_ID,
    CONF_PROVIDERS,
    CONF_SMART_FADES_MODE,
    CONF_VALUE_DISABLED,
    CONF_VALUE_ENABLED,
    CONF_VOLUME_NORMALIZATION,
    CONF_VOLUME_NORMALIZATION_TARGET,
)
from music_assistant.controllers.player_queues.constants import (
    CONF_SMART_SHUFFLE_ARTIST_RECENCY,
    CONF_SMART_SHUFFLE_DUPLICATE_GAP,
    CONF_SMART_SHUFFLE_ENABLED,
    CONF_SMART_SHUFFLE_SONG_RECENCY,
)
from music_assistant.helpers.config_entries import CONF_CONNECTED_PLAYERS

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant_models.config_entries import ConfigValueType

LOGGER = logging.getLogger(__name__)

# removed player config key, only referenced by its migration
LEGACY_CONF_OUTPUT_LIMITER = "output_limiter"

# removed automatic-player-selection sentinel of the connected-player plugins, only
# referenced by their migration
LEGACY_PLAYER_ID_AUTO = "__auto__"

# shared prefix of the removed per-player Bose SoundTouch preset keys
LEGACY_BOSE_PRESET_KEY_PREFIX = "preset_"

# removed hass provider config keys, only referenced by their migration
LEGACY_CONF_TTS_ENTITY = "tts_entity"
LEGACY_CONF_AI_TASK_ENTITY = "ai_task_entity"

# engine selection keys of the providers that consume the plugin engines
CONF_AI_ENGINE = "ai_engine"
CONF_TTS_ENGINE = "tts_engine"


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
# - spotify is deliberately absent: its own _migrate_legacy_token still reads the legacy
#   "refresh_token" and "client_id" from `values`, so this migration must not move them.
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
    "nicovideo": ("mail", "password", "user_session"),
    "nugs": ("username", "password"),
    "opensubsonic": ("username", "password", "baseURL", "port", "path"),
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


async def migrate(data: dict[str, Any]) -> bool:  # noqa: PLR0915
    """Migrate the persistent settings data in-place; return True if anything changed."""
    changed = False

    # The background tasks controller originally persisted runtime state directly under
    # core/tasks, which could create a CoreConfig object without the required domain field.
    # Repair that single known corruption case on load.
    # TODO: remove after 2.9 release
    tasks_core_config = data.get(CONF_CORE, {}).get("tasks")
    if isinstance(tasks_core_config, dict) and "domain" not in tasks_core_config:
        tasks_core_config["domain"] = "tasks"
        LOGGER.warning("Repaired corrupt tasks core configuration")
        changed = True

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

    # Collapse legacy multi-instance Fully Kiosk provider configs into a single
    # provider instance with a list of devices (matching the MPD provider pattern).
    # TODO: remove after 2.10 release
    if _migrate_fully_kiosk_multi_instance(data):
        changed = True
    # Migrate default_enqueue_option_radio -> default_enqueue_option_live_sources.
    # The same setting now covers both radio stations and plugin AudioSources
    # (Spotify Connect, AirPlay receiver, etc.); preserves the user's customised
    # value if they set one.
    # TODO: remove after 2.10 release
    player_queues_cfg = data.get(CONF_CORE, {}).get("player_queues")
    if isinstance(player_queues_cfg, dict):
        values = player_queues_cfg.get("values")
        if isinstance(values, dict) and "default_enqueue_option_radio" in values:
            radio_value = values.pop("default_enqueue_option_radio")
            values.setdefault("default_enqueue_option_live_sources", radio_value)
            LOGGER.info(
                "Migrated default_enqueue_option_radio -> default_enqueue_option_live_sources"
            )
            changed = True

    # Migrate sync_group members_filter (exclusion) -> allowed_members (inclusion).
    # Inversion freezes the universe at migration time; speakers added after this
    # point must be added by the user explicitly, which matches the new design's
    # "limit to these" intent.
    # TODO: remove after 2.10 release
    all_player_configs = data.get(CONF_PLAYERS, {})
    if isinstance(all_player_configs, dict):
        group_provider_domains = {"sync_group", "universal_group"}
        universe = {
            pid
            for pid, cfg in all_player_configs.items()
            if isinstance(cfg, dict) and cfg.get("provider") not in group_provider_domains
        }
        for player_id, player_cfg in all_player_configs.items():
            if not isinstance(player_cfg, dict):
                continue
            if player_cfg.get("provider") != "sync_group":
                continue
            values = player_cfg.setdefault("values", {})
            old_exclude = values.get("members_filter") or []
            if not old_exclude or values.get("allowed_members") is not None:
                continue
            values["allowed_members"] = sorted(universe - set(old_exclude))
            values["members_filter"] = []
            LOGGER.info(
                "Migrated sync_group %s: members_filter (exclusion) -> allowed_members (inclusion)",
                player_id,
            )
            changed = True

    # Clear self-referential protocol links: a player whose protocol_parent_id or
    # linked_protocol_ids pointed at its own id was hidden as its own protocol child.
    # TODO: remove after 2.10 release
    if _migrate_self_referential_protocol_links(data):
        changed = True

    # Drop the persisted schedule for the metadata maintenance tasks that were hardcoded
    # to run at 04:00 local. They are now registered under new ("_v2") task ids with a
    # randomized full-day schedule (to avoid spiking the shared MusicBrainz mirror), so the
    # old persisted state is orphaned and can be removed.
    # TODO: remove after 2.9 release
    if _migrate_metadata_maintenance_schedule(data):
        changed = True

    # TODO: remove after 2.10 release
    if _migrate_volume_normalization_target(data):
        changed = True

    # Move queue-scoped settings (crossfade duration, volume normalization) from the per-player
    # config to the new per-queue config (queue_id == player_id, so the id maps 1:1).
    # TODO: remove after 2.11 release
    if _migrate_player_queue_settings(data):
        changed = True

    # Adopt the global-with-override model for queue settings: convert the former boolean toggles to
    # their select strings, and promote the now global-only settings (crossfade duration, smart
    # shuffle recency windows) to the Player Queues core config. Runs after the player->queue move
    # above so any values it just landed are picked up here.
    # TODO: remove after 2.10 release
    if _migrate_global_queue_settings(data):
        changed = True

    # Promote local_audio attribution stubs to regular players and fold the settings of
    # their (now obsolete) universal player wrappers back onto them.
    # TODO: remove after 2.11 release
    if _migrate_local_audio_attribution_stubs(data):
        changed = True

    # Drop ghost players that were discovered from this server's own AirPlay Receiver
    # (shairport-sync) advertisements before discovery learned to filter them out.
    # TODO: remove after 2.10 release
    if _migrate_airplay_receiver_ghost_players(data):
        changed = True

    # Give Apple TVs paired before native power control existed the current
    # default ("native") instead of the stale "none" that hid their power button.
    # TODO: remove after 2.11 release
    if _migrate_airplay_apple_power_control(data):
        changed = True

    # Drop the stored value of the removed output limiter player setting; clipping protection
    # is now an explicit Safety Limiter DSP filter instead of a fixed output stage.
    # TODO: remove after 2.10 release
    if _migrate_output_limiter(data):
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

    # Drop disabled protocol player configs that lost their parent player: the device they
    # belong to can never register again while such a config lingers, and it is not shown
    # in the UI so there is no way to enable it again.
    # TODO: remove after 2.12 release
    if _migrate_orphaned_disabled_protocol_configs(data):
        changed = True

    # Clear the stored name of players that were never renamed, so an updated default
    # name is no longer shadowed by the auto-generated name stored at creation time.
    # TODO: remove after 2.12 release
    if _migrate_unrenamed_player_names(data):
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


# TODO: remove after 2.10 release
def migrate_nfs_subfolder_into_export_path(
    data: dict[str, Any],
    encrypt: Callable[[str], str],
    decrypt: Callable[[str], str],
) -> bool:
    """
    Fold a stored NFS `subfolder` into its `export_path`, once.

    The provider mounts the export as configured and scans the subfolder inside that mount, so
    folding the two keys into one keeps an existing instance mounting what it already mounts.
    Runs after encryption is initialized, like migrate_provider_setup_data, because both keys
    live encrypted in `setup_data`.

    Guarded by CONF_NFS_SUBFOLDER_MIGRATED so it cannot run twice: a subfolder stored
    afterwards means "scan this path inside the mount" and must never be folded. Returns True
    when the settings were modified, including the first run's marker.

    :param data: The persistent settings data to migrate in-place.
    :param encrypt: Callback that encrypts a string value at rest.
    :param decrypt: Callback that decrypts a stored string value (a no-op for plain values).
    """
    if data.get(CONF_NFS_SUBFOLDER_MIGRATED):
        return False
    all_provider_configs = data.get(CONF_PROVIDERS, {})
    if not isinstance(all_provider_configs, dict):
        return False
    changed = False
    for instance_id, provider_cfg in all_provider_configs.items():
        if not isinstance(provider_cfg, dict) or provider_cfg.get("domain") != "filesystem_nfs":
            continue
        setup_data = provider_cfg.get("setup_data")
        if not isinstance(setup_data, dict):
            continue
        stored_subfolder = setup_data.get("subfolder")
        stored_export_path = setup_data.get("export_path")
        if not isinstance(stored_subfolder, str) or not isinstance(stored_export_path, str):
            continue
        try:
            subfolder = decrypt(stored_subfolder).strip()
            export_path = decrypt(stored_export_path)
        except InvalidDataError:
            # one unreadable instance must not fail config setup for the whole server; it
            # still surfaces the problem at its own setup. Name it without its values.
            LOGGER.warning(
                "Could not read the stored NFS paths of %s; skipping its subfolder migration",
                instance_id,
            )
            continue
        if not subfolder or not export_path:
            # an empty export path is broken either way and must not become a relative one
            continue
        # must come out as <export_path>/<subfolder> so the mount source is unchanged
        setup_data["export_path"] = encrypt(str(PurePosixPath(export_path) / subfolder.lstrip("/")))
        del setup_data["subfolder"]
        changed = True
    if changed:
        LOGGER.info("Migrated NFS provider subfolder into the export path")
    # claim the marker even when nothing was folded, so a subfolder stored later is safe
    data[CONF_NFS_SUBFOLDER_MIGRATED] = True
    return True


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
    removed automatic selection and players that no longer exist are cleared (so the
    provider fails into reconfigure) and their free-form device name keys are dropped.

    Runs after encryption is initialized, and after migrate_provider_setup_data so the
    pre-setup-flow values have landed in setup_data by now.

    :param data: The persistent settings data to migrate in-place.
    :param decrypt: Callback that decrypts a stored string value (a no-op for plain values).
    :param storage_path: The server storage path holding per-instance provider data dirs.
    """
    all_provider_configs = data.get(CONF_PROVIDERS, {})
    if not isinstance(all_provider_configs, dict):
        return False
    stored_players = data.get(CONF_PLAYERS, {})
    known_player_ids = set(stored_players) if isinstance(stored_players, dict) else set()
    changed = False
    for domain in ("spotify_connect", "airplay_receiver"):
        if _collapse_connected_player_instances(
            all_provider_configs, domain, known_player_ids, decrypt, storage_path
        ):
            changed = True
    if _clear_invalid_connected_players(all_provider_configs, known_player_ids, decrypt):
        changed = True
    return changed


def _collapse_connected_player_instances(
    all_provider_configs: dict[str, Any],
    domain: str,
    known_player_ids: set[str],
    decrypt: Callable[[str], str],
    storage_path: str,
) -> bool:
    """
    Collapse the instances of one per-player plugin domain into a single instance.

    :param all_provider_configs: The stored provider configurations, modified in-place.
    :param domain: The plugin domain to collapse (spotify_connect or airplay_receiver).
    :param known_player_ids: The player ids present in the stored player configurations.
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
    # automatic selection and vanished players contribute nothing
    connected_players: list[str] = []
    soloist_player_ids: dict[str, str] = {}
    for instance_id in enabled_ids:
        player_id = decrypted.get(instance_id, {}).get("mass_player_id")
        if (
            not isinstance(player_id, str)
            or player_id == LEGACY_PLAYER_ID_AUTO
            or player_id not in known_player_ids
        ):
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
    known_player_ids: set[str],
    decrypt: Callable[[str], str],
) -> bool:
    """
    Enforce the now-mandatory connected player on the single-player plugins.

    :param all_provider_configs: The stored provider configurations, modified in-place.
    :param known_player_ids: The player ids present in the stored player configurations.
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
        if player_id == LEGACY_PLAYER_ID_AUTO or player_id not in known_player_ids:
            del setup_data["mass_player_id"]
            changed = True
            LOGGER.warning(
                "The connected player of %s is no longer valid; open its settings and run "
                "the setup again to select a player",
                instance_id,
            )
    return changed


# TODO: remove after 2.10 release
def migrate_hass_engine_selection(data: dict[str, Any], encrypt: Callable[[str], str]) -> bool:
    """
    Hand the removed Home Assistant TTS/AI entity choice over to the providers consuming it.

    The Home Assistant plugin exposes every TTS/AI entity as a selectable engine now and each
    consuming provider picks one itself, so the single choice that used to live on the plugin
    is copied to the installed consumers that have no choice of their own yet. Providers
    installed later pick an engine themselves at load. Returns True if anything changed.

    Runs after encryption is initialized (like migrate_provider_setup_data), since the ai_radio
    selection belongs in its encrypted `setup_data`.

    :param data: The persistent settings data to migrate in-place.
    :param encrypt: Callback that encrypts a string value, used for the values that land in
        `setup_data`.
    """
    all_provider_configs = data.get(CONF_PROVIDERS, {})
    if not isinstance(all_provider_configs, dict):
        return False
    hass_configs = {
        instance_id: provider_cfg
        for instance_id, provider_cfg in all_provider_configs.items()
        if isinstance(provider_cfg, dict) and provider_cfg.get("domain") == "hass"
    }
    if len(hass_configs) > 1:
        # there is no correct winner between several choices, so let the user pick per provider
        LOGGER.warning(
            "Skipped migrating the Home Assistant TTS/AI entity selection: "
            "%s Home Assistant configurations found, select the engines manually",
            len(hass_configs),
        )
        return False
    changed = False
    for instance_id, hass_cfg in hass_configs.items():
        values = hass_cfg.get("values")
        if not isinstance(values, dict):
            continue
        if not any(key in values for key in (LEGACY_CONF_TTS_ENTITY, LEGACY_CONF_AI_TASK_ENTITY)):
            continue
        tts_entity = values.pop(LEGACY_CONF_TTS_ENTITY, None)
        ai_task_entity = values.pop(LEGACY_CONF_AI_TASK_ENTITY, None)
        changed = True
        if isinstance(ai_task_entity, str) and ai_task_entity:
            ai_engine = f"{instance_id}/{ai_task_entity}"
            _set_engine_selection(
                all_provider_configs, "music_quiz", "values", CONF_AI_ENGINE, ai_engine
            )
            _set_engine_selection(
                all_provider_configs, "smart_playlist", "values", CONF_AI_ENGINE, ai_engine
            )
            _set_engine_selection(
                all_provider_configs, "ai_radio", "setup_data", CONF_AI_ENGINE, encrypt(ai_engine)
            )
        if isinstance(tts_entity, str) and tts_entity:
            _set_engine_selection(
                all_provider_configs,
                "ai_radio",
                "setup_data",
                CONF_TTS_ENGINE,
                encrypt(f"{instance_id}/{tts_entity}"),
            )
        LOGGER.info("Migrated the Home Assistant TTS/AI entity selection to the plugin engines")
    return changed


def _set_engine_selection(
    all_provider_configs: dict[str, Any], domain: str, section: str, key: str, value: str
) -> None:
    """Store an engine selection on each config of the given domain that has none of its own."""
    for provider_cfg in all_provider_configs.values():
        if not isinstance(provider_cfg, dict) or provider_cfg.get("domain") != domain:
            continue
        section_values = provider_cfg.get(section)
        if not isinstance(section_values, dict):
            section_values = {}
            provider_cfg[section] = section_values
        section_values.setdefault(key, value)


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


def _migrate_global_queue_settings(data: dict[str, Any]) -> bool:
    """
    Adopt the global-with-override model for the per-queue settings.

    The two former boolean toggles become their select strings (so a queue can also follow the
    global value), and the settings that are now global-only are promoted to the Player Queues core
    config. Queues that stored nothing keep nothing and therefore fall back to the new "global"
    default. Idempotent: a second run finds only select strings and no per-queue global-only values.
    """
    all_queue_configs = data.get(CONF_PLAYER_QUEUES, {})
    if not isinstance(all_queue_configs, dict):
        return False
    changed = False
    # 1. convert the former booleans (True/False) to their select strings (enabled/disabled)
    bool_to_select = {True: CONF_VALUE_ENABLED, False: CONF_VALUE_DISABLED}
    for queue_cfg in all_queue_configs.values():
        if not isinstance(queue_cfg, dict):
            continue
        values = queue_cfg.get("values")
        if not isinstance(values, dict):
            continue
        for key in (CONF_VOLUME_NORMALIZATION, CONF_SMART_SHUFFLE_ENABLED):
            if isinstance(values.get(key), bool):
                values[key] = bool_to_select[values[key]]
                changed = True
    # 2. promote the now global-only settings to the Player Queues core config
    global_only_keys = (
        CONF_CROSSFADE_DURATION,
        CONF_SMART_SHUFFLE_SONG_RECENCY,
        CONF_SMART_SHUFFLE_ARTIST_RECENCY,
        CONF_SMART_SHUFFLE_DUPLICATE_GAP,
    )
    for key in global_only_keys:
        if _promote_queue_setting_to_global(data, key):
            changed = True
    return changed


def _promote_queue_setting_to_global(data: dict[str, Any], key: str) -> bool:
    """
    Promote a (now global-only) per-queue setting to global config and drop the per-queue copies.

    A single value shared by every queue that set it is promoted so the user's preference is kept;
    mixed values fall back to the new default. Mirrors _migrate_volume_normalization_target.
    """
    all_queue_configs = data.get(CONF_PLAYER_QUEUES, {})
    if not isinstance(all_queue_configs, dict):
        return False
    stored_values: set[Any] = set()
    for queue_cfg in all_queue_configs.values():
        if not isinstance(queue_cfg, dict):
            continue
        values = queue_cfg.get("values")
        if isinstance(values, dict) and key in values:
            stored_values.add(values[key])
    if not stored_values:
        return False
    # promote only a single consistent value (mixed values fall back to the new default), and never
    # clobber a value the user already set globally; only touch the core config when promoting
    existing_core = data.get(CONF_CORE, {}).get(CONF_PLAYER_QUEUES, {})
    existing_values = existing_core.get("values", {}) if isinstance(existing_core, dict) else {}
    if len(stored_values) == 1 and key not in existing_values:
        core_values = (
            data.setdefault(CONF_CORE, {})
            .setdefault(CONF_PLAYER_QUEUES, {"domain": CONF_PLAYER_QUEUES})
            .setdefault("values", {})
        )
        core_values[key] = next(iter(stored_values))
        LOGGER.info("Promoted per-queue %s to the global Player Queues config", key)
    # the setting is global-only now, so drop every per-queue copy
    for queue_cfg in all_queue_configs.values():
        if not isinstance(queue_cfg, dict):
            continue
        values = queue_cfg.get("values")
        if isinstance(values, dict):
            values.pop(key, None)
    return True


def _migrate_volume_normalization_target(data: dict[str, Any]) -> bool:
    """
    Migrate volume_normalization_target from per-player to the global streams setting.

    Collects all explicitly stored per-player values; if they all agree on a single value,
    that value is promoted to the streams core config so the user's preference is preserved.
    """
    all_player_configs = data.get(CONF_PLAYERS, {})
    if not isinstance(all_player_configs, dict):
        return False
    per_player_values: set[int] = set()
    for player_cfg in all_player_configs.values():
        if not isinstance(player_cfg, dict):
            continue
        values = player_cfg.get("values")
        if not isinstance(values, dict):
            continue
        if CONF_VOLUME_NORMALIZATION_TARGET in values:
            per_player_values.add(int(values[CONF_VOLUME_NORMALIZATION_TARGET]))

    if not per_player_values:
        return False

    streams_core = data.setdefault(CONF_CORE, {}).setdefault("streams", {})
    streams_values = streams_core.setdefault("values", {})
    # only promote when not already globally configured
    if CONF_VOLUME_NORMALIZATION_TARGET not in streams_values:
        # single consistent value across all players → promote it; mixed → use new default
        promoted = per_player_values.pop() if len(per_player_values) == 1 else None
        if promoted is not None:
            streams_values[CONF_VOLUME_NORMALIZATION_TARGET] = promoted
            LOGGER.info(
                "Promoted volume_normalization_target %s LUFS to global streams setting",
                promoted,
            )

    for player_id, player_cfg in all_player_configs.items():
        if not isinstance(player_cfg, dict):
            continue
        values = player_cfg.get("values")
        if not isinstance(values, dict):
            continue
        if CONF_VOLUME_NORMALIZATION_TARGET in values:
            del values[CONF_VOLUME_NORMALIZATION_TARGET]
            LOGGER.info(
                "Removed per-player volume_normalization_target for player %s",
                player_id,
            )
    return True


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
        values.pop(CONF_PROTOCOL_PARENT_ID, None)
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
        CONF_LINKED_PROTOCOL_IDS,
        CONF_PROTOCOL_PARENT_ID,
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
        pid for pid in (universal_values.get(CONF_LINKED_PROTOCOL_IDS) or []) if pid != player_id
    ]
    if linked_ids:
        existing_ids = list(values.get(CONF_LINKED_PROTOCOL_IDS) or [])
        values[CONF_LINKED_PROTOCOL_IDS] = existing_ids + [
            pid for pid in linked_ids if pid not in existing_ids
        ]
    all_player_configs = data.get(CONF_PLAYERS, {})
    for protocol_id in linked_ids:
        protocol_cfg = all_player_configs.get(protocol_id)
        if not isinstance(protocol_cfg, dict):
            continue
        protocol_values = protocol_cfg.setdefault("values", {})
        if protocol_values.get(CONF_PROTOCOL_PARENT_ID) == universal_id:
            protocol_values[CONF_PROTOCOL_PARENT_ID] = player_id

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


def _migrate_self_referential_protocol_links(data: dict[str, Any]) -> bool:
    """Clear protocol links that point a player at its own id."""
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
        repaired = False
        if values.get(CONF_PROTOCOL_PARENT_ID) == player_id:
            values[CONF_PROTOCOL_PARENT_ID] = None
            repaired = True
        linked = values.get(CONF_LINKED_PROTOCOL_IDS)
        if isinstance(linked, list) and player_id in linked:
            values[CONF_LINKED_PROTOCOL_IDS] = [pid for pid in linked if pid != player_id]
            repaired = True
        if repaired:
            LOGGER.warning("Repaired self-referential protocol link for %s", player_id)
            changed = True
    return changed


def _migrate_metadata_maintenance_schedule(data: dict[str, Any]) -> bool:
    """Remove the orphaned persisted state for the pre-randomization metadata task ids."""
    core_config = data.get(CONF_CORE)
    if not isinstance(core_config, dict):
        return False
    tasks_config = core_config.get("tasks")
    if not isinstance(tasks_config, dict):
        return False
    task_states = tasks_config.get("scheduled_task_states")
    if not isinstance(task_states, dict):
        return False
    legacy_task_ids = (
        "metadata_missing_artist_metadata_scan",
        "metadata_playlist_metadata_scan",
        "metadata_thumb_cache_cleanup",
    )
    removed = [task_id for task_id in legacy_task_ids if task_id in task_states]
    for task_id in removed:
        del task_states[task_id]
    if removed:
        LOGGER.info("Removed orphaned metadata maintenance schedule state for %s", removed)
    return bool(removed)


def _migrate_fully_kiosk_multi_instance(data: dict[str, Any]) -> bool:
    """Collapse legacy multi-instance Fully Kiosk configs into a single provider instance."""
    providers = data.get(CONF_PROVIDERS, {})
    legacy_ids = [
        iid
        for iid, conf in providers.items()
        if isinstance(conf, dict) and conf.get("domain") == "fully_kiosk" and iid != "fully_kiosk"
    ]
    if not legacy_ids:
        return False

    ip_entries: list[str] = []
    players = data.setdefault(CONF_PLAYERS, {})
    for iid in legacy_ids:
        old_values = providers[iid].get("values") or {}
        host = old_values.get("ip_address")
        if not host:
            del providers[iid]
            continue
        try:
            port = int(old_values.get("port") or 2323)
        except TypeError, ValueError:
            port = 2323
        entry = host if port == 2323 else f"{host}:{port}"
        if entry not in ip_entries:
            ip_entries.append(entry)

        new_player_id = f"fully_kiosk_{host}_{port}"
        player_conf = players.setdefault(
            new_player_id,
            {
                "player_id": new_player_id,
                "provider": "fully_kiosk",
                "enabled": True,
                "values": {},
            },
        )
        player_values = player_conf.setdefault("values", {})
        for key in ("password", "use_ssl", "verify_ssl", "ssl_fingerprint"):
            if old_values.get(key) is not None and key not in player_values:
                player_values[key] = old_values[key]

        del providers[iid]

    if "fully_kiosk" in providers:
        existing_values = providers["fully_kiosk"].setdefault("values", {})
        existing_ips = list(existing_values.get("manual_discovery_ip_addresses") or [])
        for entry in ip_entries:
            if entry not in existing_ips:
                existing_ips.append(entry)
        existing_values["manual_discovery_ip_addresses"] = existing_ips
    else:
        providers["fully_kiosk"] = {
            "type": "player",
            "domain": "fully_kiosk",
            "instance_id": "fully_kiosk",
            "enabled": True,
            "values": {"manual_discovery_ip_addresses": ip_entries},
        }

    LOGGER.warning(
        "Migrated %d legacy Fully Kiosk provider instance(s) into a single instance. "
        "Devices and their passwords have been preserved, but any Fully Kiosk player "
        "that was part of a universal group will need to be re-added to it. ",
        len(legacy_ids),
    )
    return True


def _migrate_airplay_receiver_ghost_players(data: dict[str, Any]) -> bool:
    """
    Remove ghost players left behind by this server's own AirPlay Receiver instances.

    The AirPlay provider could discover the server's own AirPlay Receiver
    (shairport-sync) advertisements as regular AirPlay players. shairport-sync
    derives its device id from the receiver name plus a host interface MAC, which
    can change per boot (e.g. virtual interface MACs), so every restart could mint
    a new player id: the previous ids linger as permanently unavailable players and
    universal player wrappers. Discovery now filters these advertisements out; this
    migration drops the leftovers.
    """
    all_provider_configs = data.get(CONF_PROVIDERS, {})
    all_player_configs = data.get(CONF_PLAYERS, {})
    if not isinstance(all_provider_configs, dict) or not isinstance(all_player_configs, dict):
        return False
    # the advertised name of every enabled receiver instance
    # (key and default mirror the airplay_receiver provider's config entry).
    # Disabled instances are skipped, consistent with the discovery filter: they
    # run no daemon and cannot have produced the ghosts, so their name is too weak
    # a signal to delete a config on (it could be a legitimate same-named device).
    receiver_names: set[str] = set()
    for provider_cfg in all_provider_configs.values():
        if not isinstance(provider_cfg, dict) or provider_cfg.get("domain") != "airplay_receiver":
            continue
        if not provider_cfg.get("enabled", True):
            continue
        setup_data = provider_cfg.get("setup_data")
        if isinstance(setup_data, dict) and "airplay_name" in setup_data:
            # New setup-flow instances cannot have produced legacy ghosts. Their
            # encrypted receiver name is unavailable during this early migration.
            continue
        provider_values = provider_cfg.get("values")
        if isinstance(provider_values, dict) and CONF_CONNECTED_PLAYERS in provider_values:
            # a collapsed per-player instance advertises player-derived names; the
            # legacy default names this cleanup matches on cannot originate here
            continue
        airplay_name = (
            provider_values.get("airplay_name") if isinstance(provider_values, dict) else None
        )
        receiver_names.add(str(airplay_name) if airplay_name else "Music Assistant")
    if not receiver_names:
        return False
    # the Sendspin bridge of such a ghost registered under "<name> (AirPlay)"
    bridge_names = {f"{name} (AirPlay)" for name in receiver_names}

    # First identify the ghost protocol endpoints: the discovered AirPlay player and
    # its Sendspin bridge, each matched by its own advertised (receiver) name.
    endpoint_ghost_ids: set[str] = set()
    for player_id, player_cfg in all_player_configs.items():
        if not isinstance(player_cfg, dict):
            continue
        default_name = player_cfg.get("default_name")
        provider = player_cfg.get("provider")
        if (
            player_id.startswith("ap") and provider == "airplay" and default_name in receiver_names
        ) or (
            player_id.startswith("spb_") and provider == "sendspin" and default_name in bridge_names
        ):
            endpoint_ghost_ids.add(player_id)

    # Then add the universal player wrappers that exclusively wrap those endpoints.
    # A wrapper is only removed when it links at least one confirmed ghost endpoint
    # and nothing else, so a real player that merely shares the receiver name (with
    # no or different linked protocols) is never deleted.
    ghost_ids = set(endpoint_ghost_ids)
    for player_id, player_cfg in all_player_configs.items():
        if not isinstance(player_cfg, dict):
            continue
        if not (
            player_id.startswith("up")
            and player_cfg.get("provider") == "universal_player"
            and player_cfg.get("default_name") in receiver_names | bridge_names
        ):
            continue
        values = player_cfg.get("values")
        linked = values.get(CONF_LINKED_PROTOCOL_IDS) if isinstance(values, dict) else None
        if isinstance(linked, list) and linked and all(pid in endpoint_ghost_ids for pid in linked):
            ghost_ids.add(player_id)
    if not ghost_ids:
        return False

    for player_id in ghost_ids:
        del all_player_configs[player_id]
        # drop dead per-queue and DSP state along with the player config
        for tree_key in (CONF_PLAYER_QUEUES, CONF_PLAYER_DSP):
            tree = data.get(tree_key)
            if isinstance(tree, dict):
                tree.pop(player_id, None)
    # strip dangling references to the removed ghosts from group configurations
    for player_cfg in all_player_configs.values():
        if not isinstance(player_cfg, dict):
            continue
        values = player_cfg.get("values")
        if not isinstance(values, dict):
            continue
        for key in ("group_members", "allowed_members"):
            members = values.get(key)
            if isinstance(members, list) and any(pid in ghost_ids for pid in members):
                values[key] = [pid for pid in members if pid not in ghost_ids]
    LOGGER.info(
        "Removed %d ghost player config(s) left behind by this server's own "
        "AirPlay Receiver instances",
        len(ghost_ids),
    )
    return True


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


def _migrate_output_limiter(data: dict[str, Any]) -> bool:
    """Remove the stored values of the removed per-player output limiter setting."""
    all_player_configs = data.get(CONF_PLAYERS, {})
    if not isinstance(all_player_configs, dict):
        return False
    changed = False
    for player_cfg in all_player_configs.values():
        if not isinstance(player_cfg, dict):
            continue
        player_values = player_cfg.get("values")
        if isinstance(player_values, dict) and LEGACY_CONF_OUTPUT_LIMITER in player_values:
            del player_values[LEGACY_CONF_OUTPUT_LIMITER]
            changed = True
    if changed:
        LOGGER.info("Removed the obsolete output limiter setting from the player configuration(s)")
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
        if player_values.get(CONF_HTTP_PROFILE, FORCED_HTTP_PROFILE) != FORCED_HTTP_PROFILE:
            del player_values[CONF_HTTP_PROFILE]
            changed = True
    if changed:
        LOGGER.info("Restored the required HTTP profile on the Bluesound player configuration(s)")
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


def _migrate_orphaned_disabled_protocol_configs(data: dict[str, Any]) -> bool:
    """
    Remove disabled protocol player configs that no longer belong to a player.

    A protocol player is only ever presented as part of the player that owns it, so a
    disabled config that outlived its owner keeps the device from registering again while
    offering no way to enable it.
    """
    all_player_configs = data.get(CONF_PLAYERS, {})
    if not isinstance(all_player_configs, dict):
        return False
    linked_ids: set[str] = set()
    for player_cfg in all_player_configs.values():
        if not isinstance(player_cfg, dict):
            continue
        player_values = player_cfg.get("values")
        if not isinstance(player_values, dict):
            continue
        if isinstance(cached_ids := player_values.get(CONF_LINKED_PROTOCOL_IDS), list):
            linked_ids.update(pid for pid in cached_ids if isinstance(pid, str))
    orphaned: list[str] = []
    for player_id, player_cfg in all_player_configs.items():
        if not isinstance(player_cfg, dict):
            continue
        if player_cfg.get("player_type") != "protocol":
            continue
        if player_cfg.get("enabled", True):
            continue
        # a player owns a protocol player from either side of the link
        if player_id in linked_ids:
            continue
        player_values = player_cfg.get("values")
        parent_id = (
            player_values.get(CONF_PROTOCOL_PARENT_ID) if isinstance(player_values, dict) else None
        )
        if parent_id in all_player_configs:
            continue
        orphaned.append(player_id)
    dsp_configs = data.get(CONF_PLAYER_DSP)
    for player_id in orphaned:
        del all_player_configs[player_id]
        if isinstance(dsp_configs, dict):
            dsp_configs.pop(player_id, None)
        LOGGER.warning("Removed orphaned player configuration %s", player_id)
    return bool(orphaned)


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
