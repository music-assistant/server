"""Tests for splitting an AI Radio break into a post, from planning it to airing it."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
import wave
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import AudioError, ProviderUnavailableError
from music_assistant_models.media_items import AudioFormat, ProviderMapping, SoundEffect, Track
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.models.plugin import VoiceOver
from music_assistant.providers.ai_radio.constants import (
    ATTR_ALLOW_POST,
    POST_CLIP_MAX_AGE,
    POST_CLIP_PREFIX,
    POST_STAGED_FORMAT,
    POST_TAIL_GAP,
    TTS_CLIP_PCM_FORMAT,
    TTS_PEAK_CEILING_DB,
    TTS_SPEECHNORM_FILTER,
)
from music_assistant.providers.ai_radio.rendering import AIRadioRenderMixin, _ClipAudio, _PostPlan

_QUEUE_ID = "player_a"
_CLIP_ID = "sess_1"
_MEDIA_PATH = "http://ha.invalid/api/tts_proxy/1.mp3"
_CLIP_FORMAT = AudioFormat(content_type=ContentType.MP3)
_MEDIA = cast("Any", SimpleNamespace(path=_MEDIA_PATH, audio_format=_CLIP_FORMAT))
_LEVELLING = [
    TTS_SPEECHNORM_FILTER,
    "volume=-2.0dB",
    f"alimiter=limit={TTS_PEAK_CEILING_DB}dB:level=false:latency=true",
]
_BREAK_SECONDS = 20.0
_VOCAL_ONSET = 12.0
_OVERLAP = _VOCAL_ONSET - POST_TAIL_GAP
_HEAD = _BREAK_SECONDS - _OVERLAP
_RENDERING = "music_assistant.providers.ai_radio.rendering"


class PostRenderer(AIRadioRenderMixin):
    """Minimal harness exposing the post planning, with its lookups stubbed and counted."""

    domain = "ai_radio"
    instance_id = "ai_radio--test"

    def __init__(self, staged: Path, order: list[QueueItem]) -> None:
        """Initialize the harness around one queue played in the given order."""
        self.logger = logging.getLogger("tests.ai_radio.post")
        self.staged = staged
        self.order = order
        self.onset: float | None = _VOCAL_ONSET
        self.break_seconds = _BREAK_SECONDS
        self.onset_lookups = 0
        self.stagings = 0
        cast("Any", self).mass = SimpleNamespace(
            player_queues=SimpleNamespace(get_next_item=self._next_item)
        )

    def _next_item(self, queue_id: str, item_id: str) -> QueueItem | None:
        assert queue_id == _QUEUE_ID
        ids = [item.queue_item_id for item in self.order]
        index = ids.index(item_id) + 1
        return self.order[index] if index < len(self.order) else None

    async def _resolve_vocal_onset(self, queue_item: QueueItem) -> tuple[float | None, str]:
        self.onset_lookups += 1
        return self.onset, "" if self.onset is not None else "no lyrics found"

    async def _stage_post_clip(
        self, path: str, input_format: AudioFormat, gain_db: float | None
    ) -> tuple[str, float] | None:
        self.stagings += 1
        self.staged.write_bytes(b"voice")
        return str(self.staged), self.break_seconds


class BareRenderer(AIRadioRenderMixin):
    """Harness for the post helpers that reach outside the provider."""

    domain = "ai_radio"
    instance_id = "ai_radio--test"

    def __init__(self, **mass: Any) -> None:
        """Initialize the harness with the given stand-ins on mass."""
        self.logger = logging.getLogger("tests.ai_radio.post")
        cast("Any", self).mass = SimpleNamespace(**mass)


def _break_item(*, allow_post: bool = True) -> QueueItem:
    media_item = SoundEffect(
        item_id=_CLIP_ID,
        provider="ai_radio--test",
        name="Back announce",
        provider_mappings={
            ProviderMapping(
                item_id=_CLIP_ID, provider_domain="ai_radio", provider_instance="ai_radio--test"
            )
        },
    )
    return QueueItem(
        queue_id=_QUEUE_ID,
        queue_item_id="qi_break",
        name="Back announce",
        duration=None,
        media_item=media_item,
        extra_attributes={ATTR_ALLOW_POST: allow_post},
    )


def _track_item(name: str) -> QueueItem:
    media_item = Track(
        item_id=name,
        provider="library",
        name=name,
        provider_mappings={
            ProviderMapping(item_id=name, provider_domain="filesystem", provider_instance="fs")
        },
    )
    return QueueItem(
        queue_id=_QUEUE_ID,
        queue_item_id=f"qi_{name}",
        name=name,
        duration=200,
        media_item=media_item,
        extra_attributes={"playback_speed": 1.0},
    )


def _break_streamdetails(plan: _PostPlan | None = None) -> StreamDetails:
    """Build the StreamDetails get_stream_details hands out for a break with this plan."""
    return StreamDetails(
        provider="ai_radio--test",
        item_id=_CLIP_ID,
        audio_format=_CLIP_FORMAT,
        media_type=MediaType.SOUND_EFFECT,
        stream_type=StreamType.CUSTOM,
        path=_MEDIA_PATH,
        data=_ClipAudio(_MEDIA_PATH, _CLIP_FORMAT, -2.0, plan),
    )


async def _voice_over(renderer: AIRadioRenderMixin, track: QueueItem) -> VoiceOver | None:
    """Ask the renderer, as the streams side does, what the break carries over this track."""
    return await renderer.get_voice_over(_break_streamdetails(), track)


@pytest.fixture
def staged(tmp_path: Path) -> Path:
    """Return the path the harness stages its clip at."""
    return tmp_path / "ma_ai_radio_post_staged.mp3"


# --- planning the split ---


async def test_planned_post_is_handed_out_for_its_record(staged: Path) -> None:
    """The record after the break gets the break's tail, read from where its head stops."""
    clip, track = _break_item(), _track_item("song")
    renderer = PostRenderer(staged, [clip, track])
    plan = await renderer._plan_post(clip, _MEDIA, _CLIP_ID, gain_db=-2.0)
    assert plan is not None
    assert plan.head == pytest.approx(_HEAD)
    voice_over = await _voice_over(renderer, track)
    assert voice_over is not None
    assert voice_over.path == str(staged)
    assert voice_over.start == 0.0
    assert voice_over.end == pytest.approx(_OVERLAP)
    assert voice_over.offset == pytest.approx(_HEAD)


