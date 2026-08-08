"""Searchable listing of the Home Assistant entities that can act as a player control."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Final, NamedTuple, TypedDict

from music_assistant_models.errors import InvalidDataError, ProviderUnavailableError

from .constants import (
    CONF_MUTE_CONTROLS,
    CONF_POWER_CONTROLS,
    CONF_VOLUME_CONTROLS,
    CONTROL_DOMAINS,
)
from .helpers import get_control_capabilities

if TYPE_CHECKING:
    from collections.abc import Callable

    from . import HomeAssistantProvider

SEARCH_CONTROL_ENTITIES_LIMIT = 50
# Ceiling on what a caller may raise the limit to, so a large Home Assistant setup can never
# be asked for a response big enough to hurt, however the search is called.
SEARCH_CONTROL_ENTITIES_MAX_LIMIT = 500
# How long a search may reuse the control entity candidates of an earlier search. Resolving
# them takes a full state sweep of Home Assistant, so a picker that searches while the user
# types would otherwise sweep on every keystroke. The candidates carry entity, device and
# area names plus control capabilities - none of which change often, and none of which is
# live entity state - so briefly serving a stale listing is harmless. Which entities exist
# at all is pinned to the entity registry instead of to this window, so an entity that
# appears or disappears is picked up right away.
CONTROL_ENTITY_CACHE_TTL = 30

# The entity domains worth inspecting for each control role. A superset is harmless: the
# authoritative verdict comes from get_control_capabilities on the entity's own state,
# this only keeps the state sweep of a search away from domains that can never qualify.
CONTROL_TYPE_DOMAINS: Final[dict[str, tuple[str, ...]]] = {
    CONF_POWER_CONTROLS: ("media_player", "switch", "input_boolean"),
    CONF_VOLUME_CONTROLS: ("media_player", "number", "input_number"),
    CONF_MUTE_CONTROLS: ("media_player", "switch", "input_boolean"),
}


class HassControlEntity(TypedDict):
    """A Home Assistant entity that can be used as a player control."""

    entity_id: str
    # the entity's friendly name, falling back to its entity ID when it has none
    name: str
    power: bool
    volume: bool
    mute: bool


# Selects the entities that can serve each control role.
CONTROL_TYPE_CAPABILITIES: Final[dict[str, Callable[[HassControlEntity], bool]]] = {
    CONF_POWER_CONTROLS: lambda entity: entity["power"],
    CONF_VOLUME_CONTROLS: lambda entity: entity["volume"],
    CONF_MUTE_CONTROLS: lambda entity: entity["mute"],
}


class HassControlEntityGroup(TypedDict):
    """
    The control entities that share a device and an area.

    A device holding entities that sit in different areas is reported as one group per area,
    since Home Assistant lets an entity override the area it would inherit from its device.
    Entities without a device are grouped by area alone.
    """

    device_id: str | None
    # None for entities that belong to no device, respectively to no area
    device_name: str | None
    area_name: str | None
    entities: list[HassControlEntity]


class HassControlEntitySearchResult(TypedDict):
    """The outcome of a player control entity search."""

    groups: list[HassControlEntityGroup]
    # True when matches were left out to honor the requested limit
    truncated: bool


class ControlEntitySearch:
    """Searchable view on the Home Assistant entities that can act as a player control."""

    def __init__(self, provider: HomeAssistantProvider) -> None:
        """
        Initialize the search.

        :param provider: The Home Assistant provider to read the registries and states from;
            its client must be connected before the first search.
        """
        self._provider = provider
        self._lock = asyncio.Lock()
        self._entries: dict[tuple[str, ...], _CandidateCacheEntry] = {}
        self._closed = False

    async def search(
        self,
        search: str | None = None,
        control_type: str | None = None,
        limit: int = SEARCH_CONTROL_ENTITIES_LIMIT,
    ) -> HassControlEntitySearchResult:
        """
        Search the Home Assistant entities that can be used as a player control.

        Music Assistant's own players are never part of the result. Consecutive searches are
        served from a short lived cache that an entity registry change drops right away, so a
        newly added or removed entity shows up immediately, while a device or area rename can
        lag by up to a minute.

        :param search: Text to match, case insensitively, against the entity ID, the entity
            name, its device name and its area name. Every whitespace separated word must
            match one of those fields, though not necessarily the same one. All eligible
            entities match when omitted.
        :param control_type: Restrict the result to entities that can serve this control role,
            given as one of the provider's control config keys (``power_controls``,
            ``volume_controls`` or ``mute_controls``). All roles are returned when omitted.
        :param limit: Maximum number of entities (not groups) to return, itself capped at
            ``SEARCH_CONTROL_ENTITIES_MAX_LIMIT``.
        :return: The matching entities grouped by the device and area they belong to, ordered
            by area, device and entity name, plus a flag telling whether matches were left out
            to honor the limit.
        :raises InvalidDataError: When the control type is unknown or the limit is below one.
        :raises ProviderUnavailableError: When the provider is no longer loaded.
        """
        self._check_open()
        if control_type is not None and control_type not in CONTROL_TYPE_DOMAINS:
            msg = f"Invalid control type: {control_type}"
            raise InvalidDataError(msg)
        if limit < 1:
            msg = f"Invalid limit: {limit}"
            raise InvalidDataError(msg)
        limit = min(limit, SEARCH_CONTROL_ENTITIES_MAX_LIMIT)
        domains = CONTROL_DOMAINS if control_type is None else CONTROL_TYPE_DOMAINS[control_type]
        matches = await self._get_candidates(domains)
        if control_type is not None:
            has_capability = CONTROL_TYPE_CAPABILITIES[control_type]
            matches = [match for match in matches if has_capability(match.entity)]
        if tokens := (search or "").casefold().split():
            matches = [match for match in matches if match.matches(tokens)]
        groups: dict[tuple[str | None, str | None], HassControlEntityGroup] = {}
        for match in matches[:limit]:
            group_key = (match.device_id, match.area_id)
            if (group := groups.get(group_key)) is None:
                group = HassControlEntityGroup(
                    device_id=match.device_id,
                    device_name=match.device_name,
                    area_name=match.area_name,
                    entities=[],
                )
                groups[group_key] = group
            # the candidates outlive this response, so hand out a copy a caller may mutate
            group["entities"].append(match.entity.copy())
        return HassControlEntitySearchResult(
            groups=list(groups.values()), truncated=len(matches) > limit
        )

    def close(self) -> None:
        """Drop everything cached and refuse any further search."""
        self._closed = True
        self._entries.clear()

    async def _get_candidates(self, domains: tuple[str, ...]) -> list[_ControlEntityMatch]:
        """
        Return the player control candidates found in the given entity domains.

        :param domains: The entity domains to consider.
        """
        if (matches := self._lookup(domains)) is not None:
            return matches
        async with self._lock:
            # a concurrent search may have resolved the same domains while this one waited
            if (matches := self._lookup(domains)) is not None:
                return matches
            generation = self._generation()
            matches = await self._resolve(domains)
            # candidates resolved against a registry that changed while the sweep was in
            # flight are stale on arrival, and a provider that unloaded meanwhile must not
            # see its cache refilled: serve this caller either way, but do not store
            if not self._closed and generation == self._generation():
                self._entries[domains] = _CandidateCacheEntry(
                    expires_at=time.monotonic() + CONTROL_ENTITY_CACHE_TTL,
                    generation=generation,
                    matches=matches,
                )
            return matches

    async def _resolve(self, domains: tuple[str, ...]) -> list[_ControlEntityMatch]:
        """
        Return the entities of the given domains that can serve as a player control.

        :param domains: The entity domains to consider.
        :return: The candidates in presentation order, each carrying every control role it
            can serve plus the device and area it belongs to.
        """
        entity_registry = await self._provider.get_entity_registry()
        devices = await self._provider.get_device_registry()
        areas = await self._provider.get_area_registry()
        states = await self._provider.get_states(domains=domains)
        self._check_open()
        matches: list[_ControlEntityMatch] = []
        for state in states:
            capabilities = get_control_capabilities(state, self._provider.logger)
            if not any(capabilities):
                continue
            entity_id = state["entity_id"]
            registry_entry = entity_registry.get(entity_id)
            device_id = registry_entry.device_id if registry_entry else None
            device = devices.get(device_id) if device_id else None
            # Home Assistant lets an entity override the area it inherits from its device
            area_id = (registry_entry.area_id if registry_entry else None) or (
                device["area_id"] if device else None
            )
            area = areas.get(area_id or "")
            name = state["attributes"].get("friendly_name") or entity_id
            device_name = (device["name_by_user"] or device["name"]) if device else None
            area_name = area["name"] if area else None
            matches.append(
                _ControlEntityMatch(
                    device_id=device_id,
                    device_name=device_name,
                    area_id=area_id,
                    area_name=area_name,
                    entity=HassControlEntity(
                        entity_id=entity_id,
                        name=name,
                        power=capabilities.power,
                        volume=capabilities.volume,
                        mute=capabilities.mute,
                    ),
                    search_text="\n".join(
                        field.casefold()
                        for field in (entity_id, name, device_name, area_name)
                        if field
                    ),
                )
            )
        matches.sort(key=lambda match: match.sort_key)
        return matches

    def _lookup(self, domains: tuple[str, ...]) -> list[_ControlEntityMatch] | None:
        """Return the cached candidates of the given domains, None when there are none left."""
        if (entry := self._entries.get(domains)) is None:
            return None
        if entry.generation != self._generation() or entry.expires_at <= time.monotonic():
            del self._entries[domains]
            return None
        return entry.matches

    def _generation(self) -> int:
        """Return the generation of the entity registry the candidates are resolved against."""
        # the device and area registries carry no such marker, and the provider already
        # holds them behind a window of their own that outlives the candidates, so the
        # entity registry is the only input a fresher sweep could actually improve on
        return self._provider.entity_registry_generation

    def _check_open(self) -> None:
        """Raise when the provider this search belongs to is no longer loaded."""
        if self._closed:
            msg = "The Home Assistant provider is no longer loaded"
            raise ProviderUnavailableError(msg)


class _ControlEntityMatch(NamedTuple):
    """A control entity together with the device and area it is presented under."""

    device_id: str | None
    device_name: str | None
    area_id: str | None
    area_name: str | None
    entity: HassControlEntity
    # all searchable fields, case folded and joined on a newline. a search token can never
    # contain whitespace, so a token found in here always sits within a single field
    search_text: str

    @property
    def sort_key(self) -> tuple[bool, str, bool, str, str, str]:
        """Return the ranking key, placing entities without an area or device last."""
        return (
            self.area_name is None,
            (self.area_name or "").casefold(),
            self.device_name is None,
            (self.device_name or "").casefold(),
            self.entity["name"].casefold(),
            self.entity["entity_id"],
        )

    def matches(self, tokens: list[str]) -> bool:
        """
        Return whether every search token occurs in one of the searchable fields.

        :param tokens: The case folded search tokens.
        """
        return all(token in self.search_text for token in tokens)


class _CandidateCacheEntry(NamedTuple):
    """Cached control entity candidates together with what makes them go stale."""

    expires_at: float
    generation: int
    matches: list[_ControlEntityMatch]
