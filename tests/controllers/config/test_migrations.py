"""Tests for (settings.json) config migrations."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.constants import (
    CONF_AI_DEFAULT_ENGINES_MIGRATED,
    CONF_AIRPLAY_SYNC_ADJUST_SIGN_MIGRATED,
    CONF_APPLE_MUSIC_SYNC_SCHEDULE_MIGRATED,
    CONF_EMBY_ALBUM_ARTISTS_HEAL_MIGRATED,
    CONF_EMBY_ALBUM_ARTISTS_HEAL_PENDING,
    CONF_PLAYER_SPLIT_SESSION_ONLY,
    CONF_PLAYER_SPLIT_TREE,
    ENCRYPT_SUFFIX,
)
from music_assistant.controllers.config.migrations import (
    PROVIDER_SETUP_FLOW_DEFAULTS,
    PROVIDER_SETUP_FLOW_KEYS,
    _migrate_airplay_apple_power_control,
    _migrate_airplay_sync_adjust_sign,
    _migrate_apple_music_sync_schedule,
    _migrate_artist_catalogue_selection,
    _migrate_bluesound_http_profile,
    _migrate_bose_soundtouch_presets,
    _migrate_drop_fixed_content_length,
    _migrate_drop_http_profile,
    _migrate_emby_album_artists_heal,
    _migrate_milkdrop_drop_color_tint,
    _migrate_nicovideo_drop_mail_login,
    _migrate_output_codec_to_playback_quality,
    _migrate_player_icons,
    _migrate_player_setup_data,
    _migrate_raumfeld_icy_basic,
    _migrate_remove_retired_builtin_providers,
    _migrate_stereo_pair_player_type,
    _migrate_unrenamed_player_names,
    migrate,
    migrate_ai_default_engines,
    migrate_connected_player_plugins,
    migrate_provider_setup_data,
)

if TYPE_CHECKING:
    from pathlib import Path


def _fake_encrypt(value: str) -> str:
    """Mirror ConfigController.encrypt_string: prefix once, idempotent for encrypted values."""
    return value if value.startswith(ENCRYPT_SUFFIX) else ENCRYPT_SUFFIX + value


def _fake_decrypt(value: str) -> str:
    """Mirror ConfigController.decrypt_string: strip the prefix, pass plain values through."""
    return value.removeprefix(ENCRYPT_SUFFIX)


def test_migrate_bluesound_http_profile_drops_stored_pick() -> None:
    """A Bluesound player left on another HTTP profile is returned to the required one."""
    data: dict[str, Any] = {
        "players": {
            "b1": {
                "provider": "bluesound",
                "values": {"http_profile": "chunked", "flow_mode": True},
            },
            "b2": {"provider": "bluesound--abc1", "values": {"http_profile": "no_content_length"}},
            "c1": {"provider": "chromecast", "values": {"http_profile": "chunked"}},
        }
    }
    assert _migrate_bluesound_http_profile(data) is True
    assert data["players"]["b1"]["values"] == {"flow_mode": True}
    assert data["players"]["b2"]["values"] == {}
    # other providers are left to the step that drops the setting everywhere
    assert data["players"]["c1"]["values"] == {"http_profile": "chunked"}


def test_migrate_bluesound_http_profile_noop_when_absent() -> None:
    """Migration reports no change when no Bluesound player stored a profile."""
    data: dict[str, Any] = {"players": {"b1": {"provider": "bluesound", "values": {}}}}
    assert _migrate_bluesound_http_profile(data) is False


def test_migrate_bluesound_http_profile_noop_when_already_required() -> None:
    """A player already on the required profile needs no rewrite of the settings."""
    data: dict[str, Any] = {
        "players": {
            "b1": {"provider": "bluesound", "values": {"http_profile": "forced_content_length"}}
        }
    }
    assert _migrate_bluesound_http_profile(data) is False


def test_migrate_drop_http_profile_drops_every_stored_value() -> None:
    """The HTTP profile is dropped from every player, protocol players included."""
    data: dict[str, Any] = {
        "players": {
            "sonos_1": {
                "provider": "sonos",
                "values": {
                    "http_profile": "chunked",
                    "dlna_1||protocol||http_profile": "forced_content_length",
                    "dlna_1||protocol||flow_mode": True,
                    "output_codec": "mp3",
                },
            },
            "dlna_1": {
                "provider": "dlna",
                "player_type": "protocol",
                "values": {"http_profile": "forced_content_length", "flow_mode": True},
            },
            "cast_1": {"provider": "chromecast", "values": {"http_profile": "chunked"}},
            "ap_1": {"provider": "airplay", "values": {"output_codec": "flac"}},
        }
    }
    players = data["players"]

    assert _migrate_drop_http_profile(data) is True
    assert players["sonos_1"]["values"] == {
        "dlna_1||protocol||flow_mode": True,
        "output_codec": "mp3",
    }
    assert players["dlna_1"]["values"] == {"flow_mode": True}
    assert players["cast_1"]["values"] == {}
    assert players["ap_1"]["values"] == {"output_codec": "flac"}


async def test_migrate_drop_http_profile_idempotent() -> None:
    """The settings migration drops the HTTP profile once and reports no change after."""
    data: dict[str, Any] = {
        "players": {"dlna_1": {"provider": "dlna", "values": {"http_profile": "chunked"}}}
    }
    assert await migrate(data) is True
    assert await migrate(data) is False
    assert data["players"]["dlna_1"]["values"] == {}
    # the split gave the value to the record of the player's own protocol, where it is gone too
    assert data["player_protocols"]["dlna_1"]["values"] == {}


async def test_migrate_drops_the_http_profile_of_a_config_from_before_the_split() -> None:
    """A config from before the split keeps no HTTP profile in any record the split makes."""
    data: dict[str, Any] = {
        "players": {
            # a player with the copy it held of its protocol player's value
            "sonos_1": {
                "provider": "sonos",
                "player_type": "player",
                "values": {
                    "http_profile": "chunked",
                    "dlna_1||protocol||http_profile": "forced_content_length",
                    "output_codec": "mp3",
                },
            },
            "dlna_1": {
                "provider": "dlna",
                "player_type": "protocol",
                "values": {
                    "protocol_parent_id": "sonos_1",
                    "http_profile": "forced_content_length",
                    "flow_mode": True,
                },
            },
            # a universal player, whose values stay on the record of its player
            "up_cast": {
                "provider": "universal_player",
                "player_type": "player",
                "values": {
                    "linked_protocol_ids": ["cast_1"],
                    "cast_1||protocol||http_profile": "chunked",
                    "cast_1||protocol||output_codec": "flac",
                },
            },
            "cast_1": {
                "provider": "chromecast",
                "player_type": "protocol",
                "values": {"protocol_parent_id": "up_cast", "http_profile": "chunked"},
            },
        }
    }

    assert await migrate(data) is True

    records = [*data["players"].values(), *data["player_protocols"].values()]
    assert not [key for record in records for key in record["values"] if "http_profile" in key]
    assert data["player_protocols"]["sonos_1"]["values"] == {"playback_quality": "compressed"}
    assert data["player_protocols"]["dlna_1"]["values"] == {"flow_mode": True}
    assert data["player_protocols"]["cast_1"]["values"] == {}
    assert data["players"]["up_cast"]["values"] == {
        "cast_1||protocol||playback_quality": "lossless_compressed"
    }
    assert await migrate(data) is False


async def test_migrate_drops_the_http_profile_of_a_split_config() -> None:
    """A config split before this step existed loses the HTTP profile its protocols hold."""
    data: dict[str, Any] = {
        CONF_PLAYER_SPLIT_SESSION_ONLY: True,
        CONF_PLAYER_SPLIT_TREE: True,
        "players": {
            "pl_a": {"player_id": "pl_a", "provider": "dlna", "values": {"hide_in_ui": True}}
        },
        "player_protocols": {
            "dlna_a": {
                "protocol_id": "dlna_a",
                "provider": "dlna",
                "player_id": "pl_a",
                "role": "native",
                "enabled": True,
                "values": {"http_profile": "chunked", "flow_mode": True},
            }
        },
    }

    assert await migrate(data) is True

    assert data["player_protocols"]["dlna_a"]["values"] == {"flow_mode": True}
    assert data["players"]["pl_a"]["values"] == {"hide_in_ui": True}
    assert await migrate(data) is False


def test_migrate_drop_http_profile_tolerates_malformed_protocol_records() -> None:
    """Missing or non-dict protocol records and values are skipped without raising."""
    assert _migrate_drop_http_profile({"player_protocols": "broken"}) is False
    data: dict[str, Any] = {
        "player_protocols": {
            "a": "broken",
            "b": {"provider": "dlna"},
            "c": {"provider": "dlna", "values": None},
            "d": {"provider": "dlna", "values": {"http_profile": None}},
        }
    }
    assert _migrate_drop_http_profile(data) is True
    assert data["player_protocols"]["a"] == "broken"
    assert "values" not in data["player_protocols"]["b"]
    assert data["player_protocols"]["d"]["values"] == {}


def test_migrate_drop_http_profile_tolerates_malformed_data() -> None:
    """Missing or non-dict player configs and values are skipped without raising."""
    assert _migrate_drop_http_profile({}) is False
    assert _migrate_drop_http_profile({"players": "broken"}) is False
    data: dict[str, Any] = {
        "players": {
            "p1": "broken",
            "p2": {"provider": "dlna"},
            "p3": {"provider": "dlna", "values": None},
            "p4": {"provider": "dlna", "values": {"http_profile": None}},
        }
    }
    assert _migrate_drop_http_profile(data) is True
    assert data["players"]["p1"] == "broken"
    assert "values" not in data["players"]["p2"]
    assert data["players"]["p4"]["values"] == {}


def test_migrate_drop_fixed_content_length_drops_every_stored_value() -> None:
    """The fixed content length setting is dropped from every player and protocol."""
    data: dict[str, Any] = {
        "players": {
            # a player with the copy it held of its protocol player's value
            "sonos_1": {
                "provider": "sonos",
                "values": {
                    "dlna_1||protocol||requires_fixed_content_length": True,
                    "dlna_1||protocol||flow_mode": True,
                    "hide_in_ui": True,
                },
            },
            "ap_1": {"provider": "airplay", "values": {"hide_in_ui": False}},
        },
        "player_protocols": {
            "dlna_1": {
                "provider": "dlna",
                "values": {"requires_fixed_content_length": True, "flow_mode": False},
            },
            "wam_1": {
                "provider": "samsung_wam",
                "values": {"requires_fixed_content_length": False},
            },
            "cast_1": {"provider": "chromecast", "values": {"output_codec": "flac"}},
        },
    }

    assert _migrate_drop_fixed_content_length(data) is True
    assert data["players"]["sonos_1"]["values"] == {
        "dlna_1||protocol||flow_mode": True,
        "hide_in_ui": True,
    }
    assert data["players"]["ap_1"]["values"] == {"hide_in_ui": False}
    assert data["player_protocols"]["dlna_1"]["values"] == {"flow_mode": False}
    assert data["player_protocols"]["wam_1"]["values"] == {}
    assert data["player_protocols"]["cast_1"]["values"] == {"output_codec": "flac"}
    assert _migrate_drop_fixed_content_length(data) is False


async def test_migrate_drops_the_fixed_content_length_of_a_config_from_before_the_split() -> None:
    """A config from before the split keeps the setting in no record the split makes."""
    data: dict[str, Any] = {
        "players": {
            # a player with the copy it held of its protocol player's value
            "sonos_1": {
                "provider": "sonos",
                "player_type": "player",
                "values": {
                    "dlna_1||protocol||requires_fixed_content_length": True,
                    "output_codec": "mp3",
                },
            },
            "dlna_1": {
                "provider": "dlna",
                "player_type": "protocol",
                "values": {
                    "protocol_parent_id": "sonos_1",
                    "requires_fixed_content_length": True,
                    "flow_mode": False,
                },
            },
            "wam_1": {
                "provider": "samsung_wam",
                "player_type": "player",
                "values": {"requires_fixed_content_length": False},
            },
        }
    }

    assert await migrate(data) is True

    records = [*data["players"].values(), *data["player_protocols"].values()]
    assert not [
        key
        for record in records
        for key in record["values"]
        if "requires_fixed_content_length" in key
    ]
    assert data["player_protocols"]["sonos_1"]["values"] == {"playback_quality": "compressed"}
    assert data["player_protocols"]["dlna_1"]["values"] == {"flow_mode": False}
    assert data["player_protocols"]["wam_1"]["values"] == {}
    assert await migrate(data) is False


async def test_migrate_drops_the_fixed_content_length_of_a_split_config() -> None:
    """A split config loses the fixed content length setting its protocols hold."""
    data: dict[str, Any] = {
        CONF_PLAYER_SPLIT_SESSION_ONLY: True,
        CONF_PLAYER_SPLIT_TREE: True,
        "players": {
            "pl_a": {"player_id": "pl_a", "provider": "dlna", "values": {"hide_in_ui": True}}
        },
        "player_protocols": {
            "dlna_a": {
                "protocol_id": "dlna_a",
                "provider": "dlna",
                "player_id": "pl_a",
                "role": "native",
                "enabled": True,
                "values": {"requires_fixed_content_length": True, "flow_mode": False},
            }
        },
    }

    assert await migrate(data) is True

    assert data["player_protocols"]["dlna_a"]["values"] == {"flow_mode": False}
    assert data["players"]["pl_a"]["values"] == {"hide_in_ui": True}
    assert await migrate(data) is False


@pytest.mark.parametrize("tree", ["players", "player_protocols"])
def test_migrate_drop_fixed_content_length_tolerates_malformed_data(tree: str) -> None:
    """Missing or non-dict records and values are skipped without raising."""
    assert _migrate_drop_fixed_content_length({}) is False
    assert _migrate_drop_fixed_content_length({tree: "broken"}) is False
    data: dict[str, Any] = {
        tree: {
            "a": "broken",
            "b": {"provider": "dlna"},
            "c": {"provider": "dlna", "values": None},
            "d": {"provider": "dlna", "values": {"requires_fixed_content_length": None}},
        }
    }
    assert _migrate_drop_fixed_content_length(data) is True
    assert data[tree]["a"] == "broken"
    assert "values" not in data[tree]["b"]
    assert data[tree]["d"]["values"] == {}


def test_migrate_output_codec_moves_every_codec_to_its_playback_quality() -> None:
    """Each output codec moves to its level, in both trees and on a prefixed copy."""
    data: dict[str, Any] = {
        "players": {
            # a universal player keeps a copy of a protocol's value under a prefixed key
            "up_1": {
                "provider": "universal_player",
                "values": {"cast_1||protocol||output_codec": "aac", "hide_in_ui": True},
            },
        },
        "player_protocols": {
            "dlna_wav": {"provider": "dlna", "values": {"output_codec": "wav"}},
            "dlna_pcm": {"provider": "dlna", "values": {"output_codec": "pcm"}},
            "cast_flac": {"provider": "chromecast", "values": {"output_codec": "flac"}},
            "sonos_mp3": {"provider": "sonos", "values": {"output_codec": "mp3"}},
            "fully_aac": {"provider": "fully_kiosk", "values": {"output_codec": "aac"}},
        },
    }

    assert _migrate_output_codec_to_playback_quality(data) is True

    assert data["players"]["up_1"]["values"] == {
        "cast_1||protocol||playback_quality": "compressed",
        "hide_in_ui": True,
    }
    protocols = data["player_protocols"]
    assert protocols["dlna_wav"]["values"] == {"playback_quality": "lossless_uncompressed"}
    assert protocols["dlna_pcm"]["values"] == {"playback_quality": "lossless_uncompressed"}
    assert protocols["cast_flac"]["values"] == {"playback_quality": "lossless_compressed"}
    assert protocols["sonos_mp3"]["values"] == {"playback_quality": "compressed"}
    assert protocols["fully_aac"]["values"] == {"playback_quality": "compressed"}
    assert _migrate_output_codec_to_playback_quality(data) is False


def test_migrate_output_codec_drops_what_has_no_playback_quality() -> None:
    """An unknown value is dropped, a stored level is kept and a non-HTTP value never moves."""
    data: dict[str, Any] = {
        "providers": {"airplay--x": {"domain": "airplay"}},
        "player_protocols": {
            "dlna_unknown": {"provider": "dlna", "values": {"output_codec": "ogg"}},
            "dlna_number": {"provider": "dlna", "values": {"output_codec": 3}},
            "dlna_set": {
                "provider": "dlna",
                "values": {"output_codec": "mp3", "playback_quality": "lossless_uncompressed"},
            },
            "ap_1": {"provider": "airplay--x", "values": {"output_codec": "mp3"}},
            "snap_1": {"provider": "snapcast", "values": {"output_codec": "flac"}},
        },
    }

    assert _migrate_output_codec_to_playback_quality(data) is True

    protocols = data["player_protocols"]
    assert protocols["dlna_unknown"]["values"] == {}
    assert protocols["dlna_number"]["values"] == {}
    assert protocols["dlna_set"]["values"] == {"playback_quality": "lossless_uncompressed"}
    assert protocols["ap_1"]["values"] == {}
    assert protocols["snap_1"]["values"] == {}


def test_migrate_output_codec_tolerates_malformed_data() -> None:
    """Missing or non-dict trees, records and values are skipped without raising."""
    assert _migrate_output_codec_to_playback_quality({}) is False
    assert _migrate_output_codec_to_playback_quality({"player_protocols": "broken"}) is False
    data: dict[str, Any] = {
        "providers": "broken",
        "players": {
            "p1": "broken",
            "p2": {"provider": "dlna"},
            "p3": {"provider": "dlna", "values": None},
            "p4": {"provider": None, "values": {"output_codec": "flac"}},
        },
    }
    assert _migrate_output_codec_to_playback_quality(data) is True
    assert data["players"]["p1"] == "broken"
    assert "values" not in data["players"]["p2"]
    assert data["players"]["p4"]["values"] == {"playback_quality": "lossless_compressed"}


def test_migrate_airplay_apple_power_control_flips_stale_default() -> None:
    """Paired Apple TVs stuck on the old 'none' default get native power control."""
    data: dict[str, Any] = {
        "players": {
            "ap_paired_stale": {
                "provider": "airplay--x",
                "values": {"companion_credentials": "enc", "power_control": "none"},
            },
            "ap_unpaired": {"provider": "airplay--x", "values": {"power_control": "none"}},
            "ap_already_native": {
                "provider": "airplay--x",
                "values": {"companion_credentials": "enc", "power_control": "native"},
            },
            "cast_x": {
                "provider": "chromecast",
                "values": {"companion_credentials": "enc", "power_control": "none"},
            },
        }
    }
    assert _migrate_airplay_apple_power_control(data) is True
    values = data["players"]
    # only the paired Apple device on the stale default is changed
    assert values["ap_paired_stale"]["values"]["power_control"] == "native"
    assert values["ap_unpaired"]["values"]["power_control"] == "none"
    assert values["ap_already_native"]["values"]["power_control"] == "native"
    assert values["cast_x"]["values"]["power_control"] == "none"
    # idempotent: nothing left to migrate on a second pass
    assert _migrate_airplay_apple_power_control(data) is False


def test_migrate_provider_setup_data_moves_and_encrypts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Owned string keys move to setup_data encrypted; non-strings move raw; options stay."""
    monkeypatch.setitem(PROVIDER_SETUP_FLOW_KEYS, "demo", ("username", "password", "port"))
    data: dict[str, Any] = {
        "providers": {
            "demo": {
                "domain": "demo",
                "values": {
                    "username": "bob",
                    "password": "sekret",
                    "port": 8096,
                    "quality": "high",
                },
            }
        }
    }
    assert migrate_provider_setup_data(data, _fake_encrypt) is True
    cfg = data["providers"]["demo"]
    # the (non-owned) option key stays untouched in values
    assert cfg["values"] == {"quality": "high"}
    # owned string values are encrypted at rest, non-string values move as-is
    assert cfg["setup_data"]["username"] == ENCRYPT_SUFFIX + "bob"
    assert cfg["setup_data"]["password"] == ENCRYPT_SUFFIX + "sekret"
    assert cfg["setup_data"]["port"] == 8096


