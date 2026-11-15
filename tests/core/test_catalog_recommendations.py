"""Tests for the command that tells the Marketplace what to suggest."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.auth import UserRole
from music_assistant_models.enums import ProviderTier, ProviderType
from music_assistant_models.errors import ActionUnavailable
from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import CONF_PROVIDERS
from music_assistant.controllers.community_catalog.constants import CONF_ENABLED
from music_assistant.controllers.community_catalog.models import (
    CommunityProvider,
    CommunityProviderState,
)
from music_assistant.helpers.catalog import CatalogRecommendation, CatalogRecommendationReason
from music_assistant.testing import full_mass_context
from tests.common import as_user

if TYPE_CHECKING:
    from pathlib import Path

    from music_assistant.mass import MusicAssistant

COMMUNITY_DOMAIN = "comm_music"


async def test_admin(tmp_path: Path) -> None:
    """An admin is suggested what answered on the network, what goes with its setup and more."""
    new_file = tmp_path / "catalog_new.json"
    new_file.write_text('["sonos", "lrclib"]', encoding="utf-8")

    with patch("music_assistant.mass.CATALOG_NEW_FILE", new_file):
        async with full_mass_context(tmp_path) as mass:
            cache = mass.discovery.aiozc.zeroconf.cache.cache
            cache["kitchen._sonos._tcp.local."] = {}
            # a printer: devices of any kind announce this type, which MusicCast listens for
            cache["printer._http._tcp.local."] = {}
            musiccast_seen = mass.discovery.is_seen_on_mdns(mass.get_provider_manifest("musiccast"))
            _set_up(mass, "spotify", "airplay")
            with as_user([], role=UserRole.ADMIN):
                recommendations = await mass.get_catalog_recommendations()

    assert recommendations[:3] == [
        CatalogRecommendation("sonos", CatalogRecommendationReason.DISCOVERED),
        CatalogRecommendation(
            "spotify_connect", CatalogRecommendationReason.COMPLEMENTS, "spotify"
        ),
        CatalogRecommendation(
            "lastfm_scrobble", CatalogRecommendationReason.COMPLEMENTS, "spotify"
        ),
    ]
    reasons = _by_reason(recommendations)
    assert musiccast_seen
    assert reasons[CatalogRecommendationReason.DISCOVERED] == ["sonos"]
    # spotify allows more than one account, airplay is set up once
    assert {"spotify", "sonos", "chromecast"} <= set(reasons[CatalogRecommendationReason.POPULAR])
    assert all(rec.domain != "airplay" for rec in recommendations)
    # lrclib ships with the server
    assert reasons[CatalogRecommendationReason.NEW] == ["sonos"]


async def test_nothing_new(tmp_path: Path) -> None:
    """Without the file of the release build, no provider of the server is new."""
    with patch("music_assistant.mass.CATALOG_NEW_FILE", tmp_path / "missing.json"):
        async with full_mass_context(tmp_path) as mass:
            recommendations = await mass.get_catalog_recommendations()

    assert recommendations
    assert CatalogRecommendationReason.NEW not in _by_reason(recommendations)


async def test_member_and_guest(tmp_path: Path) -> None:
    """A user is only suggested its own music sources; a guest, who may add nothing, nothing."""
    async with full_mass_context(tmp_path) as mass:
        mass.discovery.aiozc.zeroconf.cache.cache["kitchen._sonos._tcp.local."] = {}
        _set_up(mass, "spotify")
        with as_user([], role=UserRole.USER):
            member = await mass.get_catalog_recommendations()
        with as_user([], role=UserRole.GUEST):
            guest = await mass.get_catalog_recommendations()
        manifests = {manifest.domain: manifest for manifest in mass.get_provider_manifests()}

    assert {"spotify", "tidal"} <= set(_by_reason(member)[CatalogRecommendationReason.POPULAR])
    for rec in member:
        manifest = manifests[rec.domain]
        assert manifest.type == ProviderType.MUSIC, rec
        assert manifest.multi_instance, rec
        assert manifest.self_service, rec
    assert guest == []


async def test_community(tmp_path: Path) -> None:
    """The community catalog adds to the suggestions of an admin, while it is switched on."""
    new = CatalogRecommendation(COMMUNITY_DOMAIN, CatalogRecommendationReason.NEW)
    get_providers = AsyncMock(return_value=[_community_provider()])

    async with full_mass_context(tmp_path) as mass:
        with patch.object(mass.community_catalog, "get_providers", get_providers):
            with as_user([], role=UserRole.ADMIN):
                switched_off = await mass.get_catalog_recommendations()
                # switched on as the app does it, without the check of the index that follows
                mass.community_catalog.config.values[CONF_ENABLED].value = True
                switched_on = await mass.get_catalog_recommendations()
                get_providers.side_effect = ActionUnavailable("not now")
                failing = await mass.get_catalog_recommendations()
                get_providers.side_effect = RuntimeError("a bug")
                with pytest.raises(RuntimeError):
                    await mass.get_catalog_recommendations()
            get_providers.side_effect = None
            # a user does not see the community catalog, even what it may add from there
            with as_user([], role=UserRole.USER):
                member = await mass.get_catalog_recommendations()

    assert new not in switched_off
    assert new in switched_on
    assert new not in failing
    assert _by_reason(failing)[CatalogRecommendationReason.POPULAR]
    assert new not in member
    assert _by_reason(member)[CatalogRecommendationReason.POPULAR]


def _set_up(mass: MusicAssistant, *domains: str) -> None:
    """Store a disabled instance of each provider, as one that is set up but not loaded."""
    for domain in domains:
        manifest = mass.get_provider_manifest(domain)
        instance_id = f"{domain}--test"
        mass.config.set(
            f"{CONF_PROVIDERS}/{instance_id}",
            {
                "type": manifest.type.value,
                "domain": domain,
                "instance_id": instance_id,
                "enabled": False,
                "values": {},
            },
        )


def _by_reason(
    recommendations: list[CatalogRecommendation],
) -> dict[CatalogRecommendationReason, list[str]]:
    result: dict[CatalogRecommendationReason, list[str]] = {}
    for rec in recommendations:
        result.setdefault(rec.reason, []).append(rec.domain)
    return result


def _community_provider() -> CommunityProvider:
    """Return a music source a user could add, added to the community catalog just now."""
    return CommunityProvider(
        domain=COMMUNITY_DOMAIN,
        repository=f"someone/{COMMUNITY_DOMAIN}",
        maintainers=["someone"],
        icon=None,
        manifest=ProviderManifest(
            type=ProviderType.MUSIC,
            domain=COMMUNITY_DOMAIN,
            name="Community music",
            description="test provider",
            codeowners=["@someone"],
            tier=ProviderTier.COMMUNITY,
            multi_instance=True,
        ),
        state=CommunityProviderState.AVAILABLE,
        added=datetime.now(UTC).isoformat(),
    )
