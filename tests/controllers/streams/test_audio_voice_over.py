"""Tests for mixing a plugin's voice-over into the start of the next item in StreamsAudio."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import AudioError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.models.plugin import PluginProvider, VoiceOver

_PCM_FORMAT = AudioFormat(sample_rate=44100, bit_depth=16, channels=2)
_MUSIC_CHUNKS = [b"chunk1", b"chunk2"]
_BREAK_ID = "break_item"
_TRACK_ID = "track_item"
_PLUGIN_ID = "plugin--test"
_MIXER = "music_assistant.controllers.streams.audio.get_ffmpeg_voice_over_stream"


class _Plugin(PluginProvider):
    """A plugin that hands out one voice-over and records how each is settled."""

    def __init__(self, voice_over: VoiceOver | None) -> None:
        """Initialize the stand-in without the provider machinery."""
        self.voice_over = voice_over
        self.asked: list[tuple[str, str]] = []
        self.ended: list[tuple[str, bool]] = []

    async def get_voice_over(
        self, streamdetails: StreamDetails, next_item: QueueItem
    ) -> VoiceOver | None:
        self.asked.append((streamdetails.item_id, next_item.queue_item_id))
        return self.voice_over

    async def on_voice_over_ended(self, streamdetails: StreamDetails, aired: bool) -> None:
        self.ended.append((streamdetails.item_id, aired))


def _break_item(provider: str = _PLUGIN_ID) -> QueueItem:
    item = QueueItem(queue_id="queue", queue_item_id=_BREAK_ID, name="Break", duration=20)
    item.streamdetails = StreamDetails(
        provider=provider,
        item_id="clip_1",
        audio_format=AudioFormat(content_type=ContentType.MP3),
        media_type=MediaType.SOUND_EFFECT,
        stream_type=StreamType.CUSTOM,
    )
    return item


def _track_item() -> QueueItem:
    return QueueItem(queue_id="queue", queue_item_id=_TRACK_ID, name="Song", duration=200)


def _make_streams_audio(
    order: list[QueueItem], provider: Any, last_served: str | None = _BREAK_ID
) -> StreamsAudio:
    """Build a StreamsAudio around one queue holding the given items."""
    mass = MagicMock()
    by_id = {item.queue_item_id: item for item in order}
    queues = mass.player_queues
    queues.get_item = MagicMock(side_effect=lambda _q, item_id: by_id.get(item_id))
    queues.queue_data_or_none = MagicMock(
        return_value=SimpleNamespace(last_served_item_id=last_served)
    )
    mass.get_provider = MagicMock(return_value=provider)
    return StreamsAudio(mass)


def _set_last_served(audio: StreamsAudio, item_id: str | None) -> None:
    queues = cast("Any", audio.mass).player_queues
    queues.queue_data_or_none.return_value = SimpleNamespace(last_served_item_id=item_id)


@pytest.fixture
def voice_file(tmp_path: Path) -> str:
    """Return the path of a voice clip that exists on disk."""
    clip_path = tmp_path / "voice.wav"
    clip_path.write_bytes(b"voice")
    return str(clip_path)


def _voice_over(path: str, **overrides: Any) -> VoiceOver:
    values: dict[str, Any] = {"path": path, "start": 0.0, "end": 11.6, "offset": 7.5}
    values.update(overrides)
    return VoiceOver(**values)


async def _music_stream() -> AsyncGenerator[bytes]:
    for chunk in _MUSIC_CHUNKS:
        yield chunk


def _fake_mixer(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the ffmpeg mixer with one that tags every chunk, and return its arguments."""
    mixer_kwargs: dict[str, Any] = {}

    async def _mixer(**kwargs: Any) -> AsyncGenerator[bytes]:
        mixer_kwargs.update(kwargs)
        async for chunk in kwargs["audio_input"]:
            yield b"mixed:" + chunk

    monkeypatch.setattr(_MIXER, _mixer)
    return mixer_kwargs


