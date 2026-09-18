"""
Favorite and library writes reach only the music sources the acting user may write to.

One library item maps to one instance per account on a shared server, so before this every
write fanned out to all of them: one person's star landed in everybody else's account.
Ownership decides who receives a write, which is a narrower question than who may see a
source, so a shared account is readable and still never written to.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import MediaType, ProviderSharing, ProviderType
from music_assistant_models.media_items import ProviderMapping

from music_assistant.controllers.music import MusicController
from music_assistant.models.music_provider import MusicProvider
from tests.common import set_music_source_access

if TYPE_CHECKING:
    from collections.abc import Mapping

GET_CURRENT_USER = "music_assistant.controllers.music.controller.get_current_user"

ME = "user-me"
SOMEBODY_ELSE = "user-else"
MINE = "opensubsonic--mine"
THEIRS = "opensubsonic--theirs"
A_THIRD = "opensubsonic--third"
ALSO_MINE = "opensubsonic--also-mine"
WHOLE_HOME = "filesystem--nas"


def _user(user_id: str = ME, role: UserRole = UserRole.USER) -> User:
    return User(user_id=user_id, username=user_id, role=role)


def _owned(owner: str, sharing: ProviderSharing = ProviderSharing.PRIVATE) -> ProviderAccess:
    return ProviderAccess(owner=owner, sharing=sharing)


def _mapping(instance_id: str, item_id: str, in_library: bool = False) -> ProviderMapping:
    return ProviderMapping(
        item_id=item_id,
        provider_domain=instance_id.split("--", maxsplit=1)[0],
        provider_instance=instance_id,
        in_library=in_library,
    )


def _controller(
    sources: Mapping[str, ProviderAccess | None],
    unavailable: set[str] | None = None,
    served_by: Mapping[str, str] | None = None,
) -> MusicController:
    """
    Build a controller stub whose only live parts are the access records and the registry.

    :param sources: The configured music sources and the access record each carries.
    :param unavailable: Sources whose provider is loaded but not available.
    :param served_by: Sources whose lookup widens to another instance, the way
        ``mass.get_provider`` widens to a sibling account of the same domain.
    """
    controller = MusicController.__new__(MusicController)
    controller.mass = Mock()
    set_music_source_access(controller.mass, dict(sources))
    unavailable = unavailable or set()
    served_by = served_by or {}
    providers: dict[str, Mock] = {}

    def _provider_for(instance_id: str) -> Mock:
        if instance_id not in providers:
            providers[instance_id] = Mock(
                spec=MusicProvider,
                set_favorite=AsyncMock(),
                library_add=AsyncMock(),
                library_remove=AsyncMock(),
            )
        prov = providers[instance_id]
        prov.type = ProviderType.MUSIC
        prov.instance_id = instance_id
        prov.available = instance_id not in unavailable
        return prov

    def _get_provider(
        instance_id: str, return_unavailable: bool = False, **_kwargs: object
    ) -> Mock | None:
        served = served_by.get(instance_id, instance_id)
        if served not in sources:
            return None
        if served in unavailable and not return_unavailable:
            return None
        return _provider_for(served)

    def _create_task(target: object, *_args: object, **_kwargs: object) -> Mock:
        # the real create_task runs the coroutine; here it is only recorded, so close it
        # to keep an unawaited coroutine out of the warning filter
        if hasattr(target, "close"):
            target.close()
        return Mock()

    controller.mass.get_provider.side_effect = _get_provider
    controller.mass.create_task.side_effect = _create_task
    controller.providers_seen = providers  # type: ignore[attr-defined]
    return controller


def _as_user(monkeypatch: pytest.MonkeyPatch, user: User | None) -> None:
    monkeypatch.setattr(GET_CURRENT_USER, lambda: user)


THREE_ACCOUNTS: dict[str, ProviderAccess | None] = {
    MINE: _owned(ME),
    THEIRS: _owned(SOMEBODY_ELSE, ProviderSharing.EVERYONE),
    A_THIRD: _owned("user-third", ProviderSharing.EVERYONE),
}
THREE_MAPPINGS = [
    _mapping(MINE, "mine-42"),
    _mapping(THEIRS, "theirs-42"),
    _mapping(A_THIRD, "third-42"),
]


def test_a_write_reaches_only_the_source_the_user_owns(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect this fixes: one star must not land in three accounts."""
    _as_user(monkeypatch, _user())
    controller = _controller(THREE_ACCOUNTS)

    targets = controller._write_target_mappings(THREE_MAPPINGS)

    assert [m.provider_instance for m in targets] == [MINE]


@pytest.mark.parametrize(
    "sharing",
    [ProviderSharing.EVERYONE, ProviderSharing.MEMBERS, ProviderSharing.SELECTED],
)
def test_another_persons_source_is_never_written_to(
    monkeypatch: pytest.MonkeyPatch, sharing: ProviderSharing
) -> None:
    """Sharing an account makes it readable, never writable: that is the whole point."""
    _as_user(monkeypatch, _user())
    access = ProviderAccess(owner=SOMEBODY_ELSE, sharing=sharing, shared_users=[ME])
    controller = _controller({MINE: _owned(ME), THEIRS: access})

    targets = controller._write_target_mappings(
        [_mapping(MINE, "mine-42"), _mapping(THEIRS, "theirs-42")]
    )

    assert [m.provider_instance for m in targets] == [MINE]


