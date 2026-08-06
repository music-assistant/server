"""Tests for (settings.json) config migrations."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from music_assistant_models.errors import InvalidDataError

from music_assistant.constants import (
    CONF_NFS_SUBFOLDER_MIGRATED,
    CONF_SERVER_ID,
    ENCRYPT_SUFFIX,
)
from music_assistant.controllers.config.controller import ConfigController
from music_assistant.controllers.config.migrations import (
    PROVIDER_SETUP_FLOW_KEYS,
    _migrate_airplay_apple_power_control,
    _migrate_airplay_receiver_ghost_players,
    _migrate_bluesound_http_profile,
    _migrate_bose_soundtouch_presets,
    _migrate_output_limiter,
    _migrate_player_icons,
    _migrate_player_setup_data,
    migrate_hass_engine_selection,
    migrate_nfs_subfolder_into_export_path,
    migrate_provider_setup_data,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _fake_encrypt(value: str) -> str:
    """Mirror ConfigController.encrypt_string: prefix once, idempotent for encrypted values."""
    return value if value.startswith(ENCRYPT_SUFFIX) else ENCRYPT_SUFFIX + value


def _fake_decrypt(value: str) -> str:
    """Mirror ConfigController.decrypt_string: strip the prefix, pass plain values through."""
    return value.removeprefix(ENCRYPT_SUFFIX)


def test_migrate_output_limiter_drops_stored_values() -> None:
    """The removed output limiter setting is dropped, other player values are kept."""
    data: dict[str, Any] = {
        "players": {
            "p1": {"player_id": "p1", "values": {"output_limiter": False, "flow_mode": True}},
            "p2": {"player_id": "p2", "values": {"output_limiter": True}},
            "p3": {"player_id": "p3", "values": {}},
        }
    }
    assert _migrate_output_limiter(data) is True
    assert data["players"]["p1"]["values"] == {"flow_mode": True}
    assert data["players"]["p2"]["values"] == {}
    assert data["players"]["p3"]["values"] == {}


def test_migrate_output_limiter_noop_when_absent() -> None:
    """Migration reports no change when no player stored the setting."""
    data: dict[str, Any] = {"players": {"p1": {"player_id": "p1", "values": {"flow_mode": True}}}}
    assert _migrate_output_limiter(data) is False


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
    # other providers still offer the setting, so their pick is left alone
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


def _airplay_receiver_ghost_data() -> dict[str, Any]:
    """Build a config store with AirPlay Receiver ghost players next to real players."""
    return {
        "providers": {
            "airplay_receiver--abc1": {
                "domain": "airplay_receiver",
                "instance_id": "airplay_receiver--abc1",
                "values": {"airplay_name": "Garage [AirPlay]"},
            },
            "airplay": {"domain": "airplay", "instance_id": "airplay", "values": {}},
        },
        "players": {
            # ghosts of the receiver: airplay player + sendspin bridge + universal wrapper
            "ap41cf0e23916f": {
                "player_id": "ap41cf0e23916f",
                "provider": "airplay",
                "default_name": "Garage [AirPlay]",
                "values": {},
            },
            "spb_41cf0e23916f": {
                "player_id": "spb_41cf0e23916f",
                "provider": "sendspin",
                "default_name": "Garage [AirPlay] (AirPlay)",
                "values": {},
            },
            "up41cf0e23916f": {
                "player_id": "up41cf0e23916f",
                "provider": "universal_player",
                "default_name": "Garage [AirPlay]",
                "values": {"linked_protocol_ids": ["ap41cf0e23916f", "spb_41cf0e23916f"]},
            },
            # a real airplay player with another name must be kept
            "apaabbccddeeff": {
                "player_id": "apaabbccddeeff",
                "provider": "airplay",
                "default_name": "Kitchen",
                "values": {},
            },
            # a universal player with a matching name wrapping a native (non-ghost)
            # protocol player must be kept
            "up10b41dc887f8": {
                "player_id": "up10b41dc887f8",
                "provider": "universal_player",
                "default_name": "Garage [AirPlay]",
                "values": {"linked_protocol_ids": ["10:B4:1D:C8:87:F8", "spb_10b41dc887f8"]},
            },
            # a group referencing a ghost keeps its other members
            "syncgroup1": {
                "player_id": "syncgroup1",
                "provider": "sync_group",
                "default_name": "All Speakers",
                "values": {"group_members": ["up41cf0e23916f", "apaabbccddeeff"]},
            },
        },
        "player_queues": {"up41cf0e23916f": {"queue_id": "up41cf0e23916f"}},
        "player_dsp": {"up41cf0e23916f": {"enabled": True}},
    }


def test_migrate_airplay_receiver_ghosts_removes_matching_players() -> None:
    """Ghost ap/spb/up players of an own receiver are removed with their leftover state."""
    data = _airplay_receiver_ghost_data()

    assert _migrate_airplay_receiver_ghost_players(data) is True

    players = data["players"]
    assert "ap41cf0e23916f" not in players
    assert "spb_41cf0e23916f" not in players
    assert "up41cf0e23916f" not in players
    # real players survive, including the same-name wrapper of a native player
    assert "apaabbccddeeff" in players
    assert "up10b41dc887f8" in players
    # ghost references are stripped from group membership and per-player state trees
    assert players["syncgroup1"]["values"]["group_members"] == ["apaabbccddeeff"]
    assert data["player_queues"] == {}
    assert data["player_dsp"] == {}


def test_migrate_airplay_receiver_ghosts_uses_default_name() -> None:
    """A receiver without an explicit airplay_name matches ghosts of the default name."""
    data: dict[str, Any] = {
        "providers": {
            "airplay_receiver--abc1": {
                "domain": "airplay_receiver",
                "instance_id": "airplay_receiver--abc1",
                "values": {},
            },
        },
        "players": {
            "apdb1ff0aae80e": {
                "player_id": "apdb1ff0aae80e",
                "provider": "airplay",
                "default_name": "Music Assistant",
                "values": {},
            },
        },
    }

    assert _migrate_airplay_receiver_ghost_players(data) is True
    assert data["players"] == {}


def test_migrate_airplay_receiver_ghosts_ignores_setup_flow_instances() -> None:
    """Setup-flow receivers are skipped because they cannot have produced legacy ghosts."""
    data: dict[str, Any] = {
        "providers": {
            "airplay_receiver--abc1": {
                "domain": "airplay_receiver",
                "instance_id": "airplay_receiver--abc1",
                "values": {},
                "setup_data": {"airplay_name": ENCRYPT_SUFFIX + "Garage [AirPlay]"},
            },
        },
        "players": {
            "apdb1ff0aae80e": {
                "player_id": "apdb1ff0aae80e",
                "provider": "airplay",
                "default_name": "Music Assistant",
                "values": {},
            },
        },
    }

    assert _migrate_airplay_receiver_ghost_players(data) is False
    assert "apdb1ff0aae80e" in data["players"]


def test_migrate_airplay_receiver_ghosts_keeps_same_name_wrapper_without_ghost_links() -> None:
    """A wrapper sharing the receiver name is kept unless it links only ghost endpoints."""
    data: dict[str, Any] = {
        "providers": {
            "airplay_receiver--abc1": {
                "domain": "airplay_receiver",
                "instance_id": "airplay_receiver--abc1",
                "values": {"airplay_name": "Garage [AirPlay]"},
            },
        },
        "players": {
            # empty linked list: must NOT be deleted (all([]) is True)
            "upemptylinks": {
                "player_id": "upemptylinks",
                "provider": "universal_player",
                "default_name": "Garage [AirPlay]",
                "values": {"linked_protocol_ids": []},
            },
            # no linked_protocol_ids at all: must NOT be deleted
            "upnolinks": {
                "player_id": "upnolinks",
                "provider": "universal_player",
                "default_name": "Garage [AirPlay]",
                "values": {},
            },
            # links a native (non-ghost) protocol player: must NOT be deleted
            "upnative": {
                "player_id": "upnative",
                "provider": "universal_player",
                "default_name": "Garage [AirPlay]",
                "values": {"linked_protocol_ids": ["spb_10b41dc887f8"]},
            },
        },
    }

    assert _migrate_airplay_receiver_ghost_players(data) is False
    assert set(data["players"]) == {"upemptylinks", "upnolinks", "upnative"}


def test_migrate_airplay_receiver_ghosts_ignores_disabled_receiver() -> None:
    """A disabled receiver's name is not used, so same-named players are kept."""
    data: dict[str, Any] = {
        "providers": {
            "airplay_receiver--abc1": {
                "domain": "airplay_receiver",
                "instance_id": "airplay_receiver--abc1",
                "enabled": False,
                "values": {"airplay_name": "Garage [AirPlay]"},
            },
        },
        "players": {
            "ap41cf0e23916f": {
                "player_id": "ap41cf0e23916f",
                "provider": "airplay",
                "default_name": "Garage [AirPlay]",
                "values": {},
            },
        },
    }

    assert _migrate_airplay_receiver_ghost_players(data) is False
    assert "ap41cf0e23916f" in data["players"]