def test_migrate_provider_setup_data_moves_spotify_credentials() -> None:
    """
    A spotify instance configured before setup flows gets its credentials into setup_data.

    Regression: the provider reads these with get_setup_value, which falls back to the
    declared config entries only. None of them are declared, so on an upgraded install
    they all resolved to None and the provider failed to load.
    """
    data: dict[str, Any] = {
        "providers": {
            "spotify--abc": {
                "domain": "spotify",
                "values": {
                    "refresh_token_global": "global-token",
                    "refresh_token_dev": "dev-token",
                    "librespot_credentials": "playback-blob",
                    "client_id": "dev-client-id",
                    "account_id": "bob",
                    "refresh_token": "legacy-token",
                    "sync_podcast_progress": True,
                },
            }
        }
    }
    assert migrate_provider_setup_data(data, _fake_encrypt) is True
    cfg = data["providers"]["spotify--abc"]
    setup_data = cfg["setup_data"]
    assert setup_data["refresh_token_global"] == ENCRYPT_SUFFIX + "global-token"
    assert setup_data["refresh_token_dev"] == ENCRYPT_SUFFIX + "dev-token"
    assert setup_data["librespot_credentials"] == ENCRYPT_SUFFIX + "playback-blob"
    assert setup_data["client_id"] == ENCRYPT_SUFFIX + "dev-client-id"
    assert setup_data["account_id"] == ENCRYPT_SUFFIX + "bob"
    assert cfg["values"] == {"refresh_token": "legacy-token", "sync_podcast_progress": True}


