"""Tests for scoping favorite writes to the acting user's provider filter.

A library item can map to several instances of the SAME server, one per account on it, which is
the normal setup for a household sharing one music library. Writing a favorite to every mapping
then writes one person's choice into everybody else's account.

Measured on a live install on 2026-09-05: one user starring one track produced three rows in the
server's own annotation table, 15 milliseconds apart, in provider-mapping order. A loop, not three
people.
"""

from __future__ import annotations

from unittest.mock import Mock

from music_assistant_models.enums import ProviderType
from music_assistant_models.media_items import ProviderMapping

from music_assistant.controllers.music import MusicController

INSTANCE_MINE = "opensubsonic--mine"
INSTANCE_OTHER = "opensubsonic--other"
INSTANCE_THIRD = "opensubsonic--third"
BUILTIN = "builtin"


def _mapping(instance_id: str, item_id: str) -> ProviderMapping:
    return ProviderMapping(
        item_id=item_id,
        provider_domain="opensubsonic",
        provider_instance=instance_id,
    )


def _controller(user_filter: list[str] | None, music_instances: set[str]) -> MusicController:
    """A controller stub whose only live parts are the current user and the provider registry."""
    controller = MusicController.__new__(MusicController)
    controller.mass = Mock()

    def _get_provider(instance_id: str, **_kwargs: object) -> Mock | None:
        if instance_id in music_instances:
            prov = Mock()
            prov.type = ProviderType.MUSIC
            prov.instance_id = instance_id
            return prov
        if instance_id == BUILTIN:
            prov = Mock()
            prov.type = ProviderType.PLUGIN
            prov.instance_id = instance_id
            return prov
        return None

    controller.mass.get_provider.side_effect = _get_provider
    return controller


def _patched(monkeypatch, user_filter: list[str] | None) -> None:
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


def test_favorite_reaches_only_the_acting_users_instance(monkeypatch) -> None:
    """The defect this fixes: one star must not land in three accounts."""
    _patched(monkeypatch, [BUILTIN, INSTANCE_MINE])
    controller = _controller([BUILTIN, INSTANCE_MINE], MUSIC)

    targets = controller._favorite_target_mappings(MAPPINGS)

    assert [m.provider_instance for m in targets] == [INSTANCE_MINE]


def test_unfiltered_user_keeps_the_previous_behaviour(monkeypatch) -> None:
    """An empty provider_filter expresses no preference, so nothing changes for it."""
    _patched(monkeypatch, [])
    controller = _controller([], MUSIC)

    targets = controller._favorite_target_mappings(MAPPINGS)

    assert [m.provider_instance for m in targets] == [
        INSTANCE_MINE,
        INSTANCE_OTHER,
        INSTANCE_THIRD,
    ]


def test_no_current_user_keeps_the_previous_behaviour(monkeypatch) -> None:
    """A write with no user context (internal call) must not be narrowed."""
    _patched(monkeypatch, None)
    controller = _controller(None, MUSIC)

    targets = controller._favorite_target_mappings(MAPPINGS)

    assert len(targets) == 3


def test_filter_naming_no_music_provider_expresses_no_preference(monkeypatch) -> None:
    """A built-in-only filter is not a statement about which account owns a favorite.

    Same rule as the Subsonic scrobbler: a filter that names no instance of the relevant kind
    carries no preference, so narrowing on it would remove behaviour rather than fix a leak.
    """
    _patched(monkeypatch, [BUILTIN])
    controller = _controller([BUILTIN], MUSIC)

    targets = controller._favorite_target_mappings(MAPPINGS)

    assert len(targets) == 3


def test_filter_is_an_allowlist_when_the_users_instance_holds_nothing(monkeypatch) -> None:
    """No mapping of the user's own means NOTHING is forwarded, not somebody else's account.

    This is the review finding from the scrobbler pull request applied one layer up: falling
    through to another instance is exactly the disclosure the filter exists to prevent. The library
    favorite is still set, so the user keeps seeing their own choice.
    """
    _patched(monkeypatch, [INSTANCE_MINE])
    controller = _controller([INSTANCE_MINE], MUSIC)

    targets = controller._favorite_target_mappings(
        [_mapping(INSTANCE_OTHER, "other-42"), _mapping(INSTANCE_THIRD, "third-42")]
    )

    assert targets == []


def test_multiple_own_instances_are_all_reached(monkeypatch) -> None:
    """A user allowed two instances gets the favorite on both: the filter is the whole answer."""
    _patched(monkeypatch, [INSTANCE_MINE, INSTANCE_THIRD])
    controller = _controller([INSTANCE_MINE, INSTANCE_THIRD], MUSIC)

    targets = controller._favorite_target_mappings(MAPPINGS)

    assert [m.provider_instance for m in targets] == [INSTANCE_MINE, INSTANCE_THIRD]