async def test_planned_post_leaves_the_record_itself_untouched(staged: Path) -> None:
    """The plan stays with the provider: nothing is written onto another provider's item."""
    clip, track = _break_item(), _track_item("song")
    renderer = PostRenderer(staged, [clip, track])
    await renderer._plan_post(clip, _MEDIA, _CLIP_ID, gain_db=-2.0)
    assert track.extra_attributes == {"playback_speed": 1.0}


async def test_break_that_is_not_postable_is_left_alone(staged: Path) -> None:
    """Without the opt-in nothing is looked up and nothing is handed out."""
    clip, track = _break_item(allow_post=False), _track_item("song")
    renderer = PostRenderer(staged, [clip, track])
    assert await renderer._plan_post(clip, _MEDIA, _CLIP_ID, gain_db=0.0) is None
    assert renderer.onset_lookups == 0
    assert await _voice_over(renderer, track) is None


async def test_repeat_request_gets_the_same_split(staged: Path) -> None:
    """A clip resolved more than once is planned, looked up and staged only once."""
    clip, track = _break_item(), _track_item("song")
    renderer = PostRenderer(staged, [clip, track])
    first = await renderer._plan_post(clip, _MEDIA, _CLIP_ID, gain_db=0.0)
    second = await renderer._plan_post(clip, _MEDIA, _CLIP_ID, gain_db=0.0)
    assert second is first
    assert renderer.onset_lookups == 1
    assert renderer.stagings == 1


