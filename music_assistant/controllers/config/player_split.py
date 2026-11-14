"""
Migration of the stored player configs to one record per player and one per protocol.

Before the split a record under ``players/`` was a player and its provider object at
once, and a device without a native provider had a wrapper record of the universal
player provider with the records of its protocols pointing at it. After it,
``players/{player_id}`` holds what belongs to the player and
``player_protocols/{protocol_id}`` what belongs to one protocol of it, with the id of
its player.

Both steps run once, each behind its own flag in the data. A record that cannot be
converted is logged and left alone; a step that fails as a whole raises, which stops
the startup before anything is saved, so the next start tries again.

An older version that runs on a split config stores the players it finds in the shape
from before the split. Every start of a split config removes those records: the split
records are what holds. The copy of the settings from before the split is the way back
to an older version.
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
from typing import Any

from music_assistant_models.enums import PlayerType, ProtocolRole

from music_assistant.constants import (
    CONF_CREATED_AT,
    CONF_DEVICE_IDENTIFIERS,
    CONF_PLAYER_DSP,
    CONF_PLAYER_PROTOCOLS,
    CONF_PLAYER_QUEUES,
    CONF_PLAYER_SPLIT_SESSION_ONLY,
    CONF_PLAYER_SPLIT_TREE,
    CONF_PLAYERS,
    CONF_PROTOCOL_DEFAULTS,
    CONF_SESSION_ONLY,
    PROTOCOL_PRIORITY,
)
from music_assistant.controllers.config.constants import (
    AUTOMATIC_VALUE_KEYS,
    GROUP_MEMBER_KEYS,
    PLAYER_VALUE_KEYS,
)

LOGGER = logging.getLogger(__name__)

# suffix of the copy of the settings file that is kept from before the migration
BACKUP_SUFFIX = ".pre-player-split"

# removed keys of the two-sided link between a player and its protocols
LEGACY_LINKED_PROTOCOL_IDS = "linked_protocol_ids"
LEGACY_PROTOCOL_PARENT_ID = "protocol_parent_id"

# instance id of the removed universal player provider, whose records stood in for the
# devices without a native provider
LEGACY_UNIVERSAL_PLAYER = "universal_player"

# the domains whose players are no device: their protocol has the virtual role
VIRTUAL_DOMAINS = frozenset({"sync_group", "universal_group"})

# what the Sendspin protocol of each kind of client that only exists while connected gives
# its player by default, by the type its record from before the split stores; keep in line
# with the Sendspin provider (providers/sendspin/player.py)
SESSION_ONLY_DEFAULTS: dict[str, dict[str, bool]] = {
    # a browser, app or virtual player
    PlayerType.PLAYER.value: {
        "hidden_by_default": True,
        "expose_to_ha_by_default": False,
        "private": True,
    },
    # a display, visualizer or light (SendspinVisualizerPlayer) and a source
    **{
        player_type.value: {
            "hidden_by_default": True,
            "expose_to_ha_by_default": False,
            "private": False,
        }
        for player_type in (
            PlayerType.DISPLAY,
            PlayerType.VISUALIZER,
            PlayerType.LIGHT,
            PlayerType.SOURCE,
        )
    },
}

# values the server or a provider stores by itself, which do not make a record one the
# user customised
AUTOMATIC_KEYS = AUTOMATIC_VALUE_KEYS | {LEGACY_LINKED_PROTOCOL_IDS, LEGACY_PROTOCOL_PARENT_ID}


def needs_player_split(data: dict[str, Any]) -> bool:
    """
    Return whether the stored config still has to be split.

    :param data: The persistent settings data.
    """
    return not data.get(CONF_PLAYER_SPLIT_TREE)


def mark_player_split_done(data: dict[str, Any]) -> None:
    """
    Mark a config that never held unsplit player records as split.

    :param data: The persistent settings data of a new installation.
    """
    data[CONF_PLAYER_SPLIT_SESSION_ONLY] = True
    data[CONF_PLAYER_SPLIT_TREE] = True


def backup_before_player_split(loaded_filename: str, settings_filename: str) -> None:
    """
    Keep a copy of the settings file as it was before the split, unless one exists.

    The copy is written through a temporary file and a rename, so a retry keeps the
    first copy and a crash leaves no partial one.

    :param loaded_filename: The file the settings were loaded from.
    :param settings_filename: The settings file, which the copy is named after.
    :raises OSError: When the copy can not be written.
    """
    backup = Path(f"{settings_filename}{BACKUP_SUFFIX}")
    # an empty file is what a power failure leaves of a copy that was never synced
    if backup.exists() and backup.stat().st_size:
        return
    backup_temp = Path(f"{backup}.tmp")
    with backup_temp.open("wb") as _file:
        _file.write(Path(loaded_filename).read_bytes())
        _file.flush()
        os.fsync(_file.fileno())
    backup_temp.replace(backup)
    # best effort: fsync the directory as well so the rename itself survives a power
    # failure (not supported on all platforms/filesystems)
    with contextlib.suppress(OSError):
        dir_fd = os.open(backup.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    LOGGER.info("Kept a copy of the settings from before the player migration: %s", backup)


def migrate_player_split(data: dict[str, Any]) -> bool:
    """
    Split the stored player configs in-place; return True if anything changed.

    :param data: The persistent settings data.
    """
    changed = False
    if not data.get(CONF_PLAYER_SPLIT_SESSION_ONLY):
        if not data.get(CONF_PLAYER_SPLIT_TREE):
            # the candidates are told apart by the shape from before the split
            _migrate_session_only_players(data, _owners_before_the_split(data))
        data[CONF_PLAYER_SPLIT_SESSION_ONLY] = True
        changed = True
    if not data.get(CONF_PLAYER_SPLIT_TREE):
        _migrate_player_tree(data)
        data[CONF_PLAYER_SPLIT_TREE] = True
        changed = True
    return changed


def remove_player_records_of_older_versions(data: dict[str, Any]) -> bool:
    """
    Remove the player records an older version stored in a split config; return True if any.

    Those are the records of its protocol players and universal players, whose devices are
    found again, and of browser and app players without the session-only marker, which are
    kept and marked when the user customised them. A player that a protocol belongs to is
    never removed.

    :param data: The persistent settings data.
    """
    players, protocols = data.get(CONF_PLAYERS), data.get(CONF_PLAYER_PROTOCOLS, {})
    # without a readable tree of the protocols, no record can be told apart
    if needs_player_split(data) or not isinstance(players, dict) or not isinstance(protocols, dict):
        return False
    # a player a protocol belongs to is never removed, whatever else its record holds
    owners = _player_ids_with_protocols(data)
    removed: list[str] = []
    for player_id, record in list(players.items()):
        if not isinstance(record, dict):
            LOGGER.info("Can not check the stored player %s (not a mapping)", player_id)
        elif player_id not in owners and _is_older_version_record(player_id, record, protocols):
            _drop_player(data, player_id)
            removed.append(player_id)
    # what this version created holds its creation time, which no older version stores on
    # a Sendspin player
    created_here = {
        player_id
        for player_id, record in players.items()
        if isinstance(record, dict) and CONF_CREATED_AT in _values(record)
    }
    deleted, marked = _migrate_session_only_players(data, owners | created_here)
    removed += deleted
    for player_id in removed:
        LOGGER.info("Removed the stored player %s, which an older version stored", player_id)
    if removed:
        LOGGER.warning(
            "Removed %s player records that an older version of Music Assistant stored "
            "after the player settings were migrated",
            len(removed),
        )
    return bool(removed or marked)


def _domain(record: dict[str, Any]) -> str:
    """Return the provider domain a stored record names."""
    return str(record.get("provider") or "").split("--", maxsplit=1)[0]


def _values(record: dict[str, Any]) -> dict[str, Any]:
    """Return the stored values of a record, an empty dict when it holds none."""
    values = record.get("values")
    return values if isinstance(values, dict) else {}


def _records(data: dict[str, Any], key: str) -> dict[str, Any]:
    """Return a tree of the settings data, created when it is missing or no mapping."""
    if not isinstance(data.get(key), dict):
        data[key] = {}
    tree: dict[str, Any] = data[key]
    return tree


def _drop_player(data: dict[str, Any], player_id: str) -> None:
    """Delete a player record with the DSP and queue settings stored next to it."""
    for key in (CONF_PLAYERS, CONF_PLAYER_DSP, CONF_PLAYER_QUEUES):
        if isinstance(tree := data.get(key), dict):
            tree.pop(player_id, None)


def _player_ids_with_protocols(data: dict[str, Any]) -> set[str]:
    """Return the ids of the players that a record of a protocol names as its player."""
    protocols = data.get(CONF_PLAYER_PROTOCOLS)
    if not isinstance(protocols, dict):
        return set()
    return {
        str(record["player_id"])
        for record in protocols.values()
        if isinstance(record, dict) and record.get("player_id")
    }


def _owners_before_the_split(data: dict[str, Any]) -> set[str]:
    """Return the ids of the players that own a protocol, by the records of both shapes."""
    # a player that has protocols was split before, and is no candidate anymore
    owners = _player_ids_with_protocols(data)
    for record in _records(data, CONF_PLAYERS).values():
        if not isinstance(record, dict):
            continue
        values = _values(record)
        if parent_id := values.get(LEGACY_PROTOCOL_PARENT_ID):
            owners.add(str(parent_id))
        if isinstance(linked := values.get(LEGACY_LINKED_PROTOCOL_IDS), list):
            owners.update(str(protocol_id) for protocol_id in linked)
    return owners


def _migrate_session_only_players(
    data: dict[str, Any], left_alone: set[str]
) -> tuple[list[str], list[str]]:
    """
    Delete or mark the players that only exist while their device is connected.

    Those are the Sendspin players that are no protocol of a device: browser and app
    players, virtual players, displays, visualizers, lights and sources. One the user
    customised is kept and marked, with the defaults its client gives it when the record
    tells its kind; of the others only the player record is deleted: they come back by
    themselves, with the same id, when they connect. Returns the ids of the deleted and
    of the marked players.

    :param data: The persistent settings data.
    :param left_alone: Ids of the players that are no candidates.
    """
    deleted: list[str] = []
    marked: list[str] = []
    players = _records(data, CONF_PLAYERS)
    for player_id, record in list(players.items()):
        try:
            if (
                not isinstance(record, dict)
                or _domain(record) != "sendspin"
                or record.get("player_type") == PlayerType.PROTOCOL
                or _values(record).get(LEGACY_PROTOCOL_PARENT_ID)
                or _values(record).get(CONF_SESSION_ONLY)
                or player_id in left_alone
            ):
                continue
            if _is_customised(data, player_id, record):
                if not isinstance(record.get("values"), dict):
                    record["values"] = {}
                record["values"][CONF_SESSION_ONLY] = True
                # it is listed before its client connects, as its client has it
                if defaults := SESSION_ONLY_DEFAULTS.get(str(record.get("player_type"))):
                    record["values"].setdefault(CONF_PROTOCOL_DEFAULTS, dict(defaults))
                marked.append(player_id)
            else:
                del players[player_id]
                deleted.append(player_id)
        except Exception:
            LOGGER.exception("Could not migrate the stored player %s, skipping it", player_id)
    return deleted, marked


def _is_customised(data: dict[str, Any], player_id: str, record: dict[str, Any]) -> bool:
    """Return whether the user changed something about a stored player."""
    if record.get("name") or not record.get("enabled", True):
        return True
    if any(key not in AUTOMATIC_KEYS for key in _values(record)):
        return True
    dsp_configs, queue_configs = data.get(CONF_PLAYER_DSP), data.get(CONF_PLAYER_QUEUES)
    dsp_config = dsp_configs.get(player_id) if isinstance(dsp_configs, dict) else None
    if isinstance(dsp_config, dict) and dsp_config.get("enabled"):
        return True
    return isinstance(queue_configs, dict) and player_id in queue_configs


def _is_older_version_record(
    player_id: str, record: dict[str, Any], protocols: dict[str, Any]
) -> bool:
    """Return whether a record of a split config is one of a protocol or universal player."""
    if record.get("provider") == LEGACY_UNIVERSAL_PLAYER:
        return True
    if record.get("player_type") == PlayerType.PROTOCOL:
        return True
    if _values(record).get(LEGACY_PROTOCOL_PARENT_ID):
        return True
    # the id of a protocol that belongs to a player
    protocol = protocols.get(player_id)
    return isinstance(protocol, dict) and bool(protocol.get("player_id"))


def _migrate_player_tree(data: dict[str, Any]) -> None:
    """Move what belongs to a protocol out of the player records, into records of their own."""
    players = _records(data, CONF_PLAYERS)
    protocols = _records(data, CONF_PLAYER_PROTOCOLS)
    records = {
        player_id: record for player_id, record in players.items() if isinstance(record, dict)
    }
    for player_id in players.keys() - records.keys():
        LOGGER.warning("Can not migrate the stored player %s (not a mapping)", player_id)
    _restore_missing_edges(records)
    # a record that points at a player is a protocol of it, whatever type it has stored
    protocol_ids = {
        record_id
        for record_id, record in records.items()
        if record.get("player_type") == PlayerType.PROTOCOL
        or _values(record).get(LEGACY_PROTOCOL_PARENT_ID)
    }
    edges = _valid_edges(records, protocol_ids)
    wrapper_ids = {
        record_id
        for record_id, record in records.items()
        if record_id not in protocol_ids and record.get("provider") == LEGACY_UNIVERSAL_PLAYER
    }
    _keep_one_protocol_per_domain(records, edges)
    split_before = _player_ids_with_protocols(data) | protocols.keys()
    for player_id, record in records.items():
        try:
            if player_id in protocol_ids:
                _move_protocol_record(players, protocols, player_id, record, edges.get(player_id))
            elif player_id in wrapper_ids:
                _convert_wrapper(data, player_id, record, records, edges)
            elif player_id not in split_before and record.get("provider"):
                # a player without a provider has no protocol of its own
                _split_player_record(protocols, player_id, record)
        except Exception:
            LOGGER.exception("Could not migrate the stored player %s, skipping it", player_id)
    _repair_group_members(protocols, edges)
    # the outputs of a disabled player were disabled along with it, which nothing
    # would undo when the player gets enabled again
    for protocol_id, player_id in edges.items():
        player, protocol = players.get(player_id), protocols.get(protocol_id)
        if (
            isinstance(player, dict)
            and isinstance(protocol, dict)
            and not player.get("enabled", True)
        ):
            protocol["enabled"] = True


def _restore_missing_edges(records: dict[str, dict[str, Any]]) -> None:
    """
    Restore the edge of a protocol record that only its player still lists.

    Only for a record stored as a protocol, and only when exactly one player lists it,
    where the list of a wrapper outranks that of a native player: an interrupted shutdown
    could leave a wrapper without the edges of its protocols.
    """
    listed_by: dict[str, list[str]] = {}
    for player_id, record in records.items():
        linked = _values(record).get(LEGACY_LINKED_PROTOCOL_IDS)
        if not isinstance(linked, list):
            continue
        for protocol_id in linked:
            if protocol_id != player_id and isinstance(protocol_id, str):
                listed_by.setdefault(protocol_id, []).append(player_id)
    for protocol_id, parent_ids in listed_by.items():
        child = records.get(protocol_id)
        # a player that is still listed after its type changed is a player of its own
        if child is None or child.get("player_type") != PlayerType.PROTOCOL:
            continue
        if _values(child).get(LEGACY_PROTOCOL_PARENT_ID):
            continue
        wrapper_ids = [
            parent_id
            for parent_id in parent_ids
            if records[parent_id].get("provider") == LEGACY_UNIVERSAL_PLAYER
        ]
        candidates = wrapper_ids or parent_ids
        if len(candidates) != 1:
            continue
        if not isinstance(child.get("values"), dict):
            child["values"] = {}
        child["values"][LEGACY_PROTOCOL_PARENT_ID] = candidates[0]


def _valid_edges(records: dict[str, dict[str, Any]], protocol_ids: set[str]) -> dict[str, str]:
    """
    Return the player each protocol record points at, by protocol id.

    An edge to a record that is missing, to the record itself or to another protocol
    is left out: such a protocol is matched to its player again when it attaches.
    """
    edges: dict[str, str] = {}
    for protocol_id in protocol_ids:
        parent_id = _values(records[protocol_id]).get(LEGACY_PROTOCOL_PARENT_ID)
        if (
            isinstance(parent_id, str)
            and parent_id != protocol_id
            and parent_id in records
            and parent_id not in protocol_ids
        ):
            edges[protocol_id] = parent_id
    return edges


def _keep_one_protocol_per_domain(
    records: dict[str, dict[str, Any]], edges: dict[str, str]
) -> None:
    """
    Unbind all but one of the protocols of one domain that point at the same player.

    The enabled one stays, else the one that was set up, else the one with the lowest id.
    """
    by_slot: dict[tuple[str, str], list[str]] = {}
    for protocol_id, player_id in edges.items():
        by_slot.setdefault((player_id, _domain(records[protocol_id])), []).append(protocol_id)
    for (player_id, domain), protocol_ids in by_slot.items():
        if len(protocol_ids) < 2:
            continue
        kept = min(
            protocol_ids,
            key=lambda protocol_id: (
                not records[protocol_id].get("enabled", True),
                not records[protocol_id].get("setup_data"),
                protocol_id,
            ),
        )
        LOGGER.warning(
            "Player %s has several %s protocols stored, keeping %s", player_id, domain, kept
        )
        for protocol_id in protocol_ids:
            if protocol_id != kept:
                del edges[protocol_id]


def _protocol_record(
    protocol_id: str, record: dict[str, Any], player_id: str | None, role: ProtocolRole
) -> dict[str, Any]:
    """Return the record of a protocol, from the record it was stored in before the split."""
    values = {
        key: value
        for key, value in _values(record).items()
        if key not in (LEGACY_LINKED_PROTOCOL_IDS, LEGACY_PROTOCOL_PARENT_ID)
    }
    protocol_record: dict[str, Any] = {
        "protocol_id": protocol_id,
        "provider": record.get("provider"),
        "player_id": player_id,
        "role": role.value,
        "enabled": True,
        "default_name": record.get("default_name"),
        "values": values,
    }
    if isinstance(setup_data := record.get("setup_data"), dict) and setup_data:
        protocol_record["setup_data"] = setup_data
    return protocol_record


def _move_protocol_record(
    players: dict[str, Any],
    protocols: dict[str, Any],
    protocol_id: str,
    record: dict[str, Any],
    player_id: str | None,
) -> None:
    """Move the record of a protocol to the tree of the protocols."""
    protocol_record = _protocol_record(protocol_id, record, player_id, ProtocolRole.AUDIO)
    if player_id is not None:
        # the enabled flag of a protocol that belongs to a player is the user's choice
        # for that output; of one without a player it was the enable of the device
        protocol_record["enabled"] = bool(record.get("enabled", True))
    # a record that is there already is the one of this protocol in its new shape
    if not isinstance(protocols.get(protocol_id), dict):
        protocols[protocol_id] = protocol_record
    del players[protocol_id]


def _convert_wrapper(
    data: dict[str, Any],
    player_id: str,
    record: dict[str, Any],
    records: dict[str, dict[str, Any]],
    edges: dict[str, str],
) -> None:
    """
    Turn the record of a universal player wrapper into the record of a player.

    A wrapper without any protocol is only kept when the user customised it: it stays
    as a player without a provider, which a device that returns can attach to.
    """
    child_ids = [protocol_id for protocol_id, parent_id in edges.items() if parent_id == player_id]
    if not child_ids and not _is_customised(data, player_id, record):
        LOGGER.info("Removing the stored player %s, which has no protocol left", player_id)
        _drop_player(data, player_id)
        return
    best_child = min(
        child_ids,
        key=lambda protocol_id: (
            PROTOCOL_PRIORITY.get(_domain(records[protocol_id]), 100),
            protocol_id,
        ),
        default=None,
    )
    record["provider"] = records[best_child].get("provider") if best_child else ""
    # a wrapper was created as a group and only got the type of a player at its first update
    record["player_type"] = PlayerType.PLAYER.value
    values = _values(record)
    values.pop(LEGACY_LINKED_PROTOCOL_IDS, None)
    values.pop(LEGACY_PROTOCOL_PARENT_ID, None)
    identifiers = values.get(CONF_DEVICE_IDENTIFIERS)
    if isinstance(identifiers, dict) and isinstance(uuid := identifiers.get("uuid"), str):
        # the UDN of a Sonos renderer, which is reported without this suffix
        identifiers["uuid"] = uuid.removesuffix("_MR")
    record.pop("setup_data", None)


def _split_player_record(protocols: dict[str, Any], player_id: str, record: dict[str, Any]) -> None:
    """Split the record of a player into what belongs to it and what to its own protocol."""
    role = ProtocolRole.VIRTUAL if _domain(record) in VIRTUAL_DOMAINS else ProtocolRole.NATIVE
    protocol_record = _protocol_record(player_id, record, player_id, role)
    protocol_record["values"] = {
        key: value
        for key, value in protocol_record["values"].items()
        if key not in PLAYER_VALUE_KEYS
    }
    protocols[player_id] = protocol_record
    record["values"] = {
        key: value for key, value in _values(record).items() if key in PLAYER_VALUE_KEYS
    }
    record.pop("setup_data", None)


def _repair_group_members(protocols: dict[str, Any], edges: dict[str, str]) -> None:
    """
    Let the member lists of the groups name players where they name a protocol of one.

    A member that is no id is left out.
    """
    for protocol_id, record in protocols.items():
        if not isinstance(record, dict):
            continue
        for key in GROUP_MEMBER_KEYS:
            members = _values(record).get(key)
            if not isinstance(members, list):
                continue
            member_ids = [member_id for member_id in members if isinstance(member_id, str)]
            if len(member_ids) != len(members):
                LOGGER.warning(
                    "Left %s members that are no id out of the %s of the stored group %s",
                    len(members) - len(member_ids),
                    key,
                    protocol_id,
                )
            repaired = list(
                dict.fromkeys(edges.get(member_id, member_id) for member_id in member_ids)
            )
            if repaired != members:
                record["values"][key] = repaired