def test_migrate_airplay_receiver_ghosts_noop_without_receivers() -> None:
    """Without configured receiver instances no player config is touched."""
    data: dict[str, Any] = {
        "providers": {"airplay": {"domain": "airplay", "instance_id": "airplay", "values": {}}},
        "players": {
            "apdb1ff0aae80e": {
                "player_id": "apdb1ff0aae80e",
                "provider": "airplay",
                "default_name": "Music Assistant",
                "values": {},
            },
        },
    }

    assert _migrate_airplay_receiver_ghost_players(data) is False
    assert "apdb1ff0aae80e" in data["players"]


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


def test_migrate_default_airplay_receiver_name_once() -> None:
    """The implicit receiver name is persisted so ghost cleanup cannot rerun later."""
    data: dict[str, Any] = {
        "providers": {
            "airplay_receiver--1": {
                "domain": "airplay_receiver",
                "values": {"mass_player_id": "__auto__"},
            }
        },
        "players": {
            "aplegitimate": {
                "player_id": "aplegitimate",
                "provider": "airplay",
                "default_name": "Music Assistant",
                "values": {},
            }
        },
    }

    assert migrate_provider_setup_data(data, _fake_encrypt) is True
    assert data["providers"]["airplay_receiver--1"]["setup_data"] == {
        "mass_player_id": ENCRYPT_SUFFIX + "__auto__",
        "airplay_name": ENCRYPT_SUFFIX + "Music Assistant",
    }
    assert _migrate_airplay_receiver_ghost_players(data) is False
    assert "aplegitimate" in data["players"]
    assert migrate_provider_setup_data(data, _fake_encrypt) is False


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