def _failing_mixer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_MIXER, MagicMock(side_effect=AudioError("clip could not be opened")))


async def _collect(stream: AsyncGenerator[bytes]) -> list[bytes]:
    return [chunk async for chunk in stream]


def _mixed(audio: StreamsAudio, track: QueueItem, **kwargs: Any) -> AsyncGenerator[bytes]:
    return audio.get_voice_over_mixed_stream(track, _music_stream(), _PCM_FORMAT, **kwargs)


async def test_item_with_nothing_served_before_it_passes_through() -> None:
    """With nothing before it there is no one to ask, and the item streams untouched."""
    track = _track_item()
    plugin = _Plugin(None)
    audio = _make_streams_audio([track], plugin, last_served=None)
    assert await _collect(_mixed(audio, track)) == _MUSIC_CHUNKS
    assert plugin.asked == []


async def test_item_after_a_non_plugin_item_passes_through() -> None:
    """Only a plugin can carry a voice-over, so any other provider is never asked."""
    before, track = _break_item("filesystem--x"), _track_item()
    audio = _make_streams_audio([before, track], AsyncMock(spec=["get_voice_over"]))
    assert await _collect(_mixed(audio, track)) == _MUSIC_CHUNKS


async def test_item_whose_plugin_has_nothing_to_carry_over_passes_through() -> None:
    """The plugin before is asked, and a None answer leaves the item untouched."""
    brk, track = _break_item(), _track_item()
    plugin = _Plugin(None)
    audio = _make_streams_audio([brk, track], plugin)
    assert await _collect(_mixed(audio, track)) == _MUSIC_CHUNKS
    assert plugin.asked == [("clip_1", _TRACK_ID)]
    assert plugin.ended == []


async def test_passthrough_closes_the_stream_it_wraps() -> None:
    """Closing the wrapper early closes the item's own stream along with it."""
    closed: list[bool] = []

    async def _music() -> AsyncGenerator[bytes]:
        try:
            for chunk in _MUSIC_CHUNKS:
                yield chunk
        finally:
            closed.append(True)

    track = _track_item()
    audio = _make_streams_audio([track], _Plugin(None))
    stream = audio.get_voice_over_mixed_stream(track, _music(), _PCM_FORMAT)
    assert await anext(stream) == _MUSIC_CHUNKS[0]
    await stream.aclose()
    assert closed == [True]


async def test_voice_over_airs_when_its_item_played_right_before(
    monkeypatch: pytest.MonkeyPatch, voice_file: str
) -> None:
    """An item served straight after the plugin's item gets the voice-over mixed in."""
    mixer_kwargs = _fake_mixer(monkeypatch)
    brk, track = _break_item(), _track_item()
    plugin = _Plugin(_voice_over(voice_file))
    audio = _make_streams_audio([brk, track], plugin)
    result = await _collect(_mixed(audio, track))
    assert result == [b"mixed:" + chunk for chunk in _MUSIC_CHUNKS]
    assert mixer_kwargs["voice_path"] == voice_file
    assert mixer_kwargs["voice_offset"] == 7.5
    assert mixer_kwargs["voice_start"] == 0.0
    assert mixer_kwargs["voice_end"] == 11.6
    assert plugin.ended == [("clip_1", True)]


async def test_voice_over_leaves_the_items_untouched(
    monkeypatch: pytest.MonkeyPatch, voice_file: str
) -> None:
    """Nothing is written onto either item, so nothing reaches clients or the saved queue."""
    _fake_mixer(monkeypatch)
    brk, track = _break_item(), _track_item()
    audio = _make_streams_audio([brk, track], _Plugin(_voice_over(voice_file)))
    await _collect(_mixed(audio, track))
    assert track.extra_attributes == {}
    assert brk.extra_attributes == {}
    cast("Any", audio.mass).player_queues.signal_update.assert_not_called()