def test_setup_flow_defaults_are_owned_keys() -> None:
    """Every key with a fallback default is also a key the migration owns."""
    for domain, defaults in PROVIDER_SETUP_FLOW_DEFAULTS.items():
        owned = PROVIDER_SETUP_FLOW_KEYS[domain]
        assert set(defaults).issubset(owned), f"{domain}: {set(defaults) - set(owned)}"


def test_migrate_provider_setup_data_restores_dropped_defaults() -> None:
    """
    A plex instance left on the default port gets that port back.

    Regression: a config value that matches its entry default is not persisted, so an
    instance on port 32400 had nothing in values to migrate and its server URL became
    'https://<host>:None'.
    """
    data: dict[str, Any] = {
        "providers": {
            "plex--abc": {
                "domain": "plex",
                "values": {"local_server_ip": "local.abc.plex.direct", "local_server_ssl": True},
            }
        }
    }
    assert migrate_provider_setup_data(data, _fake_encrypt) is True
    setup_data = data["providers"]["plex--abc"]["setup_data"]
    assert setup_data["local_server_port"] == 32400
    assert setup_data["local_server_verify_cert"] is True
    # the user's own value is migrated, not replaced by the default
    assert setup_data["local_server_ssl"] is True


def test_migrate_provider_setup_data_restores_defaults_after_earlier_run() -> None:
    """An install whose values were already moved by an earlier run is still repaired."""
    data: dict[str, Any] = {
        "providers": {
            "plex--abc": {
                "domain": "plex",
                "values": {},
                "setup_data": {
                    "local_server_ip": ENCRYPT_SUFFIX + "local.abc.plex.direct",
                    "local_server_ssl": True,
                },
            }
        }
    }
    assert migrate_provider_setup_data(data, _fake_encrypt) is True
    assert data["providers"]["plex--abc"]["setup_data"]["local_server_port"] == 32400


def test_migrate_provider_setup_data_keeps_explicit_values() -> None:
    """A stored choice is never overwritten by a fallback default."""
    data: dict[str, Any] = {
        "providers": {
            "jellyfin": {
                "domain": "jellyfin",
                "values": {},
                "setup_data": {"verify_ssl": False},
            }
        }
    }
    assert migrate_provider_setup_data(data, _fake_encrypt) is False
    assert data["providers"]["jellyfin"]["setup_data"]["verify_ssl"] is False


def test_migrate_provider_setup_data_encrypts_restored_strings() -> None:
    """A restored string default is encrypted at rest like a migrated one."""
    data: dict[str, Any] = {
        "providers": {
            "filesystem_local": {"domain": "filesystem_local", "values": {}},
        }
    }
    assert migrate_provider_setup_data(data, _fake_encrypt) is True
    assert data["providers"]["filesystem_local"]["setup_data"]["path"] == ENCRYPT_SUFFIX + "/media"


def test_migrate_provider_setup_data_restores_lastfm_network() -> None:
    """
    A Last.fm scrobbler that never stored its network choice gets it back.

    Regression: `_provider` defaulted to 'lastfm' so it was never persisted, and the
    None read back from setup_data made get_network raise on _NetworkType('None').
    """
    data: dict[str, Any] = {
        "providers": {
            "lastfm_scrobble": {
                "domain": "lastfm_scrobble",
                "values": {"_api_session_key": "abc123"},
            }
        }
    }
    assert migrate_provider_setup_data(data, _fake_encrypt) is True
    setup_data = data["providers"]["lastfm_scrobble"]["setup_data"]
    assert setup_data["_provider"] == ENCRYPT_SUFFIX + "lastfm"
    # an explicit librefm choice differed from the default and was persisted, so it
    # arrives via the move and is never overwritten by the fallback
    data = {
        "providers": {
            "lastfm_scrobble": {
                "domain": "lastfm_scrobble",
                "values": {"_provider": "librefm"},
            }
        }
    }
    assert migrate_provider_setup_data(data, _fake_encrypt) is True
    assert (
        data["providers"]["lastfm_scrobble"]["setup_data"]["_provider"]
        == ENCRYPT_SUFFIX + "librefm"
    )


def test_migrate_provider_setup_data_restores_smb_version() -> None:
    """
    An SMB share left on the default protocol version keeps its 3.0 pin.

    Regression: the dropped default made both mount paths read '' and skip the
    vers=3.0 mount option, silently changing protocol negotiation.
    """
    data: dict[str, Any] = {
        "providers": {
            "filesystem_smb": {"domain": "filesystem_smb", "values": {}},
        }
    }
    assert migrate_provider_setup_data(data, _fake_encrypt) is True
    setup_data = data["providers"]["filesystem_smb"]["setup_data"]
    assert setup_data["smb_version"] == ENCRYPT_SUFFIX + "3.0"
    # an explicit '' (auto-negotiate) choice was persisted and must win over the pin
    data = {
        "providers": {
            "filesystem_smb": {"domain": "filesystem_smb", "values": {"smb_version": ""}},
        }
    }
    assert migrate_provider_setup_data(data, _fake_encrypt) is True
    assert data["providers"]["filesystem_smb"]["setup_data"]["smb_version"] == ENCRYPT_SUFFIX


def test_migrate_receiver_and_connect_setup_values() -> None:
    """New setup-flow fields move to setup_data without losing typed values."""
    data: dict[str, Any] = {
        "providers": {
            "airplay_receiver--1": {
                "domain": "airplay_receiver",
                "values": {
                    "mass_player_id": "kitchen",
                    "airplay_name": "Kitchen AirPlay",
                    "log_level": "INFO",
                },
            },
            "ariacast_receiver": {
                "domain": "ariacast_receiver",
                "values": {"mass_player_id": "office"},
            },
            "spotify_connect--1": {
                "domain": "spotify_connect",
                "values": {
                    "mass_player_id": "living-room",
                    "publish_name": "Living Room Spotify",
                },
            },
            "vban_receiver--1": {
                "domain": "vban_receiver",
                "values": {
                    "bind_ip": "192.0.2.2",
                    "bind_port": 6981,
                    "sender_host": "192.0.2.10",
                    "vban_stream_name": "Studio",
                    "audio_format": "S16LE",
                    "sample_rate": 48000,
                    "audio_channels": 2,
                    "vban_queue_size": 100,
                },
            },
            "yandex_ynison--1": {
                "domain": "yandex_ynison",
                "values": {
                    "mass_player_id": "office",
                    "publish_name": "Office Yandex",
                    "allow_player_switch": False,
                },
            },
        }
    }

    assert migrate_provider_setup_data(data, _fake_encrypt) is True

    providers = data["providers"]
    assert providers["airplay_receiver--1"]["values"] == {"log_level": "INFO"}
    assert providers["airplay_receiver--1"]["setup_data"] == {
        "mass_player_id": ENCRYPT_SUFFIX + "kitchen",
        "airplay_name": ENCRYPT_SUFFIX + "Kitchen AirPlay",
    }
    assert providers["ariacast_receiver"]["values"] == {}
    assert providers["ariacast_receiver"]["setup_data"] == {
        "mass_player_id": ENCRYPT_SUFFIX + "office"
    }
    assert providers["spotify_connect--1"]["values"] == {}
    assert providers["spotify_connect--1"]["setup_data"] == {
        "mass_player_id": ENCRYPT_SUFFIX + "living-room",
        "publish_name": ENCRYPT_SUFFIX + "Living Room Spotify",
    }
    assert providers["vban_receiver--1"]["values"] == {"vban_queue_size": 100}
    assert providers["vban_receiver--1"]["setup_data"] == {
        "bind_ip": ENCRYPT_SUFFIX + "192.0.2.2",
        "bind_port": 6981,
        "sender_host": ENCRYPT_SUFFIX + "192.0.2.10",
        "vban_stream_name": ENCRYPT_SUFFIX + "Studio",
        "audio_format": ENCRYPT_SUFFIX + "S16LE",
        "sample_rate": 48000,
        "audio_channels": 2,
    }
    assert providers["yandex_ynison--1"]["values"] == {"allow_player_switch": False}
    assert providers["yandex_ynison--1"]["setup_data"] == {
        "mass_player_id": ENCRYPT_SUFFIX + "office",
        "publish_name": ENCRYPT_SUFFIX + "Office Yandex",
    }


def test_migrate_provider_setup_data_idempotent_and_preserves_existing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Already-encrypted values move unchanged, existing setup_data wins, second run is a no-op."""
    monkeypatch.setitem(PROVIDER_SETUP_FLOW_KEYS, "demo", ("token", "secret"))
    data: dict[str, Any] = {
        "providers": {
            "demo": {
                "domain": "demo",
                "values": {"token": ENCRYPT_SUFFIX + "abc", "secret": "raw"},
                # a value already collected into setup_data must not be clobbered
                "setup_data": {"secret": ENCRYPT_SUFFIX + "kept"},
            }
        }
    }
    assert migrate_provider_setup_data(data, _fake_encrypt) is True
    cfg = data["providers"]["demo"]
    # an already-encrypted value is moved without re-encrypting (no double prefix)
    assert cfg["setup_data"]["token"] == ENCRYPT_SUFFIX + "abc"
    # the pre-existing setup_data value survives; the stale values copy is dropped
    assert cfg["setup_data"]["secret"] == ENCRYPT_SUFFIX + "kept"
    assert cfg["values"] == {}
    # a second pass finds nothing left to move
    assert migrate_provider_setup_data(data, _fake_encrypt) is False


def test_migrate_provider_setup_data_multi_instance_and_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All instances of a mapped domain migrate; unmapped domains are left untouched."""
    monkeypatch.setitem(PROVIDER_SETUP_FLOW_KEYS, "demo", ("host",))
    data: dict[str, Any] = {
        "providers": {
            "demo--a": {"domain": "demo", "values": {"host": "h1"}},
            "demo--b": {"domain": "demo", "values": {"host": "h2", "quality": "x"}},
            "other": {"domain": "other", "values": {"host": "keep"}},
        }
    }
    assert migrate_provider_setup_data(data, _fake_encrypt) is True
    prov = data["providers"]
    assert prov["demo--a"]["setup_data"]["host"] == ENCRYPT_SUFFIX + "h1"
    assert prov["demo--a"]["values"] == {}
    assert prov["demo--b"]["setup_data"]["host"] == ENCRYPT_SUFFIX + "h2"
    assert prov["demo--b"]["values"] == {"quality": "x"}
    # a provider whose domain is not in the map is never touched
    assert prov["other"]["values"] == {"host": "keep"}
    assert "setup_data" not in prov["other"]


def test_migrate_provider_setup_data_noop() -> None:
    """Missing or empty provider config store reports no change."""
    assert migrate_provider_setup_data({}, _fake_encrypt) is False
    assert migrate_provider_setup_data({"providers": {}}, _fake_encrypt) is False


