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
from collections.abc import Iterable
from dataclasses import replace
from typing import TYPE_CHECKING, Any, NamedTuple

from music_assistant_models.auth import UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import MediaType, ProviderSharing, ProviderType
from music_assistant_models.errors import MediaNotFoundError, MusicAssistantError
from music_assistant_models.media_items import ItemMapping, MediaItem, Podcast, PodcastEpisode

from music_assistant.constants import CONF_PROVIDERS, MASS_LOGGER_NAME

if TYPE_CHECKING:
    from music_assistant_models.auth import User
    from music_assistant_models.media_items import ProviderMapping

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.helpers.provider_access")

# a record we can not read hides its source instead of exposing it
_UNREADABLE_ACCESS = ProviderAccess(sharing=ProviderSharing.PRIVATE)

# the media types a member who may not get explicit content is kept from by their own flag;
# a podcast episode follows its podcast
EXPLICIT_FLAGGED_MEDIA_TYPES = frozenset(
    {MediaType.TRACK, MediaType.ALBUM, MediaType.PODCAST, MediaType.AUDIOBOOK}
)


def access_allows(access: ProviderAccess | None, user: User | None) -> bool:
    """
    Return whether the given user may use a provider instance with this access record.

    :param access: The instance's access record; None means a source of the whole home.
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


def hidden_music_sources(mass: MusicAssistant, user: User | None) -> set[str]:
    """
    Return the instance ids, and service domains, of the music sources the user may not see.

    The domain of a service is included when the user may see none of its accounts, so a
    reference by domain is refused like one by instance id.

    :param mass: The MusicAssistant instance.
    :param user: The user to resolve the music sources for; None for anonymous playback.
    """
    sources = _music_sources(mass)
    hidden = {src.instance_id for src in sources if not access_allows(src.access, user)}
    visible_domains = {src.domain for src in sources if src.instance_id not in hidden}
    return hidden | {src.domain for src in sources if src.domain not in visible_domains}


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
    Return the access record of the given provider instance, or None for a source of the whole home.

    :param mass: The MusicAssistant instance.
    :param instance_id: The provider instance id to look up.
    """
    raw_conf = mass.config.get(f"{CONF_PROVIDERS}/{instance_id}", {})
    return _parse_access(instance_id, raw_conf)


def source_owner(mass: MusicAssistant, instance_id: str) -> str | None:
    """
    Return the user id owning the given provider instance, or None for a source of the whole home.

    :param mass: The MusicAssistant instance.
    :param instance_id: The provider instance id to look up.
    """
    access = source_access(mass, instance_id)
    return access.owner if access else None


def music_sources_access(mass: MusicAssistant) -> dict[str, ProviderAccess | None]:
    """
    Return the access record of every configured music source, by instance id.

    None means a source of the whole home.

    :param mass: The MusicAssistant instance.
    """
    return {source.instance_id: source.access for source in _music_sources(mass)}


def visible_provider(
    mass: MusicAssistant, instance_id_or_domain: str, user: User | None, strict: bool = False
) -> ProviderInstanceType | None:
    """
    Return the loaded provider serving this instance id or domain, if the user may see it.

    An unavailable account of a streaming service resolves to another loaded account of that
    service (see `mass.get_provider`), so the account actually serving is the one checked; one
    hidden from the user gives way to a visible account of the same service, if there is one,
    the user's own account before a shared one.
    Only music sources are narrowed; None for the user means no narrowing at all.

    :param mass: The MusicAssistant instance.
    :param instance_id_or_domain: The provider instance id or domain to look up.
    :param user: The user to check the source for, None for unfiltered access.
    :param strict: Serve exactly this instance, never another account of the service.
    """
    if strict:
        provider = exact_provider(mass, instance_id_or_domain)
    else:
        provider = mass.get_provider(instance_id_or_domain)
        if provider is None and user is not None:
            # an account that is not loaded at all hands the request to another loaded account
            # of its streaming service, as an unavailable one does
            domain = next(
                (s.domain for s in _music_sources(mass) if s.instance_id == instance_id_or_domain),
                None,
            )
            provider = next(
                (
                    other
                    for other in (mass.get_provider_instances(domain) if domain else [])
                    if getattr(other, "is_streaming_provider", False)
                ),
                None,
            )
    if provider is None or user is None or provider.type != ProviderType.MUSIC:
        return provider
    hidden = hidden_music_sources(mass, user)
    if strict or not getattr(provider, "is_streaming_provider", False):
        return None if provider.instance_id in hidden else provider
    if provider.instance_id == instance_id_or_domain and provider.instance_id not in hidden:
        return provider
    # another account of the service serves the request: choose among the visible ones
    candidates = [
        other
        for other in mass.get_provider_instances(provider.domain)
        if other.instance_id not in hidden
    ]
    # the user's own account of the service comes before one merely shared with them
    candidates.sort(key=lambda other: source_owner(mass, other.instance_id) != user.user_id)
    return candidates[0] if candidates else None


