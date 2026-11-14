"""The community catalog: providers that are maintained outside the server."""

from __future__ import annotations

import asyncio
import os
import random
from contextlib import suppress
from dataclasses import replace
from typing import TYPE_CHECKING, Final

from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import ConfigEntry, ProviderError
from music_assistant_models.enums import ConfigEntryType, EventType, ProviderTier
from music_assistant_models.errors import ActionUnavailable, ProviderUnavailableError
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
from music_assistant.controllers.community_catalog.gate import Decision, Verdict, evaluate
from music_assistant.controllers.community_catalog.index import IndexClient, server_line
from music_assistant.controllers.community_catalog.installer import (
    Installer,
    InstallError,
    content_hash,
)
from music_assistant.controllers.community_catalog.models import (
    CommunityProvider,
    CommunityProviderState,
    IndexProvider,
    IndexStatus,
    InstalledProvider,
)
from music_assistant.controllers.community_catalog.store import CatalogStore
from music_assistant.helpers.api import api_command
from music_assistant.helpers.util import TaskManager, purge_provider_modules, set_provider_tier
from music_assistant.models.core_controller import CoreController

if TYPE_CHECKING:
    from pathlib import Path

    from music_assistant_models.config_entries import CoreConfig

    from music_assistant.controllers.community_catalog.models import CatalogIndex
    from music_assistant.mass import MusicAssistant

# the timer of the daily check of the index, and the task that runs a check
_INDEX_CHECK: Final = "community_catalog_index_check"
_CHECK_INTERVAL: Final = 24 * 3600
# spreads the checks of all servers over an hour
_CHECK_SPREAD: Final = 3600
# why an installed provider does not load, as the admin reads it; the texts of the
# translation keys community_provider_<verdict> in the errors of strings.json
_REASONS: Final[dict[Verdict, str]] = {
    Verdict.CATALOG_UNREACHABLE: (
        "The community catalog could not be reached to check this, so it does not run. "
        "Restart Music Assistant when your server is online again."
    ),
    Verdict.BLOCKED: "The community catalog blocked this ({0}), so it no longer runs. "
    "You can remove it.",
    Verdict.NOT_LISTED: (
        "This is no longer in the community catalog, so it no longer runs. You can remove it."
    ),
    Verdict.NOT_FOR_THIS_VERSION: (
        "This is not available for this version of Music Assistant, so it does not run. "
        "You can remove it."
    ),
    Verdict.REQUIREMENTS_CLASH: (
        "This needs versions of Python packages that clash with Music Assistant or with "
        "something else from the community catalog, so it does not run."
    ),
    Verdict.REINSTALL_FAILED: (
        "The files of this were changed on your server and could not be downloaded again "
        "({0}), so it does not run. Restart Music Assistant to try again."
    ),
}


