"""Tests for what the Marketplace suggests to add."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from music_assistant_models.enums import ProviderStage, ProviderTier, ProviderType
from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import CATALOG_PAIRINGS, CATALOG_POPULAR
from music_assistant.controllers.community_catalog.models import (
    CommunityProvider,
    CommunityProviderState,
)
from music_assistant.helpers import catalog as catalog_module
from music_assistant.helpers.catalog import (
    COMMUNITY_RECENT,
    CatalogRecommendation,
    CatalogRecommendationReason,
    get_recommendations,
    read_new_domains,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
PROVIDERS_PATH = Path(__file__).parents[2] / "music_assistant" / "providers"


@pytest.fixture(autouse=True)
def editorial(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the editorial lists, which the team edits per release, with fixed ones."""
    monkeypatch.setattr(catalog_module, "CATALOG_POPULAR", ("zeta", "alpha", "multi"))
    monkeypatch.setattr(
        catalog_module,
        "CATALOG_PAIRINGS",
        {"source_b": ("plugin_x", "plugin_y"), "source_a": ("plugin_y", "plugin_z")},
    )


def test_discovered() -> None:
    """A provider seen on the network is suggested while it is not set up."""
    manifests = [_manifest("speaker"), _manifest("hub", multi_instance=True)]

    assert _recommend(manifests, discovered={"speaker", "hub"}, configured={"hub"}) == [
        CatalogRecommendation("speaker", CatalogRecommendationReason.DISCOVERED),
    ]


def test_complements() -> None:
    """A companion of a provider that is set up names the first one that points to it."""
    manifests = [_manifest(domain) for domain in ("plugin_x", "plugin_y", "plugin_z", "other")]

    assert _recommend(manifests, configured={"source_a", "source_b"}) == [
        CatalogRecommendation("plugin_x", CatalogRecommendationReason.COMPLEMENTS, "source_b"),
        CatalogRecommendation("plugin_y", CatalogRecommendationReason.COMPLEMENTS, "source_b"),
        CatalogRecommendation("plugin_z", CatalogRecommendationReason.COMPLEMENTS, "source_a"),
    ]
    assert _recommend(manifests, configured={"source_a"}) == [
        CatalogRecommendation("plugin_y", CatalogRecommendationReason.COMPLEMENTS, "source_a"),
        CatalogRecommendation("plugin_z", CatalogRecommendationReason.COMPLEMENTS, "source_a"),
    ]


def test_popular() -> None:
    """The popular providers come in the order of the list, as far as the server knows them."""
    manifests = [_manifest("alpha"), _manifest("other"), _manifest("zeta")]

    assert _recommend(manifests) == [
        CatalogRecommendation("zeta", CatalogRecommendationReason.POPULAR),
        CatalogRecommendation("alpha", CatalogRecommendationReason.POPULAR),
    ]


def test_new_builtin() -> None:
    """A provider of the server is new when the release build lists it."""
    manifests = [
        _manifest("fresh"),
        _manifest("other"),
        _manifest("comm", tier=ProviderTier.COMMUNITY),
    ]

    assert _recommend(manifests, new_domains={"fresh", "comm"}) == [
        CatalogRecommendation("fresh", CatalogRecommendationReason.NEW),
    ]


@pytest.mark.parametrize(
    ("added", "released", "reason"),
    [
        (NOW - COMMUNITY_RECENT, None, CatalogRecommendationReason.NEW),
        (NOW - COMMUNITY_RECENT, NOW, CatalogRecommendationReason.NEW),
        (NOW - COMMUNITY_RECENT - timedelta(seconds=1), NOW, CatalogRecommendationReason.UPDATED),
        (NOW - timedelta(days=90), NOW - COMMUNITY_RECENT, CatalogRecommendationReason.UPDATED),
        (NOW - timedelta(days=90), NOW - COMMUNITY_RECENT - timedelta(seconds=1), None),
        (None, None, None),
    ],
    ids=[
        "added_30_days_ago",
        "added_and_released",
        "added_before",
        "released_30_days_ago",
        "released_before",
        "no_dates",
    ],
)
def test_community_new_or_updated(
    added: datetime | None, released: datetime | None, reason: CatalogRecommendationReason | None
) -> None:
    """A community provider is new for 30 days after it was added, else updated after a release."""
    provider = _community("comm", added=_iso(added), released=_iso(released))

    expected = [CatalogRecommendation("comm", reason)] if reason else []
    assert _recommend([], community=[provider]) == expected