def test_migrate_provider_setup_data_real_domain_opensubsonic() -> None:
    """A real mapped domain moves its setup keys (incl. the redefined baseURL literal)."""
    data: dict[str, Any] = {
        "providers": {
            "opensubsonic--x": {
                "domain": "opensubsonic",
                "values": {
                    "username": "alice",
                    "password": "pw",
                    "baseURL": "https://music.example",
                    "port": 4533,
                    "enable_podcasts": True,
                },
            }
        }
    }
    assert migrate_provider_setup_data(data, _fake_encrypt) is True
    cfg = data["providers"]["opensubsonic--x"]
    assert cfg["setup_data"]["username"] == ENCRYPT_SUFFIX + "alice"
    assert cfg["setup_data"]["baseURL"] == ENCRYPT_SUFFIX + "https://music.example"
    assert cfg["setup_data"]["port"] == 4533
    # a genuine provider option is not part of the setup-flow key set and stays put
    assert cfg["values"] == {"enable_podcasts": True}


def test_migrate_player_setup_data_moves_credentials() -> None:
    """Player-owned credential/pairing keys move from values into setup_data."""
    data: dict[str, Any] = {
        "players": {
            "ap1": {
                "player_id": "ap1",
                "provider": "airplay",
                "values": {
                    "airplay_credentials": "ENC_ap2creds",
                    "companion_credentials": "ENC_companion",
                    "ap2password": "ENC_dead",
                    "password": "ENC_devpw",
                    "ignore_volume": True,
                },
            },
            "fk1": {
                "player_id": "fk1",
                "provider": "fully_kiosk",
                "values": {"password": "ENC_fk", "use_ssl": True},
            },
            "mpd1": {"player_id": "mpd1", "provider": "mpd", "values": {}},
            "ss1": {
                "player_id": "ss1",
                "provider": "sendspin",
                "values": {
                    "source_approval_dismissed": True,
                    "source_input_declined": True,
                    "sendspin_static_delay": 20,
                },
            },
            "sonos1": {
                "player_id": "sonos1",
                "provider": "sonos",
                "values": {"password": "keepme"},
            },
        }
    }
    assert _migrate_player_setup_data(data) is True
    ap1 = data["players"]["ap1"]
    # credentials moved to setup_data (already-encrypted, moved as-is)
    assert ap1["setup_data"] == {
        "airplay_credentials": "ENC_ap2creds",
        "companion_credentials": "ENC_companion",
    }
    # the RAOP device password (a genuine user option) and other options stay in values;
    # the vestigial ap2password is dropped entirely
    assert ap1["values"] == {"password": "ENC_devpw", "ignore_volume": True}
    # fully_kiosk password moves, unrelated option stays
    assert data["players"]["fk1"]["setup_data"] == {"password": "ENC_fk"}
    assert data["players"]["fk1"]["values"] == {"use_ssl": True}
    # the old sendspin input decline key is dropped, its replacement and options stay
    assert data["players"]["ss1"]["values"] == {
        "source_input_declined": True,
        "sendspin_static_delay": 20,
    }
    # a provider not in the map is untouched
    assert data["players"]["sonos1"]["values"] == {"password": "keepme"}
    assert "setup_data" not in data["players"]["sonos1"]
    # idempotent second run
    assert _migrate_player_setup_data(data) is False


def test_migrate_player_setup_data_preserves_existing_and_drops_null() -> None:
    """An existing setup_data value is never clobbered and stored nulls are dropped."""
    data: dict[str, Any] = {
        "players": {
            "ap1": {
                "player_id": "ap1",
                "provider": "airplay",
                "setup_data": {"airplay_credentials": "ENC_existing"},
                "values": {"airplay_credentials": "ENC_stale", "raop_credentials": None},
            }
        }
    }
    assert _migrate_player_setup_data(data) is True
    ap1 = data["players"]["ap1"]
    # the pre-existing setup_data value wins; the null raop value is just dropped
    assert ap1["setup_data"] == {"airplay_credentials": "ENC_existing"}
    assert ap1["values"] == {}


def test_migrate_player_setup_data_multi_instance_domain() -> None:
    """Domain matching handles multi-instance provider ids (<domain>--<id>)."""
    data: dict[str, Any] = {
        "players": {
            "ap1": {
                "player_id": "ap1",
                "provider": "airplay--2",
                "values": {"raop_credentials": "ENC_raop"},
            }
        }
    }
    assert _migrate_player_setup_data(data) is True
    assert data["players"]["ap1"]["setup_data"] == {"raop_credentials": "ENC_raop"}
    assert data["players"]["ap1"]["values"] == {}


def test_migrate_bose_soundtouch_presets_drops_player_values() -> None:
    """The per-player preset mappings are removed, other player values are kept."""
    data: dict[str, Any] = {
        "players": {
            "bose_soundtouch_a": {
                "player_id": "bose_soundtouch_a",
                "provider": "bose_soundtouch",
                "values": {
                    "preset_1_media": "library://playlist/12",
                    "preset_1_media_type": "playlist",
                    "preset_1_search": "morning",
                    "preset_1_selected_media": "library://playlist/12",
                    "volume_normalization": True,
                },
            },
            "bose_soundtouch_b": {
                "player_id": "bose_soundtouch_b",
                "provider": "bose_soundtouch--2",
                "values": {"preset_6_media": "library://radio/3"},
            },
        }
    }
    assert _migrate_bose_soundtouch_presets(data) is True
    assert data["players"]["bose_soundtouch_a"]["values"] == {"volume_normalization": True}
    assert data["players"]["bose_soundtouch_b"]["values"] == {}


def test_migrate_bose_soundtouch_presets_scoped_to_provider() -> None:
    """Players of other providers keep any similarly named values."""
    data: dict[str, Any] = {
        "players": {
            "other": {
                "player_id": "other",
                "provider": "sonos",
                "values": {"preset_1_media": "library://playlist/12"},
            }
        }
    }
    assert _migrate_bose_soundtouch_presets(data) is False
    assert data["players"]["other"]["values"] == {"preset_1_media": "library://playlist/12"}


def test_migrate_bose_soundtouch_presets_noop_when_absent() -> None:
    """Migration reports no change when no SoundTouch player stored presets."""
    data: dict[str, Any] = {
        "players": {
            "bose_soundtouch_a": {
                "player_id": "bose_soundtouch_a",
                "provider": "bose_soundtouch",
                "values": {"volume_normalization": True},
            }
        }
    }
    assert _migrate_bose_soundtouch_presets(data) is False


def _connected_plugins_data() -> dict[str, Any]:
    """Build a config store with legacy per-instance spotify_connect configurations."""
    return {
        "providers": {
            "spotify_connect--a": {
                "domain": "spotify_connect",
                "instance_id": "spotify_connect--a",
                "values": {"log_level": "DEBUG"},
                "setup_data": {
                    "backend": ENCRYPT_SUFFIX + "go_librespot",
                    "mass_player_id": ENCRYPT_SUFFIX + "kitchen",
                    "publish_name": ENCRYPT_SUFFIX + "Kitchen Spotify",
                },
            },
            "spotify_connect--b": {
                "domain": "spotify_connect",
                "instance_id": "spotify_connect--b",
                "values": {},
                "setup_data": {
                    "backend": ENCRYPT_SUFFIX + "soloist",
                    "soloist_api_key": ENCRYPT_SUFFIX + "secret-key",
                    "soloist_download_consent": True,
                    "mass_player_id": ENCRYPT_SUFFIX + "living-room",
                    "publish_name": ENCRYPT_SUFFIX + "Living Room Spotify",
                },
            },
        },
        "players": {
            "kitchen": {"player_id": "kitchen"},
            "living-room": {"player_id": "living-room"},
        },
    }


def test_migrate_connected_player_plugins_prefers_soloist_survivor(tmp_path: Path) -> None:
    """The soloist instance survives the collapse under the domain key, secrets intact."""
    data = _connected_plugins_data()

    assert migrate_connected_player_plugins(data, _fake_decrypt, str(tmp_path)) is True

    providers = data["providers"]
    assert set(providers) == {"spotify_connect"}
    survivor = providers["spotify_connect"]
    assert survivor["instance_id"] == "spotify_connect"
    # explicit players carried over in stored order, legacy keys dropped, secrets kept
    assert survivor["values"]["connected_players"] == ["kitchen", "living-room"]
    assert survivor["setup_data"] == {
        "backend": ENCRYPT_SUFFIX + "soloist",
        "soloist_api_key": ENCRYPT_SUFFIX + "secret-key",
        "soloist_download_consent": True,
    }
    # a second run finds the marker and changes nothing
    before = deepcopy(data)
    assert migrate_connected_player_plugins(data, _fake_decrypt, str(tmp_path)) is False
    assert data == before


def test_migrate_connected_player_plugins_skips_bare_domain_instance(tmp_path: Path) -> None:
    """
    A single instance keyed by the bare domain is never re-collapsed.

    The connected_players marker is dropped by the config store when it equals the
    entry default (empty selection), so the bare-domain id is the durable signal.
    """
    data: dict[str, Any] = {
        "providers": {
            "spotify_connect": {
                "domain": "spotify_connect",
                "instance_id": "spotify_connect",
                "values": {},
                "setup_data": {"backend": ENCRYPT_SUFFIX + "go_librespot"},
            }
        },
        "players": {},
    }
    before = deepcopy(data)

    assert migrate_connected_player_plugins(data, _fake_decrypt, str(tmp_path)) is False
    assert data == before


def test_migrate_connected_player_plugins_ignores_disabled_instances(tmp_path: Path) -> None:
    """A disabled instance neither survives the collapse nor contributes its player."""
    data = _connected_plugins_data()
    # the soloist instance was disabled by the user: the enabled go-librespot one
    # must survive and the disabled instance's player must not start advertising
    data["providers"]["spotify_connect--b"]["enabled"] = False

    assert migrate_connected_player_plugins(data, _fake_decrypt, str(tmp_path)) is True

    survivor = data["providers"]["spotify_connect"]
    assert survivor["setup_data"]["backend"] == ENCRYPT_SUFFIX + "go_librespot"
    assert survivor.get("enabled", True) is True
    assert survivor["values"]["connected_players"] == ["kitchen"]


def test_migrate_connected_player_plugins_all_disabled_stays_disabled(tmp_path: Path) -> None:
    """When every instance is disabled the collapsed provider stays disabled."""
    data = _connected_plugins_data()
    data["providers"]["spotify_connect--a"]["enabled"] = False
    data["providers"]["spotify_connect--b"]["enabled"] = False

    assert migrate_connected_player_plugins(data, _fake_decrypt, str(tmp_path)) is True

    survivor = data["providers"]["spotify_connect"]
    assert survivor["enabled"] is False
    # soloist preference still applies within the disabled pool
    assert survivor["setup_data"]["backend"] == ENCRYPT_SUFFIX + "soloist"
    assert survivor["values"]["connected_players"] == []


