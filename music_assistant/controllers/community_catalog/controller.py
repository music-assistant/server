"""The community catalog: providers that are maintained outside the server."""

from __future__ import annotations

import asyncio
import os
import random
from typing import TYPE_CHECKING, Final

from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, EventType, ProviderTier
from music_assistant_models.errors import ActionUnavailable
from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import PROVIDER_LOAD_CONCURRENCY
from music_assistant.controllers.community_catalog.constants import (
    COMMUNITY_PROVIDERS_FOLDER,
    CONF_ENABLED,
)
from music_assistant.controllers.community_catalog.dependencies import (
    CommunityDependencies,
    DependenciesUnavailable,
    DependencyConflict,
)
from music_assistant.controllers.community_catalog.index import IndexClient, server_line
from music_assistant.controllers.community_catalog.models import (
    CommunityProvider,
    CommunityProviderState,
    IndexProvider,
    IndexStatus,
)
from music_assistant.helpers.api import api_command
from music_assistant.helpers.util import TaskManager, set_provider_tier
from music_assistant.models.core_controller import CoreController

if TYPE_CHECKING:
    from music_assistant_models.config_entries import CoreConfig

    from music_assistant.mass import MusicAssistant

# the timer of the daily check of the index, and the task that runs a check
_INDEX_CHECK: Final = "community_catalog_index_check"
_CHECK_INTERVAL: Final = 24 * 3600
# spreads the checks of all servers over an hour
_CHECK_SPREAD: Final = 3600