def test_community_dates() -> None:
    """A moment without a timezone is UTC, and a date that is not one is never recent."""
    assert _recommend(
        [],
        community=[
            _community("naive", added=(NOW - COMMUNITY_RECENT).replace(tzinfo=None).isoformat()),
            _community("garbage", added="soon", released="yesterday"),
        ],
    ) == [CatalogRecommendation("naive", CatalogRecommendationReason.NEW)]


def test_community_states() -> None:
    """Only community providers that run, or are on offer, are suggested."""
    states = list(CommunityProviderState)
    providers = [
        _community(state.value, state=state, added=_iso(NOW), name=f"{index:02}")
        for index, state in enumerate(states)
    ]

    assert _recommend([], community=providers) == [
        CatalogRecommendation(state, CatalogRecommendationReason.NEW)
        for state in ("available", "installed", "update_available", "restart_required")
    ]


@pytest.mark.parametrize(
    "state",
    [
        CommunityProviderState.WITHDRAWN,
        CommunityProviderState.BLOCKED,
        CommunityProviderState.UNAVAILABLE,
    ],
)
def test_no_longer_offered(state: CommunityProviderState) -> None:
    """An installed provider that the community catalog no longer offers is never suggested."""
    domains = ("alpha", "plugin_x", "speaker")
    manifests = [_manifest(domain, tier=ProviderTier.COMMUNITY) for domain in domains]
    community = [_community(domain, state=state, added=_iso(NOW)) for domain in domains]

    def recommend(community: Collection[CommunityProvider]) -> list[CatalogRecommendation]:
        return _recommend(
            manifests, configured={"source_b"}, discovered={"speaker"}, community=community
        )

    # found, goes well with and popular, for a user who does not see the community catalog
    assert len(recommend([])) == 3
    assert recommend(community) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"builtin": True},
        {"stage": ProviderStage.DEPRECATED},
        {"type": ProviderType.CORE},
        {"type": ProviderType.UNKNOWN},
    ],
    ids=["builtin", "deprecated", "core", "unknown_type"],
)
def test_never_offered(overrides: dict[str, Any]) -> None:
    """A provider that cannot be set up is never suggested, whatever the reason."""
    manifests = [_manifest(domain, **overrides) for domain in ("alpha", "plugin_x", "speaker")]
    community = _community("comm", added=_iso(NOW), **overrides)

    assert not _recommend(
        manifests,
        configured={"source_b"},
        discovered={"speaker"},
        new_domains={"alpha"},
        community=[community],
    )


def test_set_up_single_instance() -> None:
    """A provider that is set up and allows one instance is never suggested again."""
    manifests = [_manifest(domain) for domain in ("alpha", "plugin_x", "speaker")]
    community = _community("comm", added=_iso(NOW))

    assert not _recommend(
        manifests,
        configured={"alpha", "plugin_x", "speaker", "comm", "source_b"},
        discovered={"speaker"},
        new_domains={"alpha"},
        community=[community],
    )


def test_set_up_multi_instance() -> None:
    """A provider that is set up is not suggested again, also when it allows another instance."""
    manifests = [_manifest("multi", multi_instance=True)]
    community = _community("comm", added=_iso(NOW), multi_instance=True)

    assert not _recommend(
        manifests, configured={"multi", "comm"}, new_domains={"multi"}, community=[community]
    )
    assert _recommend(manifests, new_domains={"multi"}, community=[community]) == [
        CatalogRecommendation("multi", CatalogRecommendationReason.POPULAR),
        CatalogRecommendation("multi", CatalogRecommendationReason.NEW),
        CatalogRecommendation("comm", CatalogRecommendationReason.NEW),
    ]


def test_not_allowed() -> None:
    """Only what the user may add is suggested."""
    manifests = [_manifest("alpha"), _manifest("zeta", type=ProviderType.MUSIC)]
    community = [
        _community("comm", added=_iso(NOW)),
        _community("comm_music", added=_iso(NOW), type=ProviderType.MUSIC),
    ]

    assert _recommend(
        manifests,
        can_add=lambda manifest: manifest.type == ProviderType.MUSIC,
        community=community,
    ) == [
        CatalogRecommendation("zeta", CatalogRecommendationReason.POPULAR),
        CatalogRecommendation("comm_music", CatalogRecommendationReason.NEW),
    ]