def test_migrate_connected_player_plugins_drops_auto_and_keeps_unstored_players(
    tmp_path: Path,
) -> None:
    """
    The removed automatic selection contributes nothing, every configured player carries over.

    Also one without a stored config: a player that only exists while its device is
    connected has none.
    """
    data: dict[str, Any] = {
        "providers": {
            "airplay_receiver--1": {
                "domain": "airplay_receiver",
                "instance_id": "airplay_receiver--1",
                "setup_data": {"mass_player_id": ENCRYPT_SUFFIX + "__auto__"},
            },
            "airplay_receiver--2": {
                "domain": "airplay_receiver",
                "instance_id": "airplay_receiver--2",
                "setup_data": {
                    "mass_player_id": ENCRYPT_SUFFIX + "web_player",
                    "airplay_name": ENCRYPT_SUFFIX + "Garage [AirPlay]",
                },
            },
            "airplay_receiver--3": {
                "domain": "airplay_receiver",
                "instance_id": "airplay_receiver--3",
                "setup_data": {"mass_player_id": ENCRYPT_SUFFIX + "office"},
            },
        },
        "players": {"office": {"player_id": "office"}},
    }

    assert migrate_connected_player_plugins(data, _fake_decrypt, str(tmp_path)) is True

    providers = data["providers"]
    assert set(providers) == {"airplay_receiver"}
    survivor = providers["airplay_receiver"]
    # the first instance survives; the explicitly configured players carry over
    assert survivor["instance_id"] == "airplay_receiver"
    assert survivor["values"]["connected_players"] == ["web_player", "office"]
    assert survivor["setup_data"] == {}


def test_migrate_connected_player_plugins_empty_selection_still_marks(tmp_path: Path) -> None:
    """With nothing to carry over the marker is still stored (empty selection)."""
    data: dict[str, Any] = {
        "providers": {
            "spotify_connect--1": {
                "domain": "spotify_connect",
                "instance_id": "spotify_connect--1",
                "setup_data": {"mass_player_id": ENCRYPT_SUFFIX + "__auto__"},
            }
        },
        "players": {},
    }

    assert migrate_connected_player_plugins(data, _fake_decrypt, str(tmp_path)) is True
    survivor = data["providers"]["spotify_connect"]
    assert survivor["values"]["connected_players"] == []
    assert survivor["setup_data"] == {}
    assert migrate_connected_player_plugins(data, _fake_decrypt, str(tmp_path)) is False


def test_migrate_connected_player_plugins_skips_undecryptable_instance(
    tmp_path: Path,
) -> None:
    """An unreadable instance contributes no player but never fails the migration."""

    def _failing_decrypt(value: str) -> str:
        if "broken" in value:
            raise InvalidDataError("Password decryption failed")
        return value.removeprefix(ENCRYPT_SUFFIX)

    data: dict[str, Any] = {
        "providers": {
            "spotify_connect--a": {
                "domain": "spotify_connect",
                "instance_id": "spotify_connect--a",
                "setup_data": {"mass_player_id": ENCRYPT_SUFFIX + "broken"},
            },
            "spotify_connect--b": {
                "domain": "spotify_connect",
                "instance_id": "spotify_connect--b",
                "setup_data": {"mass_player_id": ENCRYPT_SUFFIX + "kitchen"},
            },
        },
        "players": {"kitchen": {"player_id": "kitchen"}, "broken": {"player_id": "broken"}},
    }

    assert migrate_connected_player_plugins(data, _failing_decrypt, str(tmp_path)) is True
    survivor = data["providers"]["spotify_connect"]
    assert survivor["values"]["connected_players"] == ["kitchen"]


def test_migrate_connected_player_plugins_moves_soloist_data(tmp_path: Path) -> None:
    """The soloist data dir of an instance whose player survived moves to its new home."""
    data = _connected_plugins_data()
    old_dir = tmp_path / "spotify_connect" / "spotify_connect--b" / "soloist-data"
    old_dir.mkdir(parents=True)
    (old_dir / "credentials.json").write_text("{}")
    # the go-librespot instance's dir must stay where it is
    other_dir = tmp_path / "spotify_connect" / "spotify_connect--a" / "soloist-data"
    other_dir.mkdir(parents=True)

    assert migrate_connected_player_plugins(data, _fake_decrypt, str(tmp_path)) is True

    new_dir = tmp_path / "spotify_connect" / "spotify_connect_living-room" / "soloist-data"
    assert (new_dir / "credentials.json").is_file()
    assert not old_dir.exists()
    assert other_dir.is_dir()


def test_migrate_connected_player_plugins_sanitizes_player_id_in_data_path(
    tmp_path: Path,
) -> None:
    """Unsafe characters in the player id are sanitized in the soloist data dir name."""
    data: dict[str, Any] = {
        "providers": {
            "spotify_connect--a": {
                "domain": "spotify_connect",
                "instance_id": "spotify_connect--a",
                "setup_data": {
                    "backend": ENCRYPT_SUFFIX + "soloist",
                    "mass_player_id": ENCRYPT_SUFFIX + "media_player.living room",
                },
            }
        },
        "players": {"media_player.living room": {}},
    }
    old_dir = tmp_path / "spotify_connect" / "spotify_connect--a" / "soloist-data"
    old_dir.mkdir(parents=True)

    assert migrate_connected_player_plugins(data, _fake_decrypt, str(tmp_path)) is True
    new_dir = (
        tmp_path / "spotify_connect" / "spotify_connect_media_player.living_room" / "soloist-data"
    )
    assert new_dir.is_dir()


def test_migrate_connected_player_plugins_mandatory_player_cleanup(tmp_path: Path) -> None:
    """AriaCast/Ynison: the automatic selection is cleared, a player kept, names dropped."""
    data: dict[str, Any] = {
        "providers": {
            "ariacast_receiver": {
                "domain": "ariacast_receiver",
                "instance_id": "ariacast_receiver",
                "setup_data": {
                    "mass_player_id": ENCRYPT_SUFFIX + "__auto__",
                    "ariacast_name": ENCRYPT_SUFFIX + "Garage Cast",
                },
            },
            "yandex_ynison--1": {
                "domain": "yandex_ynison",
                "instance_id": "yandex_ynison--1",
                "setup_data": {
                    "mass_player_id": ENCRYPT_SUFFIX + "web_player",
                    "publish_name": ENCRYPT_SUFFIX + "Office Yandex",
                    "token": ENCRYPT_SUFFIX + "music-token",
                },
            },
            "yandex_ynison--2": {
                "domain": "yandex_ynison",
                "instance_id": "yandex_ynison--2",
                "setup_data": {"mass_player_id": ENCRYPT_SUFFIX + "office"},
            },
        },
        "players": {"office": {"player_id": "office"}},
    }

    assert migrate_connected_player_plugins(data, _fake_decrypt, str(tmp_path)) is True

    providers = data["providers"]
    assert providers["ariacast_receiver"]["setup_data"] == {}
    # a player without a stored config can be one that only exists while it is connected
    assert providers["yandex_ynison--1"]["setup_data"] == {
        "mass_player_id": ENCRYPT_SUFFIX + "web_player",
        "token": ENCRYPT_SUFFIX + "music-token",
    }
    assert providers["yandex_ynison--2"]["setup_data"] == {
        "mass_player_id": ENCRYPT_SUFFIX + "office"
    }
    assert migrate_connected_player_plugins(data, _fake_decrypt, str(tmp_path)) is False


def test_migrate_connected_player_plugins_noop(tmp_path: Path) -> None:
    """Missing or empty provider config store reports no change."""
    assert migrate_connected_player_plugins({}, _fake_decrypt, str(tmp_path)) is False
    data: dict[str, Any] = {"providers": {}}
    assert migrate_connected_player_plugins(data, _fake_decrypt, str(tmp_path)) is False


def test_migrate_player_icons_rewrites_legacy_values() -> None:
    """Legacy mdi-* and pre-1.0 picker icon values are rewritten to canonical ids."""
    data: dict[str, Any] = {
        "players": {
            "p1": {"player_id": "p1", "values": {"icon": "mdi-speaker-multiple"}},
            "p2": {"player_id": "p2", "values": {"icon": "sofa"}},
            "p3": {"player_id": "p3", "values": {"icon": "mdi-television-classic"}},
        }
    }
    assert _migrate_player_icons(data) is True
    assert data["players"]["p1"]["values"]["icon"] == "speakers"
    assert data["players"]["p2"]["values"]["icon"] == "living-room"
    assert data["players"]["p3"]["values"]["icon"] == "tv"


def test_migrate_player_icons_drops_unmappable_mdi_values() -> None:
    """An mdi-* icon with no close equivalent is dropped so the default applies."""
    data: dict[str, Any] = {
        "players": {
            "p1": {"player_id": "p1", "values": {"icon": "mdi-pac-man", "flow_mode": True}},
        }
    }
    assert _migrate_player_icons(data) is True
    assert data["players"]["p1"]["values"] == {"flow_mode": True}


def test_migrate_player_icons_keeps_canonical_and_unknown_ids() -> None:
    """Canonical ids are never touched; unknown non-mdi values are left in place."""
    data: dict[str, Any] = {
        "players": {
            "p1": {"player_id": "p1", "values": {"icon": "sonos"}},
            "p2": {"player_id": "p2", "values": {"icon": "kitchen"}},
            "p3": {"player_id": "p3", "values": {"icon": "dog"}},
        }
    }
    assert _migrate_player_icons(data) is False
    assert data["players"]["p1"]["values"]["icon"] == "sonos"
    assert data["players"]["p2"]["values"]["icon"] == "kitchen"
    assert data["players"]["p3"]["values"]["icon"] == "dog"


def test_migrate_player_icons_noop_when_absent() -> None:
    """Migration reports no change for players without a stored icon."""
    data: dict[str, Any] = {
        "players": {
            "p1": {"player_id": "p1", "values": {"flow_mode": True}},
            "p2": {"player_id": "p2"},
        }
    }
    assert _migrate_player_icons(data) is False


def test_migrate_player_icons_idempotent() -> None:
    """A second run over already-migrated data reports no change."""
    data: dict[str, Any] = {
        "players": {
            "p1": {"player_id": "p1", "values": {"icon": "mdi-speaker-multiple"}},
            "p2": {"player_id": "p2", "values": {"icon": "mdi-pac-man"}},
        }
    }
    assert _migrate_player_icons(data) is True
    assert _migrate_player_icons(data) is False
    assert data["players"]["p1"]["values"]["icon"] == "speakers"
    assert "icon" not in data["players"]["p2"]["values"]


def test_migrate_player_icons_tolerates_malformed_data() -> None:
    """Non-dict player configs/values and non-string icon values are skipped."""
    data: dict[str, Any] = {
        "players": {
            "p1": "not-a-dict",
            "p2": {"player_id": "p2", "values": "not-a-dict"},
            "p3": {"player_id": "p3", "values": {"icon": None}},
            "p4": {"player_id": "p4", "values": {"icon": 123}},
        }
    }
    assert _migrate_player_icons(data) is False
    assert data["players"]["p3"]["values"]["icon"] is None
    assert data["players"]["p4"]["values"]["icon"] == 123


