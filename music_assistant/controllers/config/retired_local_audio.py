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
from typing import TYPE_CHECKING

from music_assistant.constants import (
    CONF_PLAYERS,
    CONF_PROVIDERS,
    CONF_RETIRED_LOCAL_AUDIO_CLEANED,
    DB_TABLE_PLAYLOG,
)
from music_assistant.controllers.player_queues.constants import (
    CACHE_CATEGORY_PLAYER_QUEUE_ITEMS,
    CACHE_CATEGORY_PLAYER_QUEUE_STATE,
)

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
        used_by = await _find_playback_evidence(mass, player_ids)
    except Exception as err:
        # never torch a possibly-real setup on a degraded install: leaving the config in
        # place costs the user a banner, removing it wrongly costs them their settings
        LOGGER.warning(
            "Unable to determine whether the retired %s provider was ever used, "
            "keeping its configuration - %s",
            LOCAL_AUDIO_DOMAIN,
            err,
        )
        return
    if used_by is not None:
        LOGGER.debug(
            "Keeping the config of the retired %s provider: player %s was played to",
            LOCAL_AUDIO_DOMAIN,
            used_by,
        )
        _mark_cleanup_done(mass)
        return
    for player_id in player_ids:
        # this also drops the player's DSP/queue settings, its saved queue and the
        # unregistered protocol players (e.g. sendspin's spb_*) that were bridged to it
        mass.players.delete_player_config(player_id)
    for instance_id in instance_ids:
        mass.config.remove(f"{CONF_PROVIDERS}/{instance_id}")
    if instance_ids:
        await mass.webserver.auth.remove_from_user_filters(provider_instance_ids=instance_ids)
    LOGGER.info(
        "Removed the config of the retired %s provider and its %s unused player(s)",
        LOCAL_AUDIO_DOMAIN,
        len(player_ids),
    )
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


async def _find_playback_evidence(mass: MusicAssistant, player_ids: list[str]) -> str | None:
    """
    Return the id of the first player that was played to, or None when none was.

    :param mass: The MusicAssistant instance to query the library and cache of.
    :param player_ids: The player ids to look for evidence of playback of.
    """
    for player_id in player_ids:
        if await _has_playlog_entry(mass, player_id):
            return player_id
        if await _has_saved_queue_content(mass, player_id):
            return player_id
    return None


async def _has_playlog_entry(mass: MusicAssistant, player_id: str) -> bool:
    """Return True if something was ever played on the queue of the given player."""
    # playlog.queue_id is the queue id, which is the player id
    count = await mass.music.database.get_count_from_query(
        f"SELECT id FROM {DB_TABLE_PLAYLOG} WHERE queue_id = :queue_id",
        {"queue_id": player_id},
    )
    return count > 0


async def _has_saved_queue_content(mass: MusicAssistant, player_id: str) -> bool:
    """
    Return True if the given player has a persisted queue that holds anything.

    The mere existence of the cache entry proves nothing: the queues controller flushes
    the state of every registered queue on each clean shutdown, so an install that only
    ever booted with a sound card attached has one too. Only a non-empty payload counts.
    """
    state = await mass.cache.get(
        key=player_id,
        provider=mass.player_queues.domain,
        category=CACHE_CATEGORY_PLAYER_QUEUE_STATE,
        allow_expired_cache=True,
    )
    if isinstance(state, dict) and (state.get("enqueued_media_items") or state.get("source_items")):
        return True
    items = await mass.cache.get(
        key=player_id,
        provider=mass.player_queues.domain,
        category=CACHE_CATEGORY_PLAYER_QUEUE_ITEMS,
        allow_expired_cache=True,
    )
    return bool(items)


def _mark_cleanup_done(mass: MusicAssistant) -> None:
    """Record that this cleanup ran, so a next startup skips it without querying."""
    mass.config.set(CONF_RETIRED_LOCAL_AUDIO_CLEANED, True, immediate=True)