def has_visible_source(
    mass: MusicAssistant, provider_mappings: Iterable[ProviderMapping], user: User | None
) -> bool:
    """
    Return whether at least one of the mappings is on a source the user may see.

    Mappings on anything but a music source always count as visible, as does an item
    without mappings.

    :param mass: The MusicAssistant instance.
    :param provider_mappings: The provider mappings of the item.
    :param user: The user to check the sources for, None for unfiltered access.
    """
    if user is None:
        return True
    mappings = list(provider_mappings)
    if not mappings:
        return True
    hidden = hidden_music_sources(mass, user)
    return any(mapping.provider_instance not in hidden for mapping in mappings)


def visible_items[T](mass: MusicAssistant, items: Iterable[T], user: User | None) -> list[T]:
    """
    Return the items, leaving out the media items no source the user may see can serve.

    A media item is kept when at least one of its provider mappings resolves through
    `visible_provider`; other items, such as folders, are always kept.

    :param mass: The MusicAssistant instance.
    :param items: The items to narrow.
    :param user: The user to narrow the items for, None for no narrowing at all.
    """
    if user is None or not hidden_music_sources(mass, user):
        return list(items)
    # resolved once per provider instance, as a listing maps onto only a handful of them
    visible: dict[str, bool] = {}

    def _is_visible(instance_id: str) -> bool:
        if (known := visible.get(instance_id)) is None:
            known = visible[instance_id] = visible_provider(mass, instance_id, user) is not None
        return known

    return [
        item
        for item in items
        if not isinstance(item, MediaItem)
        # a library item is only visible through the exact accounts holding it
        or (
            has_visible_source(mass, item.provider_mappings, user)
            if item.provider == "library"
            else any(_is_visible(mapping.provider_instance) for mapping in item.provider_mappings)
        )
    ]


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
    return playback_sources_for(mass, await resolve_playback_user(mass, queue_id))


def playback_sources_for(
    mass: MusicAssistant, user: User | None
) -> tuple[list[str] | None, list[str]]:
    """
    Return the (allowed, preferred) music sources for the given playback user.

    The sources :func:`playback_sources` returns once a queue plays for this user.

    :param mass: The MusicAssistant instance.
    :param user: The user the playback is for; None for anonymous playback.
    """
    return visible_playback_sources(mass, user), _own_enabled_sources(mass, user)


class _MusicSource(NamedTuple):
    """A configured music source, as the access rules read it off the raw config."""

    instance_id: str
    domain: str
    enabled: bool
    access: ProviderAccess | None


def restricts_explicit(user: User | None) -> bool:
    """
    Return whether the given user is kept from explicit content.

    :param user: The user; None for anonymous requests and playback, which are not restricted.
    """
    return user is not None and not user.allow_explicit