def test_migrate_unrenamed_player_names() -> None:
    """A stored name that merely repeats the default name is cleared, real renames stay."""
    data: dict[str, Any] = {
        "players": {
            "never_renamed": {"name": "Living Room", "default_name": "Living Room"},
            "user_renamed": {"name": "Bathroom", "default_name": "solarium-bath-sl"},
            # without a default name, clearing the name would leave no name at all
            "without_default_name": {"name": "Kitchen"},
            "not_a_dict": "malformed",
        }
    }

    assert _migrate_unrenamed_player_names(data) is True
    assert data["players"]["never_renamed"]["name"] is None
    assert data["players"]["user_renamed"]["name"] == "Bathroom"
    assert data["players"]["without_default_name"]["name"] == "Kitchen"
    # the cleared name must not be picked up again on the next start
    assert _migrate_unrenamed_player_names(data) is False


def _sync_state(every: int) -> dict[str, Any]:
    return {"status": "idle", "schedule": {"type": "hourly", "enabled": True, "every": every}}


def test_migrate_apple_music_sync_schedule_moves_former_default() -> None:
    """Apple Music sync tasks on the former 12 hour default move to 24 hours, once."""
    data: dict[str, Any] = {
        "providers": {
            "apple_music--a": {"domain": "apple_music"},
            "spotify--s": {"domain": "spotify"},
        },
        "core": {
            "tasks": {
                "scheduled_task_states": {
                    "music_sync_apple_music--a_track": _sync_state(12),
                    "music_sync_apple_music--a_album": _sync_state(6),
                    "music_sync_spotify--s_track": _sync_state(12),
                }
            }
        },
    }
    states = data["core"]["tasks"]["scheduled_task_states"]

    assert _migrate_apple_music_sync_schedule(data) is True
    assert states["music_sync_apple_music--a_track"]["schedule"]["every"] == 24
    assert states["music_sync_apple_music--a_album"]["schedule"]["every"] == 6
    assert states["music_sync_spotify--s_track"]["schedule"]["every"] == 12
    assert data[CONF_APPLE_MUSIC_SYNC_SCHEDULE_MIGRATED] is True

    states["music_sync_apple_music--a_track"] = _sync_state(12)
    assert _migrate_apple_music_sync_schedule(data) is False
    assert states["music_sync_apple_music--a_track"]["schedule"]["every"] == 12


def test_migrate_apple_music_sync_schedule_noop_without_apple_music() -> None:
    """Installs without an Apple Music provider are left untouched."""
    data: dict[str, Any] = {
        "providers": {"spotify--s": {"domain": "spotify"}},
        "core": {"tasks": {"scheduled_task_states": {"music_sync_spotify--s_track": {}}}},
    }
    assert _migrate_apple_music_sync_schedule(data) is False
    assert CONF_APPLE_MUSIC_SYNC_SCHEDULE_MIGRATED not in data


def test_migrate_apple_music_sync_schedule_survives_malformed_state() -> None:
    """Malformed task state is skipped without raising."""
    data: dict[str, Any] = {
        "providers": {"apple_music--a": {"domain": "apple_music"}},
        "core": {"tasks": "broken"},
    }
    assert _migrate_apple_music_sync_schedule(data) is True
    assert data["core"] == {"tasks": "broken"}


async def test_migrate_emby_album_artists_heal_marks_each_emby_instance() -> None:
    """Each Emby instance gets the heal marker, once."""
    data: dict[str, Any] = {
        "providers": {
            "emby--a": {"domain": "emby", "values": {"sync": True}},
            "emby--b": {"domain": "emby"},
            "spotify--s": {"domain": "spotify", "values": {}},
        }
    }
    providers = data["providers"]

    assert await migrate(data) is True
    assert providers["emby--a"]["values"] == {
        "sync": True,
        CONF_EMBY_ALBUM_ARTISTS_HEAL_PENDING: True,
    }
    assert providers["emby--b"]["values"] == {CONF_EMBY_ALBUM_ARTISTS_HEAL_PENDING: True}
    assert providers["spotify--s"]["values"] == {}
    assert data[CONF_EMBY_ALBUM_ARTISTS_HEAL_MIGRATED] is True

    # a marker the provider cleared after healing must not come back
    del providers["emby--a"]["values"][CONF_EMBY_ALBUM_ARTISTS_HEAL_PENDING]
    assert _migrate_emby_album_artists_heal(data) is False
    assert CONF_EMBY_ALBUM_ARTISTS_HEAL_PENDING not in providers["emby--a"]["values"]


def test_migrate_emby_album_artists_heal_noop_without_emby() -> None:
    """Installs without an Emby provider are left untouched."""
    data: dict[str, Any] = {"providers": {"spotify--s": {"domain": "spotify", "values": {}}}}
    expected = deepcopy(data)
    assert _migrate_emby_album_artists_heal(data) is False
    assert data == expected


def test_migrate_emby_album_artists_heal_survives_malformed_config() -> None:
    """Malformed provider configs are skipped without raising."""
    assert _migrate_emby_album_artists_heal({"providers": "broken"}) is False
    data: dict[str, Any] = {"providers": {"emby--a": {"domain": "emby", "values": "broken"}}}
    _migrate_emby_album_artists_heal(data)
    assert data["providers"]["emby--a"]["values"] == "broken"


async def test_migrate_nicovideo_drop_mail_login_removes_credentials() -> None:
    """The mail, password and MFA code are removed, the user_session and other values kept."""
    data: dict[str, Any] = {
        "providers": {
            "nicovideo--a": {
                "domain": "nicovideo",
                "values": {"mail": "a@example.com", "password": "pw", "search_limit": 10},
                "setup_data": {
                    "mail": "enc-mail",
                    "password": "enc-pw",
                    "mfa": None,
                    "user_session": "enc-session",
                },
            },
            "nicovideo--b": {"domain": "nicovideo"},
            "spotify--s": {"domain": "spotify", "values": {"password": "pw"}},
        }
    }
    providers = data["providers"]

    assert await migrate(data) is True
    assert providers["nicovideo--a"]["values"] == {"search_limit": 10}
    assert providers["nicovideo--a"]["setup_data"] == {"user_session": "enc-session"}
    assert providers["nicovideo--b"] == {"domain": "nicovideo"}
    assert providers["spotify--s"]["values"] == {"password": "pw"}

    # a second run has nothing left to remove
    assert _migrate_nicovideo_drop_mail_login(data) is False


def test_migrate_nicovideo_drop_mail_login_survives_malformed_config() -> None:
    """Malformed provider configs are skipped without raising."""
    assert _migrate_nicovideo_drop_mail_login({"providers": "broken"}) is False
    data: dict[str, Any] = {
        "providers": {
            "nicovideo--a": {"domain": "nicovideo", "values": "broken", "setup_data": None},
            "broken": "broken",
        }
    }
    assert _migrate_nicovideo_drop_mail_login(data) is False
    assert data["providers"]["nicovideo--a"]["values"] == "broken"


async def test_migrate_milkdrop_drop_color_tint_removes_the_stored_value() -> None:
    """The color tint is removed from every milkdrop instance, whatever its value."""
    data: dict[str, Any] = {
        "providers": {
            "milkdrop_visualizer": {
                "domain": "milkdrop_visualizer",
                "values": {"color_tint": False, "log_level": "debug"},
            },
            "milkdrop_visualizer--b": {
                "domain": "milkdrop_visualizer",
                "values": {"color_tint": True},
            },
            "milkdrop_visualizer--c": {"domain": "milkdrop_visualizer"},
            "spotify--s": {"domain": "spotify", "values": {"color_tint": False}},
        }
    }
    providers = data["providers"]

    assert await migrate(data) is True
    assert providers["milkdrop_visualizer"]["values"] == {"log_level": "debug"}
    assert providers["milkdrop_visualizer--b"]["values"] == {}
    assert providers["milkdrop_visualizer--c"] == {"domain": "milkdrop_visualizer"}
    assert providers["spotify--s"]["values"] == {"color_tint": False}

    # a second run has nothing left to remove
    assert _migrate_milkdrop_drop_color_tint(data) is False


def test_migrate_milkdrop_drop_color_tint_survives_malformed_config() -> None:
    """Malformed provider configs are skipped without raising."""
    assert _migrate_milkdrop_drop_color_tint({"providers": "broken"}) is False
    data: dict[str, Any] = {
        "providers": {
            "milkdrop_visualizer": {"domain": "milkdrop_visualizer", "values": "broken"},
            "broken": "broken",
        }
    }
    assert _migrate_milkdrop_drop_color_tint(data) is False
    assert data["providers"]["milkdrop_visualizer"]["values"] == "broken"


async def test_migrate_drops_the_milkdrop_color_tint_of_a_config_from_before_the_split() -> None:
    """A config from before the split loses the color tint and is split in one start."""
    data: dict[str, Any] = {
        "providers": {
            "milkdrop_visualizer": {
                "domain": "milkdrop_visualizer",
                "values": {"color_tint": False},
            }
        },
        "players": {
            "sonos_1": {
                "provider": "sonos",
                "player_type": "player",
                # a player's value of that name is not the provider's
                "values": {"output_codec": "mp3", "color_tint": False},
            }
        },
    }

    assert await migrate(data) is True

    assert data["providers"]["milkdrop_visualizer"]["values"] == {}
    assert data["player_protocols"]["sonos_1"]["values"] == {
        "playback_quality": "compressed",
        "color_tint": False,
    }
    assert await migrate(data) is False


async def test_migrate_drops_the_milkdrop_color_tint_of_a_split_config() -> None:
    """A config split before this step existed loses the color tint, and nothing else."""
    data: dict[str, Any] = {
        CONF_PLAYER_SPLIT_SESSION_ONLY: True,
        CONF_PLAYER_SPLIT_TREE: True,
        "providers": {
            "milkdrop_visualizer": {
                "domain": "milkdrop_visualizer",
                "values": {"color_tint": True},
            }
        },
        "players": {"pl_a": {"player_id": "pl_a", "provider": "sonos", "values": {}}},
        "player_protocols": {
            "sonos_a": {
                "protocol_id": "sonos_a",
                "provider": "sonos",
                "player_id": "pl_a",
                "role": "native",
                "enabled": True,
                "values": {"playback_quality": "compressed", "color_tint": True},
            }
        },
    }
    records = deepcopy({key: data[key] for key in ("players", "player_protocols")})

    assert await migrate(data) is True

    assert data["providers"]["milkdrop_visualizer"]["values"] == {}
    assert {key: data[key] for key in ("players", "player_protocols")} == records
    assert await migrate(data) is False


def test_migrate_raumfeld_icy_basic_moves_former_default() -> None:
    """Raumfeld players on the former "full" ICY default move to "basic", once."""
    data: dict[str, Any] = {
        "players": {
            "raumfeld_bar": {"provider": "raumfeld", "values": {"enable_icy_metadata": "full"}},
            "raumfeld_bank": {
                "provider": "raumfeld",
                "values": {"enable_icy_metadata": "disabled"},
            },
            "raumfeld_tv": {"provider": "raumfeld"},
            "sonos_kitchen": {"provider": "sonos", "values": {"enable_icy_metadata": "full"}},
        }
    }
    players = data["players"]

    assert _migrate_raumfeld_icy_basic(data) is True
    assert players["raumfeld_bar"]["values"]["enable_icy_metadata"] == "basic"
    assert players["raumfeld_bank"]["values"]["enable_icy_metadata"] == "disabled"
    assert "values" not in players["raumfeld_tv"]
    assert players["sonos_kitchen"]["values"]["enable_icy_metadata"] == "full"
    assert data["raumfeld_icy_basic_migrated"] is True

    # a "full" picked afterwards is kept
    players["raumfeld_bar"]["values"]["enable_icy_metadata"] = "full"
    assert _migrate_raumfeld_icy_basic(data) is False
    assert players["raumfeld_bar"]["values"]["enable_icy_metadata"] == "full"


