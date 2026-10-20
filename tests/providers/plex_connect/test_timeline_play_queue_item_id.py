"""Tests that Plex timeline reports never fabricate playQueueItemIDs."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from music_assistant.providers.plex_connect.timeline import TimelineMixin


class _TimelineHandler(TimelineMixin):
    """TimelineMixin with the host-class attributes mocked."""

    def __init__(self) -> None:
        self.provider = Mock()
        self.provider.instance_id = "plex--instance1"
        self.provider._baseurl = "http://plex.local:32400"
        self.provider._plex_server = Mock(machineIdentifier="machine1")
        self._ma_player_id = "player1"
        self.play_queue_id: str | None = "77"
        self.play_queue_version = 1
        self.play_queue_item_ids: dict[int, int] = {}


def _make_queue(current_index: int) -> SimpleNamespace:
    return SimpleNamespace(current_index=current_index)


def _build_attrs(handler: _TimelineHandler, queue: Any) -> list[str]:
    track = Mock()
    with patch(
        "music_assistant.providers.plex_connect.timeline.plex_key_for_item",
        return_value="/library/metadata/1234",
    ):
        return handler._build_timeline_attributes(
            track,
            state="playing",
            duration=180000,
            time_ms=1000,
            volume=50,
            shuffle=0,
            repeat=0,
            controllable="playPause",
            queue=queue,
        )


def test_known_play_queue_item_id_is_reported() -> None:
    """A track with a known playQueueItemID includes it in the timeline."""
    handler = _TimelineHandler()
    handler.play_queue_item_ids = {21: 900021}
    attrs = _build_attrs(handler, _make_queue(current_index=21))
    assert 'playQueueItemID="900021"' in attrs
    assert 'containerKey="/playQueues/77"' in attrs


def test_unknown_play_queue_item_id_is_omitted() -> None:
    """
    A track beyond the fetched play queue window must not fabricate an ID.

    playQueueItemIDs are server-global; a made-up value can reference an
    unrelated item (e.g. a TV episode) and corrupt the Plex play history.
    """
    handler = _TimelineHandler()
    handler.play_queue_item_ids = {i: 900000 + i for i in range(21)}
    attrs = _build_attrs(handler, _make_queue(current_index=21))
    assert not any(attr.startswith("playQueueItemID=") for attr in attrs)
    # the play queue itself is still referenced
    assert 'playQueueID="77"' in attrs


def test_no_play_queue_omits_queue_attributes() -> None:
    """Without a Plex play queue, no queue attributes are reported."""
    handler = _TimelineHandler()
    handler.play_queue_id = None
    attrs = _build_attrs(handler, _make_queue(current_index=0))
    assert not any("playQueue" in attr for attr in attrs)


def _setup_server_timeline(handler: _TimelineHandler, current_index: int) -> Mock:
    """Wire up the mocks needed by _send_timeline_to_server and return the server mock."""
    track = Mock(duration=180)
    queue = SimpleNamespace(
        current_index=current_index,
        current_item=SimpleNamespace(media_item=track),
        corrected_elapsed_time=1.0,
        state="playing",
    )
    provider = Mock()
    provider.instance_id = "plex--instance1"
    provider.mass.players.get_player.return_value = Mock()
    provider.mass.players.get_active_queue.return_value = queue
    handler.provider = provider
    handler._resolve_plex_state = Mock(return_value="playing")  # type: ignore[method-assign]
    server = Mock()
    handler.plex_server = server
    handler.headers = {}
    return server


async def test_server_timeline_omits_fabricated_item_id() -> None:
    """The server timeline report must drop queue context for unmapped tracks."""
    handler = _TimelineHandler()
    handler.play_queue_item_ids = {i: 900000 + i for i in range(21)}
    server = _setup_server_timeline(handler, current_index=21)
    with patch(
        "music_assistant.providers.plex_connect.timeline.plex_key_for_item",
        return_value="/library/metadata/1234",
    ):
        await handler._send_timeline_to_server()
    params = server.query.call_args.kwargs.get("params") or server.query.call_args[0][1]
    assert "playQueueItemID" not in params
    assert "containerKey" not in params
    assert params["ratingKey"] == "1234"


async def test_server_timeline_reports_known_item_id() -> None:
    """The server timeline report includes queue context for mapped tracks."""
    handler = _TimelineHandler()
    handler.play_queue_item_ids = {21: 900021}
    server = _setup_server_timeline(handler, current_index=21)
    with patch(
        "music_assistant.providers.plex_connect.timeline.plex_key_for_item",
        return_value="/library/metadata/1234",
    ):
        await handler._send_timeline_to_server()
    params = server.query.call_args.kwargs.get("params") or server.query.call_args[0][1]
    assert params["playQueueItemID"] == "900021"
    assert params["containerKey"] == "/playQueues/77"
