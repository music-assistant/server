"""The changes of version of installed community providers: updates, switches and going back."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from music_assistant_models.enums import EventType, ProviderTier
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import UNHANDLED_PROVIDER_ERROR_CODE
from music_assistant.controllers.community_catalog.dependencies import (
    DependenciesUnavailable,
    DependencyConflict,
)
from music_assistant.controllers.community_catalog.gate import _REASONS, Decision, Verdict
from music_assistant.controllers.community_catalog.installer import InstallError, _folder_hash
from music_assistant.helpers.util import load_provider_module

if TYPE_CHECKING:
    import logging

    from music_assistant.controllers.community_catalog.dependencies import (
        CommunityDependencies,
        ResolvedDependencies,
    )
    from music_assistant.controllers.community_catalog.gate import Gate
    from music_assistant.controllers.community_catalog.installer import Installer
    from music_assistant.controllers.community_catalog.loader import ProviderLoader
    from music_assistant.controllers.community_catalog.models import IndexProvider
    from music_assistant.controllers.community_catalog.store import CatalogStore
    from music_assistant.mass import MusicAssistant


class VersionChanger:
    """Changes the version of installed community providers, and goes back from one that fails."""

    def __init__(
        self,
        mass: MusicAssistant,
        logger: logging.Logger,
        store: CatalogStore,
        installer: Installer,
        dependencies: CommunityDependencies,
        gate: Gate,
        loader: ProviderLoader,
        decisions: dict[str, Decision],
        folder_hashes: dict[str, str | None],
    ) -> None:
        """
        Initialize the version changer; its callers hold the lock of the community catalog.

        :param mass: The Music Assistant instance.
        :param logger: The logger of the community catalog.
        :param store: The store that holds what is installed.
        :param installer: The installer of the provider folders.
        :param dependencies: The requirements of the community providers.
        :param gate: The gate that reads the kept copy of the index.
        :param loader: The loader that registers, loads and stops community providers.
        :param decisions: Whether each installed provider loads, by domain; kept current.
        :param folder_hashes: The content hash of the live folder of each installed provider, by
            domain; kept current.
        """
        self.mass = mass
        self.logger = logger
        self._store = store
        self._installer = installer
        self._dependencies = dependencies
        self._gate = gate
        self._loader = loader
        self._decisions = decisions
        self._folder_hashes = folder_hashes

    async def activate_pending(self) -> None:
        """Make each pending version live whose staged folder is still what the index lists."""
        for domain, installed in list(self._store.state.installed.items()):
            if (version := installed.pending_version) is None:
                continue
            entry = self._gate.own_entry(domain)
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

    async def update_at_start(
        self, providers: dict[str, ProviderManifest], *, auto_update: bool
    ) -> None:
        """
        Change the version of the installed providers that are to change, before anything loads.

        A version that is no longer listed is switched to the latest one; with automatic updates
        on, an update on offer is installed too. A provider whose switch fails does not load.

        :param providers: The manifest of each installed provider that may load, by provider
            folder; updated in place.
        :param auto_update: Whether automatic updates are on.
        """
        for domain, version in self.targets(include_updates=auto_update).items():
            # targets only come from the index of this server's line
            entry = self._gate.own_entry(domain)
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

    async def update(self, domain: str, version: str) -> None:
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
        entry = self._gate.own_entry(domain)
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
            await self._loader.stop(name)
        try:
            await self._activate_version(domain, entry, version)
            restart[domain] = self._installer.live_folder(domain)
        finally:
            self._dependencies.activate()
            for folder in restart.values():
                await self._loader.register(folder)
            self.mass.signal_event(EventType.COMMUNITY_CATALOG_UPDATED)
            if domain in restart:
                await self._loader.load_instances({domain})
        await self.judge(domain)

    async def judge(self, domain: str, *, requirements_installed: bool = True) -> None:
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
            await self._store.save_installed(domain, replace(installed, verified=True))
        elif failed and installed.previous_version is not None:
            await self._go_back(domain, mark_failed=requirements_installed)

    def targets(self, *, include_updates: bool, retry_failed: bool = False) -> dict[str, str]:
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
            decision = self._gate.evaluate(domain, judged, self._folder_hashes.get(domain))
            if (
                decision.version is None
                or decision.version == installed.pending_version
                or domain in builtin
                or (decision.verdict == Verdict.LOAD and not include_updates)
            ):
                continue
            targets[domain] = decision.version
        return targets

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
                await self._store.save_installed(
                    domain, replace(installed, pending_version=version)
                )
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
        await self._store.save_installed(
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
            await self._loader.stop(name)
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
            await self._loader.register(folder)
        else:
            self.logger.warning(
                "Not loading community provider %s: %s",
                domain,
                _REASONS[decision.verdict].format(decision.detail or ""),
            )
        for dependent in dependents:
            await self._loader.register(dependent)
        self.mass.signal_event(EventType.COMMUNITY_CATALOG_UPDATED)
        if decision.verdict == Verdict.LOAD:
            await self._loader.load_instances({domain})

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
        entry = self._gate.own_entry(domain)
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
        await self._store.save_installed(domain, restored)
        self._folder_hashes[domain] = folder_hash
        decision = self._gate.evaluate(domain, restored, folder_hash)
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
            await self._store.save_installed(domain, replace(installed, pending_version=None))

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