async def test_voice_over_is_left_unsettled_by_a_stream_cut_short(
    monkeypatch: pytest.MonkeyPatch, voice_file: str
) -> None:
    """A player that drops its first request still gets the voice-over on the second one."""
    _fake_mixer(monkeypatch)
    brk, track = _break_item(), _track_item()
    plugin = _Plugin(_voice_over(voice_file))
    audio = _make_streams_audio([brk, track], plugin)
    stream = _mixed(audio, track)
    assert await anext(stream) == b"mixed:" + _MUSIC_CHUNKS[0]
    await stream.aclose()
    assert plugin.ended == []

    # by now the track itself is the item whose audio last went out
    _set_last_served(audio, _TRACK_ID)
    assert await _collect(_mixed(audio, track)) == [b"mixed:" + c for c in _MUSIC_CHUNKS]
    assert plugin.ended == [("clip_1", True)]


async def test_source_of_a_stream_cut_short_is_replaced_by_the_next_item_served(
    monkeypatch: pytest.MonkeyPatch, voice_file: str
) -> None:
    """What a queue remembers for a repeat fetch is one item, so a cut stream leaks nothing."""
    _fake_mixer(monkeypatch)
    brk, track = _break_item(), _track_item()
    plugin = _Plugin(_voice_over(voice_file))
    audio = _make_streams_audio([brk, track], plugin)
    stream = _mixed(audio, track)
    await anext(stream)
    await stream.aclose()
    sources = cast("Any", audio)._voice_over_sources
    assert set(sources) == {"queue"}

    # another break and track play; the queue's one entry now belongs to the new track
    later_break, later_track = _break_item(), _track_item()
    later_break.queue_item_id, later_track.queue_item_id = "break_2", "track_2"
    audio = _make_streams_audio([later_break, later_track], plugin, last_served="break_2")
    cast("Any", audio)._voice_over_sources = sources
    stream = _mixed(audio, later_track)
    await anext(stream)
    await stream.aclose()
    assert set(sources) == {"queue"}
    assert sources["queue"][0] == "track_2"


async def test_settled_voice_over_is_not_asked_for_on_a_later_fetch(
    monkeypatch: pytest.MonkeyPatch, voice_file: str
) -> None:
    """Once settled, a fetch that finds the item itself last served has no item before."""
    _fake_mixer(monkeypatch)
    brk, track = _break_item(), _track_item()
    plugin = _Plugin(_voice_over(voice_file))
    audio = _make_streams_audio([brk, track], plugin)
    await _collect(_mixed(audio, track))

    _set_last_served(audio, _TRACK_ID)
    assert await _collect(_mixed(audio, track)) == _MUSIC_CHUNKS
    assert len(plugin.asked) == 1


@pytest.mark.parametrize(
    "order",
    [
        pytest.param(["track", "break"], id="repeat all wraps from the break to the first item"),
        pytest.param(["break", "unavailable", "track"], id="an unavailable item was skipped"),
    ],
)
async def test_voice_over_follows_the_item_served_before_not_the_queue_index(
    monkeypatch: pytest.MonkeyPatch, voice_file: str, order: list[str]
) -> None:
    """The item before is whatever played right before, wherever it sits in the queue."""
    _fake_mixer(monkeypatch)
    brk, track = _break_item(), _track_item()
    unavailable = QueueItem(queue_id="queue", queue_item_id="gone", name="Gone", duration=100)
    items = {"break": brk, "track": track, "unavailable": unavailable}
    plugin = _Plugin(_voice_over(voice_file))
    audio = _make_streams_audio([items[name] for name in order], plugin)
    assert await _collect(_mixed(audio, track)) == [b"mixed:" + c for c in _MUSIC_CHUNKS]
    assert plugin.ended == [("clip_1", True)]