async def test_break_that_cannot_post_stays_whole_on_a_repeat_request(staged: Path) -> None:
    """A clip handed out whole must not turn into a split one on a later request."""
    clip, track = _break_item(), _track_item("song")
    renderer = PostRenderer(staged, [clip, track])
    renderer.onset = None
    assert await renderer._plan_post(clip, _MEDIA, _CLIP_ID, gain_db=0.0) is None
    renderer.onset = _VOCAL_ONSET
    assert await renderer._plan_post(clip, _MEDIA, _CLIP_ID, gain_db=0.0) is None
    assert renderer.onset_lookups == 1
    assert await _voice_over(renderer, track) is None


async def test_plan_is_redone_when_another_record_follows_the_break(staged: Path) -> None:
    """The tail moves to the record that now follows, and is no longer due on the one that did."""
    clip, first, second = _break_item(), _track_item("first"), _track_item("second")
    renderer = PostRenderer(staged, [clip, first, second])
    await renderer._plan_post(clip, _MEDIA, _CLIP_ID, gain_db=0.0)
    assert await _voice_over(renderer, first) is not None

    renderer.order = [clip, second, first]
    plan = await renderer._plan_post(clip, _MEDIA, _CLIP_ID, gain_db=0.0)
    assert plan is not None
    assert plan.track_item_id == "qi_second"
    assert await _voice_over(renderer, second) is not None
    assert await _voice_over(renderer, first) is None


async def test_plan_is_redone_when_the_staged_clip_is_gone(staged: Path) -> None:
    """A pruned staged clip is fetched again rather than handed out as a dead path."""
    clip, track = _break_item(), _track_item("song")
    renderer = PostRenderer(staged, [clip, track])
    await renderer._plan_post(clip, _MEDIA, _CLIP_ID, gain_db=0.0)
    staged.unlink()

    plan = await renderer._plan_post(clip, _MEDIA, _CLIP_ID, gain_db=0.0)
    assert plan is not None
    assert renderer.stagings == 2
    assert staged.is_file()
    voice_over = await _voice_over(renderer, track)
    assert voice_over is not None
    assert voice_over.path == str(staged)


async def test_break_too_short_to_carry_over_leaves_no_staged_copy(staged: Path) -> None:
    """The copy is rendered before the split is known, so a split that fails deletes it."""
    clip, track = _break_item(), _track_item("song")
    renderer = PostRenderer(staged, [clip, track])
    renderer.break_seconds = 2.0  # keeping 1 s for itself leaves less than the 1.5 s minimum

    assert await renderer._plan_post(clip, _MEDIA, _CLIP_ID, gain_db=0.0) is None
    assert renderer.stagings == 1
    assert not staged.exists()


async def test_replanned_break_deletes_the_copy_of_its_old_split(staged: Path) -> None:
    """A plan redone for another record stages afresh and does not leave the old copy."""
    clip, first, second = _break_item(), _track_item("first"), _track_item("second")
    renderer = PostRenderer(staged, [clip, first, second])
    await renderer._plan_post(clip, _MEDIA, _CLIP_ID, gain_db=0.0)
    old_copy = staged.with_name("old_copy.wav")
    staged.rename(old_copy)
    cast("Any", renderer)._post_plans[_CLIP_ID].staged = str(old_copy)

    renderer.order = [clip, second, first]
    plan = await renderer._plan_post(clip, _MEDIA, _CLIP_ID, gain_db=0.0)

    assert plan is not None
    assert not old_copy.exists()
    assert staged.is_file()


# --- handing the tail to the streams side ---


async def test_tail_is_only_handed_out_for_its_own_record(staged: Path) -> None:
    """Another record, or the same record id in another queue, gets nothing."""
    clip, track, other = _break_item(), _track_item("song"), _track_item("other")
    renderer = PostRenderer(staged, [clip, track])
    await renderer._plan_post(clip, _MEDIA, _CLIP_ID, gain_db=0.0)
    elsewhere = _track_item("song")
    elsewhere.queue_id = "player_b"
    assert await _voice_over(renderer, other) is None
    assert await _voice_over(renderer, elsewhere) is None


