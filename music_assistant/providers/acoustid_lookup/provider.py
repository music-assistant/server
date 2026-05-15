"""AcoustID Lookup provider — fingerprints local audio and resolves MusicBrainz recording IDs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp
import chromaprint
import numpy as np
from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.errors import (
    MusicAssistantError,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
)

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.tags import (
    write_acoustid_tag,
    write_musicbrainz_recording_id_tag,
)
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.models.audio_analysis_provider import AudioAnalysisProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant


CONF_API_KEY = "api_key"
CONF_MIN_SCORE = "min_score"
CONF_WRITE_TAGS_BACK = "write_tags_back"

DEFAULT_MIN_SCORE = 0.85
MAX_FINGERPRINT_SECONDS = 120
MAX_CANDIDATES = 5
ACOUSTID_LOOKUP_URL = "https://api.acoustid.org/v2/lookup"
ACOUSTID_LOOKUP_CACHE_TTL = 86400 * 30


@dataclass
class _AcoustidSessionData:
    """Per-session state for a fingerprinting job."""

    fingerprinter: Any
    sample_rate: int
    channels: int
    sample_width: int
    track_duration: int
    expected_album_title: str | None = None
    pcm_seconds_fed: float = 0.0
    error: str | None = None


class AcoustidLookupProvider(AudioAnalysisProvider):
    """Audio analysis provider that identifies tracks via AcoustID/Chromaprint."""

    analysis_version: int = 1
    throttler = ThrottlerManager(rate_limit=3, period=1)

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature] | None = None,
    ) -> None:
        """Initialize the provider with an empty per-session state container."""
        super().__init__(mass, manifest, config, supported_features)
        self._data: dict[str, _AcoustidSessionData] = {}

    async def _start_analysis(
        self,
        session_id: str,
        streamdetails: StreamDetails,
        audio_format: AudioFormat,
    ) -> bool:
        """
        Accept a new analysis session and start a chromaprint fingerprinter.

        Rejects non-local-file tracks. Tracks that already have a MusicBrainz
        recording ID short-circuit: a sentinel row is persisted so subsequent
        scans are gated out at the version check without decoding the file.

        :param session_id: Session ID assigned by the AudioAnalysisController.
        :param streamdetails: Stream details for the track being analysed.
        :param audio_format: PCM format of the incoming audio stream.
        """
        if streamdetails.media_type != MediaType.TRACK:
            return False
        if streamdetails.stream_type != StreamType.LOCAL_FILE:
            return False

        try:
            track = await self.mass.music.tracks.get_library_item_by_prov_id(
                streamdetails.item_id, streamdetails.provider
            )
        except MusicAssistantError as err:
            self.logger.debug(
                "acoustid: library lookup failed for %s/%s (%s); proceeding without row",
                streamdetails.provider,
                streamdetails.item_id,
                err,
            )
            track = None

        if track is not None and track.mbid:
            self.logger.debug("acoustid: skip %s — track already has mbid", session_id)
            await self.mass.streams.audio_analysis.set_audio_analysis(
                item_id=streamdetails.item_id,
                provider_instance_id_or_domain=streamdetails.provider,
                aa_provider_domain=self.domain,
                analysis=AudioAnalysisData(extra_data={"skipped": "mbid_present"}),
                analysis_version=self.analysis_version,
                media_type=streamdetails.media_type,
            )
            return False

        fingerprinter = self._create_fingerprinter(audio_format.sample_rate, audio_format.channels)
        if fingerprinter is None:
            return False

        bit_depth = audio_format.bit_depth or 16
        sample_width = max(1, bit_depth // 8)
        track_duration = int(streamdetails.duration or 0)
        expected_album_title = _extract_album_title(track)
        self._data[session_id] = _AcoustidSessionData(
            fingerprinter=fingerprinter,
            sample_rate=int(audio_format.sample_rate),
            channels=int(audio_format.channels),
            sample_width=sample_width,
            track_duration=track_duration,
            expected_album_title=expected_album_title,
        )
        self.logger.debug(
            "acoustid: armed %s (sr=%d ch=%d bit_depth=%d track_duration=%ds album=%r)",
            session_id,
            audio_format.sample_rate,
            audio_format.channels,
            audio_format.bit_depth or 16,
            track_duration,
            expected_album_title,
        )
        return True

    def _create_fingerprinter(self, sample_rate: int, channels: int) -> Any | None:
        """
        Construct and start a chromaprint Fingerprinter, or return None on failure.

        :param sample_rate: Audio sample rate in Hz.
        :param channels: Number of audio channels.
        """
        try:
            fp = chromaprint.Fingerprinter()
            fp.start(int(sample_rate), int(channels))
        except chromaprint.FingerprintError as err:
            self.logger.warning("Failed to initialise chromaprint fingerprinter: %s", err)
            return None
        return fp

    async def process_pcm_chunk(self, session_id: str, pcm_chunk: bytes) -> None:
        """
        Feed a PCM chunk into the session's fingerprinter (capped at MAX_FINGERPRINT_SECONDS of audio).

        :param session_id: Active analysis session ID.
        :param pcm_chunk: PCM audio bytes at the session's declared sample rate / format.
        """
        data = self._data.get(session_id)
        if data is None or data.error:
            return
        if data.pcm_seconds_fed >= MAX_FINGERPRINT_SECONDS:
            return

        if data.sample_width != 2:
            try:
                pcm_chunk = _downconvert_to_s16(pcm_chunk, data.sample_width)
            except ValueError as err:
                self.logger.debug(
                    "acoustid: session %s pcm conversion failed (sample_width=%d): %s",
                    session_id,
                    data.sample_width,
                    err,
                )
                data.error = f"pcm conversion failed: {err}"
                return

        try:
            data.fingerprinter.feed(pcm_chunk)
        except (chromaprint.FingerprintError, TypeError) as err:
            self.logger.debug("acoustid: session %s feed() failed: %s", session_id, err)
            data.error = f"feed failed: {err}"
            return

        # chunk is now guaranteed s16le; one frame = 2 bytes * channels
        frame_bytes = 2 * data.channels
        if frame_bytes <= 0 or data.sample_rate <= 0:
            return
        prev_seconds = data.pcm_seconds_fed
        data.pcm_seconds_fed += len(pcm_chunk) / (frame_bytes * data.sample_rate)
        if (
            prev_seconds < MAX_FINGERPRINT_SECONDS
            and data.pcm_seconds_fed >= MAX_FINGERPRINT_SECONDS
        ):
            self.logger.debug(
                "acoustid: session %s reached MAX_FINGERPRINT_SECONDS (%ds of audio)",
                session_id,
                MAX_FINGERPRINT_SECONDS,
            )

    async def cancel(self, session_id: str) -> None:
        """
        Drop fingerprinter state then defer to base-class cleanup.

        :param session_id: Session ID to cancel.
        """
        self._data.pop(session_id, None)
        await super().cancel(session_id)

    async def _finalize(self, session_id: str) -> AudioAnalysisData | None:
        """
        Compute the fingerprint, query AcoustID, and return the chosen IDs.

        :param session_id: Active analysis session ID.
        """
        data = self._data.pop(session_id, None)
        if not data:
            return None
        self.logger.debug(
            "acoustid: finalize %s — pcm_seconds_fed=%.2f track_duration=%ds error=%s",
            session_id,
            data.pcm_seconds_fed,
            data.track_duration,
            data.error,
        )
        if data.error or data.pcm_seconds_fed <= 0:
            return None

        try:
            fingerprint_raw = data.fingerprinter.finish()
        except chromaprint.FingerprintError as err:
            self.logger.debug("acoustid: chromaprint finish() failed: %s", err)
            return None

        if not isinstance(fingerprint_raw, (bytes, bytearray)):
            self.logger.debug(
                "acoustid: fingerprint not bytes (type=%s)", type(fingerprint_raw).__name__
            )
            return None
        try:
            fingerprint = bytes(fingerprint_raw).decode("ascii")
        except UnicodeDecodeError:
            self.logger.debug("acoustid: fingerprint not ASCII")
            return None
        if not fingerprint:
            self.logger.debug("acoustid: empty fingerprint")
            return None

        api_key = self.config.get_value(CONF_API_KEY)
        if not api_key:
            self.logger.debug("acoustid: no API key configured; skipping lookup")
            return None

        duration_for_lookup = data.track_duration or round(data.pcm_seconds_fed)
        if duration_for_lookup <= 0:
            self.logger.debug("acoustid: no usable duration for lookup")
            return None
        if not data.track_duration:
            self.logger.debug(
                "acoustid: track_duration unknown, using pcm_seconds_fed=%d", duration_for_lookup
            )

        try:
            response = await self._lookup(str(api_key), fingerprint, duration_for_lookup)
        except (aiohttp.ClientError, RetriesExhausted, TimeoutError, json.JSONDecodeError) as err:
            self.logger.warning("acoustid: lookup failed: %s", err)
            return None
        if not response:
            return None

        chosen_score, chosen_acoustid, chosen_mbid, candidates, album_matched = _parse_response(
            response,
            expected_album_title=data.expected_album_title,
        )
        self.logger.debug(
            "acoustid: parsed — score=%.3f mbid=%s candidates=%d",
            chosen_score,
            chosen_mbid,
            len(candidates),
        )
        if album_matched:
            self.logger.debug(
                "acoustid: chose recording — release title matched album %r",
                data.expected_album_title,
            )
        if chosen_mbid is None:
            return None

        raw_min_score = self.config.get_value(CONF_MIN_SCORE)
        min_score = (
            float(raw_min_score) if isinstance(raw_min_score, (int, float)) else DEFAULT_MIN_SCORE
        )
        if chosen_score < min_score:
            self.logger.debug(
                "acoustid: score %.3f below threshold %.2f; discarding", chosen_score, min_score
            )
            return None

        return AudioAnalysisData(
            extra_data={
                "acoustid": chosen_acoustid,
                "mbid": chosen_mbid,
                "match_score": round(chosen_score, 4),
                "candidates": candidates,
            }
        )

    @use_cache(ACOUSTID_LOOKUP_CACHE_TTL)
    @throttle_with_retries
    async def _lookup(self, api_key: str, fingerprint: str, duration: int) -> dict[str, Any] | None:
        """
        Call AcoustID v2/lookup behind the global throttler and a 30-day cache.

        Cache and throttler keep API-call volume to a minimum and respect AcoustID's
        rate-limit policy; transient HTTP problems are surfaced as
        :class:`ResourceTemporarilyUnavailable` so the throttler retries them.

        :param api_key: AcoustID API key for this deployment.
        :param fingerprint: Base64 chromaprint fingerprint string.
        :param duration: Full track duration in seconds (the AcoustID API expects the
            track length, not the duration of the audio that was actually fingerprinted).
        :raises ResourceTemporarilyUnavailable: For 429 or 5xx responses.
        """
        params = {
            "client": api_key,
            # Space-separated so aiohttp encodes it as 'recordings+releases' in the URL;
            # passing '+' here would be re-encoded as %2B and AcoustID would treat the
            # value as a single unknown token, returning a result with no recordings.
            "meta": "recordings releases",
            "fingerprint": fingerprint,
            "duration": str(duration),
            "format": "json",
        }
        self.logger.debug(
            "acoustid: HTTP GET (cache miss, fingerprint_len=%d duration=%d)",
            len(fingerprint),
            duration,
        )
        async with self.mass.http_session.get(ACOUSTID_LOOKUP_URL, params=params) as response:
            if response.status == 429:
                backoff = int(response.headers.get("Retry-After", 0))
                raise ResourceTemporarilyUnavailable("AcoustID rate limit", backoff_time=backoff)
            if 500 <= response.status < 600:
                raise ResourceTemporarilyUnavailable("AcoustID server error", backoff_time=30)
            if response.status in (401, 403):
                self.logger.error(
                    "AcoustID lookup unauthorised (HTTP %d) — check the configured API key",
                    response.status,
                )
                return None
            if response.status >= 400:
                self.logger.debug("acoustid: HTTP %d — discarding", response.status)
                return None
            payload = await response.json()
        if not isinstance(payload, dict):
            self.logger.debug("acoustid: payload not a dict (type=%s)", type(payload).__name__)
            return None
        if payload.get("status") != "ok":
            self.logger.debug("acoustid: payload status=%s — discarding", payload.get("status"))
            return None
        return payload

    async def post_analysis(
        self,
        streamdetails: StreamDetails,
        analysis: AudioAnalysisData,
    ) -> None:
        """
        Persist the matched MBID/AcoustID to the library row and (optionally) the file.

        :param streamdetails: Stream details for the analysed item.
        :param analysis: Analysis data produced by :meth:`_finalize`.
        """
        extra = analysis.extra_data or {}
        mbid = extra.get("mbid")
        acoustid = extra.get("acoustid")
        if not mbid and not acoustid:
            return

        await self.mass.streams.audio_analysis.set_track_identifiers(
            item_id=streamdetails.item_id,
            provider_instance_id_or_domain=streamdetails.provider,
            mbid=mbid,
            acoustid=acoustid,
        )
        self.logger.debug(
            "acoustid: persisted %s/%s — mbid=%s acoustid=%s",
            streamdetails.provider,
            streamdetails.item_id,
            mbid,
            acoustid,
        )

        if not self.config.get_value(CONF_WRITE_TAGS_BACK):
            return
        if not isinstance(streamdetails.path, str) or not streamdetails.path:
            self.logger.debug(
                "acoustid: tag write skipped — no usable path (got %r)", streamdetails.path
            )
            return

        mbid_ok = (
            await write_musicbrainz_recording_id_tag(streamdetails.path, mbid) if mbid else None
        )
        acoustid_ok = await write_acoustid_tag(streamdetails.path, acoustid) if acoustid else None
        self.logger.debug(
            "acoustid: tag write — %s mbid_ok=%s acoustid_ok=%s",
            streamdetails.path,
            mbid_ok,
            acoustid_ok,
        )


def _downconvert_to_s16(pcm_chunk: bytes, sample_width: int) -> bytes:
    """
    Downconvert a PCM buffer to little-endian 16-bit signed PCM.

    :param pcm_chunk: Raw PCM bytes at the source's native bit-depth.
    :param sample_width: Bytes-per-sample of the source PCM (1, 2, 3, or 4).
    :raises ValueError: When sample_width is unsupported.
    """
    if sample_width == 2:
        return pcm_chunk
    if sample_width == 4:
        # treat the high 16 bits of int32 as the s16 sample — accurate for s32 and
        # a benign quantisation for float, since chromaprint downsamples internally.
        arr = np.frombuffer(pcm_chunk, dtype="<i4")
        return (arr >> 16).astype("<i2").tobytes()
    if sample_width == 3:
        raw = np.frombuffer(pcm_chunk, dtype="<u1")
        trimmed = raw[: (raw.size // 3) * 3].reshape(-1, 3).astype("<i4")
        samples = trimmed[:, 0] | (trimmed[:, 1] << 8) | (trimmed[:, 2] << 16)
        samples = np.where(samples & 0x800000, samples - 0x1000000, samples)
        return (samples >> 8).astype("<i2").tobytes()
    if sample_width == 1:
        arr = np.frombuffer(pcm_chunk, dtype="<u1").astype("<i2")
        return ((arr - 128) << 8).astype("<i2").tobytes()
    msg = f"unsupported sample_width: {sample_width}"
    raise ValueError(msg)


def _parse_response(
    response: dict[str, Any],
    expected_album_title: str | None = None,
) -> tuple[float, str | None, str | None, list[dict[str, Any]], bool]:
    """
    Pick the best (score, acoustid, mbid) and a trimmed candidate list.

    Within a single AcoustID result, recordings whose release title matches
    the library track's album are preferred; metadata richness breaks ties
    when no album hint is supplied or no release matches.

    :param response: Raw AcoustID `/v2/lookup` JSON payload.
    :param expected_album_title: Album title known from the library track, if any.
    """
    results = response.get("results") or []
    best_score = 0.0
    best_acoustid: str | None = None
    best_mbid: str | None = None
    best_album_matched = False

    candidates: list[dict[str, Any]] = []
    for result in results[:MAX_CANDIDATES]:
        acoustid_id = result.get("id")
        score = float(result.get("score") or 0.0)
        recordings = result.get("recordings") or []
        rec_ids = [r.get("id") for r in recordings if r.get("id")]
        candidates.append({"acoustid": acoustid_id, "score": score, "recordings": rec_ids})

        chosen_recording: dict[str, Any] | None = None
        chosen_quality: tuple[int, int] = (-1, -1)
        for rec in recordings:
            if not rec.get("id"):
                continue
            quality = (
                1 if _release_title_matches(rec, expected_album_title) else 0,
                _recording_richness(rec),
            )
            if quality > chosen_quality:
                chosen_recording = rec
                chosen_quality = quality

        if chosen_recording is None:
            continue
        if score > best_score:
            best_score = score
            best_acoustid = acoustid_id
            best_mbid = chosen_recording.get("id")
            best_album_matched = chosen_quality[0] == 1

    return best_score, best_acoustid, best_mbid, candidates, best_album_matched


def _recording_richness(recording: dict[str, Any]) -> int:
    """
    Score a recording dict by the breadth of reference data it carries.

    :param recording: Single recording entry from an AcoustID result.
    """
    score = 0
    if recording.get("artists"):
        score += 1
    if recording.get("releases"):
        score += 1
    if recording.get("title"):
        score += 1
    return score


def _release_title_matches(recording: dict[str, Any], expected_title: str | None) -> bool:
    """Return True when any of the recording's releases has a matching title."""
    if not expected_title:
        return False
    normalized = _normalize_for_match(expected_title)
    if not normalized:
        return False
    for rel in recording.get("releases") or []:
        title = rel.get("title")
        if isinstance(title, str) and _normalize_for_match(title) == normalized:
            return True
    return False


def _normalize_for_match(value: str) -> str:
    """Casefold and collapse whitespace for a forgiving title comparison."""
    return " ".join(value.casefold().split())


def _extract_album_title(track: Any) -> str | None:
    """Pull the library track's album title, tolerating missing attributes."""
    if track is None:
        return None
    album = getattr(track, "album", None)
    if album is None:
        return None
    title = getattr(album, "name", None)
    return title if isinstance(title, str) and title else None
