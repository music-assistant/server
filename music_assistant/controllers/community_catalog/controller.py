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
from music_assistant_models.enums import ConfigEntryType, EventType, PlaybackState, ProviderTier
from music_assistant_models.errors import (
    ActionUnavailable,
    MusicAssistantError,
    ProviderUnavailableError,
)
from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import CONF_PROVIDERS
from music_assistant.controllers.community_catalog.constants import (
    COMMUNITY_PROVIDERS_FOLDER,
    CONF_AUTO_UPDATE,
    CONF_ENABLED,
    MOVED_PROVIDER_DOMAINS,
)
from music_assistant.controllers.community_catalog.dependencies import CommunityDependencies
from music_assistant.controllers.community_catalog.gate import _REASONS, Decision, Gate, Verdict
from music_assistant.controllers.community_catalog.index import IndexClient
from music_assistant.controllers.community_catalog.installer import (
    Installer,
    InstallError,
    _folder_hash,
)
from music_assistant.controllers.community_catalog.listing import (
    available_provider,
    installed_manifest,
    installed_provider,
)
from music_assistant.controllers.community_catalog.loader import ProviderLoader
from music_assistant.controllers.community_catalog.models import (
    CommunityProvider,
    IndexProvider,
    InstalledProvider,
)
from music_assistant.controllers.community_catalog.store import CatalogStore
from music_assistant.controllers.community_catalog.versions import VersionChanger
from music_assistant.helpers.api import api_command
from music_assistant.models.core_controller import CoreController

if TYPE_CHECKING:
    from music_assistant_models.config_entries import CoreConfig

    from music_assistant.mass import MusicAssistant