class CommunityCatalogController(CoreController):
    """Loads the providers that community members maintain outside the server."""

    domain: str = "community_catalog"

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize the community catalog controller."""
        super().__init__(mass)
        self.manifest.name = "Community catalog"
        self.manifest.description = (
            "Music sources, player support, metadata sources and plugins "
            "that people in the community build and maintain."
        )
        self._dependencies = CommunityDependencies(mass)
        self._index_client = IndexClient(
            mass, os.path.join(mass.storage_path, COMMUNITY_PROVIDERS_FOLDER), self.logger
        )

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return all Config Entries for this core module (if any)."""
        return (
            ConfigEntry(
                key=CONF_ENABLED,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                required=False,
                # the app switches it on from the catalog, after the user confirmed its description
                hidden=True,
            ),
        )

    async def update_config(self, config: CoreConfig, changed_keys: set[str]) -> None:
        """Handle logic when the config is updated."""
        await super().update_config(config, changed_keys)
        if f"values/{CONF_ENABLED}" not in changed_keys or self.mass.safe_mode:
            return
        if self._needs_index():
            self.mass.create_task(
                self._check_index(initial=True), task_id=_INDEX_CHECK, abort_existing=True
            )
            return
        self.mass.cancel_timer(_INDEX_CHECK)
        self.mass.cancel_task(_INDEX_CHECK)
        self.mass.signal_event(EventType.COMMUNITY_CATALOG_UPDATED)

    async def start(self) -> None:
        """
        Bring the index up to date, then load the community providers and their enabled instances.

        Runs once, after the server's own providers are loaded; never in safe mode.
        """
        if self._needs_index():
            await self._check_index(initial=True)
        if (location := self.mass.dev_providers_path) is None:
            return
        if not self.mass.dev_mode:
            self.logger.warning(
                "Not loading the providers in %s: the server does not run in dev mode", location
            )
            return
        await self._load_providers(await self._read_dev_providers(location))

    @api_command("community_catalog/providers", required_scope=Scope.SYSTEM_MANAGE)
    async def get_providers(self) -> list[CommunityProvider]:
        """Return the providers of the community catalog, none while it is switched off."""
        self._raise_in_safe_mode()
        index = self._index_client.index
        # a kept copy of another line offers what another server version can install
        if (
            not self._needs_index()
            or index is None
            or index.server_line != server_line(self.mass.version)
        ):
            return []
        return [
            provider
            for domain, entry in index.providers.items()
            if (provider := self._available_provider(domain, entry)) is not None
        ]

    @api_command("community_catalog/refresh", required_scope=Scope.SYSTEM_MANAGE)
    async def refresh(self) -> list[CommunityProvider]:
        """Fetch the index of the community catalog now, and return its providers."""
        self._raise_in_safe_mode()
        if self._needs_index() and await self._index_client.fetch():
            self.mass.signal_event(EventType.COMMUNITY_CATALOG_UPDATED)
        return await self.get_providers()

    def _needs_index(self) -> bool:
        """Return whether the server keeps the index of the community catalog current."""
        return bool(self.config.get_value(CONF_ENABLED))

    async def _check_index(self, initial: bool = False) -> None:
        """
        Fetch the index, then schedule the next daily check.

        :param initial: Read the kept copy first and signal the list as changed, for the first
            check after the start or after the catalog was switched on.
        """
        try:
            if initial:
                await self._index_client.load()
            if await self._index_client.fetch() or initial:
                self.mass.signal_event(EventType.COMMUNITY_CATALOG_UPDATED)
        finally:
            if self._needs_index():
                delay = _CHECK_INTERVAL + random.uniform(0, _CHECK_SPREAD)  # noqa: S311 - spread
                self.mass.call_later(delay, self._check_index, task_id=_INDEX_CHECK)

    def _available_provider(self, domain: str, entry: IndexProvider) -> CommunityProvider | None:
        """
        Return a provider of the index that can be installed, or None.

        :param domain: The domain of the provider.
        :param entry: The provider in the index.
        """
        latest = entry.latest
        if entry.status != IndexStatus.LISTED or latest is None or latest not in entry.versions:
            return None
        try:
            manifest = ProviderManifest.from_dict(entry.manifest)
        except (ValueError, LookupError) as err:
            self.logger.warning(
                "Not listing community provider %s: invalid manifest: %s", domain, err
            )
            return None
        set_provider_tier(manifest, ProviderTier.COMMUNITY)
        return CommunityProvider(
            domain=domain,
            repository=entry.repository,
            maintainers=entry.maintainers,
            icon=entry.icon,
            manifest=manifest,
            state=CommunityProviderState.AVAILABLE,
            latest_version=latest,
            added=entry.added,
            released=entry.versions[latest].released,
            reason=entry.reason,
            reason_url=entry.reason_url,
        )

    def _raise_in_safe_mode(self) -> None:
        """Raise when the server runs in safe mode, which loads no community provider."""
        if self.mass.safe_mode:
            msg = "The community catalog is not available in safe mode"
            raise ActionUnavailable(msg)

    async def _read_dev_providers(self, location: str) -> dict[str, ProviderManifest]:
        """
        Return the manifests of the providers in a local folder that may load, by provider folder.

        :param location: The folder that holds the provider folders.
        """
        taken = {manifest.domain for manifest in self.mass.get_provider_manifests()}
        providers: dict[str, ProviderManifest] = {}
        for name in sorted(await asyncio.to_thread(os.listdir, location)):
            provider_path = os.path.join(location, name)
            manifest_path = os.path.join(provider_path, "manifest.json")
            if name.startswith(".") or not await asyncio.to_thread(os.path.isfile, manifest_path):
                continue
            try:
                manifest = await ProviderManifest.parse(manifest_path)
            except Exception as err:
                reason = f"its manifest is invalid: {err}"
            else:
                # registering refuses these too, but only after the requirements are installed
                if manifest.domain != name:
                    reason = f"the folder is not named after its domain {manifest.domain}"
                elif manifest.domain in taken:
                    reason = f"the server already has a provider named {manifest.domain}"
                else:
                    providers[provider_path] = manifest
                    continue
            self.logger.warning("Not loading the provider in %s: %s", provider_path, reason)
        return providers

    async def _load_providers(self, providers: dict[str, ProviderManifest]) -> None:
        """
        Install the requirements of community providers, then register and load them.

        A provider whose requirements cannot be installed is left out.

        :param providers: The manifest of each provider, by provider folder.
        """
        if not providers:
            return
        accepted = {manifest.domain: manifest.requirements for manifest in providers.values()}
        while True:
            try:
                await self._dependencies.sync(await self._dependencies.resolve(accepted))
            except DependencyConflict as err:
                self.logger.warning("Not loading community provider %s: %s", err.domain, err)
                del accepted[err.domain]
                continue
            except DependenciesUnavailable as err:
                # a provider that misses a requirement fails to import, which its instance shows
                self.logger.warning("Could not install the community requirements: %s", err)
            break
        self._dependencies.activate()
        for provider_path, manifest in providers.items():
            if manifest.domain in accepted:
                self.mass.add_provider_location(os.path.dirname(provider_path))
                await self.mass.register_provider_manifest(provider_path, ProviderTier.COMMUNITY)
        # clients fetched the manifests before these were registered
        self.mass.signal_event(EventType.COMMUNITY_CATALOG_UPDATED)
        async with TaskManager(self.mass, PROVIDER_LOAD_CONCURRENCY) as tg:
            for conf in await self.mass.config.get_provider_configs():
                if conf.enabled and conf.domain in accepted:
                    await tg.create_task_with_limit(
                        self.mass.load_provider(conf.instance_id, allow_retry=True)
                    )
