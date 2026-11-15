"""The executor of a group plan: failures, cancellation and how calls are made."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.errors import PlayerCommandFailed, PlayerUnavailableError

from music_assistant.controllers.players import grouping
from music_assistant.controllers.players.grouping_plan import (
    ChangeKind,
    Detach,
    Failure,
    GroupChange,
    GroupingSnapshot,
    GroupPlan,
    GroupProgress,
    MemberChange,
    MemberLists,
    MemberStage,
    Refuse,
    SetMembers,
    Ungroup,
)

CHANGE = GroupChange(kind=ChangeKind.MEMBERS, target_id="leader")
EMPTY = GroupingSnapshot(players={}, protocols={})


class _Players:
    """What the executor uses of the players controller, every call recorded."""

    def __init__(self) -> None:
        """Initialize empty registries and recording entries."""
        self._players: dict[str, Any] = {}
        self._protocols: dict[str, Any] = {}
        self._legacy_ids: dict[str, str] = {}
        self.mass = MagicMock()
        self.mass.config.get = MagicMock(return_value={})
        self.logger = MagicMock()
        self.translation_owner = "players"
        self.protocols: dict[str, Any] = {}
        self.player_objects: dict[str, Any] = {}
        self._handle_set_members = AsyncMock()
        self.waits: list[tuple[str, float]] = []
        self._detach_member = AsyncMock()
        self.cmd_set_members = AsyncMock()

    def get_protocol(self, protocol_id: str) -> Any:
        """Return a protocol by id."""
        return self.protocols.get(protocol_id)

    def get_player(self, player_id: str) -> Any:
        """Return a player by id, creating a stand-in on first use."""
        return self.player_objects.setdefault(player_id, MagicMock(player_id=player_id))

    @contextlib.asynccontextmanager
    async def wait_for_player_update(
        self, player_id: str, timeout: float = 5
    ) -> AsyncIterator[None]:
        """Run the body; the wait is recorded, no update is waited for."""
        self.waits.append((player_id, timeout))
        yield


def _protocol(token: object | None = None) -> MagicMock:
    """Return a protocol whose calls are recorded."""
    protocol = MagicMock()
    protocol.set_members = AsyncMock()
    protocol.stop = AsyncMock()
    protocol.ungroup = AsyncMock()
    protocol.on_group_content_takeover = AsyncMock(return_value=token)
    protocol.on_group_content_takeover_aborted = AsyncMock()
    return protocol


@contextlib.contextmanager
def _plans(*plans: GroupPlan) -> Iterator[MagicMock]:
    """Let the executor get the given plans, one per call, recording what it passed."""
    scripted = MagicMock(side_effect=list(plans))
    with (
        patch.object(grouping, "plan_group_change", scripted),
        patch.object(grouping, "grouping_snapshot", MagicMock(return_value=EMPTY)),
    ):
        yield scripted


async def test_a_failing_release_is_warned_and_the_change_goes_on() -> None:
    """A release with WARN logs its line, the next step runs."""
    players = _Players()
    players._detach_member.side_effect = PlayerCommandFailed("no")
    plan = GroupPlan(
        (
            Detach(
                member_id="m",
                wait_for_update=True,
                failure=Failure.WARN,
                message="w %s",
                args=("m",),
            ),
            MemberChange(target_id="leader", add=("m",)),
        )
    )
    with _plans(plan):
        await grouping.apply_group_change(players, CHANGE)  # type: ignore[arg-type]
    players.logger.warning.assert_called_once_with("w %s", "m")
    players._handle_set_members.assert_awaited_once()


@pytest.mark.parametrize(
    "step",
    [
        Detach(member_id="m", wait_for_update=True, failure=Failure.WARN, message="w"),
        MemberChange(target_id="m", remove=("x",), failure=Failure.LEAVE_OUT, message="l"),
        Ungroup(protocol_id="airplay", failure=Failure.UNAVAILABLE, message="u"),
    ],
)
async def test_a_cancellation_propagates(step: Any) -> None:
    """A cancellation is never a warning, a member left out or an unavailable player."""
    players = _Players()
    players._detach_member.side_effect = asyncio.CancelledError
    players._handle_set_members.side_effect = asyncio.CancelledError
    players.protocols["airplay"] = _protocol()
    players.protocols["airplay"].ungroup.side_effect = asyncio.CancelledError
    with _plans(GroupPlan((step,))), pytest.raises(asyncio.CancelledError):
        await grouping.apply_group_change(players, CHANGE)  # type: ignore[arg-type]
    players.logger.warning.assert_not_called()


async def test_a_failed_dissolve_continues_from_the_progress_that_leaves_the_member_out() -> None:
    """LEAVE_OUT warns and plans on from progress_left_out."""
    players = _Players()
    players._handle_set_members.side_effect = PlayerCommandFailed("no")
    dissolved = GroupProgress(next_add=0, stage=MemberStage.DISSOLVED)
    left_out = GroupProgress(next_add=1)
    first = GroupPlan(
        (MemberChange(target_id="m", remove=("x",), failure=Failure.LEAVE_OUT, message="l"),),
        progress=dissolved,
        progress_left_out=left_out,
    )
    with _plans(first, GroupPlan(())) as scripted:
        await grouping.apply_group_change(players, CHANGE)  # type: ignore[arg-type]
    assert scripted.call_args_list[1].args[2] == left_out
    players.logger.warning.assert_called_once_with("l")


async def test_a_cleanup_failure_is_logged_at_debug_and_other_errors_propagate() -> None:
    """The cleanup's known errors are debug lines, others propagate."""
    players = _Players()
    players._handle_set_members.side_effect = PlayerUnavailableError("gone")
    step = MemberChange(
        target_id="leader", remove=("m",), failure=Failure.DEBUG, message="d %s: %s", args=("m",)
    )
    with _plans(GroupPlan((step,))):
        await grouping.apply_group_change(players, CHANGE)  # type: ignore[arg-type]
    assert players.logger.debug.call_args_list[-1].args[:2] == ("d %s: %s", "m")
    players._handle_set_members.side_effect = ValueError("bug")
    with _plans(GroupPlan((step,))), pytest.raises(ValueError, match="bug"):
        await grouping.apply_group_change(players, CHANGE)  # type: ignore[arg-type]


