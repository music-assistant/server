"""Authorization tests for command-specific Music Assistant target filters."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError

from music_assistant.providers.fastmcp_server.target_filters import (
    TargetKind,
    enforce_target_filters,
    filter_collection_result,
    target_rule,
)


def _user(
    *,
    players: tuple[str, ...] = ("kitchen",),
    providers: tuple[str, ...] = ("spotify--user",),
) -> SimpleNamespace:
    return SimpleNamespace(
        role="user",
        player_filter=list(players),
        provider_filter=list(providers),
    )


@pytest.mark.parametrize(
    ("command", "argument", "kind"),
    [
        ("player_queues/transfer", "source_queue_id", TargetKind.PLAYER),
        ("player_queues/transfer", "target_queue_id", TargetKind.PLAYER),
        ("players/cmd/group_many", "child_player_ids", TargetKind.PLAYERS),
        ("players/cmd/set_members", "player_ids_to_add", TargetKind.PLAYERS),
        ("players/cmd/set_members", "player_ids_to_remove", TargetKind.PLAYERS),
        ("players/create_group_player", "members", TargetKind.PLAYERS),
        (
            "audio_analysis/wave_form",
            "provider_instance_id_or_domain",
            TargetKind.MUSIC_PROVIDER,
        ),
        (
            "music/albums/get",
            "provider_instance_id_or_domain",
            TargetKind.MUSIC_PROVIDER,
        ),
    ],
)
def test_live_target_argument_variants_have_declarative_rules(
    command: str,
    argument: str,
    kind: TargetKind,
) -> None:
    """Removing a live target rule must make this parity matrix fail."""
    rule = target_rule(command, argument)

    assert rule is not None
    assert rule.kind is kind


def test_create_group_player_members_are_checked() -> None:
    """Group creation cannot pull in players outside the current user's filter."""
    with pytest.raises(ToolError, match="not permitted"):
        enforce_target_filters(
            MagicMock(),
            _user(),
            "players/create_group_player",
            {"provider": "playerprov", "name": "Kitchen", "members": ["kitchen", "bedroom"]},
        )


def test_player_sequences_are_checked_for_the_exact_command() -> None:
    """A forbidden group member must not bypass a scalar-only player check."""
    mass = MagicMock()

    with pytest.raises(ToolError, match="not permitted"):
        enforce_target_filters(
            mass,
            _user(),
            "players/cmd/group_many",
            {"target_player": "kitchen", "child_player_ids": ["bedroom"]},
        )


def test_music_provider_domain_resolves_to_filtered_instance() -> None:
    """A domain is permitted only when MA resolves it to an allowed music instance."""
    provider = SimpleNamespace(
        instance_id="spotify--user",
        domain="spotify",
        type=SimpleNamespace(value="music"),
    )
    mass = MagicMock()
    mass.get_provider.return_value = provider

    enforce_target_filters(
        mass,
        _user(),
        "music/albums/get",
        {"provider_instance_id_or_domain": "spotify"},
    )

    assert mass.get_provider.call_count >= 1


def test_music_provider_domain_cannot_alias_a_filtered_out_instance() -> None:
    """Domain lookup must compare the resolved instance id, not the submitted domain."""
    provider = SimpleNamespace(
        instance_id="spotify--other",
        domain="spotify",
        type=SimpleNamespace(value="music"),
    )
    mass = MagicMock()
    mass.get_provider.return_value = provider

    with pytest.raises(ToolError, match="not permitted"):
        enforce_target_filters(
            mass,
            _user(),
            "music/albums/get",
            {"provider_instance_id_or_domain": "spotify"},
        )


def test_unavailable_instance_does_not_alias_to_an_allowed_instance() -> None:
    """An unavailable forbidden instance must not resolve to another allowed one."""
    requested = SimpleNamespace(
        instance_id="spotify--down",
        domain="spotify",
        type=SimpleNamespace(value="music"),
    )
    allowed = SimpleNamespace(
        instance_id="spotify--user",
        domain="spotify",
        type=SimpleNamespace(value="music"),
    )

    def get_provider(key: str, return_unavailable: bool = False) -> object | None:
        if key == "spotify--down":
            return requested if return_unavailable else allowed
        if key == "spotify":
            return allowed
        return None

    mass = MagicMock()
    mass.get_provider.side_effect = get_provider

    with pytest.raises(ToolError, match="not permitted"):
        enforce_target_filters(
            mass,
            _user(),
            "music/albums/get",
            {"provider_instance_id_or_domain": "spotify--down"},
        )


def test_queue_collection_hides_queues_outside_the_player_filter() -> None:
    """Zero-argument queue listings cannot enumerate another user's queues."""
    result = filter_collection_result(
        _user(),
        "player_queues/all",
        (
            SimpleNamespace(queue_id="kitchen"),
            SimpleNamespace(queue_id="bedroom"),
        ),
    )

    assert result == (SimpleNamespace(queue_id="kitchen"),)


def test_provider_filter_is_not_applied_to_provider_management() -> None:
    """Player/core/plugin provider configuration is outside the music-provider filter."""
    mass = MagicMock()

    enforce_target_filters(
        mass,
        _user(),
        "config/providers/get",
        {"instance_id": "hass--core"},
    )

    mass.get_provider.assert_not_called()
