"""The loading and stopping of community providers in the running server."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderTier
from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import PROVIDER_LOAD_CONCURRENCY
from music_assistant.controllers.community_catalog.dependencies import (
    DependenciesUnavailable,
    DependencyConflict,
)
from music_assistant.controllers.community_catalog.gate import Decision, Verdict
from music_assistant.helpers.util import TaskManager, purge_provider_modules

if TYPE_CHECKING:
    import logging
    from pathlib import Path

    from music_assistant.controllers.community_catalog.dependencies import CommunityDependencies
    from music_assistant.controllers.community_catalog.store import CatalogStore
    from music_assistant.mass import MusicAssistant


class ProviderLoader:
    """Registers, loads and stops community providers: installed ones and those of the dev folder."""

    def __init__(
        self,
        mass: MusicAssistant,
        logger: logging.Logger,
        store: CatalogStore,
        dependencies: CommunityDependencies,
        decisions: dict[str, Decision],
    ) -> None:
        """
        Initialize the loader.

        :param mass: The Music Assistant instance.
        :param logger: The logger of the community catalog.
        :param store: The store that holds what is installed.
        :param dependencies: The requirements of the community providers.
        :param decisions: Whether each installed provider loads, by domain; a provider whose
            requirements clash is marked in it.
        """
        self.mass = mass
        self.logger = logger
        self._store = store
        self._dependencies = dependencies
        self._decisions = decisions

    async def load_providers(self, providers: dict[str, ProviderManifest]) -> tuple[set[str], bool]:
        """
        Install the requirements of community providers, then register them.

        A provider whose requirements clash is left out. Returns the domains that were
        registered, and whether their requirements were installed.

        :param providers: The manifest of each provider, by provider folder.
        """
        if not providers:
            return set(), True
        accepted = {manifest.domain: manifest.requirements for manifest in providers.values()}
        synced = True
        while True:
            try:
                await self._dependencies.sync(await self._dependencies.resolve(accepted))
            except DependencyConflict as err:
                self.logger.warning("Not loading community provider %s: %s", err.domain, err)
                if err.domain in self._decisions:
                    self._decisions[err.domain] = Decision(Verdict.REQUIREMENTS_CLASH)
                del accepted[err.domain]
                continue
            except DependenciesUnavailable as err:
                # a provider that misses a requirement fails to import, which its instance shows
                self.logger.warning("Could not install the community requirements: %s", err)
                synced = False
            break
        self._dependencies.activate()
        for provider_path, manifest in providers.items():
            if manifest.domain in accepted:
                await self.register(provider_path)
        return set(accepted), synced

    async def register(self, provider_path: str | Path) -> None:
        """
        Register a community provider, so its settings entries can load.

        :param provider_path: The folder of the provider.
        """
        self.mass.add_provider_location(os.path.dirname(provider_path))
        manifest = await self.mass.register_provider_manifest(
            str(provider_path), ProviderTier.COMMUNITY
        )
        installed = self._store.state.installed.get(manifest.domain)
        if installed is not None and not manifest.support_url:
            # help with a community provider is asked of its maintainer
            manifest.support_url = f"https://github.com/{installed.repository}"

    async def load_instances(self, domains: set[str]) -> None:
        """
        Load the enabled settings entries of community providers.

        :param domains: The domains of the providers.
        """
        if not domains:
            return
        async with TaskManager(self.mass, PROVIDER_LOAD_CONCURRENCY) as tg:
            for conf in await self.mass.config.get_provider_configs():
                if conf.enabled and conf.domain in domains:
                    await tg.create_task_with_limit(
                        self.mass.load_provider(conf.instance_id, allow_retry=True)
                    )

    async def stop(self, domain: str) -> None:
        """
        Stop a community provider: no instance of it runs and it can no longer be loaded.

        :param domain: The domain of the provider.
        """
        # unregistered first, so a retry that is due cannot load it again
        await self.mass.unregister_provider_manifest(domain)
        for provider in self.mass.providers:
            if provider.domain == domain:
                await self.mass.unload_provider(provider.instance_id)
        purge_provider_modules(domain)

    async def dev_providers(self) -> dict[str, ProviderManifest]:
        """Return the manifests of the providers of the dev folder that may load, by provider folder."""
        if (location := self.mass.dev_providers_path) is None:
            return {}
        if not self.mass.dev_mode:
            self.logger.warning(
                "Not loading the providers in %s: the server does not run in dev mode", location
            )
            return {}
        return await self._read_dev_providers(location)

    async def _read_dev_providers(self, location: str) -> dict[str, ProviderManifest]:
        """
        Return the manifests of the providers in a local folder that may load, by provider folder.

        :param location: The folder that holds the provider folders.
        """
        taken = {manifest.domain for manifest in self.mass.get_provider_manifests()}
        taken.update(self._store.state.installed)
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