# the timer of the daily check of the index, and the task that runs a check
_INDEX_CHECK: Final = "community_catalog_index_check"
_CHECK_INTERVAL: Final = 24 * 3600
# spreads the checks of all servers over an hour
_CHECK_SPREAD: Final = 3600
# the timer that tries the automatic updates again that waited for playback to stop
_UPDATE_RETRY: Final = "community_catalog_update_retry"
_UPDATE_RETRY_INTERVAL: Final = 3600


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
        self._gate = Gate(mass, self._store)
        # held by everything that changes what is installed or loaded
        self._lock = asyncio.Lock()
        # by installed domain: whether it loads, and the content hash of its live folder
        self._decisions: dict[str, Decision] = {}
        self._folder_hashes: dict[str, str | None] = {}
        # by moved domain that waits: why the last try to install it failed
        self._move_errors: dict[str, str] = {}
        self._loader = ProviderLoader(
            mass, self.logger, self._store, self._dependencies, self._decisions
        )
        self._versions = VersionChanger(
            mass,
            self.logger,
            self._store,
            self._installer,
            self._dependencies,
            self._gate,
            self._loader,
            self._decisions,
            self._folder_hashes,
        )
        # whether the start decided what loads and registered it
        self._started = False

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

    async def setup(self, config: CoreConfig) -> None:
        """Read what is installed; the settings entries of it tell why it does not run yet."""
        await self._store.load()

    async def update_config(self, config: CoreConfig, changed_keys: set[str]) -> None:
        """
        Handle logic when the config is updated.

        :raises ActionUnavailable: The community catalog is switched off while something the
            admin installed from it is still installed.
        """
        if (
            f"values/{CONF_ENABLED}" in changed_keys
            and not config.get_value(CONF_ENABLED)
            and self._installed_by_admin()
        ):
            # the config controller puts the saved switch back on
            msg = "Remove what you installed from the community catalog before you switch it off"
            raise ActionUnavailable(msg)
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
            if self._needs_index():
                await self._index_client.fetch()
                self._schedule_check()
            # installed here, they load with the other installed providers
            await self._install_moved(load=False)
            await self._versions.activate_pending()
            providers = await self._gate_installed()
            await self._versions.update_at_start(providers, auto_update=self._auto_update())
            providers |= await self._loader.dev_providers()
            domains, requirements_installed = await self._loader.load_providers(providers)
            self._started = True
            if domains or self._needs_index():
                # clients fetched the manifests before these were registered
                self.mass.signal_event(EventType.COMMUNITY_CATALOG_UPDATED)
            await self._loader.load_instances(domains)
            for domain in domains:
                await self._versions.judge(domain, requirements_installed=requirements_installed)

    def unavailable_reason(self, domain: str) -> ProviderError | None:
        """
        Return why a community provider does not load, None when it loads or is not one.

        :param domain: The domain of the provider.
        """
        decision = self._decisions.get(domain)
        if not self._started and domain in self._store.state.installed:
            # what the start decides is only final, and registered, once it is done
            decision = Decision(Verdict.SAFE_MODE if self.mass.safe_mode else Verdict.STARTING)
        elif decision is None and domain in self._waiting_moves():
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
        if not self.config.get_value(CONF_ENABLED) or (index := self._gate.own_index()) is None:
            return providers
        providers.extend(
            provider
            for domain, entry in index.providers.items()
            if domain not in installed
            and (provider := available_provider(domain, entry, self.logger)) is not None
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
                await self._loader.stop(domain)
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
            await self._store.save_installed(domain, replace(installed, notice=None))
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
            targets = self._versions.targets(include_updates=True, retry_failed=domain is not None)
            if domain is not None:
                if domain not in self._store.state.installed:
                    msg = f"{domain} is not installed from the community catalog"
                    raise ActionUnavailable(msg)
                if domain not in targets:
                    msg = f"There is no update of {domain} to install"
                    raise ActionUnavailable(msg)
                await self._versions.update(domain, targets[domain])
            else:
                for target, version in targets.items():
                    try:
                        await self._versions.update(target, version)
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

    def _installed_by_admin(self) -> set[str]:
        """Return the installed providers that the server did not install by itself after a move."""
        state = self._store.state
        return set(state.installed) - set(state.moved)

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
            decision = self._gate.evaluate(domain, installed, folder_hash)
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
        return (
            self._gate.evaluate(domain, installed, installed.content_hash),
            installed.content_hash,
        )

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
        await self._loader.register(self._installer.live_folder(domain))
        self.mass.signal_event(EventType.COMMUNITY_CATALOG_UPDATED)
        await self._loader.load_instances({domain})
        await self._versions.judge(domain)

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
            decision = self._gate.evaluate(domain, installed, self._folder_hashes.get(domain))
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
                await self._loader.stop(domain)
            self._decisions[domain] = decision

    async def _apply_updates(self) -> None:
        """
        Carry out the switches and, with automatic updates on, the updates on offer.

        Waits while a player is playing: the remaining changes are tried again later.
        """
        self.mass.cancel_timer(_UPDATE_RETRY)
        async with self._lock:
            for domain, version in self._versions.targets(
                include_updates=self._auto_update()
            ).items():
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
                    await self._versions.update(domain, version)
                except (MusicAssistantError, OSError) as err:
                    self.logger.warning(
                        "Could not update community provider %s to %s: %s", domain, version, err
                    )

    def _auto_update(self) -> bool:
        """Return whether the updates on offer are installed without the admin asking."""
        return bool(self.config.get_value(CONF_ENABLED) and self.config.get_value(CONF_AUTO_UPDATE))

    def _is_playing(self) -> bool:
        """Return whether any player is playing."""
        return any(
            player.state.playback_state == PlaybackState.PLAYING
            for player in self.mass.players.iter_players()
        )

    def _installable(self, domain: str) -> IndexProvider:
        """
        Return the index entry of a provider that may be installed now.

        :param domain: The domain of the provider.
        :raises ActionUnavailable: The provider may not be installed; the message tells why.
        """
        index = self._gate.own_index()
        entry = index.providers.get(domain) if index is not None else None
        if entry is None or available_provider(domain, entry, self.logger) is None:
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
        manifest = await installed_manifest(self.mass, self._installer, domain, entry, self.logger)
        if manifest is None:
            return None
        return installed_provider(
            domain,
            installed,
            manifest,
            entry,
            own_entry=self._gate.own_entry(domain),
            decision=self._decisions.get(domain),
            update=self._gate.evaluate(domain, installed, self._folder_hashes.get(domain)).version,
            error=self.unavailable_reason(domain),
        )

    def _raise_in_safe_mode(self) -> None:
        """Raise when the server runs in safe mode, which loads no community provider."""
        if self.mass.safe_mode:
            msg = "The community catalog is not available in safe mode"
            raise ActionUnavailable(msg)