async def test_unknown_break_has_nothing_to_hand_out(staged: Path) -> None:
    """A break the provider planned nothing for, such as one from before a restart, is None."""
    renderer = PostRenderer(staged, [_break_item(), _track_item("song")])
    assert await _voice_over(renderer, _track_item("song")) is None
    await renderer.on_voice_over_ended(_break_streamdetails(), aired=True)


async def test_aired_tail_is_not_handed_out_again(staged: Path) -> None:
    """Once mixed in, the tail is disarmed: a replay of the record plays it clean."""
    clip, track = _break_item(), _track_item("song")
    renderer = PostRenderer(staged, [clip, track])
    await renderer._plan_post(clip, _MEDIA, _CLIP_ID, gain_db=0.0)

    await renderer.on_voice_over_ended(_break_streamdetails(), aired=True)

    assert await _voice_over(renderer, track) is None


async def test_break_that_airs_again_after_its_tail_aired_carries_it_again(staged: Path) -> None:
    """The plan and its staged copy outlive the airing, so a replayed break posts again."""
    clip, track = _break_item(), _track_item("song")
    renderer = PostRenderer(staged, [clip, track])
    await renderer._plan_post(clip, _MEDIA, _CLIP_ID, gain_db=0.0)
    await renderer.on_voice_over_ended(_break_streamdetails(), aired=True)
    assert staged.is_file()

    assert await renderer._plan_post(clip, _MEDIA, _CLIP_ID, gain_db=0.0) is not None
    assert renderer.stagings == 1
    assert await _voice_over(renderer, track) is not None


async def test_dropped_tail_is_disarmed_but_kept_for_a_replayed_break(staged: Path) -> None:
    """A tail that could not air stays staged, and the break airing again re-arms it."""
    clip, track = _break_item(), _track_item("song")
    renderer = PostRenderer(staged, [clip, track])
    await renderer._plan_post(clip, _MEDIA, _CLIP_ID, gain_db=0.0)

    await renderer.on_voice_over_ended(_break_streamdetails(), aired=False)
    assert await _voice_over(renderer, track) is None
    assert staged.is_file()

    await renderer._plan_post(clip, _MEDIA, _CLIP_ID, gain_db=0.0)
    assert await _voice_over(renderer, track) is not None
    assert renderer.stagings == 1


async def test_unload_deletes_every_staged_tail(staged: Path) -> None:
    """Nothing staged for a post outlives the provider."""
    clip, track = _break_item(), _track_item("song")
    renderer = PostRenderer(staged, [clip, track])
    await renderer._plan_post(clip, _MEDIA, _CLIP_ID, gain_db=0.0)

    await renderer._discard_post_plans()

    assert not staged.exists()
    assert await _voice_over(renderer, track) is None


# --- settling the split when the break's audio is produced ---


@pytest.fixture
def ffmpeg_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Stand in for the ffmpeg run that produces a break's audio, recording each request."""
    calls: list[dict[str, Any]] = []

    async def _fake_ffmpeg_stream(**kwargs: Any) -> AsyncGenerator[bytes]:
        calls.append(kwargs)
        yield b"pcm"

    monkeypatch.setattr(f"{_RENDERING}.get_ffmpeg_stream", _fake_ffmpeg_stream)
    return calls


async def _produce(renderer: PostRenderer, plan: _PostPlan | None) -> None:
    """Produce the break's audio the way the streams side asks for it ahead of the airing."""
    chunks = [chunk async for chunk in renderer.get_audio_stream(_break_streamdetails(plan))]
    assert chunks == [b"pcm"]


def _is_cut(ffmpeg_call: dict[str, Any]) -> bool:
    return any(str(param).startswith("atrim=") for param in ffmpeg_call["filter_params"])