def test_migrate_nfs_subfolder_folds_into_export_path() -> None:
    """The subfolder is appended to the export path and the now-obsolete key is dropped."""
    data: dict[str, Any] = {
        "providers": {
            "filesystem_nfs--1": {
                "domain": "filesystem_nfs",
                "setup_data": {
                    "host": ENCRYPT_SUFFIX + "nas.local",
                    "export_path": ENCRYPT_SUFFIX + "/mnt/vault",
                    "subfolder": ENCRYPT_SUFFIX + "Music",
                    "nfs_version": ENCRYPT_SUFFIX + "3",
                },
                "values": {"sync_tracks": True},
            }
        }
    }

    assert migrate_nfs_subfolder_into_export_path(data, _fake_encrypt, _fake_decrypt) is True

    setup_data = data["providers"]["filesystem_nfs--1"]["setup_data"]
    assert setup_data["export_path"] == ENCRYPT_SUFFIX + "/mnt/vault/Music"
    assert "subfolder" not in setup_data
    assert setup_data["host"] == ENCRYPT_SUFFIX + "nas.local"
    assert setup_data["nfs_version"] == ENCRYPT_SUFFIX + "3"
    assert data["providers"]["filesystem_nfs--1"]["values"] == {"sync_tracks": True}
    # the marker stops a second pass
    assert data[CONF_NFS_SUBFOLDER_MIGRATED] is True
    assert migrate_nfs_subfolder_into_export_path(data, _fake_encrypt, _fake_decrypt) is False
    assert setup_data["export_path"] == ENCRYPT_SUFFIX + "/mnt/vault/Music"


