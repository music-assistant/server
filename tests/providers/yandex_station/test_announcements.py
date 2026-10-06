"""Tests for Yandex Station announcements."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest import mock

import pytest

from music_assistant.providers.yandex_station.player import YandexStationPlayer
from music_assistant.providers.yandex_station.protobuf import loads

if TYPE_CHECKING:
    from music_assistant import MusicAssistant
    from music_assistant.models.player import PlayerMedia
    from music_assistant.providers.yandex_station.glagol import YandexGlagol


async def test_announcement_waits_for_resolved_duration() -> None:
    """Wait for the effective stream duration instead of incomplete media metadata."""
    player = YandexStationPlayer.__new__(YandexStationPlayer)
    player._player_id = "test_player"
    vars(player)["_audio_client"] = False
    player._attr_volume_level = 20
    player.glagol = cast(
        "YandexGlagol",
        SimpleNamespace(send=mock.AsyncMock(return_value={"status": "SUCCESS"})),
    )
    duration_resolver = mock.AsyncMock(return_value=4)
    player.mass = cast(
        "MusicAssistant",
        SimpleNamespace(streams=SimpleNamespace(get_announcement_duration=duration_resolver)),
    )
    announcement = cast(
        "PlayerMedia", SimpleNamespace(uri="http://ma.local/announcement.mp3", duration=1)
    )

    with mock.patch(
        "music_assistant.providers.yandex_station.player.asyncio.sleep",
        new=mock.AsyncMock(),
    ) as sleep:
        await player.play_announcement(announcement)

    duration_resolver.assert_awaited_once_with(announcement)
    sleep.assert_awaited_once_with(5)


async def test_announcement_without_known_duration_waits_for_timeout() -> None:
    """An unknown announcement duration falls back to the bounded wait."""
    player = YandexStationPlayer.__new__(YandexStationPlayer)
    player._player_id = "test_player"
    vars(player)["_audio_client"] = False
    player._attr_volume_level = 20
    player.glagol = cast(
        "YandexGlagol",
        SimpleNamespace(send=mock.AsyncMock(return_value={"status": "SUCCESS"})),
    )
    player.mass = cast(
        "MusicAssistant",
        SimpleNamespace(
            streams=SimpleNamespace(get_announcement_duration=mock.AsyncMock(return_value=None))
        ),
    )
    announcement = cast(
        "PlayerMedia", SimpleNamespace(uri="http://ma.local/announcement.mp3", duration=0)
    )

    with mock.patch(
        "music_assistant.providers.yandex_station.player.asyncio.sleep",
        new=mock.AsyncMock(),
    ) as sleep:
        await player.play_announcement(announcement)

    sleep.assert_awaited_once_with(30)


async def test_announcement_restores_volume_after_failure() -> None:
    """A failed announcement still restores the volume it changed."""
    player = YandexStationPlayer.__new__(YandexStationPlayer)
    player._player_id = "test_player"
    vars(player)["_audio_client"] = False
    player._attr_volume_level = 20
    player.glagol = cast(
        "YandexGlagol",
        SimpleNamespace(send=mock.AsyncMock(return_value={"status": "SUCCESS"})),
    )
    player.mass = cast(
        "MusicAssistant",
        SimpleNamespace(
            streams=SimpleNamespace(
                get_announcement_duration=mock.AsyncMock(side_effect=RuntimeError("boom"))
            )
        ),
    )
    volume_set = mock.AsyncMock()
    object.__setattr__(player, "volume_set", volume_set)
    announcement = cast(
        "PlayerMedia", SimpleNamespace(uri="http://ma.local/announcement.mp3", duration=0)
    )

    with pytest.raises(RuntimeError, match="boom"):
        await player.play_announcement(announcement, volume_level=60)

    assert volume_set.await_args_list == [mock.call(60), mock.call(20)]


async def test_announcement_uses_audio_play_on_current_firmware() -> None:
    """Announcements share the feature-gated URL playback path."""
    player = YandexStationPlayer.__new__(YandexStationPlayer)
    player._player_id = "test_player"
    vars(player)["_audio_client"] = True
    player._attr_volume_level = 20
    send = mock.AsyncMock(return_value={"status": "SUCCESS"})
    player.glagol = cast("YandexGlagol", SimpleNamespace(send=send))
    player.mass = cast(
        "MusicAssistant",
        SimpleNamespace(
            streams=SimpleNamespace(get_announcement_duration=mock.AsyncMock(return_value=0))
        ),
    )
    announcement = cast(
        "PlayerMedia",
        SimpleNamespace(
            uri="http://ma.local/announcement.flac",
            duration=0,
            title="Announcement",
            artist=None,
            image_url=None,
        ),
    )

    with mock.patch(
        "music_assistant.providers.yandex_station.player.asyncio.sleep",
        new=mock.AsyncMock(),
    ):
        await player.play_announcement(announcement)

    assert send.await_args is not None
    command = send.await_args.args[0]
    decoded = loads(base64.b64decode(command["data"]))
    name = decoded[1]
    payload = decoded[2]
    assert isinstance(name, bytes)
    assert isinstance(payload, bytes)
    assert name.decode() == "audio_play"
    assert json.loads(payload)["stream"] == {
        "url": "http://ma.local/announcement.flac",
        "format": "MP3",
        "type": "Track",
        "offset_ms": 0,
    }
