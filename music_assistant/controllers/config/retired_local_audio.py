"""
One-shot cleanup of the retired local_audio provider on installs that never used it.

The provider was builtin, so every install carries an auto-created config for it, and it
enumerated every output device of the host into a player config of its own. Now that it is
retired and its `setup()` fails with the retirement notice, those artefacts raise a red
"this provider requires attention" banner on machines that merely happened to have a sound
card. The banner is only worth showing to someone who actually played through one, so this
decides on evidence of playback and removes everything where there is none.

Unlike the `settings.json` migrations in `migrations.py`, answering that question needs the
library and cache databases, so this runs from `MusicAssistant.start()` once the core
controllers are up - and before the providers load, so the tombstone never gets the chance
to record an INCOMPATIBLE status.

TODO: remove after 2.12 release
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from music_assistant.constants import (
    CONF_PLAYERS,
    CONF_PROVIDERS,
    CONF_RETIRED_LOCAL_AUDIO_CLEANED,
    DB_TABLE_CACHE,
    DB_TABLE_PLAYLOG,
)
from music_assistant.controllers.player_queues.constants import (
    CACHE_CATEGORY_PLAYER_QUEUE_ITEMS,
    CACHE_CATEGORY_PLAYER_QUEUE_STATE,
)
from music_assistant.helpers.json import async_json_loads

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(__name__)

LOCAL_AUDIO_DOMAIN = "local_audio"


async def cleanup_retired_local_audio(mass: MusicAssistant) -> None:
    """
    Remove the retired local_audio provider and its players when they were never used.

    Runs at most once per install and never raises: an install whose databases cannot
    answer the question keeps everything and gets the retirement notice instead.

    :param mass: The MusicAssistant instance, with its core controllers set up.
    """
    if mass.config.get(CONF_RETIRED_LOCAL_AUDIO_CLEANED, False):
        return
    instance_ids = _local_audio_provider_instance_ids(mass)
    player_ids = _local_audio_player_ids(mass)
    if not instance_ids and not player_ids:
        _mark_cleanup_done(mass)
        return
    try:
        if (used_by := await _find_playback_evidence(mass, player_ids)) is not None:
            LOGGER.debug(
                "Keeping the config of the retired %s provider: player %s was played to",
                LOCAL_AUDIO_DOMAIN,
                used_by,
            )
        else:
            await _remove_local_audio_config(mass, instance_ids, player_ids)
    except Exception as err:
        # keeping a config costs a banner, removing it wrongly costs the user their
        # settings. Broad and around the removal too: an escape here would abort the boot.
        # The flag stays unset, so the next startup retries the (idempotent) removal.
        LOGGER.warning(
            "Unable to clean up the retired %s provider, keeping its configuration - %s: %s",
            LOCAL_AUDIO_DOMAIN,
            type(err).__name__,
            err,
            exc_info=err,
        )
        return
    _mark_cleanup_done(mass)


def _local_audio_provider_instance_ids(mass: MusicAssistant) -> list[str]:
    """Return the instance ids of all stored local_audio provider configs."""
    all_provider_configs = mass.config.get(CONF_PROVIDERS, {})
    if not isinstance(all_provider_configs, dict):
        return []
    return [
        instance_id
        for instance_id, prov_cfg in all_provider_configs.items()
        if isinstance(prov_cfg, dict) and prov_cfg.get("domain") == LOCAL_AUDIO_DOMAIN
    ]


def _local_audio_player_ids(mass: MusicAssistant) -> list[str]:
    """Return the player ids of all stored local_audio player configs."""
    all_player_configs = mass.config.get(CONF_PLAYERS, {})
    if not isinstance(all_player_configs, dict):
        return []
    return [
        player_id
        for player_id, player_cfg in all_player_configs.items()
        if isinstance(player_cfg, dict) and player_cfg.get("provider") == LOCAL_AUDIO_DOMAIN
    ]


async def _remove_local_audio_config(
    mass: MusicAssistant, instance_ids: list[str], player_ids: list[str]
) -> None:
    """
    Wipe every trace of the retired provider from the config.

    :param mass: The MusicAssistant instance to remove the configuration from.
    :param instance_ids: Instance ids of the local_audio provider configs to remove.
    :param player_ids: Player ids of the local_audio player configs to remove.
    """
    for player_id in player_ids:
        # also drops its DSP/queue settings, saved queue and bridged spb_* children
        mass.players.delete_player_config(player_id)
        # no config names the obsolete wrapper anymore, so nothing else would purge it
        mass.player_queues.purge_saved_queue(_legacy_universal_player_id(player_id))
    for instance_id in instance_ids:
        mass.config.remove(f"{CONF_PROVIDERS}/{instance_id}")
    if instance_ids:
        await mass.webserver.auth.remove_from_user_filters(provider_instance_ids=instance_ids)
    LOGGER.info(
        "Removed the config of the retired %s provider and its %s unused player(s)",
        LOCAL_AUDIO_DOMAIN,
        len(player_ids),
    )


async def _find_playback_evidence(mass: MusicAssistant, player_ids: list[str]) -> str | None:
    """
    Return the id a playback was recorded under, or None when there is no evidence.

    :param mass: The MusicAssistant instance to query the library and cache of.
    :param player_ids: The player ids to look for evidence of playback of.
    """
    # playback from before the stubs were promoted is still keyed to the universal
    # player that wrapped them, which is not the player_id its settings ended up on
    queue_ids = list(
        dict.fromkeys(
            queue_id
            for player_id in player_ids
            for queue_id in (player_id, _legacy_universal_player_id(player_id))
        )
    )
    if not queue_ids:
        return None
    played = await _queue_ids_in_playlog(mass, queue_ids)
    saved_queues = await _saved_queue_payloads(mass, queue_ids)
    for queue_id in queue_ids:
        if queue_id in played:
            return queue_id
        if any(_payload_holds_content(payload) for payload in saved_queues.get(queue_id, ())):
            return queue_id
    return None


def _legacy_universal_player_id(player_id: str) -> str:
    """Return the id of the universal player that used to wrap the given local_audio player."""
    # mirrors the key _migrate_local_audio_attribution_stubs derives to find the wrapper
    return f"up{player_id.replace('-', '').lower()}"


async def _queue_ids_in_playlog(mass: MusicAssistant, queue_ids: list[str]) -> set[str]:
    """
    Return the subset of the given queue ids that something was ever played on.

    :param mass: The MusicAssistant instance to query the library of.
    :param queue_ids: The queue ids to look for, which for a player are its player ids.
    """
    # one query: playlog.queue_id has no index, so every lookup is a full scan
    params = {f"id_{index}": queue_id for index, queue_id in enumerate(queue_ids)}
    placeholders = ",".join(f":{name}" for name in params)
    rows = await mass.music.database.get_rows_from_query(
        f"SELECT DISTINCT queue_id FROM {DB_TABLE_PLAYLOG} WHERE queue_id IN ({placeholders})",
        params,
        limit=0,
    )
    return {str(row["queue_id"]) for row in rows}


async def _saved_queue_payloads(mass: MusicAssistant, queue_ids: list[str]) -> dict[str, list[Any]]:
    """
    Return the decoded payloads of the persisted queues of the given queue ids.

    Raises if an entry cannot be decoded, rather than reporting it as absent.

    :param mass: The MusicAssistant instance to query the cache of.
    :param queue_ids: The queue ids to read the persisted state and items of.
    """
    # not CacheController.get: it reports an entry it cannot decode as a miss, so a corrupt
    # queue would read as one never used. Expired entries are evidence too, hence no filter.
    assert mass.cache.database is not None
    params: dict[str, Any] = {
        "provider": mass.player_queues.domain,
        "state": CACHE_CATEGORY_PLAYER_QUEUE_STATE,
        "items": CACHE_CATEGORY_PLAYER_QUEUE_ITEMS,
    }
    params.update({f"id_{index}": queue_id for index, queue_id in enumerate(queue_ids)})
    placeholders = ",".join(f":id_{index}" for index in range(len(queue_ids)))
    rows = await mass.cache.database.get_rows_from_query(
        f"SELECT key, data FROM {DB_TABLE_CACHE} WHERE provider = :provider "
        f"AND category IN (:state, :items) AND key IN ({placeholders})",
        params,
        limit=0,
    )
    payloads: dict[str, list[Any]] = {}
    for row in rows:
        payloads.setdefault(str(row["key"]), []).append(await async_json_loads(row["data"]))
    return payloads


def _payload_holds_content(payload: Any) -> bool:
    """Return whether one persisted queue payload holds anything."""
    # the entry's presence proves nothing: every registered queue is flushed on shutdown
    if isinstance(payload, dict):
        # the state payload; its items are cached under their own category
        return bool(payload.get("enqueued_media_items") or payload.get("source_items"))
    return bool(payload)


def _mark_cleanup_done(mass: MusicAssistant) -> None:
    """Record that this cleanup ran, so a next startup skips it without querying."""
    mass.config.set(CONF_RETIRED_LOCAL_AUDIO_CLEANED, True, immediate=True)