def test_migrate_raumfeld_icy_basic_survives_malformed_players() -> None:
    """Malformed player configs are skipped without raising."""
    data: dict[str, Any] = {
        "players": {
            "raumfeld_bar": "broken",
            "raumfeld_bank": {"provider": "raumfeld", "values": 1},
        }
    }
    assert _migrate_raumfeld_icy_basic(data) is True
    assert data["players"]["raumfeld_bar"] == "broken"


def test_migrate_airplay_sync_adjust_sign_negates_once() -> None:
    """Every stored AirPlay sync adjust is negated once; garbage and other protocols stay."""
    data: dict[str, Any] = {
        "players": {
            # a record the split could not convert keeps its values on the player
            "ap_unsplit": {"provider": "airplay", "values": {"sync_adjust": -120}},
            "sonos_1": {"provider": "sonos", "values": {"icon": "speaker"}},
            "broken": "not a mapping",
        },
        "player_protocols": {
            "ap_tv": {"provider": "airplay", "player_id": "pl_a", "values": {"sync_adjust": -150}},
            "ap_late": {"provider": "airplay", "player_id": "pl_b", "values": {"sync_adjust": 90}},
            "ap_zero": {"provider": "airplay", "values": {"sync_adjust": 0}},
            "ap_none": {"provider": "airplay", "values": {"sync_adjust": None}},
            "ap_text": {"provider": "airplay", "values": {"sync_adjust": "-50"}},
            "ap_unset": {"provider": "airplay", "values": {"output_codec": "flac"}},
            "ap_no_values": {"provider": "airplay"},
            "ap_bad_values": {"provider": "airplay", "values": ["sync_adjust"]},
            "ap_no_provider": {"values": {"sync_adjust": -30}},
            "sq_amp": {"provider": "squeezelite", "values": {"sync_adjust": 30}},
        },
    }
    expected = deepcopy(data)
    expected["players"]["ap_unsplit"]["values"]["sync_adjust"] = 120
    expected["player_protocols"]["ap_tv"]["values"]["sync_adjust"] = 150
    expected["player_protocols"]["ap_late"]["values"]["sync_adjust"] = -90
    expected[CONF_AIRPLAY_SYNC_ADJUST_SIGN_MIGRATED] = True

    assert _migrate_airplay_sync_adjust_sign(data) is True
    assert data == expected

    # the marker makes a second run a no-op
    assert _migrate_airplay_sync_adjust_sign(data) is False
    assert data == expected


def test_migrate_airplay_sync_adjust_sign_claims_the_marker_without_airplay() -> None:
    """A config without AirPlay is marked too, so an AirPlay value stored later keeps its sign."""
    data: dict[str, Any] = {"players": "broken", "player_protocols": None}

    assert _migrate_airplay_sync_adjust_sign(data) is True
    assert data[CONF_AIRPLAY_SYNC_ADJUST_SIGN_MIGRATED] is True

    data["player_protocols"] = {"ap_tv": {"provider": "airplay", "values": {"sync_adjust": 100}}}
    assert _migrate_airplay_sync_adjust_sign(data) is False
    assert data["player_protocols"]["ap_tv"]["values"]["sync_adjust"] == 100


async def test_migrate_negates_the_airplay_sync_adjust_of_a_config_from_before_the_split() -> None:
    """A config from before the split gets the AirPlay sync adjust negated in the split records."""
    data: dict[str, Any] = {
        "players": {
            # an AirPlay device without another provider
            "ap_tv": {
                "provider": "airplay",
                "player_type": "player",
                "values": {"sync_adjust": -150, "hide_in_ui": True},
            },
            # an AirPlay output of a Sonos speaker
            "sonos_1": {"provider": "sonos", "player_type": "player", "values": {}},
            "ap_sonos": {
                "provider": "airplay",
                "player_type": "protocol",
                "values": {"protocol_parent_id": "sonos_1", "sync_adjust": 40},
            },
            "sq_amp": {
                "provider": "squeezelite",
                "player_type": "player",
                "values": {"sync_adjust": 30},
            },
        }
    }

    assert await migrate(data) is True

    protocols = data["player_protocols"]
    assert protocols["ap_tv"]["values"]["sync_adjust"] == 150
    assert protocols["ap_sonos"]["values"]["sync_adjust"] == -40
    assert protocols["sq_amp"]["values"]["sync_adjust"] == 30
    assert not [record for record in data["players"].values() if "sync_adjust" in record["values"]]
    stored = deepcopy(data)
    assert await migrate(data) is False
    assert data == stored


async def test_migrate_artist_catalogue_selection_moves_to_library_album_tracks() -> None:
    """A stored artist selection that listed the whole catalogue plays the library albums."""
    for stored in ("all_tracks", "all_album_tracks"):
        values: dict[str, Any] = {"default_enqueue_select_artist": stored, "other": "kept"}
        data: dict[str, Any] = {"core": {"player_queues": {"values": values}}}

        assert await migrate(data) is True
        assert values == {
            "default_enqueue_select_artist": "library_album_tracks",
            "other": "kept",
        }
        assert _migrate_artist_catalogue_selection(data) is False


async def test_migrate_moves_the_artist_selection_of_a_config_from_before_the_split() -> None:
    """A config from before the split gets the new artist selection and its split in one start."""
    values: dict[str, Any] = {"default_enqueue_select_artist": "all_tracks"}
    data: dict[str, Any] = {
        "core": {"player_queues": {"values": values}},
        "players": {
            "sonos_1": {
                "provider": "sonos",
                "player_type": "player",
                "values": {"output_codec": "mp3"},
            }
        },
    }

    assert await migrate(data) is True

    assert values == {"default_enqueue_select_artist": "library_album_tracks"}
    assert data["player_protocols"]["sonos_1"]["values"] == {"playback_quality": "compressed"}
    assert await migrate(data) is False


async def test_migrate_moves_the_artist_selection_of_a_split_config() -> None:
    """A config split before this step existed gets the new artist selection, and nothing else."""
    values: dict[str, Any] = {"default_enqueue_select_artist": "all_album_tracks"}
    data: dict[str, Any] = {
        CONF_PLAYER_SPLIT_SESSION_ONLY: True,
        CONF_PLAYER_SPLIT_TREE: True,
        "core": {"player_queues": {"values": values}},
        "players": {"pl_a": {"player_id": "pl_a", "provider": "sonos", "values": {}}},
        "player_protocols": {
            "sonos_a": {
                "protocol_id": "sonos_a",
                "provider": "sonos",
                "player_id": "pl_a",
                "role": "native",
                "enabled": True,
                "values": {"playback_quality": "compressed"},
            }
        },
    }
    records = deepcopy({key: data[key] for key in ("players", "player_protocols")})

    assert await migrate(data) is True

    assert values == {"default_enqueue_select_artist": "library_album_tracks"}
    assert {key: data[key] for key in ("players", "player_protocols")} == records
    assert await migrate(data) is False


def test_migrate_artist_catalogue_selection_leaves_other_data_alone() -> None:
    """An offered selection, or a missing or malformed config, is left as it is."""
    cases: list[dict[str, Any]] = [
        {},
        {"core": "broken"},
        {"core": {"player_queues": "broken"}},
        {"core": {"player_queues": {"values": None}}},
        {"core": {"player_queues": {"values": {}}}},
        {"core": {"player_queues": {"values": {"default_enqueue_select_artist": ["odd"]}}}},
        {"core": {"player_queues": {"values": {"default_enqueue_select_artist": "top_tracks"}}}},
    ]
    for data in cases:
        before = deepcopy(data)
        assert _migrate_artist_catalogue_selection(data) is False
        assert data == before


def test_migrate_raumfeld_icy_basic_noop_without_raumfeld() -> None:
    """Installs without a Raumfeld player are left untouched."""
    data: dict[str, Any] = {"players": {"sonos_kitchen": {"provider": "sonos"}}}
    assert _migrate_raumfeld_icy_basic(data) is False
    assert "raumfeld_icy_basic_migrated" not in data


def test_migrate_provider_setup_data_moves_opensubsonic_legacy_auth() -> None:
    """A legacy auth choice stored as an Open Subsonic option moves to setup_data unencrypted."""
    data: dict[str, Any] = {
        "providers": {
            "opensubsonic--a": {
                "domain": "opensubsonic",
                "values": {"enable_legacy_auth": True, "enable_radio_stations": False},
                "setup_data": {"baseURL": ENCRYPT_SUFFIX + "https://sonic.local"},
            },
            "opensubsonic--b": {
                "domain": "opensubsonic",
                "values": {"enable_legacy_auth": False},
                # a choice already collected by the setup flow wins
                "setup_data": {"enable_legacy_auth": True},
            },
        }
    }
    assert migrate_provider_setup_data(data, _fake_encrypt) is True
    first = data["providers"]["opensubsonic--a"]
    assert first["values"] == {"enable_radio_stations": False}
    assert first["setup_data"]["enable_legacy_auth"] is True
    second = data["providers"]["opensubsonic--b"]
    assert second["values"] == {}
    assert second["setup_data"]["enable_legacy_auth"] is True
    assert migrate_provider_setup_data(data, _fake_encrypt) is False


async def test_migrate_removes_the_retired_builtin_provider_configs() -> None:
    """The configs of the retired builtin providers go, every other provider config stays."""
    data: dict[str, Any] = {
        CONF_PLAYER_SPLIT_SESSION_ONLY: True,
        CONF_PLAYER_SPLIT_TREE: True,
        "providers": {
            "builtin_player": {"domain": "builtin_player", "type": "player", "values": {}},
            "universal_player": {"domain": "universal_player", "type": "player", "values": {}},
            # a community provider that is not installed keeps its settings
            "community_provider--a": {
                "domain": "community_provider",
                "type": "music",
                "values": {"username": "me"},
            },
            "sonos": {"domain": "sonos", "type": "player", "values": {}},
        },
    }
    kept = deepcopy({key: data["providers"][key] for key in ("community_provider--a", "sonos")})

    await migrate(data)

    assert data["providers"] == kept
    # a second run has nothing left to remove
    assert _migrate_remove_retired_builtin_providers(data) is False
    assert await migrate(data) is False
    assert data["providers"] == kept


