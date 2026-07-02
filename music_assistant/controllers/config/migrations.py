"""Settings (settings.json) migration logic for the config controller."""

from __future__ import annotations

import logging
from typing import Any

from music_assistant_models.enums import CrossfadeMode

from music_assistant.constants import (
    CONF_CORE,
    CONF_CROSSFADE_MODE,
    CONF_ENTRY_CROSSFADE_DURATION,
    CONF_ENTRY_VOLUME_NORMALIZATION,
    CONF_LINKED_PROTOCOL_IDS,
    CONF_PLAYER_QUEUES,
    CONF_PLAYERS,
    CONF_PROTOCOL_PARENT_ID,
    CONF_PROVIDERS,
    CONF_SMART_FADES_MODE,
    CONF_VOLUME_NORMALIZATION_TARGET,
)

LOGGER = logging.getLogger(__name__)


async def migrate(data: dict[str, Any]) -> bool:
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

    return changed


def _migrate_player_queue_settings(data: dict[str, Any]) -> bool:
    """Move queue-scoped settings from the per-player config to the per-queue config."""
    moved_keys = (
        CONF_ENTRY_CROSSFADE_DURATION.key,
        CONF_ENTRY_VOLUME_NORMALIZATION.key,
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