def test_migrate_nfs_subfolder_normalizes_leading_slash_and_nesting() -> None:
    """A leading slash is tolerated and a nested subfolder keeps its full depth."""
    data: dict[str, Any] = {
        "providers": {
            "rooted": {
                "domain": "filesystem_nfs",
                "setup_data": {
                    "export_path": ENCRYPT_SUFFIX + "/volume1",
                    "subfolder": ENCRYPT_SUFFIX + "/music",
                },
            },
            "nested": {
                "domain": "filesystem_nfs",
                "setup_data": {
                    "export_path": ENCRYPT_SUFFIX + "/exports/media/",
                    "subfolder": ENCRYPT_SUFFIX + " albums/A-K ",
                },
            },
        }
    }

    assert migrate_nfs_subfolder_into_export_path(data, _fake_encrypt, _fake_decrypt) is True

    providers = data["providers"]
    assert providers["rooted"]["setup_data"]["export_path"] == ENCRYPT_SUFFIX + "/volume1/music"
    assert (
        providers["nested"]["setup_data"]["export_path"]
        == ENCRYPT_SUFFIX + "/exports/media/albums/A-K"
    )


def test_migrate_nfs_subfolder_noop_cases() -> None:
    """Instances without a usable subfolder, and other providers, are left untouched."""
    data: dict[str, Any] = {
        "providers": {
            "empty_subfolder": {
                "domain": "filesystem_nfs",
                "setup_data": {
                    "export_path": ENCRYPT_SUFFIX + "/mnt/vault",
                    "subfolder": ENCRYPT_SUFFIX + "",
                },
            },
            "no_subfolder_key": {
                "domain": "filesystem_nfs",
                "setup_data": {"export_path": ENCRYPT_SUFFIX + "/mnt/vault"},
            },
            "no_export_path": {
                "domain": "filesystem_nfs",
                "setup_data": {"subfolder": ENCRYPT_SUFFIX + "Music"},
            },
            "smb": {
                "domain": "filesystem_smb",
                "setup_data": {
                    "share": ENCRYPT_SUFFIX + "media",
                    "subfolder": ENCRYPT_SUFFIX + "Music",
                },
            },
        }
    }
    providers_before = deepcopy(data["providers"])

    # only the one-shot marker is written; no provider config is touched
    assert migrate_nfs_subfolder_into_export_path(data, _fake_encrypt, _fake_decrypt) is True
    assert data["providers"] == providers_before
    assert data[CONF_NFS_SUBFOLDER_MIGRATED] is True


def test_migrate_nfs_subfolder_never_folds_a_post_fix_config() -> None:
    """A subfolder stored after the marker is set is never folded, however often this runs."""
    data: dict[str, Any] = {
        CONF_NFS_SUBFOLDER_MIGRATED: True,
        "providers": {
            "filesystem_nfs--1": {
                "domain": "filesystem_nfs",
                "setup_data": {
                    "export_path": ENCRYPT_SUFFIX + "/mnt/vault",
                    "subfolder": ENCRYPT_SUFFIX + "Music",
                },
            }
        },
    }
    before = deepcopy(data)

    assert migrate_nfs_subfolder_into_export_path(data, _fake_encrypt, _fake_decrypt) is False
    assert data == before


def test_migrate_nfs_subfolder_handles_plaintext_values() -> None:
    """A value that was never encrypted at rest folds just the same."""
    data: dict[str, Any] = {
        "providers": {
            "filesystem_nfs--1": {
                "domain": "filesystem_nfs",
                "setup_data": {"export_path": "/mnt/vault", "subfolder": "Music"},
            }
        }
    }

    assert migrate_nfs_subfolder_into_export_path(data, _fake_encrypt, _fake_decrypt) is True
    assert (
        data["providers"]["filesystem_nfs--1"]["setup_data"]["export_path"]
        == ENCRYPT_SUFFIX + "/mnt/vault/Music"
    )