async def test_break_is_cut_where_its_record_comes_in(
    staged: Path, ffmpeg_calls: list[dict[str, Any]]
) -> None:
    """With its record still next, the break stops where the record takes over its voice."""
    clip, track = _break_item(), _track_item("song")
    renderer = PostRenderer(staged, [clip, track])
    plan = await renderer._plan_post(clip, _MEDIA, _CLIP_ID, gain_db=-2.0)

    await _produce(renderer, plan)

    (call,) = ffmpeg_calls
    assert call["audio_input"] == str(staged)
    assert call["input_format"] == POST_STAGED_FORMAT
    assert call["filter_params"] == [f"atrim=end={_HEAD:.3f}"]
    assert await _voice_over(renderer, track) is not None


async def test_break_airs_whole_once_another_record_follows_it(
    staged: Path, ffmpeg_calls: list[dict[str, Any]]
) -> None:
    """A queue change after the plan leaves the break whole, and its tail due on no record."""
    clip, first, second = _break_item(), _track_item("first"), _track_item("second")
    renderer = PostRenderer(staged, [clip, first, second])
    plan = await renderer._plan_post(clip, _MEDIA, _CLIP_ID, gain_db=-2.0)
    renderer.order = [clip, second, first]

    await _produce(renderer, plan)

    (call,) = ffmpeg_calls
    assert call["audio_input"] == str(staged)
    assert call["filter_params"] == []
    assert await _voice_over(renderer, first) is None
    assert await _voice_over(renderer, second) is None


async def test_break_airs_whole_once_its_record_left_the_queue(
    staged: Path, ffmpeg_calls: list[dict[str, Any]]
) -> None:
    """With nothing after it any more, the break has nowhere to carry its tail."""
    clip, track = _break_item(), _track_item("song")
    renderer = PostRenderer(staged, [clip, track])
    plan = await renderer._plan_post(clip, _MEDIA, _CLIP_ID, gain_db=-2.0)
    renderer.order = [clip]

    await _produce(renderer, plan)

    (call,) = ffmpeg_calls
    assert call["audio_input"] == str(staged)
    assert call["filter_params"] == []


async def test_break_airs_whole_when_its_staged_audio_is_gone(
    staged: Path, ffmpeg_calls: list[dict[str, Any]]
) -> None:
    """Its tail could no longer be mixed in, so the break keeps it, levelled from the source."""
    clip, track = _break_item(), _track_item("song")
    renderer = PostRenderer(staged, [clip, track])
    plan = await renderer._plan_post(clip, _MEDIA, _CLIP_ID, gain_db=-2.0)
    staged.unlink()

    await _produce(renderer, plan)

    (call,) = ffmpeg_calls
    assert call["audio_input"] == _MEDIA_PATH
    assert call["input_format"] == _CLIP_FORMAT
    assert call["filter_params"] == _LEVELLING
    assert await _voice_over(renderer, track) is None


async def test_break_that_airs_again_is_cut_and_rearmed(
    staged: Path, ffmpeg_calls: list[dict[str, Any]]
) -> None:
    """After its tail was dropped, a replayed break is cut again and hands the tail out again."""
    clip, track = _break_item(), _track_item("song")
    renderer = PostRenderer(staged, [clip, track])
    plan = await renderer._plan_post(clip, _MEDIA, _CLIP_ID, gain_db=-2.0)
    await renderer.on_voice_over_ended(_break_streamdetails(), aired=False)

    await _produce(renderer, plan)

    assert _is_cut(ffmpeg_calls[0])
    assert await _voice_over(renderer, track) is not None


# --- staging the rendered break ---


