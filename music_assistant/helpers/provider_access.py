"""
Helpers to derive which music sources a user may see and use.

Ownership and sharing live on the provider instance (`ProviderConfig.access`); a user's
set of music sources is derived from those records. The derived set is read straight from
the raw provider configs, so disabled and unavailable instances count too.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from music_assistant_models.auth import UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import ProviderSharing, ProviderType

from music_assistant.constants import CONF_PROVIDERS, MASS_LOGGER_NAME

if TYPE_CHECKING:
    from collections.abc import Iterator

    from music_assistant_models.auth import User

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.helpers.provider_access")

# a record we can not read hides its source instead of exposing it
_UNREADABLE_ACCESS = ProviderAccess(sharing=ProviderSharing.PRIVATE)


def access_allows(access: ProviderAccess | None, user: User | None) -> bool:
    """
    Return whether the given user may use a provider instance with this access record.

    :param access: The instance's access record; None means a household source.
    :param user: The user to check; None means anonymous playback.
    """
    if access is None:
        return True
    if user is None:
        return access.sharing == ProviderSharing.EVERYONE
    if access.owner == user.user_id:
        return True
    if access.sharing == ProviderSharing.EVERYONE:
        return True
    if access.sharing == ProviderSharing.MEMBERS:
        return user.role != UserRole.GUEST
    if access.sharing == ProviderSharing.SELECTED:
        return user.user_id in access.shared_users
    return False


def visible_music_sources(mass: MusicAssistant, user: User) -> list[str] | None:
    """
    Return the instance ids of the music sources the given user may see.

    None means the user may see every configured music source.

    :param mass: The MusicAssistant instance.
    :param user: The user to resolve the music sources for.
    """
    return _visible_sources(mass, user)


def visible_playback_sources(mass: MusicAssistant, user: User | None) -> list[str] | None:
    """
    Return the instance ids of the music sources the given playback user may use.

    None means every configured music source may be used.

    :param mass: The MusicAssistant instance.
    :param user: The user the playback is for; None for anonymous playback.
    """
    return _visible_sources(mass, user)


def own_music_sources(mass: MusicAssistant, user: User | None) -> list[str]:
    """
    Return the instance ids of the music sources the given user owns.

    :param mass: The MusicAssistant instance.
    :param user: The user to resolve the music sources for; None for anonymous playback.
    """
    if user is None:
        return []
    return [
        instance_id
        for instance_id, access in _music_source_access(mass)
        if access is not None and access.owner == user.user_id
    ]


def source_access(mass: MusicAssistant, instance_id: str) -> ProviderAccess | None:
    """
    Return the access record of the given provider instance, or None for a household source.

    :param mass: The MusicAssistant instance.
    :param instance_id: The provider instance id to look up.
    """
    raw_conf = mass.config.get(f"{CONF_PROVIDERS}/{instance_id}", {})
    return _parse_access(instance_id, raw_conf)


def source_owner(mass: MusicAssistant, instance_id: str) -> str | None:
    """
    Return the user id owning the given provider instance, or None for a household source.

    :param mass: The MusicAssistant instance.
    :param instance_id: The provider instance id to look up.
    """
    access = source_access(mass, instance_id)
    return access.owner if access else None


def exact_provider(mass: MusicAssistant, instance_id: str) -> ProviderInstanceType | None:
    """
    Return the loaded and available provider with exactly this instance id, if there is one.

    Where `mass.get_provider` widens to another instance of the same domain, this never does,
    so a mapping that passed an access check is not served by an account the user may not use.

    :param mass: The MusicAssistant instance.
    :param instance_id: The provider instance id to look up.
    """
    provider = mass.get_provider(instance_id, return_unavailable=True)
    if provider is None or not provider.available or provider.instance_id != instance_id:
        return None
    return provider


def derived_provider_filter(mass: MusicAssistant, user: User) -> list[str]:
    """
    Return the user's visible music sources as a legacy provider filter list.

    An empty list means the user may see every configured music source.

    :param mass: The MusicAssistant instance.
    :param user: The user to resolve the music sources for.
    """
    visible = visible_music_sources(mass, user)
    return [] if visible is None else visible


def with_derived_provider_filter(mass: MusicAssistant, user: User) -> User:
    """
    Return the user as the API serves it, with its music sources as provider filter.

    :param mass: The MusicAssistant instance.
    :param user: The user to serve.
    """
    return replace(user, provider_filter=derived_provider_filter(mass, user))


async def resolve_playback_user(mass: MusicAssistant, queue_id: str) -> User | None:
    """
    Return the user the queue plays for, None for anonymous playback.

    :param mass: The MusicAssistant instance.
    :param queue_id: The queue the playback belongs to.
    """
    if (pq_data := mass.player_queues.queue_data_or_none(queue_id)) and pq_data.userid:
        return await mass.webserver.auth.get_user(pq_data.userid)
    return None


async def playback_sources(
    mass: MusicAssistant, queue_id: str
) -> tuple[list[str] | None, list[str]]:
    """
    Return the (allowed, preferred) music sources for the queue's playback user.

    ``allowed`` holds the sources that may serve this playback, or None when every
    configured music source may; ``preferred`` holds the sources the playback user owns,
    which are to be tried first.

    :param mass: The MusicAssistant instance.
    :param queue_id: The queue the playback belongs to.
    """
    user = await resolve_playback_user(mass, queue_id)
    return visible_playback_sources(mass, user), own_music_sources(mass, user)


def _visible_sources(mass: MusicAssistant, user: User | None) -> list[str] | None:
    """Return the music sources allowed for the user, or None if that is all of them."""
    allowed: list[str] = []
    restricted = False
    for instance_id, access in _music_source_access(mass):
        if access_allows(access, user):
            allowed.append(instance_id)
        else:
            restricted = True
    return allowed if restricted else None


def _music_source_access(mass: MusicAssistant) -> Iterator[tuple[str, ProviderAccess | None]]:
    """Yield the (instance id, access record) of every configured music source."""
    for instance_id, raw_conf in mass.config.get(CONF_PROVIDERS, {}).items():
        if raw_conf.get("type") != ProviderType.MUSIC:
            continue
        yield instance_id, _parse_access(instance_id, raw_conf)


def _parse_access(instance_id: str, raw_conf: dict[str, Any]) -> ProviderAccess | None:
    """Return the access record stored on a raw provider config, if it carries one."""
    if (raw_access := raw_conf.get("access")) is None:
        return None
    try:
        return ProviderAccess.from_dict(raw_access)
    except (ValueError, TypeError) as err:
        LOGGER.debug("Ignoring malformed access record of %s: %s", instance_id, err)
        return _UNREADABLE_ACCESS