def test_a_source_of_the_whole_home_is_written_to(monkeypatch: pytest.MonkeyPatch) -> None:
    """A source nobody owns (the household NAS) belongs to everyone, so it takes the write."""
    _as_user(monkeypatch, _user())
    controller = _controller({WHOLE_HOME: None, THEIRS: _owned(SOMEBODY_ELSE)})

    targets = controller._write_target_mappings(
        [_mapping(WHOLE_HOME, "nas-42"), _mapping(THEIRS, "theirs-42")]
    )

    assert [m.provider_instance for m in targets] == [WHOLE_HOME]


def test_an_unowned_source_the_user_may_not_see_is_not_written_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner None is not by itself a write right: the user must be allowed to see it too."""
    _as_user(monkeypatch, _user())
    hidden = ProviderAccess(sharing=ProviderSharing.PRIVATE)
    controller = _controller({MINE: _owned(ME), WHOLE_HOME: hidden})

    targets = controller._write_target_mappings(
        [_mapping(MINE, "mine-42"), _mapping(WHOLE_HOME, "nas-42")]
    )

    assert [m.provider_instance for m in targets] == [MINE]


def test_a_guest_is_not_written_to_a_members_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """A guest may not use a members-only source, so a write never reaches it either."""
    _as_user(monkeypatch, _user("guest", UserRole.GUEST))
    members_only = ProviderAccess(sharing=ProviderSharing.MEMBERS)
    controller = _controller({WHOLE_HOME: members_only})

    assert controller._write_target_mappings([_mapping(WHOLE_HOME, "nas-42")]) == []


def test_every_own_source_is_written_to(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two accounts of one person are both theirs, so both take the write."""
    _as_user(monkeypatch, _user())
    controller = _controller(
        {MINE: _owned(ME), ALSO_MINE: _owned(ME), THEIRS: _owned(SOMEBODY_ELSE)}
    )

    targets = controller._write_target_mappings(
        [_mapping(MINE, "a"), _mapping(ALSO_MINE, "b"), _mapping(THEIRS, "c")]
    )

    assert [m.provider_instance for m in targets] == [MINE, ALSO_MINE]


def test_nothing_is_forwarded_when_the_user_owns_none_of_the_mapped_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No source of the user's own means NOTHING is written, not somebody else's account."""
    _as_user(monkeypatch, _user())
    controller = _controller(THREE_ACCOUNTS)

    targets = controller._write_target_mappings(THREE_MAPPINGS[1:])

    assert targets == []


def test_an_unreadable_access_record_is_not_written_to(monkeypatch: pytest.MonkeyPatch) -> None:
    """A record we can not parse hides its source, so a write must not reach it either."""
    _as_user(monkeypatch, _user())
    controller = _controller({MINE: _owned(ME), THEIRS: None})
    controller.mass.config.get = Mock(  # type: ignore[method-assign]
        side_effect=lambda key, default=None: (
            {
                MINE: {
                    "type": ProviderType.MUSIC.value,
                    "domain": "opensubsonic",
                    "instance_id": MINE,
                    "access": _owned(ME).to_dict(),
                },
                THEIRS: {
                    "type": ProviderType.MUSIC.value,
                    "domain": "opensubsonic",
                    "instance_id": THEIRS,
                    "access": {"sharing": "not-a-sharing-value"},
                },
            }
            if key == "providers"
            else {
                "type": ProviderType.MUSIC.value,
                "domain": "opensubsonic",
                "instance_id": THEIRS,
                "access": {"sharing": "not-a-sharing-value"},
            }
            if key.endswith(THEIRS)
            else default
        )
    )

    targets = controller._write_target_mappings(
        [_mapping(MINE, "mine-42"), _mapping(THEIRS, "theirs-42")]
    )

    assert [m.provider_instance for m in targets] == [MINE]


def test_no_current_user_writes_everywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """An internal caller (library sync, a plugin) is not a person and is not narrowed."""
    _as_user(monkeypatch, None)
    controller = _controller(THREE_ACCOUNTS)

    targets = controller._write_target_mappings(THREE_MAPPINGS)

    assert len(targets) == 3


async def test_add_item_to_favorites_writes_only_to_the_own_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The star reaches the user's own account and no other."""
    _as_user(monkeypatch, _user())
    controller = _controller(THREE_ACCOUNTS)
    full_item = Mock(
        provider="library",
        item_id="42",
        media_type=MediaType.TRACK,
        provider_mappings=THREE_MAPPINGS,
    )
    controller.get_item = AsyncMock(return_value=full_item)  # type: ignore[method-assign]
    ctrl = Mock(set_favorite=AsyncMock())
    controller.get_controller = Mock(return_value=ctrl)  # type: ignore[method-assign]
    controller.library_favorites_edit_supported = Mock(return_value=True)  # type: ignore[method-assign]

    await controller.add_item_to_favorites(full_item)

    seen = controller.providers_seen  # type: ignore[attr-defined]
    seen[MINE].set_favorite.assert_awaited_once_with("mine-42", MediaType.TRACK, True)
    assert THEIRS not in seen
    assert A_THIRD not in seen
    ctrl.set_favorite.assert_awaited_once_with("42", True)