def _stub_render(monkeypatch: pytest.MonkeyPatch, seconds: float = 2.0) -> list[dict[str, Any]]:
    """Stand in for the ffmpeg render of the break, yielding that many seconds of PCM."""
    calls: list[dict[str, Any]] = []

    async def _fake_ffmpeg_stream(**kwargs: Any) -> AsyncGenerator[bytes]:
        calls.append(kwargs)
        for _ in range(int(seconds * 2)):
            yield b"\x00" * (TTS_CLIP_PCM_FORMAT.pcm_sample_size // 2)

    monkeypatch.setattr(f"{_RENDERING}.get_ffmpeg_stream", _fake_ffmpeg_stream)
    return calls


@pytest.fixture
def temp_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the temp dir that staged clips go to at this test's own directory."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


def _staged_clips(directory: Path) -> list[Path]:
    return sorted(directory.glob(f"{POST_CLIP_PREFIX}*"))


async def test_clip_is_rendered_once_into_a_levelled_local_copy(
    temp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One levelled render of the whole break, as a WAV both of its parts are read from."""
    calls = _stub_render(monkeypatch, seconds=2.0)
    renderer = BareRenderer()

    staged = await renderer._stage_post_clip(_MEDIA_PATH, _CLIP_FORMAT, gain_db=-2.0)

    (call,) = calls
    assert call["audio_input"] == _MEDIA_PATH
    assert call["input_format"] == _CLIP_FORMAT
    assert call["output_format"] == TTS_CLIP_PCM_FORMAT
    assert call["filter_params"] == _LEVELLING
    assert staged is not None
    path, seconds = staged
    assert seconds == 2.0
    assert _staged_clips(temp_dir) == [Path(path)]
    with wave.open(path) as copy:
        assert copy.getnchannels() == TTS_CLIP_PCM_FORMAT.channels
        assert copy.getframerate() == TTS_CLIP_PCM_FORMAT.sample_rate
        assert copy.getnframes() == 2 * TTS_CLIP_PCM_FORMAT.sample_rate


@pytest.mark.usefixtures("temp_dir")
async def test_clip_that_airs_as_rendered_is_copied_without_levelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With normalization off the copy is a plain decode, as the clip would otherwise play."""
    calls = _stub_render(monkeypatch)
    assert await BareRenderer()._stage_post_clip(_MEDIA_PATH, _CLIP_FORMAT, gain_db=None)
    assert calls[0]["filter_params"] == []


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(AudioError("ffmpeg exited with 1"), id="render failed"),
        pytest.param(None, id="empty render"),
    ],
)
async def test_clip_that_cannot_be_rendered_stages_nothing(
    temp_dir: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception | None
) -> None:
    """Without the audio in hand there is no post, and nothing is left behind."""

    async def _fake_ffmpeg_stream(**_kwargs: Any) -> AsyncGenerator[bytes]:
        yield b""
        if failure is not None:
            raise failure

    monkeypatch.setattr(f"{_RENDERING}.get_ffmpeg_stream", _fake_ffmpeg_stream)
    assert await BareRenderer()._stage_post_clip(_MEDIA_PATH, _CLIP_FORMAT, gain_db=-2.0) is None
    assert _staged_clips(temp_dir) == []