def test_migrate_removes_the_sendspin_source_plugin_config() -> None:
    """The config of the retired Sendspin Source plugin goes, the one of Sendspin stays."""
    data: dict[str, Any] = {
        "providers": {
            "sendspin_source": {
                "domain": "sendspin_source",
                "type": "plugin",
                "values": {"target_latency": 800},
            },
            "sendspin": {"domain": "sendspin", "type": "player", "values": {}},
        }
    }
    kept = deepcopy(data["providers"]["sendspin"])

    assert _migrate_remove_retired_builtin_providers(data) is True
    assert data["providers"] == {"sendspin": kept}
    assert _migrate_remove_retired_builtin_providers(data) is False


def test_migrate_remove_retired_builtin_providers_leaves_other_data_alone() -> None:
    """Settings without a retired provider config, or with malformed ones, are left as they are."""
    cases: list[dict[str, Any]] = [
        {},
        {"providers": "broken"},
        {"providers": None},
        {"providers": {}},
        {
            "providers": {
                "sonos": {"domain": "sonos", "type": "player"},
                "broken": "builtin_player",
                "no_domain": {"type": "player", "values": {}},
                "odd_domain": {"domain": ["universal_player"]},
                "none_domain": {"domain": None},
            }
        },
    ]
    for data in cases:
        before = deepcopy(data)
        assert _migrate_remove_retired_builtin_providers(data) is False
        assert data == before


def test_migrate_stereo_pair_player_type_stores_a_multi_channel_player() -> None:
    """A player stored as a stereo pair becomes a multi-channel player; other types stay."""
    data: dict[str, Any] = {
        "players": {
            "cast_pair": {"provider": "chromecast", "player_type": "stereo_pair"},
            "cast_speaker": {"provider": "chromecast", "player_type": "player"},
            "cast_group": {"provider": "chromecast", "player_type": "group"},
            "no_type": {"provider": "sonos"},
        }
    }
    players = data["players"]

    assert _migrate_stereo_pair_player_type(data) is True
    assert players["cast_pair"]["player_type"] == "multi_channel"
    assert players["cast_speaker"]["player_type"] == "player"
    assert players["cast_group"]["player_type"] == "group"
    assert "player_type" not in players["no_type"]

    assert _migrate_stereo_pair_player_type(data) is False


def test_migrate_stereo_pair_player_type_survives_malformed_players() -> None:
    """Malformed player configs are skipped without raising."""
    cases: list[dict[str, Any]] = [
        {},
        {"players": "broken"},
        {"players": {"cast_pair": "broken", "cast_other": {"player_type": 1}}},
    ]
    for data in cases:
        before = deepcopy(data)
        assert _migrate_stereo_pair_player_type(data) is False
        assert data == before


def _ai_engines_data() -> dict[str, Any]:
    """Return settings in the shape stored before the default AI engines existed."""
    return {
        "core": {
            "players": {
                "domain": "players",
                "values": {"announce_tts_engine": "hass--1/tts.cloud"},
            },
        },
        "providers": {
            "hass--1": {"domain": "hass", "instance_id": "hass--1"},
            "openai--1": {"domain": "openai_compatible", "instance_id": "openai--1"},
            "ai_radio": {
                "domain": "ai_radio",
                "instance_id": "ai_radio",
                "setup_data": {
                    "ai_engine": ENCRYPT_SUFFIX + "hass--1/ai_task.a",
                    "tts_engine": ENCRYPT_SUFFIX + "hass--1/tts.other",
                    "weather_city": ENCRYPT_SUFFIX + "Amsterdam",
                },
            },
            "smart_playlist": {
                "domain": "smart_playlist",
                "instance_id": "smart_playlist",
                "values": {"ai_engine": "openai--1/gpt", "ai_descriptions": True},
            },
            "music_quiz": {
                "domain": "music_quiz",
                "instance_id": "music_quiz",
                "values": {"ai_engine": "hass--1/ai_task.a"},
            },
            "playlist_metadata": {
                "domain": "playlist_metadata",
                "instance_id": "playlist_metadata",
                "values": {
                    "ai_text_engine": "openai--1/gpt",
                    "ai_image_engine": "openai--1/image",
                },
            },
        },
    }


def test_migrate_ai_default_engines_seeds_the_defaults() -> None:
    """Each default is seeded from the picks and the picks equal to it follow it."""
    data = _ai_engines_data()

    assert migrate_ai_default_engines(data, _fake_decrypt) is True

    assert data["core"]["ai"] == {
        "domain": "ai",
        "values": {
            # two picks each: the AI Radio pick breaks the tie
            "text_engine": "hass--1/ai_task.a",
            "speech_engine": "hass--1/tts.cloud",
            "image_engine": "openai--1/image",
        },
    }
    providers = data["providers"]
    assert data["core"]["players"]["values"] == {}
    # a different pick stays the feature's own choice, still encrypted
    assert providers["ai_radio"]["setup_data"] == {
        "tts_engine": ENCRYPT_SUFFIX + "hass--1/tts.other",
        "weather_city": ENCRYPT_SUFFIX + "Amsterdam",
    }
    assert providers["smart_playlist"]["values"] == {
        "ai_engine": "openai--1/gpt",
        "ai_descriptions": True,
    }
    assert providers["music_quiz"]["values"] == {}
    assert providers["playlist_metadata"]["values"] == {"ai_text_engine": "openai--1/gpt"}
    assert data[CONF_AI_DEFAULT_ENGINES_MIGRATED] is True


def test_migrate_ai_default_engines_seeds_the_most_used_text_engine() -> None:
    """The text engine most features picked becomes the default, whatever came first."""
    data = _ai_engines_data()
    data["providers"]["music_quiz"]["values"]["ai_engine"] = "openai--1/gpt"

    migrate_ai_default_engines(data, _fake_decrypt)

    assert data["core"]["ai"]["values"]["text_engine"] == "openai--1/gpt"
    providers = data["providers"]
    assert providers["ai_radio"]["setup_data"]["ai_engine"] == ENCRYPT_SUFFIX + "hass--1/ai_task.a"
    assert "ai_engine" not in providers["smart_playlist"]["values"]
    assert "ai_engine" not in providers["music_quiz"]["values"]
    assert "ai_text_engine" not in providers["playlist_metadata"]["values"]


def test_migrate_ai_default_engines_seeds_speech_from_ai_radio() -> None:
    """Without an announcement engine the AI Radio speech engine becomes the default."""
    data = _ai_engines_data()
    del data["core"]["players"]

    migrate_ai_default_engines(data, _fake_decrypt)

    assert data["core"]["ai"]["values"]["speech_engine"] == "hass--1/tts.other"
    assert "tts_engine" not in data["providers"]["ai_radio"]["setup_data"]


def test_migrate_ai_default_engines_skips_engines_of_unusable_plugins() -> None:
    """An engine whose plugin is removed or disabled is not seeded, and its picks stay."""
    data = _ai_engines_data()
    del data["providers"]["hass--1"]
    data["providers"]["openai--1"]["enabled"] = False

    migrate_ai_default_engines(data, _fake_decrypt)

    assert "ai" not in data["core"]
    assert data["core"]["players"]["values"] == {"announce_tts_engine": "hass--1/tts.cloud"}
    assert data["providers"]["music_quiz"]["values"] == {"ai_engine": "hass--1/ai_task.a"}
    assert data[CONF_AI_DEFAULT_ENGINES_MIGRATED] is True


def test_migrate_ai_default_engines_keeps_a_stored_default() -> None:
    """A default that is set already is kept, and the picks equal to it follow it."""
    data = _ai_engines_data()
    data["core"]["ai"] = {"domain": "ai", "values": {"text_engine": "openai--1/gpt"}}

    migrate_ai_default_engines(data, _fake_decrypt)

    ai_values = data["core"]["ai"]["values"]
    assert ai_values["text_engine"] == "openai--1/gpt"
    assert ai_values["speech_engine"] == "hass--1/tts.cloud"
    providers = data["providers"]
    assert "ai_engine" not in providers["smart_playlist"]["values"]
    assert providers["music_quiz"]["values"] == {"ai_engine": "hass--1/ai_task.a"}
    assert providers["ai_radio"]["setup_data"]["ai_engine"] == ENCRYPT_SUFFIX + "hass--1/ai_task.a"


def test_migrate_ai_default_engines_skips_an_undecryptable_pick() -> None:
    """A pick that can not be decrypted is neither seeded nor removed."""

    def _failing_decrypt(value: str) -> str:
        if "broken" in value:
            raise InvalidDataError("Password decryption failed")
        return value.removeprefix(ENCRYPT_SUFFIX)

    data = _ai_engines_data()
    data["providers"]["ai_radio"]["setup_data"]["ai_engine"] = ENCRYPT_SUFFIX + "broken"

    assert migrate_ai_default_engines(data, _failing_decrypt) is True

    assert data["providers"]["ai_radio"]["setup_data"]["ai_engine"] == ENCRYPT_SUFFIX + "broken"
    # without the AI Radio pick, the Smart Playlists pick wins the tie
    assert data["core"]["ai"]["values"]["text_engine"] == "openai--1/gpt"


def test_migrate_ai_default_engines_runs_once() -> None:
    """The marker is claimed without any pick, so later picks equal to a default are kept."""
    data: dict[str, Any] = {}

    assert migrate_ai_default_engines(data, _fake_decrypt) is True
    assert data == {CONF_AI_DEFAULT_ENGINES_MIGRATED: True}

    data.update(_ai_engines_data())
    data["core"]["ai"] = {"domain": "ai", "values": {"text_engine": "openai--1/gpt"}}
    before = deepcopy(data)
    assert migrate_ai_default_engines(data, _fake_decrypt) is False
    assert data == before


def test_migrate_ai_default_engines_survives_malformed_data() -> None:
    """Malformed blocks and values are skipped without raising."""
    cases: list[dict[str, Any]] = [
        {"core": "broken", "providers": "broken"},
        {"core": {"players": "broken", "ai": "broken"}, "providers": {"x": "broken"}},
        {
            "core": {"players": {"values": {"announce_tts_engine": 5}}, "ai": {"values": []}},
            "providers": {
                "hass--1": {"domain": "hass"},
                "smart_playlist": {"domain": "smart_playlist", "values": "broken"},
                "music_quiz": {"domain": "music_quiz", "values": {"ai_engine": "no-separator"}},
                "ai_radio": {"domain": "ai_radio", "setup_data": {"ai_engine": None}},
            },
        },
    ]
    for data in cases:
        assert migrate_ai_default_engines(data, _fake_decrypt) is True
        assert data[CONF_AI_DEFAULT_ENGINES_MIGRATED] is True
    assert cases[2]["providers"]["music_quiz"]["values"] == {"ai_engine": "no-separator"}


def test_migrate_ai_default_engines_repairs_a_stored_block_without_domain() -> None:
    """A seeded block always carries its domain, so it parses as a core config."""
    data = _ai_engines_data()
    data["core"]["ai"] = {"values": "broken"}

    migrate_ai_default_engines(data, _fake_decrypt)

    assert data["core"]["ai"]["domain"] == "ai"
    assert data["core"]["ai"]["values"]["text_engine"] == "hass--1/ai_task.a"