def test_migrate_nfs_subfolder_skips_an_undecryptable_instance() -> None:
    """An unreadable value costs that instance its migration, not the server its startup."""

    def _failing_decrypt(_value: str) -> str:
        raise InvalidDataError("Password decryption failed")

    data: dict[str, Any] = {
        "providers": {
            "broken": {
                "domain": "filesystem_nfs",
                "setup_data": {
                    "export_path": ENCRYPT_SUFFIX + "unreadable",
                    "subfolder": ENCRYPT_SUFFIX + "unreadable",
                },
            }
        }
    }

    assert migrate_nfs_subfolder_into_export_path(data, _fake_encrypt, _failing_decrypt) is True

    # left as it was, but the marker is still claimed
    assert data["providers"]["broken"]["setup_data"] == {
        "export_path": ENCRYPT_SUFFIX + "unreadable",
        "subfolder": ENCRYPT_SUFFIX + "unreadable",
    }
    assert data[CONF_NFS_SUBFOLDER_MIGRATED] is True


def test_migrate_nfs_subfolder_round_trips_through_real_encryption(tmp_path: Path) -> None:
    """The folded export path is re-encrypted with the live key, so a migrated config loads."""
    mass = SimpleNamespace(storage_path=str(tmp_path))
    controller = ConfigController(mass)  # type: ignore[arg-type]
    controller.initialized = True
    controller.save = lambda **_kwargs: None  # type: ignore[method-assign]
    controller.set(CONF_SERVER_ID, uuid4().hex)
    controller._init_encryption()

    data: dict[str, Any] = {
        "providers": {
            "filesystem_nfs--1": {
                "domain": "filesystem_nfs",
                "setup_data": {
                    "export_path": controller.encrypt_string("/mnt/vault"),
                    "subfolder": controller.encrypt_string("Music"),
                },
            }
        }
    }

    assert (
        migrate_nfs_subfolder_into_export_path(
            data, controller.encrypt_string, controller.decrypt_string
        )
        is True
    )

    stored = data["providers"]["filesystem_nfs--1"]["setup_data"]["export_path"]
    assert stored.startswith(ENCRYPT_SUFFIX)
    assert controller.decrypt_string(stored) == "/mnt/vault/Music"


def _hass_engine_data(instance_id: str = "hass", **hass_values: Any) -> dict[str, Any]:
    """Build a config store with a hass provider and all three engine consumers."""
    return {
        "providers": {
            instance_id: {
                "domain": "hass",
                "instance_id": instance_id,
                "values": {"verify_ssl": True, **hass_values},
            },
            "ai_radio": {"domain": "ai_radio", "instance_id": "ai_radio", "values": {}},
            "music_quiz": {"domain": "music_quiz", "instance_id": "music_quiz", "values": {}},
            "smart_playlist": {
                "domain": "smart_playlist",
                "instance_id": "smart_playlist",
                "values": {},
            },
        }
    }


def test_migrate_hass_engine_selection_fans_out_to_all_consumers() -> None:
    """Both stored entity ids become engine uids on every consumer, encrypted for ai_radio."""
    data = _hass_engine_data(tts_entity="tts.piper", ai_task_entity="ai_task.google")
    assert migrate_hass_engine_selection(data, _fake_encrypt) is True
    prov = data["providers"]
    assert prov["ai_radio"]["setup_data"] == {
        "ai_engine": ENCRYPT_SUFFIX + "hass/ai_task.google",
        "tts_engine": ENCRYPT_SUFFIX + "hass/tts.piper",
    }
    # the plain providers store their pick unencrypted in values
    assert prov["music_quiz"]["values"] == {"ai_engine": "hass/ai_task.google"}
    assert prov["smart_playlist"]["values"] == {"ai_engine": "hass/ai_task.google"}
    # the dead keys are consumed, the hass config is otherwise untouched
    assert prov["hass"]["values"] == {"verify_ssl": True}
    # a second pass finds nothing left to migrate
    assert migrate_hass_engine_selection(data, _fake_encrypt) is False