async def test_a_failing_own_side_ungroup_is_an_unavailable_player() -> None:
    """The error of the ungroup is chained to a PlayerUnavailableError."""
    players = _Players()
    players.protocols["airplay"] = _protocol()
    players.protocols["airplay"].ungroup.side_effect = OSError("down")
    step = Ungroup(protocol_id="airplay", failure=Failure.UNAVAILABLE, message="could not leave")
    with (
        _plans(GroupPlan((step,))),
        pytest.raises(PlayerUnavailableError, match="could not leave") as err,
    ):
        await grouping.apply_group_change(players, CHANGE)  # type: ignore[arg-type]
    assert isinstance(err.value.__cause__, OSError)


@pytest.mark.parametrize(
    ("step", "expected"),
    [
        (
            SetMembers(protocol_id="p", lists=MemberLists.AS_THEY_ARE),
            {"player_ids_to_add": [], "player_ids_to_remove": []},
        ),
        (
            SetMembers(protocol_id="p", remove=("a",), lists=MemberLists.REMOVE_ONLY),
            {"player_ids_to_remove": ["a"]},
        ),
    ],
)
async def test_set_members_passes_its_lists_in_the_shape_the_step_names(
    step: SetMembers, expected: dict[str, Any]
) -> None:
    """The call shapes: the lists as they are, or the removals only."""
    players = _Players()
    protocol = players.protocols["p"] = _protocol()
    with _plans(GroupPlan((step,))):
        await grouping.apply_group_change(players, CHANGE)  # type: ignore[arg-type]
    protocol.set_members.assert_awaited_once_with(**expected)


async def test_a_nested_change_passes_only_the_lists_it_has() -> None:
    """A nested change is called with the keyword of each list it carries."""
    players = _Players()
    with _plans(GroupPlan((MemberChange(target_id="leader", remove=("m",)),))):
        await grouping.apply_group_change(players, CHANGE)  # type: ignore[arg-type]
    players._handle_set_members.assert_awaited_once_with(
        players.get_player("leader"), player_ids_to_remove=["m"]
    )


async def test_a_held_protocol_is_called_after_it_was_replaced() -> None:
    """The protocol object a change holds is the one it calls, also after it was replaced."""
    players = _Players()
    held = _protocol()
    held.protocol_id = "old"
    # replaced by a new object under the same id once the change holds it
    players.protocols["old"] = _protocol()
    with _plans(GroupPlan((Ungroup(protocol_id="old"),))):
        await grouping.apply_group_change(players, CHANGE, protocol=held)  # type: ignore[arg-type]
    held.ungroup.assert_awaited_once()


async def test_a_refusal_raises_with_its_translation() -> None:
    """Refuse: the error carries its translation key and args."""
    players = _Players()
    refusal = Refuse(
        error=PlayerCommandFailed,
        message="no",
        translation_key="member_cannot_follow_protocol",
        translation_args=("a", "b"),
    )
    with _plans(GroupPlan((refusal,))), pytest.raises(PlayerCommandFailed) as err:
        await grouping.apply_group_change(players, CHANGE)  # type: ignore[arg-type]
    assert err.value.translation_key == "member_cannot_follow_protocol"


async def test_a_release_waits_up_to_5_s_for_the_member_to_report_it() -> None:
    """A release from another leader is waited for, up to 5 seconds."""
    players = _Players()
    release = Detach(member_id="m", wait_for_update=True, failure=Failure.WARN, message="w")
    with _plans(GroupPlan((release,))):
        await grouping.apply_group_change(players, CHANGE)  # type: ignore[arg-type]
    assert players.waits == [("m", 5)]
    players._detach_member.assert_awaited_once_with(players.get_player("m"))


async def test_the_change_calls_the_target_object_it_holds() -> None:
    """The target a change is about is the object the entry passed, not a lookup by id."""
    players = _Players()
    held = MagicMock(player_id="leader")
    with _plans(GroupPlan((MemberChange(target_id="leader", add=("m",)),))):
        await grouping.apply_group_change(players, CHANGE, held)  # type: ignore[arg-type]
    players._handle_set_members.assert_awaited_once_with(held, player_ids_to_add=["m"])