async def test_wedged_render_gives_up_instead_of_holding_up_the_break(
    temp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A render that never finishes ends in no post, long before the break is due."""
    monkeypatch.setattr(f"{_RENDERING}.POST_STAGE_TIMEOUT", 0.05)

    async def _stalled_ffmpeg_stream(**_kwargs: Any) -> AsyncGenerator[bytes]:
        await asyncio.sleep(60)
        yield b""

    monkeypatch.setattr(f"{_RENDERING}.get_ffmpeg_stream", _stalled_ffmpeg_stream)
    async with asyncio.timeout(5):
        assert (
            await BareRenderer()._stage_post_clip(_MEDIA_PATH, _CLIP_FORMAT, gain_db=-2.0) is None
        )
    assert _staged_clips(temp_dir) == []


async def test_only_clips_left_by_posts_that_never_aired_are_pruned(temp_dir: Path) -> None:
    """Staged clips past their age go; fresh ones and other files in the temp dir stay."""
    prefix = POST_CLIP_PREFIX
    long_ago = time.time() - POST_CLIP_MAX_AGE - 60
    stale, fresh = temp_dir / f"{prefix}stale.mp3", temp_dir / f"{prefix}fresh.mp3"
    foreign = temp_dir / "someone_elses.mp3"
    for path in (stale, fresh, foreign):
        path.write_bytes(b"x")
    for path in (stale, foreign):
        os.utime(path, (long_ago, long_ago))

    BareRenderer()._prune_post_clips()

    assert not stale.exists()
    assert fresh.exists()
    assert foreign.exists()


# --- finding where the singing starts ---


def _lyrics_renderer(lookup: AsyncMock) -> BareRenderer:
    return BareRenderer(metadata=SimpleNamespace(get_track_lyrics=lookup))


def _track_with_lyrics(lrc_lyrics: str | None) -> QueueItem:
    track = _track_item("song")
    cast("Track", track.media_item).metadata.lrc_lyrics = lrc_lyrics
    return track


async def test_stored_synced_lyrics_are_used_without_a_lookup() -> None:
    """Lyrics already on the track cost nothing, so no provider is asked."""
    lookup = AsyncMock()
    renderer = _lyrics_renderer(lookup)
    track = _track_with_lyrics("[00:00.00]♪\n[00:09.50]First line")
    assert await renderer._resolve_vocal_onset(track) == (9.5, "")
    lookup.assert_not_awaited()


async def test_lyrics_are_looked_up_when_none_are_stored() -> None:
    """A track without stored lyrics gets them from Music Assistant's own lookup."""
    lookup = AsyncMock(return_value=(None, "[00:07.00]First line"))
    renderer = _lyrics_renderer(lookup)
    assert await renderer._resolve_vocal_onset(_track_with_lyrics(None)) == (7.0, "")
    lookup.assert_awaited_once()


@pytest.mark.parametrize(
    ("found", "reason"),
    [
        pytest.param(
            ("Plain words", None),
            "only unsynced lyrics available, so no vocal timing",
            id="plain lyrics only",
        ),
        pytest.param(
            (None, "[00:00.00][Intro]\n[00:20.00](Instrumental)"),
            "synced lyrics have no sung line",
            id="no sung line",
        ),
        pytest.param((None, None), "no lyrics found", id="nothing found"),
    ],
)
async def test_lyrics_without_vocal_timing_say_why(
    found: tuple[str | None, str | None], reason: str
) -> None:
    """Each way of coming up empty is logged with its own reason."""
    renderer = _lyrics_renderer(AsyncMock(return_value=found))
    assert await renderer._resolve_vocal_onset(_track_with_lyrics(None)) == (None, reason)


async def test_failing_lyrics_lookup_says_why() -> None:
    """A lyrics failure costs the post, never the clip."""
    renderer = _lyrics_renderer(AsyncMock(side_effect=ProviderUnavailableError("provider down")))
    assert await renderer._resolve_vocal_onset(_track_with_lyrics(None)) == (
        None,
        "lyrics lookup failed (provider down)",
    )


async def test_slow_lyrics_lookup_is_abandoned(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lookup that walks every metadata provider must not hold up a break about to air."""
    monkeypatch.setattr(f"{_RENDERING}.POST_LYRICS_TIMEOUT", 0.05)

    async def _slow_lookup(_track: Track) -> tuple[str | None, str | None]:
        await asyncio.sleep(60)
        return None, "[00:07.00]Too late"

    renderer = _lyrics_renderer(AsyncMock(side_effect=_slow_lookup))
    async with asyncio.timeout(5):
        onset, reason = await renderer._resolve_vocal_onset(_track_with_lyrics(None))
    assert onset is None
    assert reason.startswith("lyrics lookup took longer than")


async def test_item_that_is_not_a_track_has_no_vocal_onset() -> None:
    """Only a track has lyrics to read the vocal entry from."""
    renderer = _lyrics_renderer(AsyncMock())
    assert await renderer._resolve_vocal_onset(_break_item()) == (None, "no track details")
