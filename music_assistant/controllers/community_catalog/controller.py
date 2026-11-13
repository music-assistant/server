"""The community catalog: providers that are maintained outside the server."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

from music_assistant_models.enums import EventType, ProviderTier
from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import PROVIDER_LOAD_CONCURRENCY
from music_assistant.controllers.community_catalog.dependencies import (
    CommunityDependencies,
    DependenciesUnavailable,
    DependencyConflict,
)
from music_assistant.helpers.util import TaskManager
from music_assistant.models.core_controller import CoreController

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


class CommunityCatalogController(CoreController):
    """Loads the providers that community members maintain outside the server."""

    domain: str = "community_catalog"

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize the community catalog controller."""
        super().__init__(mass)
        self.manifest.name = "Community catalog"
        self.manifest.description = "Providers that community members maintain."
        self._dependencies = CommunityDependencies(mass)

    async def start(self) -> None:
        """
        Load the community providers and their enabled instances.

        Runs once, after the server's own providers are loaded; never in safe mode.
        """
        if (location := self.mass.dev_providers_path) is None:
            return
        if not self.mass.dev_mode:
            self.logger.warning(
                "Not loading the providers in %s: the server does not run in dev mode", location
            )
            return
        await self._load_providers(await self._read_dev_providers(location))

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
