"""
Favorite and library writes reach only the acting user's own provider instance(s).

One library item maps to one instance per account on a shared server; before this, every write
fanned out to all of them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

from music_assistant_models.enums import MediaType, ProviderType
from music_assistant_models.media_items import ProviderMapping

from music_assistant.controllers.music import MusicController

if TYPE_CHECKING:
    import pytest

INSTANCE_MINE = "opensubsonic--mine"
INSTANCE_OTHER = "opensubsonic--other"
INSTANCE_THIRD = "opensubsonic--third"
INSTANCE_GONE = "opensubsonic--gone"
BUILTIN = "builtin"


def _mapping(instance_id: str, item_id: str, in_library: bool = False) -> ProviderMapping:
    return ProviderMapping(
        item_id=item_id,
        provider_domain="opensubsonic",
        provider_instance=instance_id,
        in_library=in_library,
    )


def _controller(music_instances: set[str], unavailable: set[str] | None = None) -> MusicController:
    """Build a controller stub whose only live parts are the provider registry lookups."""
    controller = MusicController.__new__(MusicController)
    controller.mass = Mock()
    unavailable = unavailable or set()
    providers: dict[str, Mock] = {}

    def _get_provider(
        instance_id: str, return_unavailable: bool = False, **_kwargs: object
    ) -> Mock | None:
        if instance_id in music_instances:
            if instance_id in unavailable and not return_unavailable:
                return None
            prov = providers.setdefault(instance_id, Mock())
            prov.type = ProviderType.MUSIC
            prov.instance_id = instance_id
            prov.available = instance_id not in unavailable
            return prov
        if instance_id == BUILTIN:
            prov = providers.setdefault(instance_id, Mock())
            prov.type = ProviderType.PLUGIN
            prov.instance_id = instance_id
            prov.available = True
            return prov
        return None

    controller.mass.get_provider.side_effect = _get_provider
    controller._providers = providers  # type: ignore[attr-defined]
    return controller


def _patched(monkeypatch: pytest.MonkeyPatch, user_filter: list[str] | None) -> None:
    user = None if user_filter is None else Mock(provider_filter=user_filter)
    monkeypatch.setattr(
        "music_assistant.controllers.music.controller.get_current_user",
        lambda: user,
    )


MAPPINGS = [
    _mapping(INSTANCE_MINE, "mine-42"),
    _mapping(INSTANCE_OTHER, "other-42"),
    _mapping(INSTANCE_THIRD, "third-42"),
]
MUSIC = {INSTANCE_MINE, INSTANCE_OTHER, INSTANCE_THIRD}


def test_favorite_reaches_only_the_acting_users_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect this fixes: one star must not land in three accounts."""
    _patched(monkeypatch, [BUILTIN, INSTANCE_MINE])
    controller = _controller(MUSIC)

    targets = controller._user_target_mappings(MAPPINGS)

    assert [m.provider_instance for m in targets] == [INSTANCE_MINE]


def test_unfiltered_user_keeps_the_previous_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty provider_filter expresses no preference, so nothing changes for it."""
    _patched(monkeypatch, [])
    controller = _controller(MUSIC)

    targets = controller._user_target_mappings(MAPPINGS)

    assert [m.provider_instance for m in targets] == [
        INSTANCE_MINE,
        INSTANCE_OTHER,
        INSTANCE_THIRD,
    ]


def test_no_current_user_keeps_the_previous_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    """A write with no user context (internal call) must not be narrowed."""
    _patched(monkeypatch, None)
    controller = _controller(MUSIC)

    targets = controller._user_target_mappings(MAPPINGS)

    assert len(targets) == 3


def test_filter_naming_no_music_provider_expresses_no_preference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A built-in-only filter is not a statement about which account owns a favorite."""
    _patched(monkeypatch, [BUILTIN])
    controller = _controller(MUSIC)

    targets = controller._user_target_mappings(MAPPINGS)

    assert len(targets) == 3


