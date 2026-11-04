"""Tests for browsing every Discover row in full under the discover:// scheme."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import ANY, AsyncMock, MagicMock, Mock, patch

from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import (
    MediaType,
    ProviderFeature,
    ProviderSharing,
    ProviderType,
)
from music_assistant_models.media_items import (
    BrowseFolder,
    ItemMapping,
    RecommendationFolder,
    UniqueList,
)

from music_assistant.controllers.music.constants import RECOMMENDATIONS_BROWSE_LIMIT
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.recommendations import LibraryRecommendationsProvider
from tests.common import set_music_source_access

if TYPE_CHECKING:
    from collections.abc import Sequence

    import pytest
    from music_assistant_models.media_items import MediaItemType

    from music_assistant.models import ProviderInstanceType


class _RowsProvider(MusicProvider):
    """A fake provider exposing two recommendation rows with two items each."""

    async def get_recommendations(self) -> list[RecommendationFolder]:
        return [
            RecommendationFolder(
                item_id=f"row{i}",
                provider=self.instance_id,
                name=f"Row {i}",
                translation_key=f"row{i}_key",
            )
            for i in (1, 2)
        ]

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        if item_id not in ("row1", "row2"):
            return UniqueList()
        return UniqueList([_item(self.instance_id, f"{item_id}_item{n}") for n in (1, 2)])


class _SlashRowProvider(MusicProvider):
    """A fake provider whose single row id carries a slash of its own."""

    async def get_recommendations(self) -> list[RecommendationFolder]:
        return [
            RecommendationFolder(
                item_id="mixes/for you",
                provider=self.instance_id,
                name="For You",
            )
        ]

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        return UniqueList([_item(self.instance_id, "mix_item")])


def _item(provider: str, item_id: str) -> ItemMapping:
    """Return a track mapping as a provider row lists it."""
    return ItemMapping.from_dict(
        {
            "item_id": item_id,
            "provider": provider,
            "media_type": MediaType.TRACK.value,
            "name": item_id,
        }
    )


def _folders(items: Sequence[MediaItemType | ItemMapping | BrowseFolder]) -> list[BrowseFolder]:
    """Return the folders of a browse listing."""
    return [item for item in items if isinstance(item, BrowseFolder)]


def _build(
    provider_cls: type[MusicProvider],
    instance_id: str = "fake_instance",
    name: str = "Fake Provider",
) -> MusicProvider:
    """Construct a minimal provider with stubbed mass/manifest/config."""
    manifest = MagicMock()
    manifest.type = ProviderType.MUSIC
    manifest.domain = "fake"
    config = MagicMock()
    config.name = name
    config.instance_id = instance_id
    config.get_value = MagicMock(return_value="GLOBAL")
    return provider_cls(
        MagicMock(), manifest, config, supported_features={ProviderFeature.RECOMMENDATIONS}
    )


def _serve(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch, **providers: MusicProvider
) -> None:
    """Let the server resolve the given fake providers, keeping the real ones working."""
    real_get_provider = mass.get_provider

    def get_provider(
        instance_id_or_domain: str, *_args: object, **_kwargs: object
    ) -> ProviderInstanceType | None:
        return providers.get(instance_id_or_domain) or real_get_provider(instance_id_or_domain)

    monkeypatch.setattr(mass, "get_provider", get_provider)


def _restricted_user(mass: MusicAssistant) -> User:
    """Return a user that may not use the "restricted_instance" music source."""
    set_music_source_access(
        mass,
        {"restricted_instance": ProviderAccess(owner="user-b", sharing=ProviderSharing.PRIVATE)},
    )
    return User(user_id="user-a", username="user-a", role=UserRole.USER)


async def test_browse_lists_one_folder_per_row_provider(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The discover root lists a folder per provider that owns rows, in listing order."""
    builtin = mass.get_provider("recommendations")
    assert isinstance(builtin, LibraryRecommendationsProvider)
    prov_a = _build(_RowsProvider, instance_id="prov_a", name="Provider A")
    prov_b = _build(_RowsProvider, instance_id="prov_b", name="Provider B")
    monkeypatch.setattr(
        mass, "get_providers_supporting_feature", lambda *_a, **_k: [builtin, prov_a, prov_b]
    )
    _serve(mass, monkeypatch, prov_a=prov_a, prov_b=prov_b)

    items = await mass.music.browse("discover://")

    folders = _folders(items)
    assert folders[0].path == "root"
    assert [folder.path for folder in folders[1:]] == [
        "discover://recommendations",
        "discover://prov_a",
        "discover://prov_b",
    ]
    assert [folder.name for folder in folders[1:]] == [builtin.name, "Provider A", "Provider B"]


