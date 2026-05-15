"""AcoustID Lookup provider — fingerprints local audio and resolves MusicBrainz recording IDs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp
import chromaprint
import numpy as np
from music_assistant_models.enums import ExternalID, MediaType, StreamType
from music_assistant_models.errors import (
    MusicAssistantError,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
)

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.compare import create_safe_string
from music_assistant.helpers.tags import (
    write_acoustid_tag,
    write_isrc_tag,
    write_musicbrainz_artist_id_tag,
    write_musicbrainz_recording_id_tag,
)
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.models.audio_analysis_provider import AudioAnalysisProvider
from music_assistant.providers.musicbrainz import MusicbrainzProvider

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
# Popular tracks routinely have 60 to 80 release-groups in MB; the cap is mostly
# a defence against a pathological compilation entry, so set it well above
# realistic counts. ~100 bytes per stored entry keeps even a 100-entry list
# under 10 KB per track.
MAX_RELEASE_GROUPS_PER_RECORDING = 100
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
    expected_track_title: str | None = None
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
        Accept or decline an analysis session for the given track.

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
            # Sentinel row makes the version-gate skip this provider on the next
            # scan without decoding the file.
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
        expected_track_title = _extract_track_title(track)
        self._data[session_id] = _AcoustidSessionData(
            fingerprinter=fingerprinter,
            sample_rate=int(audio_format.sample_rate),
            channels=int(audio_format.channels),
            sample_width=sample_width,
            track_duration=track_duration,
            expected_track_title=expected_track_title,
            expected_album_title=expected_album_title,
        )
        self.logger.debug(
            "acoustid: armed %s (sr=%d ch=%d bit_depth=%d track_duration=%ds track=%r album=%r)",
            session_id,
            audio_format.sample_rate,
            audio_format.channels,
            audio_format.bit_depth or 16,
            track_duration,
            expected_track_title,
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
        Cancel an in-progress analysis session.

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
            # Some aiohttp exceptions (e.g. ClientResponseError) stringify with the
            # full request URL, which includes the API key as a query param.
            self.logger.warning(
                "acoustid: lookup failed: %s: %s",
                type(err).__name__,
                str(err).replace(str(api_key), "***"),
            )
            return None
        if not response:
            return None

        raw_min_score = self.config.get_value(CONF_MIN_SCORE)
        min_score = (
            float(raw_min_score) if isinstance(raw_min_score, (int, float)) else DEFAULT_MIN_SCORE
        )

        (
            chosen_score,
            chosen_acoustid,
            chosen_mbid,
            candidates,
            album_matched,
            release_groups,
        ) = _parse_response(
            response,
            expected_track_title=data.expected_track_title,
            expected_album_title=data.expected_album_title,
            min_score=min_score,
        )
        self.logger.debug(
            "acoustid: parsed — score=%.3f mbid=%s candidates=%d release_groups=%d",
            chosen_score,
            chosen_mbid,
            len(candidates),
            len(release_groups),
        )
        if album_matched:
            self.logger.debug(
                "acoustid: chose recording — release title matched album %r",
                data.expected_album_title,
            )
        if chosen_mbid is None:
            if data.expected_track_title:
                self.logger.debug(
                    "acoustid: discarding — no recording title matches track %r",
                    data.expected_track_title,
                )
            return None

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
                "release_groups": release_groups,
            }
        )

    @use_cache(ACOUSTID_LOOKUP_CACHE_TTL)
    @throttle_with_retries
    async def _lookup(self, api_key: str, fingerprint: str, duration: int) -> dict[str, Any] | None:
        """
        Look up a fingerprint against the AcoustID web service.

        :param api_key: AcoustID API key for this deployment.
        :param fingerprint: Base64 chromaprint fingerprint string.
        :param duration: Full track duration in seconds. AcoustID expects the
            track length here, not the duration of audio actually fingerprinted.
        :raises ResourceTemporarilyUnavailable: On a 429 or 5xx response.
        """
        params = {
            "client": api_key,
            # Space-separated so aiohttp encodes the selectors as '+' in the URL;
            # passing '+' here would be re-encoded as %2B and AcoustID would treat the
            # value as a single unknown token, returning a result with no recordings.
            # 'releasegroups' surfaces per-recording release-group entries used by the
            # album-level consensus in post_analysis.
            "meta": "recordings releases releasegroups",
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
        Persist the matched identifiers to the library row and (optionally) the file.

        :param streamdetails: Stream details for the analysed item.
        :param analysis: Analysis data produced by :meth:`_finalize`.
        """
        extra = analysis.extra_data or {}
        mbid = extra.get("mbid")
        acoustid = extra.get("acoustid")
        if not mbid and not acoustid:
            return

        # Pull ISRCs and (when write_tags_back is on) artist MBIDs from MB.
        # ISRCs go to the DB and file tag; artist MBIDs are file-tag only —
        # the filesystem tag-parser handles the artist-row update on next sync
        # rather than us reproducing its entity-matching here.
        want_artist_mbids = bool(self.config.get_value(CONF_WRITE_TAGS_BACK))
        isrcs, artist_mbids = (
            await self._fetch_mb_extras(mbid, include_artist_mbids=want_artist_mbids)
            if mbid
            else ([], [])
        )

        await self.mass.streams.audio_analysis.set_track_identifiers(
            item_id=streamdetails.item_id,
            provider_instance_id_or_domain=streamdetails.provider,
            mbid=mbid,
            acoustid=acoustid,
            isrcs=isrcs,
        )
        self.logger.debug(
            "acoustid: persisted %s/%s — mbid=%s acoustid=%s isrcs=%s artist_mbids=%s",
            streamdetails.provider,
            streamdetails.item_id,
            mbid,
            acoustid,
            isrcs,
            artist_mbids,
        )

        # Album-level consensus is a pure DB write on the album row and is
        # independent of write_tags_back — must run for every analysis so
        # tag-write-off users still get MB_RELEASEGROUP populated (which is
        # what unblocks CoverArtArchive / fanart.tv / TheAudioDB).
        try:
            await self._maybe_set_album_release_group(streamdetails)
        except Exception as err:
            self.logger.warning("acoustid: album consensus failed: %s", err, exc_info=True)

        if not self.config.get_value(CONF_WRITE_TAGS_BACK):
            if mbid:
                self.logger.debug(
                    "acoustid: write_tags_back is off; MBID/AcoustID/ISRC/artist-MBID tags "
                    "will not be written to %s",
                    streamdetails.path,
                )
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
        isrc_ok = await write_isrc_tag(streamdetails.path, isrcs) if isrcs else None
        artist_mbid_ok = (
            await write_musicbrainz_artist_id_tag(streamdetails.path, artist_mbids)
            if artist_mbids
            else None
        )
        self.logger.debug(
            "acoustid: tag write — %s mbid_ok=%s acoustid_ok=%s isrc_ok=%s artist_mbid_ok=%s",
            streamdetails.path,
            mbid_ok,
            acoustid_ok,
            isrc_ok,
            artist_mbid_ok,
        )

    async def _fetch_mb_extras(
        self, mbid: str, *, include_artist_mbids: bool = True
    ) -> tuple[list[str], list[str]]:
        """
        Return (isrcs, artist_mbids) from MB for the given recording MBID, ([], []) on failure.

        :param mbid: MusicBrainz recording UUID.
        :param include_artist_mbids: When False, the second tuple element is always [].
        """
        mb = self.mass.get_provider("musicbrainz", provider_type=MusicbrainzProvider)
        if mb is None:
            return [], []
        try:
            recording = await mb.get_recording_details(mbid)
        except (
            MusicAssistantError,
            aiohttp.ClientError,
            TimeoutError,
            json.JSONDecodeError,
        ) as err:
            self.logger.debug("acoustid: MB extras lookup failed for %s: %s", mbid, err)
            return [], []
        isrcs = [
            isrc
            for isrc in (getattr(recording, "isrcs", None) or [])
            if isinstance(isrc, str) and isrc
        ]
        artist_mbids: list[str] = []
        if include_artist_mbids:
            for credit in getattr(recording, "artist_credit", None) or []:
                artist_id = getattr(getattr(credit, "artist", None), "id", None)
                if isinstance(artist_id, str) and artist_id and artist_id not in artist_mbids:
                    artist_mbids.append(artist_id)
        return isrcs, artist_mbids

    async def _maybe_set_album_release_group(self, streamdetails: StreamDetails) -> None:
        """
        Run the album-level release-group consensus for the track's album.

        :param streamdetails: Stream details for the track that just finished analysis.
        """
        track = await self.mass.music.tracks.get_library_item_by_prov_id(
            streamdetails.item_id, streamdetails.provider
        )
        if track is None or track.album is None:
            return

        album_item_id_raw = getattr(track.album, "item_id", None)
        if not album_item_id_raw:
            return
        try:
            album_item_id = int(album_item_id_raw)
        except (TypeError, ValueError):
            return

        try:
            library_album = await self.mass.music.albums.get_library_item(album_item_id)
        except MusicAssistantError as err:
            self.logger.debug("acoustid: cannot load album %s: %s", album_item_id, err)
            return
        if library_album.get_external_id(ExternalID.MB_RELEASEGROUP):
            return

        album_tracks = await self.mass.music.albums.get_library_album_tracks(album_item_id)
        total = len(album_tracks)

        # Audio_analysis rows are keyed per music-provider; voting can only span
        # tracks served by the same provider as the play that triggered us.
        native_ids: list[str] = []
        for at in album_tracks:
            for pm in at.provider_mappings:
                if streamdetails.provider in (pm.provider_instance, pm.provider_domain):
                    native_ids.append(pm.item_id)
                    break

        extras = await self.mass.streams.audio_analysis.get_acoustid_extra_data_for_album_tracks(
            native_ids, streamdetails.provider
        )
        voting_rows = [
            extra
            for extra in extras
            if isinstance(extra.get("release_groups"), list) and extra["release_groups"]
        ]
        # <50% of total tracks have AcoustID data — wait for another play to top us up.
        if len(voting_rows) * 2 < total:
            return

        winner = self._pick_consensus_winner(voting_rows, library_album.name)
        if winner is None:
            return
        winner_id, winner_title, top_coverage = winner

        self.logger.debug(
            "acoustid: album consensus — album=%s release_group=%s title=%r coverage=%d/%d",
            album_item_id,
            winner_id,
            winner_title,
            top_coverage,
            len(voting_rows),
        )
        await self.mass.streams.audio_analysis.set_album_release_group(album_item_id, winner_id)

    def _pick_consensus_winner(
        self,
        voting_rows: list[dict[str, Any]],
        album_name: str | None,
    ) -> tuple[str, str | None, int] | None:
        """
        Run the coverage tally and tiebreak; return ``(rg_id, title, coverage)``.

        :param voting_rows: ``extra_data`` dicts for tracks that contributed
            release-group data.
        :param album_name: Library album title used for the title-match tiebreak.
        """
        # Coverage tally: count distinct voting tracks that mention each RG.
        # Dedup within a track first so a track listing the same RG under
        # multiple recordings still contributes a single vote.
        coverage: dict[str, int] = {}
        rg_data: dict[str, dict[str, Any]] = {}
        for extra in voting_rows:
            seen_in_track: set[str] = set()
            for rg in extra["release_groups"]:
                rg_id = rg.get("id") if isinstance(rg, dict) else None
                if not isinstance(rg_id, str) or rg_id in seen_in_track:
                    continue
                seen_in_track.add(rg_id)
                coverage[rg_id] = coverage.get(rg_id, 0) + 1
                rg_data.setdefault(rg_id, rg)

        voting_size = len(voting_rows)
        if not coverage:
            return None

        top_coverage = max(coverage.values())
        # Tolerate one missing track to absorb occasional AcoustID misses; for
        # singletons the floor degenerates to 1/1 (the one voting track must
        # contribute), and the name-match filter below is what really protects.
        required = voting_size if voting_size <= 2 else voting_size - 1
        survivors = [rg_id for rg_id, count in coverage.items() if count == top_coverage]
        if top_coverage < required:
            return None

        album_name_norm = _normalize_for_match(album_name) if album_name else ""

        def _title_matches(rg_id: str) -> bool:
            title = rg_data[rg_id].get("title") or ""
            return bool(album_name_norm) and _normalize_for_match(title) == album_name_norm

        # Refuse to promote an RG whose title doesn't match the library album:
        # coverage alone proves the RG contains the played tracks, not that it
        # is the album the user has tagged. A coverage-correct, name-wrong RG
        # would feed metadata providers art for the wrong album.
        if album_name_norm:
            title_matched = [rg_id for rg_id in survivors if _title_matches(rg_id)]
            if not title_matched:
                return None
            survivors = title_matched

        def _rank(rg_id: str) -> tuple[int, str]:
            rg = rg_data[rg_id]
            is_album = 1 if (rg.get("primary_type") or "").casefold() == "album" else 0
            return (-is_album, rg_id)

        survivors.sort(key=_rank)
        winner_id = survivors[0]
        return winner_id, rg_data[winner_id].get("title"), top_coverage


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
    expected_track_title: str | None = None,
    expected_album_title: str | None = None,
    min_score: float = 0.0,
) -> tuple[
    float,
    str | None,
    str | None,
    list[dict[str, Any]],
    bool,
    list[dict[str, Any]],
]:
    """
    Reduce an AcoustID lookup payload to the chosen identifiers and RG candidates.

    Returns ``(score, acoustid, mbid, candidates, album_title_matched, release_groups)``.

    :param response: Raw AcoustID `/v2/lookup` JSON payload.
    :param expected_track_title: Track title from the library row, if any.
    :param expected_album_title: Album title from the library row, if any.
    :param min_score: Score floor below which a result contributes no release-groups.
    """
    # When expected_track_title is provided and nothing title-matches we refuse
    # the result and clear release_groups so a misidentified track cannot
    # poison downstream album consensus.
    results = response.get("results") or []

    # Union of release-groups across every recording of every passing result —
    # the consensus path needs the widest plausible candidate set to find the
    # album RG that every track shares.
    release_groups: list[dict[str, Any]] = []
    seen_rg_ids: set[str] = set()

    candidates: list[dict[str, Any]] = []
    # Per-result chosen recording: (track_match, score, recording, acoustid_id, album_match)
    result_picks: list[tuple[int, float, dict[str, Any], str | None, int]] = []
    for result in results[:MAX_CANDIDATES]:
        acoustid_id = result.get("id")
        score = float(result.get("score") or 0.0)
        recordings = result.get("recordings") or []
        rec_ids = [r.get("id") for r in recordings if r.get("id")]
        candidates.append({"acoustid": acoustid_id, "score": score, "recordings": rec_ids})

        chosen_recording: dict[str, Any] | None = None
        chosen_quality: tuple[int, int, int] = (-1, -1, -1)
        for rec in recordings:
            if not rec.get("id"):
                continue
            quality = (
                1 if _recording_title_matches(rec, expected_track_title) else 0,
                1 if _release_title_matches(rec, expected_album_title) else 0,
                _recording_richness(rec),
            )
            if quality > chosen_quality:
                chosen_recording = rec
                chosen_quality = quality

        if chosen_recording is not None:
            result_picks.append(
                (chosen_quality[0], score, chosen_recording, acoustid_id, chosen_quality[1])
            )

        if score >= min_score:
            for rec in recordings:
                for rg in _extract_release_groups(rec):
                    rg_id = rg["id"]
                    if rg_id in seen_rg_ids:
                        continue
                    seen_rg_ids.add(rg_id)
                    release_groups.append(rg)

    if not result_picks:
        return 0.0, None, None, candidates, False, release_groups

    # Across-result selection: prefer track-title match, then album-title match,
    # then score. Two recordings can both title-match for the same fingerprint
    # (e.g. a 5.1 remix on one release and the original on another); the album
    # tag is what disambiguates which release the user actually owns.
    best = max(result_picks, key=lambda item: (item[0], item[4], item[1]))
    best_track_match, best_score, best_rec, best_acoustid, best_album_match = best

    # Hard track-title filter — when the library provides a track name, refuse
    # any pick that doesn't title-match. Also empty release_groups so a
    # suspect track can't poison the album consensus vote.
    if expected_track_title and not best_track_match:
        return 0.0, None, None, candidates, False, []

    return (
        best_score,
        best_acoustid,
        best_rec.get("id"),
        candidates,
        bool(best_album_match),
        release_groups,
    )


