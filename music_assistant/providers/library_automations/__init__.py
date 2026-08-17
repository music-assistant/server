"""
Library Automations Plugin Provider for Music Assistant.

Generic trigger + condition + action rules for library events. The motivating example:
move a track to a playlist the moment it is unfavorited. See models.py for the rule shape,
triggers.py/actions.py for the registries, and conditions.py for the predicate evaluator.

There is no dedicated favorite/unfavorite event in Music Assistant: favorite changes are
signalled as a plain EventType.MEDIA_ITEM_UPDATED, indistinguishable at the event level from
any other metadata update to the same item. This provider therefore keeps its own in-memory
favorite-state cache and only fires unfavorited/favorited triggers on an actual True<->False
transition (see _on_media_item_updated below) - a naive check on the event's current favorite
value would misfire on every unrelated update to an already-unfavorited item.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any

from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, EventType, MediaType, ProviderFeature
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError

from music_assistant.helpers.security import is_safe_name
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.library_automations import actions, conditions, triggers
from music_assistant.providers.library_automations.models import (
    RULES_FILENAME,
    TRIGGER_MEDIA_ITEM_ADDED_TO_LIBRARY,
    TRIGGER_MEDIA_ITEM_FAVORITED,
    TRIGGER_MEDIA_ITEM_UNFAVORITED,
    AutomationRule,
    new_rule_id,
    validate_rule_media_types,
)
from music_assistant.providers.library_automations.storage import read_json, write_json

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.media_items import MediaItemType
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_MAX_RULES = "max_rules"
CONF_LOG_MATCHES = "log_matches"
DEFAULT_MAX_RULES = 50

TRACKED_MEDIA_TYPES = (MediaType.TRACK, MediaType.ALBUM, MediaType.ARTIST)

SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return LibraryAutomationsProvider(mass, manifest, config, SUPPORTED_FEATURES)


class LibraryAutomationsProvider(PluginProvider):
    """Generic trigger + condition + action automations for library events."""

    _rules_dir: str
    _rules: dict[str, AutomationRule]
    _favorite_cache: dict[tuple[MediaType, int], bool]
    _unregister_handles: list[Callable[[], None]]
    _flush_lock: asyncio.Lock

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return (
            ConfigEntry(
                key=CONF_MAX_RULES,
                type=ConfigEntryType.INTEGER,
                required=False,
                default_value=DEFAULT_MAX_RULES,
            ),
            ConfigEntry(
                key=CONF_LOG_MATCHES,
                type=ConfigEntryType.BOOLEAN,
                required=False,
                default_value=False,
            ),
        )

    async def handle_async_init(self) -> None:
        """Handle async initialization."""
        self._rules = {}
        self._favorite_cache = {}
        self._unregister_handles = []
        self._flush_lock = asyncio.Lock()
        self._rules_dir = os.path.join(self.mass.storage_path, "library_automations")
        if not await asyncio.to_thread(os.path.exists, self._rules_dir):
            await asyncio.to_thread(os.makedirs, self._rules_dir, exist_ok=True)
        await self._load_rules_from_disk()

    async def loaded_in_mass(self) -> None:
        """Register API commands and event subscriptions after the provider is loaded."""
        self._unregister_handles.append(
            self.mass.register_api_command(
                "library_automations/list_rules",
                self.list_rules,
                required_scope=Scope.LIBRARY_READ,
            )
        )
        self._unregister_handles.append(
            self.mass.register_api_command(
                "library_automations/get_rule", self.get_rule, required_scope=Scope.LIBRARY_READ
            )
        )
        self._unregister_handles.append(
            self.mass.register_api_command(
                "library_automations/create_rule",
                self.create_rule,
                required_scope=Scope.LIBRARY_WRITE,
            )
        )
        self._unregister_handles.append(
            self.mass.register_api_command(
                "library_automations/update_rule",
                self.update_rule,
                required_scope=Scope.LIBRARY_WRITE,
            )
        )
        self._unregister_handles.append(
            self.mass.register_api_command(
                "library_automations/delete_rule",
                self.delete_rule,
                required_scope=Scope.LIBRARY_WRITE,
            )
        )
        self._unregister_handles.append(
            self.mass.register_api_command(
                "library_automations/set_rule_enabled",
                self.set_rule_enabled,
                required_scope=Scope.LIBRARY_WRITE,
            )
        )
        self._unregister_handles.append(
            self.mass.register_api_command(
                "library_automations/list_trigger_types",
                self.list_trigger_types,
                required_scope=Scope.LIBRARY_READ,
            )
        )
        self._unregister_handles.append(
            self.mass.register_api_command(
                "library_automations/list_action_types",
                self.list_action_types,
                required_scope=Scope.LIBRARY_READ,
            )
        )
        self._unregister_handles.append(
            self.mass.subscribe(self._on_media_item_updated, EventType.MEDIA_ITEM_UPDATED)
        )
        self._unregister_handles.append(
            self.mass.subscribe(self._on_media_item_added, EventType.MEDIA_ITEM_ADDED)
        )
        self.logger.info(
            "Library Automations provider loaded with %d stored rule(s)", len(self._rules)
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        for unregister in self._unregister_handles:
            unregister()
        self._unregister_handles.clear()
        if is_removed:
            rules_file = os.path.join(self._rules_dir, RULES_FILENAME)
            if await asyncio.to_thread(os.path.isfile, rules_file):
                await asyncio.to_thread(os.remove, rules_file)

    # --- API commands ---

    async def list_rules(self) -> list[dict[str, Any]]:
        """Return all stored automation rules."""
        return [rule.to_dict() for rule in self._rules.values()]

    async def get_rule(self, rule_id: str) -> dict[str, Any] | None:
        """Return a single automation rule by id."""
        rule = self._rules.get(rule_id)
        return rule.to_dict() if rule else None

    async def create_rule(
        self,
        name: str,
        trigger: dict[str, Any],
        action: dict[str, Any],
        conditions: list[dict[str, Any]] | None = None,
        condition_logic: str = "AND",
        enabled: bool = True,
    ) -> dict[str, Any]:
        """
        Create a new automation rule.

        :param name: Display name for the rule.
        :param trigger: AutomationTrigger fields as dict (type, media_types, params).
        :param action: AutomationAction fields as dict (type, params).
        :param conditions: Optional list of AutomationCondition fields as dicts.
        :param condition_logic: "AND" or "OR" between conditions.
        :param enabled: Whether the rule is active immediately.
        :return: The created rule as a dict.
        """
        if not is_safe_name(name):
            msg = f"{name} is not a valid rule name"
            raise InvalidDataError(msg)
        max_rules = self.get_config_value(CONF_MAX_RULES, DEFAULT_MAX_RULES, return_type=int)
        if len(self._rules) >= max_rules:
            msg = f"Maximum number of rules ({max_rules}) reached"
            raise InvalidDataError(msg)
        rule = AutomationRule.from_dict(
            {
                "id": new_rule_id(),
                "name": name,
                "trigger": trigger,
                "conditions": conditions or [],
                "condition_logic": condition_logic,
                "action": action,
                "enabled": enabled,
            }
        )
        self._validate_rule(rule)
        await self.persist_rule(rule)
        return rule.to_dict()

    async def update_rule(self, rule_id: str, **fields: Any) -> dict[str, Any]:
        """
        Update an existing automation rule.

        :param rule_id: Id of the rule to update.
        :param fields: Any subset of the rule's dict fields to overwrite.
        :return: The updated rule as a dict.
        """
        existing = self._rules.get(rule_id)
        if existing is None:
            msg = f"Automation rule {rule_id} not found"
            raise MediaNotFoundError(msg)
        merged = existing.to_dict()
        merged.update(fields)
        merged["id"] = rule_id
        rule = AutomationRule.from_dict(merged)
        self._validate_rule(rule)
        await self.persist_rule(rule)
        return rule.to_dict()

    async def delete_rule(self, rule_id: str) -> None:
        """Delete an automation rule."""
        if self._rules.pop(rule_id, None) is not None:
            await self._flush_rules_to_disk()

    async def set_rule_enabled(self, rule_id: str, enabled: bool) -> None:
        """Enable or disable an automation rule without deleting it."""
        rule = self._rules.get(rule_id)
        if rule is None:
            msg = f"Automation rule {rule_id} not found"
            raise MediaNotFoundError(msg)
        rule.enabled = enabled
        await self.persist_rule(rule)

    async def list_trigger_types(self) -> list[dict[str, Any]]:
        """Return metadata about all available trigger types (for a future UI)."""
        return [
            {"id": t.id, "label": t.label, "description": t.description}
            for t in triggers.TRIGGER_TYPES.values()
        ]

    async def list_action_types(self) -> list[dict[str, Any]]:
        """Return metadata about all available action types (for a future UI)."""
        return [
            {"id": a.id, "label": a.label, "description": a.description}
            for a in actions.ACTION_TYPES.values()
        ]

    # --- persistence ---

    async def persist_rule(self, rule: AutomationRule) -> None:
        """Store/update a rule in memory and flush all rules to disk."""
        self._rules[rule.id] = rule
        await self._flush_rules_to_disk()

    # --- event handling ---

    async def _on_media_item_updated(self, event: MassEvent) -> None:
        """
        Detect favorite/unfavorite transitions and dispatch matching rules.

        MEDIA_ITEM_UPDATED fires on ANY metadata change to a library item, not just favorite
        toggles. Only a real True<->False change in our own cache counts as a transition; the
        first update seen for an item after startup just warms the cache (previous is None) and
        is deliberately not treated as a trigger, since its prior state is unknown.
        """
        item = event.data
        media_type = getattr(item, "media_type", None)
        if media_type not in TRACKED_MEDIA_TYPES:
            return
        if not str(item.item_id).isdigit():
            return  # only library items (numeric db id) are tracked
        key = (media_type, int(item.item_id))
        previous = self._favorite_cache.get(key)
        self._favorite_cache[key] = item.favorite
        if previous is None or previous == item.favorite:
            return
        trigger_type = TRIGGER_MEDIA_ITEM_UNFAVORITED if previous else TRIGGER_MEDIA_ITEM_FAVORITED
        await self._run_matching_rules(trigger_type, item)

    async def _on_media_item_added(self, event: MassEvent) -> None:
        """Dispatch rules listening for newly-added library items."""
        item = event.data
        media_type = getattr(item, "media_type", None)
        if media_type not in TRACKED_MEDIA_TYPES:
            return
        await self._run_matching_rules(TRIGGER_MEDIA_ITEM_ADDED_TO_LIBRARY, item)

    async def _run_matching_rules(self, trigger_type: str, item: MediaItemType) -> None:
        """Evaluate all enabled rules against a fired trigger and run matching actions."""
        for rule in list(self._rules.values()):
            if not rule.enabled:
                continue
            if not triggers.trigger_matches(rule.trigger, trigger_type, item):
                continue
            if not await conditions.evaluate_conditions(
                rule.conditions, rule.condition_logic, item, self
            ):
                continue
            if self.config.get_value(CONF_LOG_MATCHES):
                self.logger.info("Rule '%s' matched for %s", rule.name, item.uri)
            await actions.execute_action(self, rule, item)

    def _validate_rule(self, rule: AutomationRule) -> None:
        """Raise InvalidDataError if the rule references an unknown trigger/action type."""
        if rule.trigger.type not in triggers.TRIGGER_TYPES:
            msg = (
                f"Unknown trigger type: {rule.trigger.type!r}. "
                f"Must be one of {sorted(triggers.TRIGGER_TYPES)}"
            )
            raise InvalidDataError(msg)
        if rule.action.type not in actions.ACTION_TYPES:
            msg = (
                f"Unknown action type: {rule.action.type!r}. "
                f"Must be one of {sorted(actions.ACTION_TYPES)}"
            )
            raise InvalidDataError(msg)
        validate_rule_media_types(rule)

    async def _load_rules_from_disk(self) -> None:
        """Load all persisted rules from disk."""
        rules_file = os.path.join(self._rules_dir, RULES_FILENAME)
        if not await asyncio.to_thread(os.path.isfile, rules_file):
            return
        try:
            data = await read_json(rules_file)
            for rule_id, entry in data.items():
                self._rules[rule_id] = AutomationRule.from_dict(entry)
        except Exception as exc:
            self.logger.warning("Failed to load library automation rules: %s", exc)

    async def _flush_rules_to_disk(self) -> None:
        """Write all rules to disk as a single JSON file."""
        async with self._flush_lock:
            rules_file = os.path.join(self._rules_dir, RULES_FILENAME)
            data = {rule_id: rule.to_dict() for rule_id, rule in self._rules.items()}
            await write_json(rules_file, data)