async def test_browse_lists_a_providers_rows_as_folders(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider level lists its rows, each opening the row's full item list."""
    builtin = mass.get_provider("recommendations")
    assert isinstance(builtin, LibraryRecommendationsProvider)
    prov_a = _build(_RowsProvider, instance_id="prov_a")
    monkeypatch.setattr(
        mass, "get_providers_supporting_feature", lambda *_a, **_k: [builtin, prov_a]
    )
    _serve(mass, monkeypatch, prov_a=prov_a)

    items = await mass.music.browse("discover://prov_a")

    folders = _folders(items)
    assert folders[0].path == "discover://"
    rows = folders[1:]
    assert [folder.path for folder in rows] == [
        "discover://prov_a/row1",
        "discover://prov_a/row2",
    ]
    assert [folder.name for folder in rows] == ["Row 1", "Row 2"]
    assert [folder.translation_key for folder in rows] == ["row1_key", "row2_key"]


async def test_browse_a_row_lists_all_of_its_items(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row level lists the row's items, with the way back to its provider first."""
    builtin = mass.get_provider("recommendations")
    assert isinstance(builtin, LibraryRecommendationsProvider)
    prov_a = _build(_RowsProvider, instance_id="prov_a")
    monkeypatch.setattr(
        mass, "get_providers_supporting_feature", lambda *_a, **_k: [builtin, prov_a]
    )
    _serve(mass, monkeypatch, prov_a=prov_a)

    items = await mass.music.browse("discover://prov_a/row1")

    folders = _folders(items)
    assert folders[0].path == "discover://prov_a"
    assert [item.item_id for item in items[1:]] == ["row1_item1", "row1_item2"]


async def test_browse_a_row_keeps_a_slash_in_the_row_id(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row id carrying a slash is browsed whole, not cut at its first slash."""
    builtin = mass.get_provider("recommendations")
    assert isinstance(builtin, LibraryRecommendationsProvider)
    prov_a = _build(_SlashRowProvider, instance_id="prov_a")
    monkeypatch.setattr(
        mass, "get_providers_supporting_feature", lambda *_a, **_k: [builtin, prov_a]
    )
    _serve(mass, monkeypatch, prov_a=prov_a)

    folders = _folders(await mass.music.browse("discover://prov_a"))
    assert [folder.path for folder in folders[1:]] == ["discover://prov_a/mixes/for you"]

    items = await mass.music.browse("discover://prov_a/mixes/for you")
    assert _folders(items)[0].path == "discover://prov_a"
    assert [item.item_id for item in items[1:]] == ["mix_item"]


async def test_browse_a_library_row_asks_for_the_full_browse_limit(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A library row is browsed with the full limit, not the handful its Discover row shows."""
    builtin = mass.get_provider("recommendations")
    assert isinstance(builtin, LibraryRecommendationsProvider)
    spy = AsyncMock(return_value=UniqueList())
    monkeypatch.setattr(builtin, "get_recommendation_items", spy)

    await mass.music.browse("discover://recommendations/recently_played")

    spy.assert_awaited_once_with(
        "recently_played",
        providers=None,
        limit=RECOMMENDATIONS_BROWSE_LIMIT,
        random_seed=ANY,
    )


async def test_browse_an_unknown_provider_or_row_lists_nothing(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown provider or row answers with the way back only, never an error."""
    builtin = mass.get_provider("recommendations")
    assert isinstance(builtin, LibraryRecommendationsProvider)
    prov_a = _build(_RowsProvider, instance_id="prov_a")
    monkeypatch.setattr(
        mass, "get_providers_supporting_feature", lambda *_a, **_k: [builtin, prov_a]
    )
    _serve(mass, monkeypatch, prov_a=prov_a)

    unknown_provider = _folders(await mass.music.browse("discover://no_such_provider/row1"))
    assert [(folder.item_id, folder.path) for folder in unknown_provider] == [
        ("back", "discover://no_such_provider")
    ]

    unknown_row = _folders(await mass.music.browse("discover://prov_a/no_such_row"))
    assert [(folder.item_id, folder.path) for folder in unknown_row] == [
        ("back", "discover://prov_a")
    ]


@patch("music_assistant.controllers.music.controller.get_current_user")
async def test_browse_a_row_of_a_hidden_source_lists_no_items(
    mock_get_user: Mock, mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Browsing a row of a music source the user may not use returns no items."""
    mock_get_user.return_value = _restricted_user(mass)
    provider = _build(_RowsProvider, instance_id="restricted_instance")
    provider.get_recommendation_items = AsyncMock()  # type: ignore[method-assign]
    _serve(mass, monkeypatch, restricted_instance=provider)

    folders = _folders(await mass.music.browse("discover://restricted_instance/row1"))

    assert [folder.item_id for folder in folders] == ["back"]
    provider.get_recommendation_items.assert_not_awaited()


async def test_browse_a_random_row_keeps_one_order_across_pages(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A random row is browsed with the same seed on every page request."""
    builtin = mass.get_provider("recommendations")
    assert isinstance(builtin, LibraryRecommendationsProvider)
    spy = AsyncMock(return_value=UniqueList())
    monkeypatch.setattr(builtin, "get_recommendation_items", spy)

    await mass.music.browse("discover://recommendations/random_albums", limit=50, offset=0)
    await mass.music.browse("discover://recommendations/random_albums", limit=50, offset=50)

    seeds = [call.kwargs["random_seed"] for call in spy.await_args_list]
    assert len(seeds) == 2
    assert seeds[0] is not None
    assert seeds[0] == seeds[1]


async def test_browse_a_random_row_uses_the_callers_seed(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seed passed to the browse command wins over the row's own."""
    builtin = mass.get_provider("recommendations")
    assert isinstance(builtin, LibraryRecommendationsProvider)
    spy = AsyncMock(return_value=UniqueList())
    monkeypatch.setattr(builtin, "get_recommendation_items", spy)

    await mass.music.browse("discover://recommendations/random_albums", random_seed=4242)

    assert spy.await_args is not None
    assert spy.await_args.kwargs["random_seed"] == 4242


async def test_random_library_rows_forward_the_seed_to_the_listing(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The random library rows hand the seed to the library listing they read."""
    builtin = mass.get_provider("recommendations")
    assert isinstance(builtin, LibraryRecommendationsProvider)
    for row_id in ("random_albums", "random_artists", "favorite_playlists"):
        spy = AsyncMock(return_value=[])
        monkeypatch.setattr(mass.music.albums, "library_items", spy)
        monkeypatch.setattr(mass.music.artists, "library_items", spy)
        monkeypatch.setattr(mass.music.playlists, "library_items", spy)

        await builtin.get_recommendation_items(row_id, limit=50, random_seed=99)

        assert spy.await_args is not None
        assert spy.await_args.kwargs["random_seed"] == 99, row_id
