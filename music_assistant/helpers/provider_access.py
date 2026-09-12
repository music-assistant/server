"""
Helpers to derive which music sources a user may see and use.

Ownership and sharing live on the provider instance (`ProviderConfig.access`); a user's
set of music sources is derived from those records. The derived set is read straight from
the raw provider configs, so disabled and unavailable instances count too. Playback narrows
that set once more: a streaming service the user has an enabled account of is served by that
account only.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any, NamedTuple

from music_assistant_models.auth import UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import ProviderSharing, ProviderType

from music_assistant.constants import CONF_PROVIDERS, MASS_LOGGER_NAME

if TYPE_CHECKING:
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


def visible_music_sources(mass: MusicAssistant, user: User | None) -> list[str] | None:
    """
    Return the instance ids of the music sources the given user may see.

    None means the user may see every configured music source.

    :param mass: The MusicAssistant instance.
    :param user: The user to resolve the music sources for; None for anonymous playback.
    """
    return _visible_sources(_music_sources(mass), user)


def visible_playback_sources(mass: MusicAssistant, user: User | None) -> list[str] | None:
    """
    Return the instance ids of the music sources the given playback user may use.

    Another account of a streaming service counts only when the user has no enabled account
    of that same service, so playback never moves off their own account. Sources whose
    instances each hold a library of their own are never narrowed this way.
    None means every configured music source may be used.

    :param mass: The MusicAssistant instance.
    :param user: The user the playback is for; None for anonymous playback.
    """
    sources = _music_sources(mass)
    return _without_other_instances_of_own_services(
        mass, sources, user, _visible_sources(sources, user)
    )


def playback_instance_for(
    mass: MusicAssistant, instance_id: str, allowed: list[str] | None
) -> str | None:
    """
    Return the music source that serves an item sitting on the given source, if any.

    An account of a streaming service is served through another allowed account of that
    same service, which resolves the very same item ids. None means no allowed source
    can serve the item.

    :param mass: The MusicAssistant instance.
    :param instance_id: The provider instance the item to play sits on.
    :param allowed: The music sources the playback user may use, or None for all of them.
    """
    if allowed is None or instance_id in allowed:
        return instance_id
    domain = _source_domain(mass, instance_id)
    if not _is_streaming_service(mass, instance_id, domain):
        return None
    # the allowed set is read off the raw configs and keeps disabled instances, so only a
    # loaded and available account of the service can stand in
    return next(
        (
            allowed_id
            for allowed_id in allowed
            if _source_domain(mass, allowed_id) == domain
            and exact_provider(mass, allowed_id) is not None
        ),
        None,
    )


def own_music_sources(mass: MusicAssistant, user: User | None) -> list[str]:
    """
    Return the instance ids of the music sources the given user owns.

    :param mass: The MusicAssistant instance.
    :param user: The user to resolve the music sources for; None for anonymous playback.
    """
    if user is None:
        return []
    return [source.instance_id for source in _music_sources(mass) if _is_owned_by(source, user)]


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
    configured music source may; ``preferred`` holds the enabled sources the playback user
    owns, which are to be tried first. Another account of a streaming service the user has
    an enabled account of never serves them.

    :param mass: The MusicAssistant instance.
    :param queue_id: The queue the playback belongs to.
    """
    user = await resolve_playback_user(mass, queue_id)
    return visible_playback_sources(mass, user), _own_enabled_sources(mass, user)


class _MusicSource(NamedTuple):
    """A configured music source, as the access rules read it off the raw config."""

    instance_id: str
    domain: str
    enabled: bool
    access: ProviderAccess | None


def _visible_sources(sources: list[_MusicSource], user: User | None) -> list[str] | None:
    """Return the music sources allowed for the user, or None if that is all of them."""
    allowed: list[str] = []
    restricted = False
    for source in sources:
        if access_allows(source.access, user):
            allowed.append(source.instance_id)
        else:
            restricted = True
    return allowed if restricted else None


def _without_other_instances_of_own_services(
    mass: MusicAssistant,
    sources: list[_MusicSource],
    user: User | None,
    allowed: list[str] | None,
) -> list[str] | None:
    """
    Return the allowed sources without the other accounts of the user's own services.

    Only a streaming service has one catalog behind its accounts; instances of a local or
    self-hosted source are libraries of their own and never narrow one another.
    """
    if user is None:
        return allowed
    own_domains = {
        source.domain
        for source in sources
        if source.enabled
        and _is_owned_by(source, user)
        and _is_streaming_service(mass, source.instance_id, source.domain)
    }
    dropped = {
        source.instance_id
        for source in sources
        if source.domain in own_domains and not _is_owned_by(source, user)
    }
    if not dropped:
        return allowed
    # an unrestricted user needs a list of their own once a source is taken out of it
    remaining = allowed if allowed is not None else [source.instance_id for source in sources]
    return [instance_id for instance_id in remaining if instance_id not in dropped]


def _own_enabled_sources(mass: MusicAssistant, user: User | None) -> list[str]:
    """Return the instance ids of the enabled music sources the given user owns."""
    if user is None:
        return []
    return [
        source.instance_id
        for source in _music_sources(mass)
        if source.enabled and _is_owned_by(source, user)
    ]


def _is_owned_by(source: _MusicSource, user: User) -> bool:
    """Return whether the given user owns the given music source."""
    return source.access is not None and source.access.owner == user.user_id


def _source_domain(mass: MusicAssistant, instance_id: str) -> str:
    """Return the provider domain of a music source, read straight off its raw config."""
    raw_conf: dict[str, Any] = mass.config.get(f"{CONF_PROVIDERS}/{instance_id}", {})
    return raw_conf.get("domain") or instance_id


def _is_streaming_service(mass: MusicAssistant, instance_id: str, domain: str) -> bool:
    """
    Return whether the instances of this music service are accounts of one shared catalog.

    Accounts of a streaming service resolve the same item ids, where instances of a local or
    self-hosted source each hold a library of their own. A service without a single loaded
    instance is not one, since none of its instances can play.
    """
    provider = mass.get_provider(instance_id, return_unavailable=True) or next(
        iter(mass.get_provider_instances(domain, return_unavailable=True)), None
    )
    if provider is None:
        # with no instance of the service loaded, none of them can serve playback, so
        # there is nothing to narrow
        return False
    # only the music provider model carries the notion of a shared catalog
    return bool(getattr(provider, "is_streaming_provider", False))


def _music_sources(mass: MusicAssistant) -> list[_MusicSource]:
    """Return every configured music source, read straight off the raw provider configs."""
    return [
        _MusicSource(
            instance_id=instance_id,
            domain=raw_conf.get("domain") or instance_id,
            enabled=raw_conf.get("enabled", True),
            access=_parse_access(instance_id, raw_conf),
        )
        for instance_id, raw_conf in mass.config.get(CONF_PROVIDERS, {}).items()
        if raw_conf.get("type") == ProviderType.MUSIC
    ]


def _parse_access(instance_id: str, raw_conf: dict[str, Any]) -> ProviderAccess | None:
    """Return the access record stored on a raw provider config, if it carries one."""
    if (raw_access := raw_conf.get("access")) is None:
        return None
    try:
        return ProviderAccess.from_dict(raw_access)
    except (ValueError, TypeError) as err:
        LOGGER.debug("Ignoring malformed access record of %s: %s", instance_id, err)
        return _UNREADABLE_ACCESS
