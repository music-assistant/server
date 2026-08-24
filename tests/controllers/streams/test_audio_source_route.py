"""
Tests for the stream route a live source playing on a player is served from.

The url carries the session it was built for, so a renderer that reconnects after the
player moved on is turned away rather than being handed whatever is playing now. The
same holds for the direct-PCM consumers, which never reach the route at all.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import AudioError
from music_assistant_models.media_items import AudioFormat, AudioSource

from music_assistant.controllers.players.audio_sources import AudioSourceSession
from music_assistant.controllers.streams import StreamsController
from music_assistant.models.player import PlayerMedia
from music_assistant.models.plugin import PluginProvider

OWNER_ID = "player_1"
CONSUMER_ID = "spb_bridge_1"
INSTANCE_ID = "spotify_connect--abc"


def _session(player_id: str = OWNER_ID) -> AudioSourceSession:
    return AudioSourceSession(
        player_id=player_id,
        source=AudioSource(
            item_id="main", provider=INSTANCE_ID, name="Spotify Connect", provider_mappings=set()
        ),
        provider_instance_id=INSTANCE_ID,
    )


def _controller(session: AudioSourceSession | None) -> tuple[Any, MagicMock, MagicMock]:
    """Build a bare streams controller whose player controller holds ``session``."""
    ctrl = StreamsController.__new__(StreamsController)
    ctrl.mass = MagicMock()
    ctrl.logger = MagicMock()
    # a truthy MagicMock here would send _log_request down the verbose path
    ctrl.logger.isEnabledFor = MagicMock(return_value=False)
    provider = MagicMock(spec=PluginProvider)
    provider.instance_id = INSTANCE_ID
    provider.on_source_selected = AsyncMock()
    provider.on_source_unselected = AsyncMock()
    ctrl.mass.get_provider = MagicMock(return_value=provider)
    ctrl.mass.players.get_audio_source_session = MagicMock(
        return_value=session, side_effect=lambda pid: session if pid == OWNER_ID else None
    )
    player = MagicMock()
    player.player_id = CONSUMER_ID
    ctrl.mass.players.get_player = MagicMock(return_value=player)
    ctrl.mass.players.deselect_source = AsyncMock()
    return ctrl, provider, player


def _request(*, session_id: str, source_player_id: str = OWNER_ID) -> Any:
    return SimpleNamespace(
        method="GET",
        match_info={
            "session_id": session_id,
            "source_player_id": source_player_id,
            "player_id": CONSUMER_ID,
        },
        # the request logger reads these off every request it is handed
        path=f"/source/{session_id}/{source_player_id}/{CONSUMER_ID}.flac",
        remote="10.0.0.5",
        version=SimpleNamespace(major=1, minor=1),
        headers={},
    )


def test_the_url_resolves_to_the_session_it_names() -> None:
    """A url carrying the live session's own token resolves to it."""
    session = _session()
    ctrl, provider, player = _controller(session)

    resolved, resolved_player, resolved_prov = ctrl._resolve_audio_source_request(
        _request(session_id=session.playback_session_id)
    )

    assert resolved is session
    assert resolved_player is player
    assert resolved_prov is provider


def test_a_url_from_a_superseded_session_is_turned_away() -> None:
    """
    A renderer reconnecting with a stale token gets a 404, not the current source.

    The token is what separates the session the url was built for from whatever the
    player happens to be playing now.
    """
    ctrl, _provider, _player = _controller(_session())

    with pytest.raises(web.HTTPNotFound):
        ctrl._resolve_audio_source_request(_request(session_id="a-token-from-before"))


def test_a_url_for_a_player_playing_nothing_is_turned_away() -> None:
    """Without a session there is nothing to serve."""
    ctrl, _provider, _player = _controller(None)

    with pytest.raises(web.HTTPNotFound):
        ctrl._resolve_audio_source_request(_request(session_id="anything"))


def test_an_unknown_consuming_player_is_turned_away() -> None:
    """The url also names who is consuming, which has to exist."""
    session = _session()
    ctrl, _provider, _player = _controller(session)
    ctrl.mass.players.get_player = MagicMock(return_value=None)

    with pytest.raises(web.HTTPNotFound):
        ctrl._resolve_audio_source_request(_request(session_id=session.playback_session_id))