def explicit_allowed_by_flag(item: object, user: User | None) -> bool:
    """
    Return whether the user may see and play the item, judged by the flags it carries.

    The part of :func:`explicit_allowed` that needs no look up: a library reference, or an
    episode that only maps its podcast, counts as allowed here.

    :param item: A media item, item mapping or browse folder.
    :param user: The user the item is for, None for no restriction.
    """
    if not restricts_explicit(user) or isinstance(item, ItemMapping):
        return True
    if getattr(item, "media_type", None) in EXPLICIT_FLAGGED_MEDIA_TYPES:
        return not _flagged_explicit(item)
    if isinstance(item, PodcastEpisode) and isinstance(item.podcast, Podcast):
        return not _flagged_explicit(item.podcast)
    return True


async def explicit_allowed(
    mass: MusicAssistant,
    item: object,
    user: User | None,
    podcasts: dict[tuple[str, str], bool] | None = None,
) -> bool:
    """
    Return whether the user may see and play the item, as far as explicit content goes.

    Tracks, albums, podcasts and audiobooks go by their own flag, a podcast episode by its
    podcast. An item without a flag, or one that can not be resolved, counts as allowed.

    :param mass: The MusicAssistant instance.
    :param item: A media item, item mapping or browse folder.
    :param user: The user the item is for, None for no restriction.
    :param podcasts: Whether each podcast (by provider and item id) is explicit, as already
        looked up for an earlier item, so a listing looks each podcast up once.
    """
    if not restricts_explicit(user):
        return True
    if isinstance(item, ItemMapping):
        if item.provider != "library" or item.media_type not in EXPLICIT_FLAGGED_MEDIA_TYPES:
            # a reference without metadata that is not a library item can not be judged
            return True
        try:
            item = await mass.music.get_controller(item.media_type).get_library_item(item.item_id)
        except MusicAssistantError:
            return True
    if isinstance(item, PodcastEpisode) and isinstance(item.podcast, ItemMapping):
        return not await _podcast_explicit(mass, item, podcasts if podcasts is not None else {})
    return explicit_allowed_by_flag(item, user)


async def ensure_explicit_allowed(mass: MusicAssistant, item: Any, user: User | None) -> None:
    """
    Raise when the user may not play the item for it being explicit.

    :param mass: The MusicAssistant instance.
    :param item: The already-resolved item about to be enqueued.
    :param user: The user the playback is for; None for anonymous playback.
    :raises MediaNotFoundError: The item is explicit content the user may not play.
    """
    if await explicit_allowed(mass, item, user):
        return
    msg = f"{item.name} is explicit content, which this user may not play"
    raise MediaNotFoundError(msg, translation_key="media_explicit_not_allowed")


async def without_disallowed_explicit[T](
    mass: MusicAssistant, items: Iterable[T], user: User | None
) -> list[T]:
    """
    Return the items, leaving out the explicit ones the user may not see and play.

    :param mass: The MusicAssistant instance.
    :param items: The items to narrow.
    :param user: The user to narrow the items for, None for no narrowing at all.
    """
    if not restricts_explicit(user):
        return list(items)
    podcasts: dict[tuple[str, str], bool] = {}
    return [item for item in items if await explicit_allowed(mass, item, user, podcasts)]


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


def _flagged_explicit(item: object) -> bool:
    """Return whether the item carries the explicit flag of its music source."""
    metadata = getattr(item, "metadata", None)
    return metadata is not None and metadata.explicit is True


async def _podcast_explicit(
    mass: MusicAssistant, episode: PodcastEpisode, podcasts: dict[tuple[str, str], bool]
) -> bool:
    """Return whether the podcast an episode maps to is marked explicit, looking it up."""
    podcast = episode.podcast
    key = (podcast.provider, podcast.item_id)
    if key not in podcasts:
        try:
            full_podcast = await mass.music.podcasts.get(podcast.item_id, podcast.provider)
        except MusicAssistantError:
            # a podcast that can not be looked up does not keep its episodes from the user
            podcasts[key] = False
        else:
            podcasts[key] = _flagged_explicit(full_podcast)
    return podcasts[key]