def _extract_release_groups(recording: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Return the recording's release-groups in the shape the consensus path expects.

    :param recording: Single recording entry from an AcoustID result.
    """
    # Dedupe by id and cap at MAX_RELEASE_GROUPS_PER_RECORDING so a runaway
    # compilation entry cannot bloat the persisted JSON.
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for rg in recording.get("releasegroups") or []:
        rg_id = rg.get("id")
        if not isinstance(rg_id, str) or rg_id in seen:
            continue
        seen.add(rg_id)
        title = rg.get("title")
        primary_type = rg.get("type")
        secondary_types = rg.get("secondarytypes") or []
        out.append(
            {
                "id": rg_id,
                "title": title if isinstance(title, str) else "",
                "primary_type": primary_type if isinstance(primary_type, str) else "",
                "secondary_types": [s for s in secondary_types if isinstance(s, str)],
            }
        )
        if len(out) >= MAX_RELEASE_GROUPS_PER_RECORDING:
            break
    return out


def _recording_richness(recording: dict[str, Any]) -> int:
    """
    Score a recording dict by how well-attested it is in MusicBrainz.

    :param recording: Single recording entry from an AcoustID result.
    """
    # Within-result tiebreaker for when track-title and album-title signals
    # are tied. Counts releases quantitatively — the canonical version of a
    # track is almost always linked to many more releases (compilations,
    # reissues, country variants) than niche remixes or 5.1 mixes.
    score = 0
    if recording.get("artists"):
        score += 1
    # AcoustID nests releases inside release-groups when both selectors are
    # requested, leaving the top-level recording.releases empty, so sum both
    # surfaces for an accurate count.
    score += len(recording.get("releases") or [])
    score += sum(len(rg.get("releases") or []) for rg in (recording.get("releasegroups") or []))
    if recording.get("title"):
        score += 1
    return score


def _release_title_matches(recording: dict[str, Any], expected_title: str | None) -> bool:
    """Return True when any of the recording's releases or release-groups has a matching title."""
    if not expected_title:
        return False
    expected_norm = _normalize_for_match(expected_title)
    if not expected_norm:
        return False
    # The library "album name" is conceptually the release-group title, so check
    # both surfaces — release-groups for the canonical album identity, releases
    # for country/edition variants that may not roll up to the same RG title.
    for entry in (recording.get("releasegroups") or []) + (recording.get("releases") or []):
        title = entry.get("title")
        if isinstance(title, str) and _titles_match(expected_norm, _normalize_for_match(title)):
            return True
    return False


def _recording_title_matches(recording: dict[str, Any], expected_title: str | None) -> bool:
    """Return True when the recording's title matches ``expected_title``."""
    if not expected_title:
        return False
    expected_norm = _normalize_for_match(expected_title)
    if not expected_norm:
        return False
    title = recording.get("title")
    if not isinstance(title, str):
        return False
    return _titles_match(expected_norm, _normalize_for_match(title))


def _titles_match(a_norm: str, b_norm: str) -> bool:
    """Return True when two already-normalised titles refer to the same work."""
    if not a_norm or not b_norm:
        return False
    if a_norm == b_norm:
        return True
    # Word-boundary substring fallback: lets an MB title match a user tag that
    # adds a prefix or suffix (medleys, "(Remastered)" etc.), gated on the
    # shorter side having ≥2 substantive tokens to avoid single-word collisions.
    shorter, longer = (a_norm, b_norm) if len(a_norm) <= len(b_norm) else (b_norm, a_norm)
    substantive = sum(1 for tok in shorter.split() if len(tok) >= 2)
    if substantive < 2:
        return False
    return f" {shorter} " in f" {longer} "


def _normalize_for_match(value: str) -> str:
    """Return a casefolded, accent-stripped, punctuation-free form for title comparison."""
    # Delegated to helpers/compare.create_safe_string so unidecode handles
    # accents (Björk → bjork) and the SPECIAL_COMPARE map handles stylised
    # spellings (P!nk → pink, KoЯn → korn). Non-alphanumerics are pre-spaced
    # so separator variation ("AC/DC" vs "AC DC") doesn't break the match.
    spaced = "".join(c if c.isalnum() else " " for c in value)
    return " ".join(create_safe_string(spaced).split())


def _extract_album_title(track: Any) -> str | None:
    """Pull the library track's album title, tolerating missing attributes."""
    if track is None:
        return None
    album = getattr(track, "album", None)
    if album is None:
        return None
    title = getattr(album, "name", None)
    return title if isinstance(title, str) and title else None


def _extract_track_title(track: Any) -> str | None:
    """Pull the library track's title, tolerating missing attributes."""
    if track is None:
        return None
    name = getattr(track, "name", None)
    return name if isinstance(name, str) and name else None
