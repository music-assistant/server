"""The community catalog: providers that are maintained outside the server."""

from __future__ import annotations

import asyncio
import os
import random
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Final

from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import ConfigEntry, ProviderError
from music_assistant_models.enums import ConfigEntryType, EventType, PlaybackState, ProviderTier
from music_assistant_models.errors import (
    ActionUnavailable,
    MusicAssistantError,
    ProviderUnavailableError,
)
from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import (
    CONF_PROVIDERS,
    PROVIDER_LOAD_CONCURRENCY,
    UNHANDLED_PROVIDER_ERROR_CODE,
)
from music_assistant.controllers.community_catalog.constants import (
    COMMUNITY_PROVIDERS_FOLDER,
    CONF_AUTO_UPDATE,
    CONF_ENABLED,
    MOVED_PROVIDER_DOMAINS,
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
from music_assistant.helpers.util import (
    TaskManager,
    load_provider_module,
    purge_provider_modules,
    set_provider_tier,
)
from music_assistant.models.core_controller import CoreController

if TYPE_CHECKING:
    from music_assistant_models.config_entries import CoreConfig

    from music_assistant.controllers.community_catalog.dependencies import ResolvedDependencies
    from music_assistant.controllers.community_catalog.models import CatalogIndex
    from music_assistant.mass import MusicAssistant

# the timer of the daily check of the index, and the task that runs a check
_INDEX_CHECK: Final = "community_catalog_index_check"
_CHECK_INTERVAL: Final = 24 * 3600
# spreads the checks of all servers over an hour
_CHECK_SPREAD: Final = 3600
# the timer that tries the automatic updates again that waited for playback to stop
_UPDATE_RETRY: Final = "community_catalog_update_retry"
_UPDATE_RETRY_INTERVAL: Final = 3600
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
    Verdict.WENT_BACK: (
        "Version {0} of this failed to load, and the version before it is no longer "
        "available, so it does not run."
    ),
    Verdict.RESTART_TO_GO_BACK: (
        "Version {0} of this failed to load. Restart Music Assistant to finish going back "
        "to the version before it."
    ),
    Verdict.RESTART_TO_UPDATE: (
        "Version {0} of this is ready. Restart Music Assistant to finish the update."
    ),
    Verdict.MOVED: (
        "This moved to the community catalog. Music Assistant installs it from there by itself "
        "as soon as the catalog offers it for this version and your server can reach it."
    ),
    Verdict.MOVE_FAILED: (
        "This moved to the community catalog, but Music Assistant could not install it from "
        "there ({0}). It tries again at every start and whenever it checks the catalog."
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
        # by moved domain that waits: why the last try to install it failed
        self._move_errors: dict[str, str] = {}

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
            ConfigEntry(
                key=CONF_AUTO_UPDATE,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                required=False,
                hidden=True,
                depends_on=CONF_ENABLED,
            ),
        )

    async def update_config(self, config: CoreConfig, changed_keys: set[str]) -> None:
        """Handle logic when the config is updated."""
        await super().update_config(config, changed_keys)
        if self.mass.safe_mode:
            return
        if f"values/{CONF_ENABLED}" in changed_keys:
            # what the list offers depends on the switch
            self.mass.signal_event(EventType.COMMUNITY_CATALOG_UPDATED)
            if self.config.get_value(CONF_ENABLED):
                # a check that already runs is left to finish: it may be stopping a provider;
                # the check installs the updates on offer after it
                self.mass.create_task(self._check_index(), task_id=_INDEX_CHECK)
            elif not self._needs_index():
                self._stop_checks()
        elif f"values/{CONF_AUTO_UPDATE}" in changed_keys and self._auto_update():
            self.mass.create_task(self._apply_updates(), task_id=_UPDATE_RETRY)

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
            # installed here, they load with the other installed providers
            await self._install_moved(load=False)
            await self._activate_pending()
            providers = await self._gate_installed()
            await self._update_at_start(providers)
            providers |= await self._dev_providers()
            domains, requirements_installed = await self._load_providers(providers)
            if domains or self._needs_index():
                # clients fetched the manifests before these were registered
                self.mass.signal_event(EventType.COMMUNITY_CATALOG_UPDATED)
            await self._load_instances(domains)
            for domain in domains:
                await self._judge(domain, requirements_installed=requirements_installed)

    def unavailable_reason(self, domain: str) -> ProviderError | None:
        """
        Return why a community provider does not load, None when it loads or is not one.

        :param domain: The domain of the provider.
        """
        decision = self._decisions.get(domain)
        if decision is None and domain in self._waiting_moves():
            error = self._move_errors.get(domain)
            decision = Decision(Verdict.MOVE_FAILED, error) if error else Decision(Verdict.MOVED)
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
            if not self.config.get_value(CONF_ENABLED):
                msg = "Switch the community catalog on to install from it"
                raise ActionUnavailable(msg)
            installed = await self._install(domain)
            await self._load_installed(domain)
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

    @api_command("community_catalog/dismiss_notice", required_scope=Scope.SYSTEM_MANAGE)
    async def dismiss_notice(self, domain: str) -> None:
        """
        Dismiss the notice of an installed community provider; nothing happens when it has none.

        :param domain: The domain of the provider.
        """
        self._raise_in_safe_mode()
        async with self._lock:
            installed = self._store.state.installed.get(domain)
            if installed is None or installed.notice is None:
                return
            await self._save_installed(domain, replace(installed, notice=None))
        self.mass.signal_event(EventType.COMMUNITY_CATALOG_UPDATED)

    @api_command("community_catalog/update", required_scope=Scope.SYSTEM_MANAGE)
    async def update_providers(self, domain: str | None = None) -> list[CommunityProvider]:
        """
        Update an installed community provider now, or every one that has an update on offer.

        An update that changes a Python package in use is finished by the next start of the
        server; until then the old version keeps running and the list says a restart is needed.
        A version this server went back from (`failed_version` in the list) is only installed
        again when asked for by domain. Returns the providers as `community_catalog/providers`
        does.

        :param domain: The domain of the provider, None for every provider with an update on
            offer; one that cannot be updated is then skipped.
        :raises ActionUnavailable: The provider is not installed or has no update on offer.
        :raises InstallError: The update could not be downloaded or is not what the catalog lists.
        :raises DependencyConflict: Its requirements clash with those of the server or of
            another community provider.
        :raises DependenciesUnavailable: Its requirements could not be installed.
        """
        self._raise_in_safe_mode()
        async with self._lock:
            targets = self._targets(include_updates=True, retry_failed=domain is not None)
            if domain is not None:
                if domain not in self._store.state.installed:
                    msg = f"{domain} is not installed from the community catalog"
                    raise ActionUnavailable(msg)
                if domain not in targets:
                    msg = f"There is no update of {domain} to install"
                    raise ActionUnavailable(msg)
                await self._update(domain, targets[domain])
            else:
                for target, version in targets.items():
                    try:
                        await self._update(target, version)
                    except (MusicAssistantError, OSError) as err:
                        self.logger.warning(
                            "Could not update community provider %s to %s: %s", target, version, err
                        )
        return await self.get_providers()

    def _needs_index(self) -> bool:
        """Return whether the server keeps the index of the community catalog current."""
        # what is installed must still be stopped when the catalog blocks it, and a moved
        # provider that waits is installed from it
        return bool(
            self.config.get_value(CONF_ENABLED)
            or self._store.state.installed
            or self._waiting_moves()
        )

    def _waiting_moves(self) -> set[str]:
        """Return the moved providers that the server is to install from the catalog by itself."""
        state = self._store.state
        configured = {conf["domain"] for conf in self.mass.config.get(CONF_PROVIDERS, {}).values()}
        taken = {manifest.domain for manifest in self.mass.get_provider_manifests()}
        return {
            domain
            for domain in MOVED_PROVIDER_DOMAINS & configured
            if domain not in state.installed and domain not in state.moved and domain not in taken
        }

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
        """Fetch and apply the index, carry out what is to change, and return whether it changed."""
        async with self._lock:
            if changed := await self._index_client.fetch():
                await self._apply_index()
            await self._install_moved(load=True)
        await self._apply_updates()
        return changed

    def _schedule_check(self) -> None:
        """Schedule the next daily check of the index, while the index is needed."""
        if self._needs_index():
            delay = _CHECK_INTERVAL + random.uniform(0, _CHECK_SPREAD)  # noqa: S311 - spread
            self.mass.call_later(delay, self._check_index, task_id=_INDEX_CHECK)

    def _stop_checks(self) -> None:
        """Cancel the daily check of the index and a retry of the automatic updates."""
        self.mass.cancel_timer(_INDEX_CHECK)
        self.mass.cancel_task(_INDEX_CHECK)
        self.mass.cancel_timer(_UPDATE_RETRY)

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
            if decision.verdict in (Verdict.LOAD, Verdict.SWITCH) and domain in taken:
                # the server has a provider of its own by this name
                decision = Decision(Verdict.NOT_FOR_THIS_VERSION)
            self._decisions[domain] = decision
            self._folder_hashes[domain] = folder_hash
            if decision.verdict == Verdict.LOAD:
                providers[str(folder)] = await ProviderManifest.parse(str(folder / "manifest.json"))
            elif decision.verdict != Verdict.SWITCH:
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
            await self._installer.activate(domain, reinstall=True)
        except InstallError as err:
            return Decision(Verdict.REINSTALL_FAILED, str(err)), folder_hash
        return self._evaluate(domain, installed, installed.content_hash), installed.content_hash

    async def _install(self, domain: str, *, moved: bool = False) -> InstalledProvider:
        """
        Install the latest version of a provider the community catalog offers, without loading it.

        Returns what was installed. A refused or failed install leaves the installed providers
        as they were.

        :param domain: The domain of the provider.
        :param moved: Whether the server installs a moved provider by itself; it then gets the
            notice and is never installed by itself again.
        :raises ActionUnavailable: The provider cannot be installed; the message tells why.
        :raises InstallError: The provider could not be downloaded or is not what the catalog lists.
        :raises DependencyConflict: Its requirements clash with those of the server or of
            another community provider.
        :raises DependenciesUnavailable: Its requirements could not be installed.
        """
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
            notice="moved" if moved else None,
        )
        state = self._store.state
        try:
            await self._store.save_state(
                replace(
                    state,
                    installed={**state.installed, domain: installed},
                    moved=[*state.moved, domain] if moved else state.moved,
                )
            )
        except OSError as err:
            with suppress(InstallError):
                await self._installer.remove(domain)
            msg = f"Could not install {domain}: {err}"
            raise InstallError(msg) from err
        self._folder_hashes[domain] = release.content_hash
        self._decisions[domain] = Decision(Verdict.LOAD)
        self.logger.info("Installed community provider %s %s", domain, version)
        return installed

    async def _install_moved(self, *, load: bool) -> None:
        """
        Install each moved provider that waits; one that cannot be installed now keeps waiting.

        :param load: Whether to load what was installed; False at start, where it loads with
            the other installed providers.
        """
        for domain in self._waiting_moves():
            try:
                await self._install(domain, moved=True)
            except (MusicAssistantError, OSError) as err:
                self._move_errors[domain] = str(err)
                self.logger.warning(
                    "Could not install %s from the community catalog, where it moved: %s",
                    domain,
                    err,
                )
                continue
            if load:
                await self._load_installed(domain)

    async def _load_installed(self, domain: str) -> None:
        """
        Register a provider that was just installed, and load its enabled settings entries.

        :param domain: The domain of the provider.
        """
        self._dependencies.activate()
        await self._register(self._installer.live_folder(domain))
        self.mass.signal_event(EventType.COMMUNITY_CATALOG_UPDATED)
        await self._load_instances({domain})
        await self._judge(domain)

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
            if decision.verdict in (Verdict.LOAD, Verdict.REINSTALL, Verdict.SWITCH):
                # one that does not load keeps its reason until the next start or its switch
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

    async def _activate_pending(self) -> None:
        """Make each pending version live whose staged folder is still what the index lists."""
        for domain, installed in list(self._store.state.installed.items()):
            if (version := installed.pending_version) is None:
                continue
            entry = self._own_entry(domain)
            release = entry.versions.get(version) if entry is not None else None
            staged_hash = await asyncio.to_thread(
                _folder_hash, self._installer.staged_folder(domain)
            )
            if (
                entry is None
                or release is None
                or version in entry.blocked_versions
                or staged_hash != release.content_hash
            ):
                self.logger.warning(
                    "Dropping version %s of community provider %s: it is not what the "
                    "community catalog lists",
                    version,
                    domain,
                )
                await self._discard_staged(domain)
                continue
            try:
                await self._activate_version(domain, entry, version)
            except InstallError as err:
                self.logger.warning("Could not update community provider %s: %s", domain, err)

    async def _update_at_start(self, providers: dict[str, ProviderManifest]) -> None:
        """
        Change the version of the installed providers that are to change, before anything loads.

        A version that is no longer listed is switched to the latest one; with automatic updates
        on, an update on offer is installed too. A provider whose switch fails does not load.

        :param providers: The manifest of each installed provider that may load, by provider
            folder; updated in place.
        """
        for domain, version in self._targets(include_updates=self._auto_update()).items():
            # targets only come from the index of this server's line
            entry = self._own_entry(domain)
            assert entry is not None
            requirements = {
                manifest.domain: manifest.requirements for manifest in providers.values()
            }
            try:
                if staged := await self._stage_version(domain, entry, version, requirements):
                    await self._activate_version(domain, entry, version)
                    providers[str(self._installer.live_folder(domain))] = staged[0]
            except (MusicAssistantError, OSError) as err:
                self.logger.warning(
                    "Could not update community provider %s to %s: %s", domain, version, err
                )
        for domain, decision in self._decisions.items():
            if decision.verdict == Verdict.SWITCH:
                self._decisions[domain] = Decision(Verdict.NOT_FOR_THIS_VERSION)

    async def _apply_updates(self) -> None:
        """
        Carry out the switches and, with automatic updates on, the updates on offer.

        Waits while a player is playing: the remaining changes are tried again later.
        """
        self.mass.cancel_timer(_UPDATE_RETRY)
        async with self._lock:
            for domain, version in self._targets(include_updates=self._auto_update()).items():
                if self._is_playing():
                    self.logger.debug(
                        "Putting off the update of community provider %s while a player plays",
                        domain,
                    )
                    self.mass.call_later(
                        _UPDATE_RETRY_INTERVAL, self._apply_updates, task_id=_UPDATE_RETRY
                    )
                    return
                try:
                    await self._update(domain, version)
                except (MusicAssistantError, OSError) as err:
                    self.logger.warning(
                        "Could not update community provider %s to %s: %s", domain, version, err
                    )

    async def _update(self, domain: str, version: str) -> None:
        """
        Change the version of an installed provider in the running server.

        The provider and the community providers that depend on it are stopped and loaded
        again; other providers keep running. A version whose requirements change a package in
        use stays staged for the next start, and the running version keeps running.

        :param domain: The domain of the provider.
        :param version: The version to change to, listed in the index of this server's line.
        :raises InstallError: The version cannot be downloaded or is not what the catalog lists.
        :raises DependencyConflict: Its requirements clash with those of the server or of
            another community provider.
        :raises DependenciesUnavailable: Its requirements could not be installed.
        """
        entry = self._own_entry(domain)
        assert entry is not None
        manifests = self._community_manifests()
        requirements = {name: manifest.requirements for name, manifest in manifests.items()}
        if (staged := await self._stage_version(domain, entry, version, requirements)) is None:
            self.logger.info(
                "Version %s of community provider %s is staged: restart Music Assistant to "
                "finish the update",
                version,
                domain,
            )
            decision = self._decisions.get(domain)
            if decision is None or decision.verdict != Verdict.LOAD:
                self._decisions[domain] = Decision(Verdict.RESTART_TO_UPDATE, version)
            self.mass.signal_event(EventType.COMMUNITY_CATALOG_UPDATED)
            return
        try:
            await self._dependencies.sync(staged[1])
        except Exception:
            await self._discard_staged(domain)
            raise
        # what ran before runs again, also when the new version could not be made live; the
        # server loads the dependents again once the provider loaded
        restart = {
            name: self._provider_folder(name)
            for name, manifest in manifests.items()
            if domain in (name, manifest.depends_on)
        }
        for name in {domain, *restart}:
            await self._stop(name)
        try:
            await self._activate_version(domain, entry, version)
            restart[domain] = self._installer.live_folder(domain)
        finally:
            self._dependencies.activate()
            for folder in restart.values():
                await self._register(folder)
            self.mass.signal_event(EventType.COMMUNITY_CATALOG_UPDATED)
            if domain in restart:
                await self._load_instances({domain})
        await self._judge(domain)

    async def _stage_version(
        self,
        domain: str,
        entry: IndexProvider,
        version: str,
        requirements: dict[str, list[str]],
    ) -> tuple[ProviderManifest, ResolvedDependencies] | None:
        """
        Stage a version of an installed provider and resolve its requirements with the others.

        Returns its manifest and the resolved requirements of all, or None when they change a
        package that is imported: the version is then kept as the pending version. On failure
        nothing stays staged, so no version is pending; the live version is never touched.

        :param domain: The domain of the provider.
        :param entry: The provider in the index of this server's line.
        :param version: The version to stage.
        :param requirements: The requirements of the other community providers that load, by
            domain.
        :raises InstallError: The version cannot be downloaded or is not what the catalog lists.
        :raises DependencyConflict: Its requirements clash with those of the server or of
            another community provider.
        :raises DependenciesUnavailable: The requirements could not be resolved.
        """
        others = {name: reqs for name, reqs in requirements.items() if name != domain}
        try:
            # staging replaces a pending version
            manifest = await self._installer.stage(domain, entry, version)
            # the last one is refused on a clash
            resolved = await self._dependencies.resolve({**others, domain: manifest.requirements})
            if await self._dependencies.touches_imported(resolved.pins):
                installed = self._store.state.installed[domain]
                await self._save_installed(domain, replace(installed, pending_version=version))
                return None
        except Exception:
            await self._discard_staged(domain)
            raise
        return manifest, resolved

    async def _activate_version(self, domain: str, entry: IndexProvider, version: str) -> None:
        """
        Make the staged version of an installed provider live, and record it as not verified.

        :param domain: The domain of the provider.
        :param entry: The provider in the index of this server's line.
        :param version: The staged version, listed in the entry.
        :raises InstallError: There is no staged version, or the folders cannot be renamed.
        :raises OSError: The state cannot be written.
        """
        installed = self._store.state.installed[domain]
        release = entry.versions[version]
        await self._installer.activate(domain)
        await self._save_installed(
            domain,
            replace(
                installed,
                version=version,
                repository=entry.repository,
                commit=release.commit,
                content_hash=release.content_hash,
                verified=False,
                previous_version=installed.version,
                pending_version=None,
                # set again when it fails again
                failed_version=None
                if version == installed.failed_version
                else installed.failed_version,
            ),
        )
        self._folder_hashes[domain] = release.content_hash
        self._decisions[domain] = Decision(Verdict.LOAD)
        self.logger.info(
            "Updated community provider %s from %s to %s", domain, installed.version, version
        )

    async def _judge(self, domain: str, *, requirements_installed: bool = True) -> None:
        """
        Judge the load of an installed version that has not loaded on this server before.

        The version is verified once an enabled settings entry of it loaded or, without any,
        once its module imports. When it failed with an error that is not a handled condition
        and a previous version is kept, the server goes back to that version.

        :param domain: The domain of the provider, loaded just before.
        :param requirements_installed: Whether its requirements were installed; when not, a
            version that failed is gone back from but not marked as failed.
        """
        installed = self._store.state.installed.get(domain)
        decision = self._decisions.get(domain)
        if installed is None or installed.verified or decision is None:
            return
        if decision.verdict != Verdict.LOAD:
            return
        configs = [
            conf
            for conf in await self.mass.config.get_provider_configs(provider_domain=domain)
            if conf.enabled
        ]
        if configs:
            verified = any(self.mass.get_provider(conf.instance_id) for conf in configs)
            failed = not verified and any(
                conf.last_error is not None
                and conf.last_error.error_code == UNHANDLED_PROVIDER_ERROR_CODE
                for conf in configs
            )
        else:
            try:
                await load_provider_module(self.mass.get_provider_manifest(domain))
            except Exception as err:
                self.logger.warning(
                    "Community provider %s %s does not import: %s", domain, installed.version, err
                )
                verified, failed = False, True
            else:
                verified, failed = True, False
        if verified:
            await self._save_installed(domain, replace(installed, verified=True))
        elif failed and installed.previous_version is not None:
            await self._go_back(domain, mark_failed=requirements_installed)

    async def _go_back(self, domain: str, *, mark_failed: bool) -> None:
        """
        Put back the previous version of an installed provider whose live version failed to load.

        A version marked as failed is only installed again when the admin asks for it. The
        previous version loads when the index lets it and its requirements can be put back;
        otherwise the provider stays stopped with the reason.

        :param domain: The domain of the provider, which keeps a previous version.
        :param mark_failed: Whether to mark the live version as failed.
        """
        installed = self._store.state.installed[domain]
        failed, previous = installed.version, installed.previous_version
        self.logger.warning(
            "Version %s of community provider %s failed to load, going back to %s",
            failed,
            domain,
            previous,
        )
        manifests = self._community_manifests()
        requirements = {name: m.requirements for name, m in manifests.items() if name != domain}
        dependents = [
            self._provider_folder(name)
            for name, manifest in manifests.items()
            if manifest.depends_on == domain
        ]
        for name in (domain, *(folder.name for folder in dependents)):
            await self._stop(name)
        folder = self._installer.live_folder(domain)
        try:
            await self._installer.restore(domain)
        except InstallError as err:
            self.logger.warning("Could not go back: %s", err)
            decision = Decision(Verdict.WENT_BACK, failed)
        else:
            decision = await self._record_previous(
                domain, failed, requirements, mark_failed=mark_failed
            )
        self._decisions[domain] = decision
        if decision.verdict == Verdict.LOAD:
            await self._register(folder)
        else:
            self.logger.warning(
                "Not loading community provider %s: %s",
                domain,
                _REASONS[decision.verdict].format(decision.detail or ""),
            )
        for dependent in dependents:
            await self._register(dependent)
        self.mass.signal_event(EventType.COMMUNITY_CATALOG_UPDATED)
        if decision.verdict == Verdict.LOAD:
            await self._load_instances({domain})

    async def _record_previous(
        self, domain: str, failed: str, requirements: dict[str, list[str]], *, mark_failed: bool
    ) -> Decision:
        """
        Record the restored previous version of a provider and put its requirements back.

        Returns whether it may load now.

        :param domain: The domain of the provider, whose previous version is live again.
        :param failed: The version that failed to load.
        :param requirements: The requirements of the other community providers that load, by
            domain.
        :param mark_failed: Whether to mark the failed version as failed.
        """
        installed = self._store.state.installed[domain]
        assert installed.previous_version is not None
        folder = self._installer.live_folder(domain)
        folder_hash = await asyncio.to_thread(_folder_hash, folder)
        entry = self._own_entry(domain)
        release = entry.versions.get(installed.previous_version) if entry is not None else None
        restored = replace(
            installed,
            version=installed.previous_version,
            # a version that is no longer listed keeps what its folder holds; it does not load
            commit=release.commit if release is not None else "",
            content_hash=release.content_hash if release is not None else folder_hash or "",
            verified=True,
            previous_version=None,
            pending_version=None,
            failed_version=failed if mark_failed else installed.failed_version,
        )
        await self._save_installed(domain, restored)
        self._folder_hashes[domain] = folder_hash
        decision = self._evaluate(domain, restored, folder_hash)
        if decision.verdict == Verdict.BLOCKED:
            return decision
        if decision.verdict != Verdict.LOAD:
            return Decision(Verdict.WENT_BACK, failed)
        manifest = await ProviderManifest.parse(str(folder / "manifest.json"))
        try:
            resolved = await self._dependencies.resolve(
                {**requirements, domain: manifest.requirements}
            )
            if await self._dependencies.touches_imported(resolved.pins):
                return Decision(Verdict.RESTART_TO_GO_BACK, failed)
            await self._dependencies.sync(resolved)
        except DependencyConflict:
            return Decision(Verdict.REQUIREMENTS_CLASH)
        except DependenciesUnavailable as err:
            # a provider that misses a requirement fails to import, which its instance shows
            self.logger.warning("Could not install the community requirements: %s", err)
        return decision

    async def _save_installed(self, domain: str, installed: InstalledProvider) -> None:
        """
        Write what is installed of a provider to the state.

        :param domain: The domain of the provider.
        :param installed: What is installed now.
        :raises OSError: The state cannot be written; the state in use stays as it was.
        """
        state = self._store.state
        await self._store.save_state(
            replace(state, installed={**state.installed, domain: installed})
        )

    async def _discard_staged(self, domain: str) -> None:
        """
        Remove the staged version of an installed provider, which is then no longer pending.

        :param domain: The domain of the provider.
        :raises OSError: The state cannot be written.
        """
        with suppress(InstallError):
            await self._installer.discard(domain)
        installed = self._store.state.installed[domain]
        if installed.pending_version is not None:
            await self._save_installed(domain, replace(installed, pending_version=None))

    def _targets(self, *, include_updates: bool, retry_failed: bool = False) -> dict[str, str]:
        """
        Return the version each installed provider is to change to, by domain.

        A version that waits for a restart already is left out.

        :param include_updates: Whether to include the updates on offer, besides the switches
            from a version that is no longer listed.
        :param retry_failed: Whether to include a version this server went back from.
        """
        builtin = {
            manifest.domain
            for manifest in self.mass.get_provider_manifests()
            if manifest.tier != ProviderTier.COMMUNITY
        }
        targets: dict[str, str] = {}
        for domain, installed in self._store.state.installed.items():
            judged = replace(installed, failed_version=None) if retry_failed else installed
            decision = self._evaluate(domain, judged, self._folder_hashes.get(domain))
            if (
                decision.version is None
                or decision.version == installed.pending_version
                or domain in builtin
                or (decision.verdict == Verdict.LOAD and not include_updates)
            ):
                continue
            targets[domain] = decision.version
        return targets

    def _auto_update(self) -> bool:
        """Return whether the updates on offer are installed without the admin asking."""
        return bool(self.config.get_value(CONF_ENABLED) and self.config.get_value(CONF_AUTO_UPDATE))

    def _is_playing(self) -> bool:
        """Return whether any player is playing."""
        return any(
            player.state.playback_state == PlaybackState.PLAYING
            for player in self.mass.players.iter_players()
        )

    def _own_entry(self, domain: str) -> IndexProvider | None:
        """
        Return a provider in the index of this server's line, None when it is not in it.

        :param domain: The domain of the provider.
        """
        index = self._own_index()
        return index.providers.get(domain) if index is not None else None

    def _community_manifests(self) -> dict[str, ProviderManifest]:
        """Return the manifests of the registered community providers, by domain."""
        return {
            manifest.domain: manifest
            for manifest in self.mass.get_provider_manifests()
            if manifest.tier == ProviderTier.COMMUNITY
        }

    def _provider_folder(self, domain: str) -> Path:
        """
        Return the folder of a registered community provider: installed, or in the dev folder.

        :param domain: The domain of the provider.
        """
        if domain in self._store.state.installed:
            return self._installer.live_folder(domain)
        # only these two locations hold community providers
        assert self.mass.dev_providers_path is not None
        return Path(self.mass.dev_providers_path, domain)

    def _installable(self, domain: str) -> IndexProvider:
        """
        Return the index entry of a provider that may be installed now.

        :param domain: The domain of the provider.
        :raises ActionUnavailable: The provider may not be installed; the message tells why.
        """
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
        elif installed.pending_version is not None:
            state = CommunityProviderState.RESTART_REQUIRED
        elif self._evaluate(domain, installed, self._folder_hashes.get(domain)).version:
            state = CommunityProviderState.UPDATE_AVAILABLE
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
            pending_version=installed.pending_version,
            failed_version=installed.failed_version,
            added=entry.added if entry is not None else None,
            released=release.released if release is not None else None,
            reason=reason,
            reason_url=reason_url,
            error=self.unavailable_reason(domain),
            notice=installed.notice,
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
        if (latest := entry.offered_version) is None:
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

    async def _load_providers(
        self, providers: dict[str, ProviderManifest]
    ) -> tuple[set[str], bool]:
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
                await self._register(provider_path)
        return set(accepted), synced

    async def _register(self, provider_path: str | Path) -> None:
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
