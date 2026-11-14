"""The community providers as the app lists them."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderTier
from music_assistant_models.provider import ProviderManifest

from music_assistant.controllers.community_catalog.gate import Verdict
from music_assistant.controllers.community_catalog.models import (
    CommunityProvider,
    CommunityProviderState,
    IndexStatus,
)
from music_assistant.helpers.util import set_provider_tier

if TYPE_CHECKING:
    import logging

    from music_assistant_models.config_entries import ProviderError

    from music_assistant.controllers.community_catalog.gate import Decision
    from music_assistant.controllers.community_catalog.installer import Installer
    from music_assistant.controllers.community_catalog.models import (
        IndexProvider,
        InstalledProvider,
    )
    from music_assistant.mass import MusicAssistant


def installed_provider(
    domain: str,
    installed: InstalledProvider,
    manifest: ProviderManifest,
    entry: IndexProvider | None,
    own_entry: IndexProvider | None,
    decision: Decision | None,
    update: str | None,
    error: ProviderError | None,
) -> CommunityProvider:
    """
    Return an installed provider as the app shows it.

    :param domain: The domain of the provider.
    :param installed: What was installed.
    :param manifest: Its manifest, as installed_manifest returns it.
    :param entry: The provider in the kept copy of the index.
    :param own_entry: The provider in the index of this server's line.
    :param decision: Whether it loads, None when it was not decided.
    :param update: The version the kept copy of the index offers to change to, if any.
    :param error: Why it does not load, as its settings entries show it.
    """
    latest = own_entry.latest if own_entry is not None else None
    release = own_entry.versions.get(latest) if own_entry and latest else None
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
    elif update:
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
        error=error,
        notice=installed.notice,
    )


async def installed_manifest(
    mass: MusicAssistant,
    installer: Installer,
    domain: str,
    entry: IndexProvider | None,
    logger: logging.Logger,
) -> ProviderManifest | None:
    """
    Return the manifest of an installed provider: of its live folder, else of the index.

    :param mass: The Music Assistant instance.
    :param installer: The installer that knows the live folder.
    :param domain: The domain of the provider.
    :param entry: The provider in the kept copy of the index.
    :param logger: The logger to report a provider that cannot be listed on.
    """
    for manifest in mass.get_provider_manifests():
        if manifest.domain == domain and manifest.tier == ProviderTier.COMMUNITY:
            return manifest
    folder = installer.live_folder(domain)
    try:
        manifest = await ProviderManifest.parse(str(folder / "manifest.json"))
        set_provider_tier(manifest, ProviderTier.COMMUNITY)
    except (OSError, ValueError, LookupError) as err:
        # the live folder is gone or broken when installing it again failed
        if entry is not None and (available := available_provider(domain, entry, logger)):
            return available.manifest
        logger.warning("Not listing community provider %s: %s", domain, err)
        return None
    return manifest


def available_provider(
    domain: str, entry: IndexProvider, logger: logging.Logger
) -> CommunityProvider | None:
    """
    Return a provider of the index that can be installed, or None.

    :param domain: The domain of the provider.
    :param entry: The provider in the index.
    :param logger: The logger to report a provider that cannot be listed on.
    """
    if (latest := entry.offered_version) is None:
        return None
    try:
        manifest = ProviderManifest.from_dict(entry.manifest)
    except (ValueError, LookupError) as err:
        logger.warning("Not listing community provider %s: invalid manifest: %s", domain, err)
        return None
    if manifest.domain != domain:
        logger.warning(
            "Not listing community provider %s: its manifest is of %s", domain, manifest.domain
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