def test_order() -> None:
    """By reason; the curated lists in their order, the community catalog newest first."""
    manifests = [
        _manifest("speaker_b", name="A speaker"),
        _manifest("speaker_a", name="B speaker"),
        _manifest("plugin_z"),
        _manifest("plugin_x"),
        _manifest("alpha"),
        _manifest("zeta"),
        _manifest("fresh", name="Fresh"),
    ]
    community = [
        _community("comm_b", name="beta", added=_iso(NOW - timedelta(days=2))),
        _community("comm_a", name="Gamma", added=_iso(NOW - timedelta(days=1))),
        _community("comm_e", name="alpha", added=_iso(NOW - timedelta(days=2))),
        _community("comm_d", name="delta", released=_iso(NOW - timedelta(days=3))),
        _community("comm_c", name="Epsilon", released=_iso(NOW)),
    ]

    assert _recommend(
        manifests,
        configured={"source_a", "source_b"},
        discovered={"speaker_a", "speaker_b"},
        new_domains={"fresh"},
        community=community,
    ) == [
        CatalogRecommendation("speaker_b", CatalogRecommendationReason.DISCOVERED),
        CatalogRecommendation("speaker_a", CatalogRecommendationReason.DISCOVERED),
        CatalogRecommendation("plugin_x", CatalogRecommendationReason.COMPLEMENTS, "source_b"),
        CatalogRecommendation("plugin_z", CatalogRecommendationReason.COMPLEMENTS, "source_a"),
        CatalogRecommendation("zeta", CatalogRecommendationReason.POPULAR),
        CatalogRecommendation("alpha", CatalogRecommendationReason.POPULAR),
        CatalogRecommendation("fresh", CatalogRecommendationReason.NEW),
        CatalogRecommendation("comm_a", CatalogRecommendationReason.NEW),
        CatalogRecommendation("comm_e", CatalogRecommendationReason.NEW),
        CatalogRecommendation("comm_b", CatalogRecommendationReason.NEW),
        CatalogRecommendation("comm_c", CatalogRecommendationReason.UPDATED),
        CatalogRecommendation("comm_d", CatalogRecommendationReason.UPDATED),
    ]


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (None, frozenset()),
        ("", frozenset()),
        ("[not json", frozenset()),
        ('{"fresh": true}', frozenset()),
        ("[]", frozenset()),
        ('["fresh", 3, "other"]', frozenset({"fresh", "other"})),
    ],
    ids=["missing", "empty", "broken", "not_a_list", "empty_list", "list"],
)
def test_read_new_domains(tmp_path: Path, content: str | None, expected: frozenset[str]) -> None:
    """The file the release build writes lists the new domains; anything else means none."""
    path = tmp_path / "catalog_new.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")

    assert read_new_domains(path) == expected


def test_editorial_lists() -> None:
    """Every provider the editorial lists suggest exists and can be added."""
    suggested = {*CATALOG_POPULAR, *(d for pair in CATALOG_PAIRINGS.values() for d in pair)}
    for domain in suggested:
        manifest = json.loads((PROVIDERS_PATH / domain / "manifest.json").read_text())
        assert not manifest.get("builtin", False), domain
        assert manifest.get("stage") != ProviderStage.DEPRECATED, domain
    for domain in CATALOG_PAIRINGS:
        assert (PROVIDERS_PATH / domain / "manifest.json").is_file(), domain


def _recommend(
    manifests: Iterable[ProviderManifest],
    *,
    configured: Collection[str] = (),
    can_add: Callable[[ProviderManifest], bool] = lambda _: True,
    discovered: Collection[str] = (),
    new_domains: Collection[str] = (),
    community: Collection[CommunityProvider] = (),
) -> list[CatalogRecommendation]:
    return get_recommendations(
        manifests,
        configured=configured,
        can_add=can_add,
        discovered=discovered,
        new_domains=new_domains,
        community=community,
        now=NOW,
    )


def _manifest(domain: str, **overrides: Any) -> ProviderManifest:
    values: dict[str, Any] = {
        "type": ProviderType.PLUGIN,
        "domain": domain,
        "name": domain,
        "description": "test provider",
        "codeowners": ["@someone"],
        "tier": ProviderTier.CORE,
    }
    return ProviderManifest(**(values | overrides))


def _community(
    domain: str,
    *,
    state: CommunityProviderState = CommunityProviderState.AVAILABLE,
    added: str | None = None,
    released: str | None = None,
    **overrides: Any,
) -> CommunityProvider:
    return CommunityProvider(
        domain=domain,
        repository=f"someone/{domain}",
        maintainers=["someone"],
        icon=None,
        manifest=_manifest(domain, tier=ProviderTier.COMMUNITY, **overrides),
        state=state,
        added=added,
        released=released,
    )


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat().replace("+00:00", "Z") if moment else None
