"""Tests for finding the connected Sendspin clients again."""

from __future__ import annotations

from unittest.mock import MagicMock

from music_assistant.providers.sendspin.provider import SendspinProvider


async def test_connected_client_without_player_is_added_again() -> None:
    """A connected client whose player was removed is added again, a registered one is not."""
    provider = SendspinProvider.__new__(SendspinProvider)
    provider.mass = MagicMock()
    provider.mass.players.get_player.side_effect = {"registered": MagicMock()}.get
    provider.server_api = MagicMock(
        connected_clients=[MagicMock(client_id="registered"), MagicMock(client_id="removed")]
    )
    provider._client_event_versions = {}
    provider._client_event_task_counts = {}
    added: list[tuple[str, int]] = []
    provider._handle_client_added = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda client_id, version: added.append((client_id, version))
    )

    await provider.discover_players()

    assert added == [("removed", 1)]
    assert provider.mass.create_task.call_count == 1