def test_filter_is_an_allowlist_when_the_users_instance_holds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No mapping of the user's own means NOTHING is forwarded, not somebody else's account."""
    _patched(monkeypatch, [INSTANCE_MINE])
    controller = _controller(MUSIC)

    targets = controller._user_target_mappings(
        [_mapping(INSTANCE_OTHER, "other-42"), _mapping(INSTANCE_THIRD, "third-42")]
    )

    assert targets == []


def test_multiple_own_instances_are_all_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user allowed two instances gets the favorite on both: the filter is the whole answer."""
    _patched(monkeypatch, [INSTANCE_MINE, INSTANCE_THIRD])
    controller = _controller(MUSIC)

    targets = controller._user_target_mappings(MAPPINGS)

    assert [m.provider_instance for m in targets] == [INSTANCE_MINE, INSTANCE_THIRD]


def test_unavailable_own_instance_stays_the_only_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The user's instance being down must not widen the write to everybody.

    The default provider lookup returns None for an unavailable non-streaming instance, which
    used to read as "the filter names no music provider" and returned every mapping.
    """
    _patched(monkeypatch, [INSTANCE_MINE])
    controller = _controller(MUSIC, unavailable={INSTANCE_MINE})

    targets = controller._user_target_mappings(MAPPINGS)

    assert [m.provider_instance for m in targets] == [INSTANCE_MINE]


def test_stale_filter_entry_forwards_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A filter entry that resolves to no provider (removed or renamed) fails closed."""
    _patched(monkeypatch, [INSTANCE_MINE, INSTANCE_GONE])
    controller = _controller(MUSIC)

    targets = controller._user_target_mappings(MAPPINGS)

    assert targets == []


async def test_library_remove_reaches_only_the_acting_users_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provider-side removal goes to the user's own account only; the row still goes."""
    _patched(monkeypatch, [INSTANCE_MINE])
    controller = _controller(MUSIC)
    full_item = Mock(
        media_type=MediaType.TRACK,
        provider_mappings=[
            _mapping(INSTANCE_MINE, "mine-42", in_library=True),
            _mapping(INSTANCE_OTHER, "other-42", in_library=True),
        ],
    )
    ctrl = Mock(
        get_library_item=AsyncMock(return_value=full_item),
        remove_item_from_library=AsyncMock(),
    )
    controller.get_controller = Mock(return_value=ctrl)  # type: ignore[method-assign]
    controller.library_edit_supported = Mock(return_value=True)  # type: ignore[method-assign]
    controller.library_sync_back_enabled = Mock(return_value=True)  # type: ignore[method-assign]

    await controller.remove_item_from_library(MediaType.TRACK, "42")

    providers = controller._providers  # type: ignore[attr-defined]
    providers[INSTANCE_MINE].library_remove.assert_called_once_with("mine-42", MediaType.TRACK)
    assert INSTANCE_OTHER not in providers or not providers[INSTANCE_OTHER].library_remove.called
    ctrl.remove_item_from_library.assert_awaited_once_with("42", True)


async def test_library_add_reaches_only_the_acting_users_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provider-side add goes to the user's own account; every mapping still reads in_library."""
    _patched(monkeypatch, [INSTANCE_MINE])
    controller = _controller(MUSIC)
    mappings = [_mapping(INSTANCE_MINE, "mine-42"), _mapping(INSTANCE_OTHER, "other-42")]
    item = Mock(provider="builtin", media_type=MediaType.TRACK, provider_mappings=mappings)
    ctrl = Mock(add_item_to_library=AsyncMock(return_value=item))
    controller.get_controller = Mock(return_value=ctrl)  # type: ignore[method-assign]
    controller.library_edit_supported = Mock(return_value=True)  # type: ignore[method-assign]
    controller.library_sync_back_enabled = Mock(return_value=True)  # type: ignore[method-assign]
    controller.mass.metadata.update_metadata = AsyncMock()  # type: ignore[method-assign]

    await controller.add_item_to_library(item)

    providers = controller._providers  # type: ignore[attr-defined]
    assert providers[INSTANCE_MINE].library_add.call_count == 1
    assert INSTANCE_OTHER not in providers or not providers[INSTANCE_OTHER].library_add.called
    assert all(m.in_library for m in mappings)