class CommunityCatalogController(CoreController):
    """Installs and loads the providers that community members maintain outside the server."""

    domain: str = "community_catalog"

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize the community catalog controller."""
        super().__init__(mass)
        self.manifest.name = "Community catalog"
        self.manifest.description = (
            "Music sources, player support, metadata sources and plugins "
            "that people in the community build and maintain."
        )
        folder = os.path.join(mass.storage_path, COMMUNITY_PROVIDERS_FOLDER)
        self._store = CatalogStore(folder, self.logger)
        self._index_client = IndexClient(mass, self._store, self.logger)
        self._installer = Installer(mass, folder, self.logger)
        self._dependencies = CommunityDependencies(mass)
        # held by everything that changes what is installed or loaded
        self._lock = asyncio.Lock()
        # by installed domain: whether it loads, and the content hash of its live folder
        self._decisions: dict[str, Decision] = {}
        self._folder_hashes: dict[str, str | None] = {}

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
        # what the list offers depends on the switch
        self.mass.signal_event(EventType.COMMUNITY_CATALOG_UPDATED)
        if self.config.get_value(CONF_ENABLED):
            # a check that already runs is left to finish: it may be stopping a provider
            self.mass.create_task(self._check_index(), task_id=_INDEX_CHECK)
        elif not self._needs_index():
            self._stop_checks()

    async def start(self) -> None:
        """
        Bring the index up to date, then load the community providers that may load.

        Runs once, after the server's own providers are loaded; never in safe mode.
        """
        async with self._lock:
            await self._store.load()
            if self._needs_index():
                await self._index_client.fetch()
                self._schedule_check()
            providers = await self._gate_installed()
            providers |= await self._dev_providers()
            domains = await self._load_providers(providers)
            if domains or self._needs_index():
                # clients fetched the manifests before these were registered
                self.mass.signal_event(EventType.COMMUNITY_CATALOG_UPDATED)
            await self._load_instances(domains)

    def unavailable_reason(self, domain: str) -> ProviderError | None:
        """
        Return why an installed community provider does not load, None when it does.

        :param domain: The domain of the provider.
        """
        decision = self._decisions.get(domain)
        if decision is None or decision.verdict == Verdict.LOAD:
            return None
        detail = decision.detail or ""
        return ProviderError(
            error_code=ProviderUnavailableError.error_code,
            message=_REASONS[decision.verdict].format(detail),
            translation_key=f"community_provider_{decision.verdict}",
            translation_args=[detail],
        )

    @api_command("community_catalog/providers", required_scope=Scope.SYSTEM_MANAGE)
    async def get_providers(self) -> list[CommunityProvider]:
        """Return the installed community providers and, while switched on, what the catalog offers."""
        self._raise_in_safe_mode()
        installed = self._store.state.installed
        providers = [
            provider
            for domain, record in installed.items()
            if (provider := await self._installed_provider(domain, record)) is not None
        ]
        if not self.config.get_value(CONF_ENABLED) or (index := self._own_index()) is None:
            return providers
        providers.extend(
            provider
            for domain, entry in index.providers.items()
            if domain not in installed
            and (provider := self._available_provider(domain, entry)) is not None
        )
        return providers

    @api_command("community_catalog/refresh", required_scope=Scope.SYSTEM_MANAGE)
    async def refresh(self) -> list[CommunityProvider]:
        """Fetch the index of the community catalog now, and return its providers."""
        self._raise_in_safe_mode()
        if self._needs_index() and await self._fetch_and_apply():
            self.mass.signal_event(EventType.COMMUNITY_CATALOG_UPDATED)
        return await self.get_providers()

    @api_command("community_catalog/install", required_scope=Scope.SYSTEM_MANAGE)
    async def install_provider(self, domain: str) -> CommunityProvider:
        """
        Install the latest version of a provider the community catalog offers.

        Its enabled settings entries load; no new one is created, the provider is added the
        usual way. A refused or failed install leaves the installed providers as they were.

        :param domain: The domain of the provider.
        :raises ActionUnavailable: The provider cannot be installed; the message tells why.
        :raises InstallError: The provider could not be downloaded or is not what the catalog lists.
        :raises DependencyConflict: Its requirements clash with those of the server or of
            another community provider.
        :raises DependenciesUnavailable: Its requirements could not be installed.
        """
        self._raise_in_safe_mode()
        async with self._lock:
            entry = self._installable(domain)
            # an offered entry always has a latest version
            version = str(entry.latest)
            manifest = await self._installer.stage(domain, entry, version)
            try:
                await self._activate_staged(manifest)
            except Exception:
                with suppress(InstallError):
                    await self._installer.discard(domain)
                raise
            release = entry.versions[version]
            installed = InstalledProvider(
                version=version,
                repository=entry.repository,
                commit=release.commit,
                content_hash=release.content_hash,
            )
            state = self._store.state
            try:
                await self._store.save_state(
                    replace(state, installed={**state.installed, domain: installed})
                )
            except OSError as err:
                with suppress(InstallError):
                    await self._installer.remove(domain)
                msg = f"Could not install {domain}: {err}"
                raise InstallError(msg) from err
            self._folder_hashes[domain] = release.content_hash
            self._decisions[domain] = Decision(Verdict.LOAD)
            self._dependencies.activate()
            await self._register(self._installer.live_folder(domain))
            self.logger.info("Installed community provider %s %s", domain, version)
            self.mass.signal_event(EventType.COMMUNITY_CATALOG_UPDATED)
            await self._load_instances({domain})
            provider = await self._installed_provider(domain, installed)
        # the manifest of the live folder was just registered
        assert provider is not None
        return provider

    @api_command("community_catalog/remove", required_scope=Scope.SYSTEM_MANAGE)
    async def remove_provider(self, domain: str) -> None:
        """
        Remove an installed community provider.

        Its settings entries stay, and are listed as entries without a provider.

        :param domain: The domain of the provider.
        :raises ActionUnavailable: The provider is not installed.
        :raises InstallError: Its files could not be removed; it no longer loads all the same.
        """
        self._raise_in_safe_mode()
        async with self._lock:
            state = self._store.state
            if domain not in state.installed:
                msg = f"{domain} is not installed from the community catalog"
                raise ActionUnavailable(msg)
            # dropped from the state first: files that cannot be removed then never load
            await self._store.save_state(
                replace(state, installed={d: i for d, i in state.installed.items() if d != domain})
            )
            self._decisions.pop(domain, None)
            self._folder_hashes.pop(domain, None)
            if not self._needs_index():
                self._stop_checks()
            try:
                await self._stop(domain)
                await self._installer.remove(domain)
            finally:
                self.mass.signal_event(EventType.COMMUNITY_CATALOG_UPDATED)
            self.logger.info("Removed community provider %s", domain)

    def _needs_index(self) -> bool:
        """Return whether the server keeps the index of the community catalog current."""
        # what is installed must still be stopped when the catalog blocks it
        return bool(self.config.get_value(CONF_ENABLED) or self._store.state.installed)

    def _own_index(self) -> CatalogIndex | None:
        """Return the kept copy of the index when it is the one of this server's line."""
        index = self._store.index
        if index is None or index.server_line != server_line(self.mass.version):
            return None
        return index

    async def _check_index(self) -> None:
        """Fetch the index and apply it, then schedule the next daily check."""
        try:
            if await self._fetch_and_apply():
                self.mass.signal_event(EventType.COMMUNITY_CATALOG_UPDATED)
        finally:
            self._schedule_check()

    async def _fetch_and_apply(self) -> bool:
        """Fetch the index, apply it when it changed and return whether it did."""
        async with self._lock:
            if changed := await self._index_client.fetch():
                await self._apply_index()
        return changed

    def _schedule_check(self) -> None:
        """Schedule the next daily check of the index, while the index is needed."""
        if self._needs_index():
            delay = _CHECK_INTERVAL + random.uniform(0, _CHECK_SPREAD)  # noqa: S311 - spread
            self.mass.call_later(delay, self._check_index, task_id=_INDEX_CHECK)

    def _stop_checks(self) -> None:
        """Cancel the daily check of the index."""
        self.mass.cancel_timer(_INDEX_CHECK)
        self.mass.cancel_task(_INDEX_CHECK)

    def _evaluate(
        self, domain: str, installed: InstalledProvider, folder_hash: str | None
    ) -> Decision:
        """
        Return whether an installed provider may load by the kept copy of the index.

        :param domain: The domain of the provider.
        :param installed: What was installed.
        :param folder_hash: The content hash of its live folder.
        """
        index = self._store.index
        return evaluate(
            installed,
            folder_hash,
            index.providers.get(domain) if index is not None else None,
            has_index=index is not None,
            own_line=self._own_index() is not None,
        )

    async def _gate_installed(self) -> dict[str, ProviderManifest]:
        """
        Decide for every installed provider whether it may load, installing it again first where needed.

        Returns the manifest of each provider that may load, by provider folder.
        """
        taken = {manifest.domain for manifest in self.mass.get_provider_manifests()}
        providers: dict[str, ProviderManifest] = {}
        for domain, installed in self._store.state.installed.items():
            folder = self._installer.live_folder(domain)
            folder_hash = await asyncio.to_thread(_folder_hash, folder)
            decision = self._evaluate(domain, installed, folder_hash)
            if decision.verdict == Verdict.REINSTALL:
                decision, folder_hash = await self._reinstall(domain, installed, folder_hash)
            if decision.verdict == Verdict.LOAD and domain in taken:
                # the server has a provider of its own by this name
                decision = Decision(Verdict.NOT_FOR_THIS_VERSION)
            self._decisions[domain] = decision
            self._folder_hashes[domain] = folder_hash
            if decision.verdict == Verdict.LOAD:
                providers[str(folder)] = await ProviderManifest.parse(str(folder / "manifest.json"))
            else:
                self.logger.warning(
                    "Not loading community provider %s: %s",
                    domain,
                    _REASONS[decision.verdict].format(decision.detail or ""),
                )
        return providers

    async def _reinstall(
        self, domain: str, installed: InstalledProvider, folder_hash: str | None
    ) -> tuple[Decision, str | None]:
        """
        Install the version that is installed again, and return the decision and folder hash after.

        :param domain: The domain of the provider.
        :param installed: What was installed.
        :param folder_hash: The content hash of the live folder before.
        """
        # evaluate only asks for this with an entry in the index of this server's line
        assert self._store.index is not None
        entry = self._store.index.providers[domain]
        self.logger.info("Installing community provider %s again: its files changed", domain)
        try:
            await self._installer.stage(domain, entry, installed.version)
            await self._installer.activate(domain)
        except InstallError as err:
            return Decision(Verdict.REINSTALL_FAILED, str(err)), folder_hash
        return Decision(Verdict.LOAD), installed.content_hash

    async def _activate_staged(self, manifest: ProviderManifest) -> None:
        """
        Install the requirements of a staged provider, then make it live.

        :param manifest: The manifest of the staged provider.
        :raises ActionUnavailable: Its requirements change a package that is imported.
        """
        requirements = {
            loaded.domain: loaded.requirements
            for loaded in self.mass.get_provider_manifests()
            if loaded.tier == ProviderTier.COMMUNITY
        }
        # the last one is refused on a clash
        requirements[manifest.domain] = manifest.requirements
        resolved = await self._dependencies.resolve(requirements)
        if await self._dependencies.touches_imported(resolved.pins):
            msg = (
                f"{manifest.domain} needs other versions of packages that running community "
                "providers use, which cannot be changed while they run"
            )
            raise ActionUnavailable(msg)
        await self._dependencies.sync(resolved)
        await self._installer.activate(manifest.domain)

    async def _apply_index(self) -> None:
        """Stop each loaded provider the index no longer lets load, and keep the reasons current."""
        for domain, installed in self._store.state.installed.items():
            decision = self._evaluate(domain, installed, self._folder_hashes.get(domain))
            if decision.verdict in (Verdict.LOAD, Verdict.REINSTALL):
                # one that does not load keeps its reason until the next start
                continue
            current = self._decisions.get(domain)
            if current is not None and current.verdict == Verdict.LOAD:
                self.logger.warning(
                    "Stopping community provider %s: %s",
                    domain,
                    _REASONS[decision.verdict].format(decision.detail or ""),
                )
                await self._stop(domain)
            self._decisions[domain] = decision

    async def _stop(self, domain: str) -> None:
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

    def _installable(self, domain: str) -> IndexProvider:
        """
        Return the index entry of a provider that may be installed now.

        :param domain: The domain of the provider.
        :raises ActionUnavailable: The provider may not be installed; the message tells why.
        """
        if not self.config.get_value(CONF_ENABLED):
            msg = "Switch the community catalog on to install from it"
            raise ActionUnavailable(msg)
        index = self._own_index()
        entry = index.providers.get(domain) if index is not None else None
        if entry is None or self._available_provider(domain, entry) is None:
            msg = f"The community catalog does not offer {domain} for this server version"
            raise ActionUnavailable(msg)
        if domain in self._store.state.installed:
            msg = f"{domain} is installed already"
            raise ActionUnavailable(msg)
        if any(manifest.domain == domain for manifest in self.mass.get_provider_manifests()):
            msg = f"The server already has a provider named {domain}"
            raise ActionUnavailable(msg)
        return entry

    async def _installed_provider(
        self, domain: str, installed: InstalledProvider
    ) -> CommunityProvider | None:
        """
        Return an installed provider as the app shows it, None when it has no manifest to show.

        :param domain: The domain of the provider.
        :param installed: What was installed.
        """
        index = self._store.index
        entry = index.providers.get(domain) if index is not None else None
        manifest = await self._installed_manifest(domain, entry)
        if manifest is None:
            return None
        own_index = self._own_index()
        own_entry = own_index.providers.get(domain) if own_index is not None else None
        latest = own_entry.latest if own_entry is not None else None
        release = own_entry.versions.get(latest) if own_entry and latest else None
        decision = self._decisions.get(domain)
        state = CommunityProviderState.INSTALLED
        reason = reason_url = None
        if decision is not None and decision.verdict == Verdict.BLOCKED:
            state, reason, reason_url = (
                CommunityProviderState.BLOCKED,
                decision.detail,
                decision.url,
            )
        elif decision is not None and decision.verdict != Verdict.LOAD:
            state = CommunityProviderState.UNAVAILABLE
        elif entry is not None and entry.status == IndexStatus.WITHDRAWN:
            state, reason, reason_url = (
                CommunityProviderState.WITHDRAWN,
                entry.reason,
                entry.reason_url,
            )
        return CommunityProvider(
            domain=domain,
            repository=installed.repository,
            maintainers=entry.maintainers if entry is not None else [],
            icon=entry.icon if entry is not None else None,
            manifest=manifest,
            state=state,
            installed_version=installed.version,
            latest_version=latest,
            added=entry.added if entry is not None else None,
            released=release.released if release is not None else None,
            reason=reason,
            reason_url=reason_url,
            error=self.unavailable_reason(domain),
        )

    async def _installed_manifest(
        self, domain: str, entry: IndexProvider | None
    ) -> ProviderManifest | None:
        """
        Return the manifest of an installed provider: of its live folder, else of the index.

        :param domain: The domain of the provider.
        :param entry: The provider in the kept copy of the index.
        """
        for manifest in self.mass.get_provider_manifests():
            if manifest.domain == domain and manifest.tier == ProviderTier.COMMUNITY:
                return manifest
        folder = self._installer.live_folder(domain)
        try:
            manifest = await ProviderManifest.parse(str(folder / "manifest.json"))
            set_provider_tier(manifest, ProviderTier.COMMUNITY)
        except (OSError, ValueError, LookupError) as err:
            # the live folder is gone or broken when installing it again failed
            if entry is not None and (available := self._available_provider(domain, entry)):
                return available.manifest
            self.logger.warning("Not listing community provider %s: %s", domain, err)
            return None
        return manifest

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

    async def _dev_providers(self) -> dict[str, ProviderManifest]:
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

    async def _load_providers(self, providers: dict[str, ProviderManifest]) -> set[str]:
        """
        Install the requirements of community providers, then register them.

        A provider whose requirements cannot be installed is left out. Returns the domains
        that were registered.

        :param providers: The manifest of each provider, by provider folder.
        """
        if not providers:
            return set()
        accepted = {manifest.domain: manifest.requirements for manifest in providers.values()}
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
            break
        self._dependencies.activate()
        for provider_path, manifest in providers.items():
            if manifest.domain in accepted:
                await self._register(provider_path)
        return set(accepted)

    async def _register(self, provider_path: str | Path) -> None:
        """
        Register a community provider, so its settings entries can load.

        :param provider_path: The folder of the provider.
        """
        self.mass.add_provider_location(os.path.dirname(provider_path))
        await self.mass.register_provider_manifest(str(provider_path), ProviderTier.COMMUNITY)

    async def _load_instances(self, domains: set[str]) -> None:
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


def _folder_hash(folder: Path) -> str | None:
    """Return the content hash of a provider folder, None when it cannot be read (blocking)."""
    try:
        return content_hash(folder) if folder.is_dir() else None
    except OSError, ValueError:
        return None
