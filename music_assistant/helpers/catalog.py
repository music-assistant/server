"""What the Marketplace suggests to add, and why."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final

from mashumaro import DataClassDictMixin
from music_assistant_models.enums import ProviderStage, ProviderTier, ProviderType

from music_assistant.constants import CATALOG_PAIRINGS, CATALOG_POPULAR
from music_assistant.controllers.community_catalog.models import CommunityProviderState

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable

    from music_assistant_models.provider import ProviderManifest

    from music_assistant.controllers.community_catalog.models import CommunityProvider

# the domains of the built-in providers that are new in this release line, written by the
# release build
CATALOG_NEW_FILE: Final = Path(__file__).parent.parent / "catalog_new.json"
# how long an addition to the community catalog, or a release of one of its providers, is recent
COMMUNITY_RECENT: Final = timedelta(days=30)

_CATALOG_TYPES: Final = frozenset(
    {
        ProviderType.MUSIC,
        ProviderType.PLAYER,
        ProviderType.METADATA,
        ProviderType.PLUGIN,
        ProviderType.AUDIO_ANALYSIS,
    }
)
# installed and running, or not installed yet
_LISTED_COMMUNITY_STATES: Final = frozenset(
    {
        CommunityProviderState.AVAILABLE,
        CommunityProviderState.INSTALLED,
        CommunityProviderState.UPDATE_AVAILABLE,
        CommunityProviderState.RESTART_REQUIRED,
    }
)


class RecommendationReason(StrEnum):
    """Why the Marketplace suggests a provider, in the order the recommendations come in."""

    # seen on the network, and not set up
    DISCOVERED = "discovered"
    # goes well with a provider that is set up
    COMPLEMENTS = "complements"
    POPULAR = "popular"
    # new in this release line, or recently added to the community catalog
    NEW = "new"
    # a recent release of a provider of the community catalog
    UPDATED = "updated"


@dataclass
class Recommendation(DataClassDictMixin):
    """A provider the Marketplace suggests to add."""

    domain: str
    reason: RecommendationReason
    # for complements: the domain of the provider that is set up
    detail: str | None = None


def get_recommendations(
    manifests: Iterable[ProviderManifest],
    configured: Collection[str],
    can_add: Callable[[ProviderManifest], bool],
    discovered: Collection[str],
    new_domains: Collection[str],
    community: Iterable[CommunityProvider],
    now: datetime,
) -> list[Recommendation]:
    """
    Return what the Marketplace suggests to add, by reason and within a reason in a fixed order.

    Only providers that can be added are suggested: not built-in, not deprecated, of a type
    the Marketplace offers, allowed for the user, and not set up already unless more than one
    instance is allowed. A provider may be suggested for more than one reason.

    :param manifests: The manifests of the providers the server knows.
    :param configured: The domains of the providers that are set up, by anyone.
    :param can_add: Whether the user may add an instance of a provider.
    :param discovered: The domains of the providers seen on the network.
    :param new_domains: The domains of the built-in providers that are new in this release line.
    :param community: The providers of the community catalog, empty to suggest none of them.
    :param now: The current time, timezone aware.
    """

    def addable(manifest: ProviderManifest) -> bool:
        return (
            not manifest.builtin
            and manifest.stage != ProviderStage.DEPRECATED
            and manifest.type in _CATALOG_TYPES
            and (manifest.multi_instance or manifest.domain not in configured)
            and can_add(manifest)
        )

    known = {manifest.domain: manifest for manifest in manifests if addable(manifest)}
    found = [
        manifest
        for domain, manifest in known.items()
        if domain in discovered and domain not in configured
    ]
    complements: dict[str, str] = {}
    for configured_domain, suggested in CATALOG_PAIRINGS.items():
        if configured_domain in configured:
            for domain in suggested:
                if domain in known:
                    complements.setdefault(domain, configured_domain)
    new = [
        manifest
        for domain, manifest in known.items()
        if domain in new_domains and manifest.tier != ProviderTier.COMMUNITY
    ]
    updated: list[ProviderManifest] = []
    since = now - COMMUNITY_RECENT
    for provider in community:
        if provider.state not in _LISTED_COMMUNITY_STATES or not addable(provider.manifest):
            continue
        if _is_since(provider.added, since):
            new.append(provider.manifest)
        elif _is_since(provider.released, since):
            updated.append(provider.manifest)
    return [
        *_by_name(found, RecommendationReason.DISCOVERED),
        *(
            Recommendation(domain, RecommendationReason.COMPLEMENTS, configured_domain)
            for domain, configured_domain in complements.items()
        ),
        *(
            Recommendation(domain, RecommendationReason.POPULAR)
            for domain in CATALOG_POPULAR
            if domain in known
        ),
        *_by_name(new, RecommendationReason.NEW),
        *_by_name(updated, RecommendationReason.UPDATED),
    ]


def read_new_domains(path: Path) -> frozenset[str]:
    """
    Return the domains of the built-in providers that are new in this release line.

    Blocking: reads the file the release build writes. Without a readable file nothing is new.

    :param path: The file, a JSON list of domains, as CATALOG_NEW_FILE.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return frozenset()
    if not isinstance(data, list):
        return frozenset()
    return frozenset(domain for domain in data if isinstance(domain, str))


def _by_name(
    manifests: Iterable[ProviderManifest], reason: RecommendationReason
) -> list[Recommendation]:
    return [
        Recommendation(manifest.domain, reason)
        for manifest in sorted(manifests, key=lambda manifest: manifest.name.casefold())
    ]


def _is_since(timestamp: str | None, since: datetime) -> bool:
    if not timestamp:
        return False
    try:
        moment = datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    # a moment without a timezone is taken as UTC
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)) >= since