@pytest.mark.parametrize(
    "last_served",
    [
        pytest.param(None, id="explicit play, skipped break or restored queue"),
        pytest.param("another_item", id="something came between the break and the track"),
    ],
)
async def test_voice_over_is_not_asked_for_when_its_item_did_not_play_right_before(
    monkeypatch: pytest.MonkeyPatch, voice_file: str, last_served: str | None
) -> None:
    """A voice-over never airs on a playback its item did not lead into."""
    mixer_kwargs = _fake_mixer(monkeypatch)
    brk, track = _break_item(), _track_item()
    other = QueueItem(queue_id="queue", queue_item_id="another_item", name="Other", duration=9)
    plugin = _Plugin(_voice_over(voice_file))
    audio = _make_streams_audio([brk, other, track], plugin, last_served=last_served)
    assert await _collect(_mixed(audio, track)) == _MUSIC_CHUNKS
    assert mixer_kwargs == {}
    assert plugin.asked == []


async def test_voice_over_is_dropped_on_a_seeked_item(
    monkeypatch: pytest.MonkeyPatch, voice_file: str
) -> None:
    """Every offset is measured from the start of the item, so a seek plays it clean."""
    mixer_kwargs = _fake_mixer(monkeypatch)
    brk, track = _break_item(), _track_item()
    track.streamdetails = cast("Any", SimpleNamespace(seek_position=30))
    plugin = _Plugin(_voice_over(voice_file))
    audio = _make_streams_audio([brk, track], plugin)
    assert await _collect(_mixed(audio, track)) == _MUSIC_CHUNKS
    assert mixer_kwargs == {}
    assert plugin.ended == [("clip_1", False)]


async def test_voice_over_is_dropped_when_its_file_is_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A clip that no longer exists must not reach the mixer, which it would kill."""
    mixer_kwargs = _fake_mixer(monkeypatch)
    brk, track = _break_item(), _track_item()
    plugin = _Plugin(_voice_over(str(tmp_path / "pruned.wav")))
    audio = _make_streams_audio([brk, track], plugin)
    assert await _collect(_mixed(audio, track)) == _MUSIC_CHUNKS
    assert mixer_kwargs == {}
    assert plugin.ended == [("clip_1", False)]


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"end": 0.0}, id="empty window"),
        pytest.param({"start": 5.0, "end": 2.0}, id="end before start"),
        pytest.param({"start": -1.0}, id="negative start"),
    ],
)
async def test_voice_over_with_an_unusable_window_is_dropped(
    monkeypatch: pytest.MonkeyPatch, voice_file: str, overrides: dict[str, Any]
) -> None:
    """A voice-over with a window that cannot be mixed plays the item clean."""
    mixer_kwargs = _fake_mixer(monkeypatch)
    brk, track = _break_item(), _track_item()
    plugin = _Plugin(_voice_over(voice_file, **overrides))
    audio = _make_streams_audio([brk, track], plugin)
    assert await _collect(_mixed(audio, track)) == _MUSIC_CHUNKS
    assert mixer_kwargs == {}
    assert plugin.ended == [("clip_1", False)]


async def test_mixer_failure_raises_and_settles_the_voice_over(
    monkeypatch: pytest.MonkeyPatch, voice_file: str
) -> None:
    """The per-item route gets the failure to report, and the voice-over is not kept."""
    _failing_mixer(monkeypatch)
    brk, track = _break_item(), _track_item()
    plugin = _Plugin(_voice_over(voice_file))
    audio = _make_streams_audio([brk, track], plugin)
    with pytest.raises(AudioError):
        await _collect(_mixed(audio, track))
    assert plugin.ended == [("clip_1", False)]


async def test_mixer_failure_ends_the_item_quietly_in_flow_mode(
    monkeypatch: pytest.MonkeyPatch, voice_file: str
) -> None:
    """A flow stream loses this one item to a broken voice-over rather than the whole flow."""
    _failing_mixer(monkeypatch)
    brk, track = _break_item(), _track_item()
    plugin = _Plugin(_voice_over(voice_file))
    audio = _make_streams_audio([brk, track], plugin)
    assert await _collect(_mixed(audio, track, raise_on_error=False)) == []
    assert plugin.ended == [("clip_1", False)]