async def test_a_head_probe_does_not_trigger_the_plugin() -> None:
    """
    A renderer probing with HEAD must not fire the selection side effects.

    on_source_selected stops the previous player and can redirect a disallowed
    switch — none of which a probe should cause.
    """
    session = _session()
    ctrl, provider, _player = _controller(session)
    ctrl._serve_audio_source_head = AsyncMock(return_value="head-response")
    request = _request(session_id=session.playback_session_id)
    request.method = "HEAD"

    result = await ctrl.serve_audio_source_stream(request)

    assert result == "head-response"
    provider.on_source_selected.assert_not_awaited()


def test_a_direct_pcm_request_from_a_superseded_session_is_refused() -> None:
    """
    The PCM consumers are held to the same token as the url renderers.

    They resolve the session from the player rather than a url, so without this a
    stale request would silently attach to whichever source is playing now.
    """
    session = _session()
    ctrl, _provider, _player = _controller(session)

    with pytest.raises(AudioError, match="Unknown"):
        ctrl.get_stream(
            PlayerMedia(
                uri="x://audio_source/main",
                media_type=MediaType.AUDIO_SOURCE,
                source_id=OWNER_ID,
                queue_session_id="a-token-from-before",
            ),
            AudioFormat(),
            player_id=CONSUMER_ID,
        )


def test_a_direct_pcm_request_carrying_the_live_token_is_served() -> None:
    """A consumer naming the session that is playing gets its stream."""
    session = _session()
    ctrl, _provider, _player = _controller(session)
    ctrl._get_audio_source_session_stream = MagicMock(return_value="pcm-stream")

    result = ctrl.get_stream(
        PlayerMedia(
            uri="x://audio_source/main",
            media_type=MediaType.AUDIO_SOURCE,
            source_id=OWNER_ID,
            queue_session_id=session.playback_session_id,
        ),
        AudioFormat(),
        player_id=CONSUMER_ID,
    )

    assert result == "pcm-stream"
    ctrl._get_audio_source_session_stream.assert_called_once_with(
        session, AudioFormat(), CONSUMER_ID
    )


async def test_a_plugin_refusing_the_stream_takes_the_source_off_the_player() -> None:
    """
    A source that never starts is released, not left published.

    The play command that pointed the renderer here has already returned, so nothing
    else clears the session — and a plugin refusing the stream is a designed path
    (Ynison raises from the hook when it redirects to its configured target).
    """
    session = _session()
    ctrl, provider, _player = _controller(session)
    provider.on_source_selected = AsyncMock(side_effect=RuntimeError("switching disabled"))

    with pytest.raises(web.HTTPNotFound):
        await ctrl.serve_audio_source_stream(_request(session_id=session.playback_session_id))

    ctrl.mass.players.deselect_source.assert_awaited_once_with(
        OWNER_ID,
        provider_instance_id=session.provider_instance_id,
        source_id=session.source_id,
        playback_session_id=session.playback_session_id,
    )


async def test_failing_stream_details_also_takes_the_source_off_the_player() -> None:
    """The same holds when the plugin claims the source but cannot describe its stream."""
    session = _session()
    ctrl, provider, _player = _controller(session)
    provider.get_stream_details = AsyncMock(side_effect=OSError("daemon gone"))

    with pytest.raises(web.HTTPNotFound):
        await ctrl.serve_audio_source_stream(_request(session_id=session.playback_session_id))

    ctrl.mass.players.deselect_source.assert_awaited_once_with(
        OWNER_ID,
        provider_instance_id=session.provider_instance_id,
        source_id=session.source_id,
        playback_session_id=session.playback_session_id,
    )


async def test_a_session_already_superseded_is_not_released() -> None:
    """A newer session on the player is not this request's to take away."""
    session = _session()
    ctrl, provider, _player = _controller(session)
    provider.on_source_selected = AsyncMock(side_effect=RuntimeError("nope"))
    # the player moved on to a different session while this request was setting up
    ctrl.mass.players.get_audio_source_session = MagicMock(return_value=_session())

    with pytest.raises(web.HTTPNotFound):
        await ctrl.serve_audio_source_stream(_request(session_id=session.playback_session_id))

    ctrl.mass.players.deselect_source.assert_not_awaited()


async def test_a_reselected_session_is_not_released_after_setup_failure() -> None:
    """A failed request cannot release a newer selection using the same session object."""
    session = _session()
    ctrl, provider, _player = _controller(session)

    async def supersede_session(*_args: Any) -> None:
        session.playback_session_id = "replacement-session"
        raise RuntimeError("nope")

    provider.on_source_selected = AsyncMock(side_effect=supersede_session)

    with pytest.raises(web.HTTPNotFound):
        await ctrl.serve_audio_source_stream(_request(session_id=session.playback_session_id))

    ctrl.mass.players.deselect_source.assert_not_awaited()