def test_migrate_hass_engine_selection_uses_own_instance_id() -> None:
    """The engine uid is built from the hass config's own instance id, not its domain."""
    data = _hass_engine_data("hass--abc", ai_task_entity="ai_task.google")
    assert migrate_hass_engine_selection(data, _fake_encrypt) is True
    assert data["providers"]["music_quiz"]["values"] == {"ai_engine": "hass--abc/ai_task.google"}


def test_migrate_hass_engine_selection_migrates_keys_independently() -> None:
    """A single stored key migrates on its own; the other selection stays unset."""
    data = _hass_engine_data(tts_entity="tts.piper")
    assert migrate_hass_engine_selection(data, _fake_encrypt) is True
    prov = data["providers"]
    assert prov["ai_radio"]["setup_data"] == {"tts_engine": ENCRYPT_SUFFIX + "hass/tts.piper"}
    assert prov["music_quiz"]["values"] == {}
    assert prov["smart_playlist"]["values"] == {}
    assert prov["hass"]["values"] == {"verify_ssl": True}


def test_migrate_hass_engine_selection_without_consumers() -> None:
    """The dead hass keys are dropped even when no consuming provider is installed."""
    data: dict[str, Any] = {
        "providers": {
            "hass": {
                "domain": "hass",
                "instance_id": "hass",
                "values": {"tts_entity": "tts.piper", "ai_task_entity": "ai_task.google"},
            },
            "music_quiz": {"domain": "music_quiz", "instance_id": "music_quiz", "values": {}},
        }
    }
    assert migrate_hass_engine_selection(data, _fake_encrypt) is True
    assert data["providers"]["hass"]["values"] == {}
    assert data["providers"]["music_quiz"]["values"] == {"ai_engine": "hass/ai_task.google"}
    assert migrate_hass_engine_selection(data, _fake_encrypt) is False


def test_migrate_hass_engine_selection_preserves_existing_choice() -> None:
    """A selection the user already made on a consumer is never overwritten."""
    data = _hass_engine_data(tts_entity="tts.piper", ai_task_entity="ai_task.google")
    prov = data["providers"]
    prov["music_quiz"]["values"]["ai_engine"] = "openai/gpt"
    prov["ai_radio"]["setup_data"] = {"tts_engine": ENCRYPT_SUFFIX + "elevenlabs/voice"}
    assert migrate_hass_engine_selection(data, _fake_encrypt) is True
    assert prov["music_quiz"]["values"] == {"ai_engine": "openai/gpt"}
    assert prov["ai_radio"]["setup_data"] == {
        "ai_engine": ENCRYPT_SUFFIX + "hass/ai_task.google",
        "tts_engine": ENCRYPT_SUFFIX + "elevenlabs/voice",
    }
    # the consumer that had no choice yet still gets the migrated one
    assert prov["smart_playlist"]["values"] == {"ai_engine": "hass/ai_task.google"}
    assert prov["hass"]["values"] == {"verify_ssl": True}


def test_migrate_hass_engine_selection_noop_without_stored_values() -> None:
    """Nothing stored (or no hass provider at all) reports no change."""
    assert migrate_hass_engine_selection({}, _fake_encrypt) is False
    assert migrate_hass_engine_selection({"providers": {}}, _fake_encrypt) is False
    data = _hass_engine_data()
    assert migrate_hass_engine_selection(data, _fake_encrypt) is False
    assert "setup_data" not in data["providers"]["ai_radio"]
    assert data["providers"]["music_quiz"]["values"] == {}


def test_migrate_hass_engine_selection_skips_multiple_hass_configs() -> None:
    """With several hass configurations there is no correct winner, so nothing is touched."""
    data = _hass_engine_data(tts_entity="tts.piper", ai_task_entity="ai_task.google")
    data["providers"]["hass--second"] = {
        "domain": "hass",
        "instance_id": "hass--second",
        "values": {"tts_entity": "tts.cloud"},
    }
    assert migrate_hass_engine_selection(data, _fake_encrypt) is False
    prov = data["providers"]
    assert prov["hass"]["values"]["tts_entity"] == "tts.piper"
    assert prov["hass"]["values"]["ai_task_entity"] == "ai_task.google"
    assert prov["music_quiz"]["values"] == {}
    assert "setup_data" not in prov["ai_radio"]


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
