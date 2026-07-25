"""Tests for (settings.json) config migrations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant.constants import ENCRYPT_SUFFIX
from music_assistant.controllers.config.migrations import (
    PROVIDER_SETUP_FLOW_KEYS,
    _migrate_airplay_apple_power_control,
    _migrate_airplay_receiver_ghost_players,
    _migrate_output_limiter,
    _migrate_player_setup_data,
    migrate_provider_setup_data,
)

if TYPE_CHECKING:
    import pytest


def _fake_encrypt(value: str) -> str:
    """Mirror ConfigController.encrypt_string: prefix once, idempotent for encrypted values."""
    return value if value.startswith(ENCRYPT_SUFFIX) else ENCRYPT_SUFFIX + value


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
