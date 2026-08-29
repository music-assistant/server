"""Authorization tests for command-specific Music Assistant target filters."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from music_assistant_models.errors import InsufficientPermissions

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
    with pytest.raises(InsufficientPermissions, match="not permitted"):
        enforce_target_filters(
            MagicMock(),
            _user(),
            "players/create_group_player",
            {"provider": "playerprov", "name": "Kitchen", "members": ["kitchen", "bedroom"]},
        )


def test_player_sequences_are_checked_for_the_exact_command() -> None:
    """A forbidden group member must not bypass a scalar-only player check."""
    mass = MagicMock()

    with pytest.raises(InsufficientPermissions, match="not permitted"):
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

    with pytest.raises(InsufficientPermissions, match="not permitted"):
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

    with pytest.raises(InsufficientPermissions, match="not permitted"):
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


def test_search_collection_hides_items_outside_the_provider_filter() -> None:
    """Unscoped search cannot enumerate another user's providers."""
    result = filter_collection_result(
        _user(),
        "music/search",
        {
            "tracks": [
                SimpleNamespace(provider_instance_id="spotify--user"),
                SimpleNamespace(provider_instance_id="qobuz--other"),
            ],
            "albums": [SimpleNamespace(provider_instance_id="qobuz--other")],
        },
    )

    assert result == {
        "tracks": [SimpleNamespace(provider_instance_id="spotify--user")],
        "albums": [],
    }


def test_library_collection_reads_provider_mappings() -> None:
    """Library rows identify their provider through mappings, not only top-level fields."""
    result = filter_collection_result(
        _user(),
        "music/albums/library_items",
        (
            SimpleNamespace(
                provider_mappings=(SimpleNamespace(provider_instance_id="spotify--user"),)
            ),
            SimpleNamespace(
                provider_mappings=(SimpleNamespace(provider_instance_id="qobuz--other"),)
            ),
        ),
    )

    assert result == (
        SimpleNamespace(provider_mappings=(SimpleNamespace(provider_instance_id="spotify--user"),)),
    )


def test_library_collection_hides_items_outside_the_provider_filter() -> None:
    """Zero-argument library listings cannot enumerate another user's providers."""
    result = filter_collection_result(
        _user(),
        "music/tracks/library_items",
        (
            SimpleNamespace(provider_instance_id="spotify--user"),
            SimpleNamespace(provider_instance_id="qobuz--other"),
        ),
    )

    assert result == (SimpleNamespace(provider_instance_id="spotify--user"),)


def test_player_collection_hides_players_outside_the_player_filter() -> None:
    """Zero-argument player listings cannot enumerate another user's players."""
    result = filter_collection_result(
        _user(),
        "players/all",
        (
            SimpleNamespace(player_id="kitchen"),
            SimpleNamespace(player_id="bedroom"),
        ),
    )

    assert result == (SimpleNamespace(player_id="kitchen"),)


def test_undeclared_listing_is_not_filtered() -> None:
    """A listing without a collection-visibility declaration is returned intact."""
    rows = (
        SimpleNamespace(player_id="kitchen"),
        SimpleNamespace(player_id="bedroom"),
    )

    assert filter_collection_result(_user(), "players/get", rows) == rows


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


@pytest.mark.parametrize(
    ("command", "argument", "value"),
    [
        ("music/item_by_uri", "uri", "qobuz--other://track/secret"),
        ("music/verify_item_uri", "uri", "qobuz--other://track/secret"),
        ("music/item_by_uri", "uri", "spotify:track:secret"),
        ("music/item_by_uri", "uri", "https://open.qobuz.com/album/secret"),
        ("music/browse", "path", "qobuz--other://albums"),
        ("music/library/add_item", "item", {"provider": "qobuz--other"}),
        (
            "music/mark_played",
            "media_item",
            SimpleNamespace(
                provider="library",
                provider_mappings=[SimpleNamespace(provider_instance="qobuz--other")],
            ),
        ),
        ("music/add_provider_mapping", "mapping", {"provider_instance": "qobuz--other"}),
        (
            "music/playlists/add_playlist_tracks",
            "uris",
            ["spotify--user://track/ok", "qobuz--other://track/secret"],
        ),
        (
            "player_queues/play_media",
            "media",
            [SimpleNamespace(uri="qobuz--other://album/secret")],
        ),
        ("player_queues/play_media", "start_item", "qobuz--other://track/secret"),
        ("metadata/update_metadata", "item", "qobuz--other://track/secret"),
        (
            "metadata/get_track_lyrics",
            "track",
            SimpleNamespace(provider="qobuz--other"),
        ),
    ],
)
def test_embedded_provider_references_are_denied(
    command: str, argument: str, value: object
) -> None:
    """Declared URI and media shapes cannot bypass the provider filter."""
    with pytest.raises(InsufficientPermissions, match="not permitted"):
        enforce_target_filters(MagicMock(), _user(), command, {argument: value})


def test_embedded_unavailable_instance_is_resolved_exactly() -> None:
    """An unavailable URI instance cannot alias to an allowed instance of its domain."""
    unavailable = SimpleNamespace(instance_id="spotify--down", type=SimpleNamespace(value="music"))
    allowed = SimpleNamespace(instance_id="spotify--user", type=SimpleNamespace(value="music"))
    mass = MagicMock()
    mass.get_provider.side_effect = lambda key, return_unavailable=False: (
        unavailable if key == "spotify--down" and return_unavailable else allowed
    )

    with pytest.raises(InsufficientPermissions, match="not permitted"):
        enforce_target_filters(
            mass,
            _user(),
            "music/item_by_uri",
            {"uri": "spotify--down://track/secret"},
        )


def test_plain_stream_url_remains_an_internal_builtin_reference() -> None:
    """Ordinary web streams keep the core URI parser's builtin-provider semantics."""
    enforce_target_filters(
        MagicMock(),
        _user(),
        "player_queues/play_media",
        {"media": "https://radio.example/stream"},
    )


def test_library_single_item_result_requires_an_allowed_mapping() -> None:
    """A library lookup cannot reveal an item mapped only to filtered providers."""
    blocked = SimpleNamespace(
        provider="library",
        provider_mappings=[SimpleNamespace(provider_instance="qobuz--other")],
    )
    allowed = SimpleNamespace(
        provider="library",
        provider_mappings=[SimpleNamespace(provider_instance="spotify--user")],
    )

    assert filter_collection_result(_user(), "music/item_by_uri", blocked) is None
    assert filter_collection_result(_user(), "music/item_by_uri", allowed) is allowed