async def test_remove_item_from_favorites_writes_only_to_the_own_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unstarring is scoped the same way as starring."""
    _as_user(monkeypatch, _user())
    controller = _controller(THREE_ACCOUNTS)
    full_item = Mock(media_type=MediaType.TRACK, provider_mappings=THREE_MAPPINGS)
    ctrl = Mock(set_favorite=AsyncMock(), get_library_item=AsyncMock(return_value=full_item))
    controller.get_controller = Mock(return_value=ctrl)  # type: ignore[method-assign]
    controller.library_favorites_edit_supported = Mock(return_value=True)  # type: ignore[method-assign]

    await controller.remove_item_from_favorites(MediaType.TRACK, "42")

    seen = controller.providers_seen  # type: ignore[attr-defined]
    seen[MINE].set_favorite.assert_called_once_with("mine-42", MediaType.TRACK, False)
    assert THEIRS not in seen
    ctrl.set_favorite.assert_awaited_once_with("42", False)


async def test_remove_item_from_library_writes_only_to_the_own_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provider-side removal goes to the user's own account; the library row still goes."""
    _as_user(monkeypatch, _user())
    controller = _controller(THREE_ACCOUNTS)
    mappings = [
        _mapping(MINE, "mine-42", in_library=True),
        _mapping(THEIRS, "theirs-42", in_library=True),
    ]
    full_item = Mock(media_type=MediaType.TRACK, provider_mappings=mappings)
    ctrl = Mock(
        get_library_item=AsyncMock(return_value=full_item),
        remove_item_from_library=AsyncMock(),
        check_removal_allowed=Mock(),
    )
    controller.get_controller = Mock(return_value=ctrl)  # type: ignore[method-assign]
    controller.library_edit_supported = Mock(return_value=True)  # type: ignore[method-assign]
    controller.library_sync_back_enabled = Mock(return_value=True)  # type: ignore[method-assign]

    await controller.remove_item_from_library(MediaType.TRACK, "42")

    seen = controller.providers_seen  # type: ignore[attr-defined]
    seen[MINE].library_remove.assert_called_once_with("mine-42", MediaType.TRACK)
    assert THEIRS not in seen
    # the other account's mapping keeps its in_library flag: nothing was removed there
    assert [m.in_library for m in mappings] == [False, True]
    ctrl.remove_item_from_library.assert_awaited_once_with("42", True)


async def test_add_item_to_library_writes_only_to_the_own_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provider-side add goes to the own account; every mapping still reads in_library."""
    _as_user(monkeypatch, _user())
    controller = _controller(THREE_ACCOUNTS)
    mappings = [_mapping(MINE, "mine-42"), _mapping(THEIRS, "theirs-42")]
    item = Mock(provider="builtin", media_type=MediaType.TRACK, provider_mappings=mappings)
    ctrl = Mock(add_item_to_library=AsyncMock(return_value=item))
    controller.get_controller = Mock(return_value=ctrl)  # type: ignore[method-assign]
    controller.library_edit_supported = Mock(return_value=True)  # type: ignore[method-assign]
    controller.library_sync_back_enabled = Mock(return_value=True)  # type: ignore[method-assign]
    controller.mass.metadata.update_metadata = AsyncMock()  # type: ignore[method-assign]

    await controller.add_item_to_library(item)

    seen = controller.providers_seen  # type: ignore[attr-defined]
    assert seen[MINE].library_add.call_count == 1
    assert THEIRS not in seen
    assert all(m.in_library for m in mappings)


async def test_a_down_own_source_is_skipped_rather_than_served_by_a_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The user's own account being down must not let a sibling account take the write.

    ``mass.get_provider`` widens to another instance of the same domain, which is right for
    playback and wrong for a write, so the write paths resolve the exact instance instead.
    """
    _as_user(monkeypatch, _user())
    controller = _controller(THREE_ACCOUNTS, unavailable={MINE}, served_by={MINE: THEIRS})
    full_item = Mock(
        provider="library",
        item_id="42",
        media_type=MediaType.TRACK,
        provider_mappings=THREE_MAPPINGS,
    )
    controller.get_item = AsyncMock(return_value=full_item)  # type: ignore[method-assign]
    ctrl = Mock(set_favorite=AsyncMock())
    controller.get_controller = Mock(return_value=ctrl)  # type: ignore[method-assign]
    controller.library_favorites_edit_supported = Mock(return_value=True)  # type: ignore[method-assign]

    await controller.add_item_to_favorites(full_item)

    seen = controller.providers_seen  # type: ignore[attr-defined]
    assert all(not prov.set_favorite.called for prov in seen.values())
    # the library row is still the user's own, so the star is not lost
    ctrl.set_favorite.assert_awaited_once_with("42", True)
