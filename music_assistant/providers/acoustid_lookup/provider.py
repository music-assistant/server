"""AcoustID Lookup provider — fingerprints local audio and resolves MusicBrainz recording IDs."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp
import chromaprint
import numpy as np
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, ExternalID, MediaType, StreamType
from music_assistant_models.errors import (
    MusicAssistantError,
    RateLimited,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
)

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.app_vars import app_var
from music_assistant.helpers.compare import create_safe_string
from music_assistant.helpers.datetime import utc_timestamp
from music_assistant.helpers.tags import write_identifier_tags
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.helpers.util import parse_title_and_version
from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.models.audio_analysis_provider import AudioAnalysisProvider
from music_assistant.providers.musicbrainz import MusicbrainzProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.media_items import AudioFormat, Track
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant


CONF_API_KEY = "api_key"
CONF_MIN_SCORE = "min_score"
CONF_WRITE_TAGS_BACK = "write_tags_back"
CONF_ANALYSE_STREAMING = "analyse_streaming"

DEFAULT_MIN_SCORE = 0.85
# Days before a track that fingerprinted but found no match is retried. The AcoustID
# database grows over time, so an unidentifiable track may match on a later attempt.
NO_MATCH_RETRY_DAYS = 60
# No-match results are stored at this version so they always read as stale and are
# re-offered to the provider, which then gates the actual retry on NO_MATCH_RETRY_DAYS.
NO_MATCH_ANALYSIS_VERSION = -1
MAX_FINGERPRINT_SECONDS = 120
MAX_CANDIDATES = 5
# Per-recording cap on stored release-groups; sized to fit popular tracks
# without bloating the audio_analysis JSON column.
MAX_RELEASE_GROUPS_PER_RECORDING = 500
ACOUSTID_LOOKUP_URL = "https://api.acoustid.org/v2/lookup"
ACOUSTID_LOOKUP_CACHE_TTL = 86400 * 30

_LOGGER = logging.getLogger(__name__)


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

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return config entries for this provider."""
        return (
            ConfigEntry(
                key=CONF_API_KEY,
                type=ConfigEntryType.SECURE_STRING,
                required=False,
                default_value=None,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_MIN_SCORE,
                type=ConfigEntryType.FLOAT,
                default_value=DEFAULT_MIN_SCORE,
                range=(0, 1),
                required=False,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_ANALYSE_STREAMING,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                required=False,
            ),
            ConfigEntry(
                key=CONF_WRITE_TAGS_BACK,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                required=False,
            ),
        )

    async def process_pcm_chunk(self, session_id: str, pcm_chunk: bytes) -> None:
        """
        Feed a PCM chunk into the session's fingerprinter.

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
                    "Could not convert PCM to 16-bit for %s (sample_width=%d): %s",
                    session_id,
                    data.sample_width,
                    err,
                )
                data.error = f"pcm conversion failed: {err}"
                return

        try:
            data.fingerprinter.feed(pcm_chunk)
        except (chromaprint.FingerprintError, TypeError) as err:
            self.logger.debug("Chromaprint rejected PCM chunk for %s: %s", session_id, err)
            data.error = f"feed failed: {err}"
            return

        # chunk is now guaranteed s16le; one frame = 2 bytes * channels
        frame_bytes = 2 * data.channels
        if frame_bytes <= 0 or data.sample_rate <= 0:
            return
        data.pcm_seconds_fed += len(pcm_chunk) / (frame_bytes * data.sample_rate)

    async def cancel(self, session_id: str) -> None:
        """
        Cancel an in-progress analysis session.

        :param session_id: Session ID to cancel.
        """
        self._data.pop(session_id, None)
        await super().cancel(session_id)

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

        await self.mass.music.tracks.set_identifiers(
            item_id=streamdetails.item_id,
            provider_instance_id_or_domain=streamdetails.provider,
            mbid=mbid,
            acoustid=acoustid,
            isrcs=isrcs,
        )

        try:
            library_track = await self.mass.music.tracks.get_library_item_by_prov_id(
                streamdetails.item_id, streamdetails.provider
            )
        except MusicAssistantError:
            library_track = None
        track_name = _extract_track_title(library_track)
        album_name = _extract_album_title(library_track)
        self.logger.info(
            "AcoustID identified track=%r album=%r as MusicBrainz recording %s",
            track_name,
            album_name,
            mbid,
        )

        # Album-level consensus is a pure DB write on the album row and is
        # independent of write_tags_back — must run for every analysis so
        # tag-write-off users still get MB_RELEASEGROUP populated (which is
        # what unblocks CoverArtArchive / fanart.tv / TheAudioDB).
        try:
            await self._maybe_set_album_release_group(streamdetails, library_track=library_track)
        # Broad safety net: per-track persistence already succeeded above and must
        # not be rolled back by a failure in the best-effort consensus path.
        except Exception as err:
            self.logger.warning("Album release-group lookup failed: %s", err, exc_info=True)

        if not self.config.get_value(CONF_WRITE_TAGS_BACK):
            return
        if not isinstance(streamdetails.path, str) or not streamdetails.path:
            self.logger.debug(
                "Skipping tag write — no usable file path (got %r)", streamdetails.path
            )
            return
        source_provider = self.mass.get_provider(streamdetails.provider)
        if not getattr(source_provider, "write_access", False):
            self.logger.debug(
                "Skipping tag write — source provider %s has no write access",
                streamdetails.provider,
            )
            return

        # One open/save cycle for all identifier tags on this file.
        await write_identifier_tags(
            streamdetails.path,
            mbid=mbid,
            acoustid=acoustid,
            isrcs=isrcs,
            artist_mbids=artist_mbids,
        )

    def _resolve_api_key(self) -> str:
        """Return the user-supplied AcoustID API key, falling back to the shared key."""
        user_key = self.config.get_value(CONF_API_KEY)
        if isinstance(user_key, str) and user_key:
            return user_key
        return str(app_var("acoustid_api_key"))

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
        # Tracks only — podcasts and audiobooks must not reach the fingerprinter.
        if streamdetails.media_type != MediaType.TRACK:
            return False
        # Streaming-provider tracks are opt-in — local files always analyse.
        if streamdetails.stream_type != StreamType.LOCAL_FILE and not self.config.get_value(
            CONF_ANALYSE_STREAMING
        ):
            self.logger.debug(
                "Skipping %s — streaming-provider lookups are disabled in settings",
                session_id,
            )
            return False

        try:
            track = await self.mass.music.tracks.get_library_item_by_prov_id(
                streamdetails.item_id, streamdetails.provider
            )
        except MusicAssistantError as err:
            self.logger.debug(
                "Could not load library row for %s/%s: %s",
                streamdetails.provider,
                streamdetails.item_id,
                err,
            )
            return False
        # No library row → nothing to persist into; skip the fingerprint work.
        if track is None:
            self.logger.debug("Skipping %s — track is not in the library", session_id)
            return False
        if track.mbid or track.get_external_id(ExternalID.ISRC):
            # Already identified, so fingerprinting would be wasted work. Record the
            # existing identifiers as a result so coverage counts the track and the scan
            # does not revisit it.
            await self._record_existing_identifiers(streamdetails, track)
            self.logger.debug(
                "Skipping %s — track already identified (MBID/ISRC present); "
                "recorded existing identifiers",
                session_id,
            )
            return False

        if await self._within_no_match_cooldown(streamdetails):
            self.logger.debug(
                "Skipping %s — earlier AcoustID lookup found no match; still within retry cooldown",
                session_id,
            )
            return False

        # Chromaprint only fingerprints mono/stereo audio. Feeding it multichannel
        # (e.g. 5.1) trips a C-level assertion in its AudioProcessor that aborts the
        # whole process rather than raising, so it cannot be caught and logged — the
        # only safe option is to skip the file. See acoustid/chromaprint#90.
        if audio_format.channels > 2:
            self.logger.warning(
                "AcoustID can only scan mono/stereo files; skipping multichannel "
                "(%d-channel) file: %s",
                audio_format.channels,
                streamdetails.path or streamdetails.uri,
            )
            return False

        fingerprinter = self._create_fingerprinter(audio_format.sample_rate, audio_format.channels)
        if fingerprinter is None:
            return False

        bit_depth = audio_format.bit_depth
        if not bit_depth:
            return False
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
        self.logger.info(
            "AcoustID lookup started for track=%r album=%r",
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

    async def _finalize(self, session_id: str) -> AudioAnalysisData | None:
        """
        Compute the fingerprint, query AcoustID, and return the chosen IDs.

        :param session_id: Active analysis session ID.
        """
        data = self._data.pop(session_id, None)
        if not data:
            return None
        if data.error or data.pcm_seconds_fed <= 0:
            return None

        try:
            fingerprint_raw = data.fingerprinter.finish()
        except chromaprint.FingerprintError as err:
            self.logger.debug("Chromaprint failed to produce a fingerprint: %s", err)
            return None

        if not isinstance(fingerprint_raw, (bytes, bytearray)):
            self.logger.debug(
                "Discarding fingerprint — expected bytes, got %s",
                type(fingerprint_raw).__name__,
            )
            return None
        try:
            fingerprint = bytes(fingerprint_raw).decode("ascii")
        except UnicodeDecodeError:
            self.logger.debug("Discarding fingerprint — not valid ASCII")
            return None
        if not fingerprint:
            self.logger.debug("Discarding fingerprint — empty")
            return None

        api_key = self._resolve_api_key()
        duration_for_lookup = data.track_duration or round(data.pcm_seconds_fed)
        if duration_for_lookup <= 0:
            self.logger.debug("No usable track duration — cannot query AcoustID")
            return None

        try:
            response = await self._lookup(str(api_key), fingerprint, duration_for_lookup)
        except (aiohttp.ClientError, RetriesExhausted, TimeoutError, json.JSONDecodeError) as err:
            # Some aiohttp exceptions (e.g. ClientResponseError) stringify with the
            # full request URL, which includes the API key as a query param.
            self.logger.warning(
                "AcoustID lookup failed: %s: %s",
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
            _album_matched,
            release_groups,
        ) = _parse_response(
            response,
            expected_track_title=data.expected_track_title,
            expected_album_title=data.expected_album_title,
            min_score=min_score,
        )
        if chosen_mbid is None:
            if data.expected_track_title:
                self.logger.debug(
                    "No AcoustID match for %r — recording no-match result, "
                    "will retry after %d days",
                    data.expected_track_title,
                    NO_MATCH_RETRY_DAYS,
                )
            await self._persist_no_match(session_id)
            return None

        if chosen_score < min_score:
            self.logger.debug(
                "No confident AcoustID match — best score %.3f is below the configured "
                "minimum of %.2f; recording no-match result, will retry after %d days",
                chosen_score,
                min_score,
                NO_MATCH_RETRY_DAYS,
            )
            await self._persist_no_match(session_id)
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

    async def _within_no_match_cooldown(self, streamdetails: StreamDetails) -> bool:
        """
        Return whether a prior no-match result for this track is still within its retry cooldown.

        :param streamdetails: Stream details for the track being analysed.
        """
        stored = await self.mass.streams.audio_analysis.get_audio_analysis(
            streamdetails.item_id,
            streamdetails.provider,
            media_type=streamdetails.media_type,
            priority=(self.domain,),
        )
        if not stored or not stored.extra_data:
            return False
        retry_after = stored.extra_data.get("retry_after")
        return retry_after is not None and int(utc_timestamp()) < int(retry_after)

    async def _persist_no_match(self, session_id: str) -> None:
        """
        Record that a track fingerprinted but found no match, scheduling a later retry.

        :param session_id: Active analysis session ID.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return
        streamdetails = session.streamdetails
        retry_after = int(utc_timestamp()) + NO_MATCH_RETRY_DAYS * 86400
        await self.mass.streams.audio_analysis.set_audio_analysis(
            item_id=streamdetails.item_id,
            provider_instance_id_or_domain=streamdetails.provider,
            aa_provider_domain=self.domain,
            analysis=AudioAnalysisData(extra_data={"retry_after": retry_after}),
            analysis_version=NO_MATCH_ANALYSIS_VERSION,
            media_type=streamdetails.media_type,
        )

    async def _record_existing_identifiers(
        self, streamdetails: StreamDetails, track: Track
    ) -> None:
        """
        Persist a track's existing MBID/ISRC as an analysis result, as if freshly looked up.

        :param streamdetails: Stream details for the already-identified track.
        :param track: Library track carrying the existing identifiers.
        """
        extra_data: dict[str, Any] = {"source": "existing_tags"}
        if track.mbid:
            extra_data["mbid"] = track.mbid
        if isrc := track.get_external_id(ExternalID.ISRC):
            extra_data["isrc"] = isrc
        await self.mass.streams.audio_analysis.set_audio_analysis(
            item_id=streamdetails.item_id,
            provider_instance_id_or_domain=streamdetails.provider,
            aa_provider_domain=self.domain,
            analysis=AudioAnalysisData(extra_data=extra_data),
            analysis_version=self.analysis_version,
            media_type=streamdetails.media_type,
        )

    # None can signal an auth/bad-request failure as well as "no match", so don't cache it
    @use_cache(ACOUSTID_LOOKUP_CACHE_TTL, cache_none=False)
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
            # Selectors must be space-separated; AcoustID rejects '+'-joined values.
            "meta": "recordings releases releasegroups",
            "fingerprint": fingerprint,
            "duration": str(duration),
            "format": "json",
        }
        async with self.mass.http_session.get(ACOUSTID_LOOKUP_URL, params=params) as response:
            if response.status == 429:
                backoff = int(response.headers.get("Retry-After", 0))
                raise RateLimited("AcoustID rate limit", backoff_time=backoff)
            if 500 <= response.status < 600:
                raise ResourceTemporarilyUnavailable("AcoustID server error", backoff_time=30)
            if response.status in (401, 403):
                self.logger.error(
                    "AcoustID lookup unauthorised (HTTP %d) — check the configured API key",
                    response.status,
                )
                return None
            if response.status >= 400:
                self.logger.debug("AcoustID returned HTTP %d — discarding result", response.status)
                return None
            payload = await response.json()
        if not isinstance(payload, dict):
            self.logger.debug(
                "AcoustID response was not a JSON object (got %s) — discarding",
                type(payload).__name__,
            )
            return None
        if payload.get("status") != "ok":
            self.logger.debug("AcoustID response status=%s — discarding", payload.get("status"))
            return None
        return payload

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
            self.logger.debug("Could not fetch MusicBrainz details for recording %s: %s", mbid, err)
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

    async def _lookup_release_group_via_mb(
        self,
        track: Any,
        album_name: str | None,
    ) -> tuple[str, str | None] | None:
        """
        Resolve the album release-group by querying MusicBrainz directly.

        :param track: Library track row with name and artists.
        :param album_name: Library album title to match against the result.
        :returns: ``(release_group_mbid, release_group_title)`` on a confident
            match, or ``None`` when nothing usable comes back.
        """
        track_name = getattr(track, "name", None)
        artists = getattr(track, "artists", None) or []
        artist_name = next(
            (getattr(a, "name", None) for a in artists if getattr(a, "name", None)),
            None,
        )
        if not (album_name and track_name and artist_name):
            self.logger.debug(
                "Skipping MusicBrainz lookup — library row missing %s",
                ", ".join(
                    label
                    for label, val in (
                        ("album name", album_name),
                        ("track name", track_name),
                        ("artist name", artist_name),
                    )
                    if not val
                ),
            )
            return None
        mb = self.mass.get_provider("musicbrainz", provider_type=MusicbrainzProvider)
        if mb is None:
            self.logger.debug("Skipping MusicBrainz lookup — MusicBrainz provider is not loaded")
            return None
        # Pre-flatten separators MB's tokenizer drops, so the Lucene phrase
        # query matches across user-tag and MB-stored punctuation variants
        # (e.g. user "My Love - X" against MB "My Love: X" / "My Love (X)").
        flat_artist = _flatten_separators_for_lucene(artist_name)
        flat_album = _flatten_separators_for_lucene(album_name)
        flat_track = _flatten_separators_for_lucene(track_name)
        try:
            result = await mb.search(
                artistname=flat_artist,
                albumname=flat_album,
                trackname=flat_track,
            )
        except (
            MusicAssistantError,
            aiohttp.ClientError,
            TimeoutError,
            json.JSONDecodeError,
        ) as err:
            self.logger.debug("MusicBrainz lookup failed for album=%r: %s", album_name, err)
            return None
        if result is None:
            self.logger.debug(
                "MusicBrainz found no match for artist=%r album=%r track=%r",
                flat_artist,
                flat_album,
                flat_track,
            )
            return None
        _, release_group, _ = result
        # MB's Lucene query is already artist-scoped, but title-confirm the RG
        # to defend against same-title-different-artist collisions.
        if (
            _title_match_strength(
                _normalize_for_match(album_name),
                _normalize_for_match(release_group.title or ""),
                asymmetric=True,
            )
            == 0
        ):
            self.logger.debug(
                "Rejecting MusicBrainz result — release-group %s is titled %r, "
                "does not match library album %r",
                release_group.id,
                release_group.title,
                album_name,
            )
            return None
        return release_group.id, release_group.title

    async def _maybe_set_album_release_group(
        self,
        streamdetails: StreamDetails,
        library_track: Any = None,
    ) -> None:
        """
        Run the album-level release-group consensus for the track's album.

        :param streamdetails: Stream details for the track that just finished analysis.
        :param library_track: Pre-fetched library Track row; fetched here if omitted.
        """
        track = library_track
        if track is None:
            track = await self.mass.music.tracks.get_library_item_by_prov_id(
                streamdetails.item_id, streamdetails.provider
            )
        # start_analysis validated that the library row exists; if track is None
        # here the row was deleted mid-analysis and the AttributeError below is
        # the right signal — post_analysis catches it.
        album = track.album
        if album is None:
            self.logger.debug(
                "Skipping album lookup for %s/%s — track has no album in library",
                streamdetails.provider,
                streamdetails.item_id,
            )
            return

        album_item_id_raw = getattr(album, "item_id", None)
        if not album_item_id_raw:
            self.logger.debug("Skipping album lookup — track's album reference has no library id")
            return
        try:
            album_item_id = int(album_item_id_raw)
        except TypeError, ValueError:
            self.logger.debug(
                "Skipping album lookup — album id %r is not an integer", album_item_id_raw
            )
            return

        try:
            library_album = await self.mass.music.albums.get_library_item(album_item_id)
        except MusicAssistantError as err:
            self.logger.debug("Could not load library album %s: %s", album_item_id, err)
            return
        if library_album.get_external_id(ExternalID.MB_RELEASEGROUP):
            self.logger.debug(
                "Skipping album lookup — album %s already has a MusicBrainz release-group",
                album_item_id,
            )
            return

        album_tracks = await self.mass.music.albums.get_library_album_tracks(album_item_id)

        # Audio_analysis rows are keyed per music-provider; voting can only span
        # tracks served by the same provider as the play that triggered us, and
        # the quorum denominator must be scoped the same way.
        native_ids: list[str] = []
        for at in album_tracks:
            for pm in at.provider_mappings:
                if streamdetails.provider in (pm.provider_instance, pm.provider_domain):
                    native_ids.append(pm.item_id)
                    break
        total = len(native_ids)
        if total == 0:
            self.logger.debug(
                "Skipping album lookup — album %s has no library tracks from %s",
                album_item_id,
                streamdetails.provider,
            )
            return

        extras = await self.mass.streams.audio_analysis.get_extra_data_for_album_tracks(
            native_ids, streamdetails.provider, aa_provider_domain=self.domain
        )
        voting_rows = [
            extra
            for extra in extras
            if isinstance(extra.get("release_groups"), list) and extra["release_groups"]
        ]

        rg_id: str | None = None
        rg_title: str | None = None
        source: str = ""

        # First try: AcoustID-derived consensus across this album's tracks.
        if len(voting_rows) * 2 < total:
            self.logger.debug(
                "Not enough analysed tracks to identify album %s yet — %d of %d %s "
                "tracks analysed so far (need at least half)",
                library_album.name,
                len(voting_rows),
                total,
                streamdetails.provider,
            )
        else:
            winner = self._pick_consensus_winner(
                voting_rows,
                library_album.name,
                expected_artist=_extract_primary_artist_name(track),
            )
            if winner is not None:
                rg_id, rg_title, _ = winner
                source = "consensus"

        # Fallback: ask MusicBrainz directly. Covers the case where AcoustID's
        # fingerprint match is linked to a different MB recording entity than
        # the one the user's specific release uses (compilation/single splits).
        if rg_id is None:
            mb_result = await self._lookup_release_group_via_mb(
                track=track, album_name=library_album.name
            )
            if mb_result is not None:
                rg_id, rg_title = mb_result
                source = "MusicBrainz lookup"

        if rg_id is None:
            self.logger.info(
                "AcoustID release-group not identified for album=%r — leaving unchanged",
                library_album.name,
            )
            return

        self.logger.info(
            "AcoustID release-group identified for album=%r: %s (%r, via %s)",
            library_album.name,
            rg_id,
            rg_title,
            source,
        )
        await self.mass.music.albums.set_release_group(album_item_id, rg_id)

    def _pick_consensus_winner(
        self,
        voting_rows: list[dict[str, Any]],
        album_name: str | None,
        expected_artist: str | None = None,
    ) -> tuple[str, str | None, int] | None:
        """
        Pick the release-group with the strongest cross-track agreement.

        :param voting_rows: ``extra_data`` dicts for tracks that contributed
            release-group data.
        :param album_name: Library album title; restricts the pick to RGs whose
            title matches.
        :param expected_artist: Library track artist name; when set, RGs
            credited to a different artist are rejected.
        :returns: ``(release_group_mbid, release_group_title, coverage_count)``
            on a confident pick, or ``None`` to abstain.
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
            self.logger.debug(
                "Skipping album lookup — none of the %d analysed track(s) had release-group data",
                voting_size,
            )
            return None

        top_coverage = max(coverage.values())
        # Tolerate one missing track to absorb occasional AcoustID misses; for
        # singletons the floor degenerates to 1/1 (the one voting track must
        # contribute), and the name-match filter below is what really protects.
        required = voting_size if voting_size <= 2 else voting_size - 1
        survivors = [rg_id for rg_id, count in coverage.items() if count == top_coverage]
        if top_coverage < required:
            self.logger.debug(
                "Skipping album lookup — tracks disagree on the release-group "
                "(best match: %d of %d tracks, need %d)",
                top_coverage,
                voting_size,
                required,
            )
            return None

        album_name_norm = _normalize_for_match(album_name) if album_name else ""

        # Refuse to promote an RG whose title doesn't match the library album:
        # coverage alone proves the RG contains the played tracks, not that it
        # is the album the user has tagged. A coverage-correct, name-wrong RG
        # would feed metadata providers art for the wrong album. Substring is
        # accepted when MB's title is the longer side (edition prefixes etc.),
        # rejected when MB is more generic than the user tag.
        if album_name_norm:
            scored = [
                (
                    rg_id,
                    _title_match_strength(
                        album_name_norm,
                        _normalize_for_match(rg_data[rg_id].get("title") or ""),
                        asymmetric=True,
                    ),
                )
                for rg_id in survivors
            ]
            title_matched = [rg_id for rg_id, strength in scored if strength > 0]
            if not title_matched:
                sample = ", ".join(
                    f"{rg_data[rg_id].get('title')!r} (normalised "
                    f"{_normalize_for_match(rg_data[rg_id].get('title') or '')!r})"
                    for rg_id in survivors[:10]
                )
                self.logger.debug(
                    "Skipping album lookup — none of the %d candidate release-group "
                    "title(s) match library album %r (normalised %r); sample: %s",
                    len(survivors),
                    album_name,
                    album_name_norm,
                    sample,
                )
                return None
            # Prefer exact matches over substring matches when both exist.
            top_strength = max(strength for _, strength in scored)
            survivors = [rg_id for rg_id, strength in scored if strength == top_strength]

        if expected_artist:
            artist_norm = _normalize_for_match(expected_artist)

            def _artist_ok(rg_id: str) -> bool:
                # Unknown artist info is treated as compatible — only reject when
                # we positively know an RG is credited to a different artist.
                artists = rg_data[rg_id].get("artists") or []
                if not artists:
                    return True
                return any(
                    isinstance(a, str) and _normalize_for_match(a) == artist_norm for a in artists
                )

            filtered = [rg_id for rg_id in survivors if _artist_ok(rg_id)]
            if not filtered:
                self.logger.debug(
                    "Skipping album lookup — no candidate release-group is credited "
                    "to %r (rejected: %s)",
                    expected_artist,
                    [
                        (rg_id, rg_data[rg_id].get("artists"))
                        for rg_id in survivors
                        if rg_data[rg_id].get("artists")
                    ],
                )
                return None
            survivors = filtered

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
    # Per-result chosen recording: (track_strength, score, recording, acoustid_id, album_strength)
    # where strengths are 0=no match, 1=substring, 2=exact.
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
                _recording_title_match_strength(rec, expected_track_title),
                _release_title_match_strength(rec, expected_album_title),
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

    # Across-result selection: prefer stronger track-title match (exact > substring),
    # then stronger album-title match, then score. Tiering prevents a substring
    # album hit (e.g. user "Greatest Hits: 40 Trips Around The Sun" against MB's
    # generic "Greatest Hits") from beating a recording with an exact album hit.
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
    # Carry the source recording's artist credits onto each RG it contributes
    # so the consensus winner picker can reject RGs introduced by a wrong-artist
    # recording that shares the audio fingerprint.
    recording_artists = [
        a["name"]
        for a in (recording.get("artists") or [])
        if isinstance(a, dict) and isinstance(a.get("name"), str)
    ]
    raw = recording.get("releasegroups") or []
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    truncated = False
    for rg in raw:
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
                "artists": list(recording_artists),
            }
        )
        if len(out) >= MAX_RELEASE_GROUPS_PER_RECORDING:
            truncated = len(raw) > len(out)
            break
    if truncated:
        _LOGGER.debug(
            "Per-recording release-group cap (%d) reached for recording %s — kept %d of %d",
            MAX_RELEASE_GROUPS_PER_RECORDING,
            recording.get("id"),
            len(out),
            len(raw),
        )
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


def _release_title_match_strength(recording: dict[str, Any], expected_title: str | None) -> int:
    """
    Return the strongest title match across the recording's releases.

    :param recording: Single recording entry from an AcoustID result.
    :param expected_title: Album title from the library row, if any.
    :returns: 0 for no match, 1 for substring, 2 for exact.
    """
    if not expected_title:
        return 0
    expected_norm = _normalize_for_match(expected_title)
    if not expected_norm:
        return 0
    # The library "album name" is conceptually the release-group title, so check
    # both surfaces — release-groups for the canonical album identity, releases
    # for country/edition variants that may not roll up to the same RG title.
    # Album matching is asymmetric: MB may have an expanded title with edition
    # info, but a generic MB title must not claim to match a specific user tag.
    best = 0
    for entry in (recording.get("releasegroups") or []) + (recording.get("releases") or []):
        title = entry.get("title")
        if not isinstance(title, str):
            continue
        strength = _title_match_strength(
            expected_norm, _normalize_for_match(title), asymmetric=True
        )
        if strength > best:
            best = strength
            if best == 2:
                break
    return best


def _recording_title_match_strength(recording: dict[str, Any], expected_title: str | None) -> int:
    """
    Return the strength of the recording-title match against ``expected_title``.

    :param recording: Single recording entry from an AcoustID result.
    :param expected_title: Track title from the library row, if any.
    :returns: 0 for no match, 1 for substring, 2 for exact.
    """
    if not expected_title:
        return 0
    expected_norm = _normalize_for_match(expected_title)
    if not expected_norm:
        return 0
    title = recording.get("title")
    if not isinstance(title, str):
        return 0
    return _title_match_strength(expected_norm, _normalize_for_match(title))


def _title_match_strength(a_norm: str, b_norm: str, *, asymmetric: bool = False) -> int:
    """
    Return 0, 1 or 2 for two already-normalised titles.

    :param a_norm: Normalised title from the library row.
    :param b_norm: Normalised title from MusicBrainz.
    :param asymmetric: When True, the substring fallback fires only when
        ``a_norm`` is shorter than ``b_norm`` — i.e. the MB title is allowed
        to add a qualifier (edition, prefix) the user tag is missing, but a
        generic MB title cannot claim to match a more-specific user tag.
    :returns: 0 for no match, 1 for substring, 2 for exact.
    """
    if not a_norm or not b_norm:
        return 0
    if a_norm == b_norm:
        return 2
    # Substring fallback handles medleys and any version qualifier the
    # stripper missed; the ≥2 substantive-token guard blocks single-word
    # collisions like "Trouble" matching "I'm In Trouble".
    if asymmetric:
        if len(a_norm) > len(b_norm):
            return 0
        shorter, longer = a_norm, b_norm
    else:
        shorter, longer = (a_norm, b_norm) if len(a_norm) <= len(b_norm) else (b_norm, a_norm)
    substantive = sum(1 for tok in shorter.split() if len(tok) >= 2)
    if substantive < 2:
        return 0
    if f" {shorter} " in f" {longer} ":
        return 1
    return 0


def _normalize_for_match(value: str) -> str:
    """Return a casefolded, accent-stripped, punctuation-free form for title comparison."""
    # Strip version suffixes (remasters, editions, "feat." credits).
    stripped, _ = parse_title_and_version(value, strip_for_search=True)
    # "&" and "and" are interchangeable in titles; collapse to one form.
    stripped = stripped.replace("&", " and ")
    # create_safe_string casefolds and accent-strips; pre-spacing non-alnum
    # keeps separator variants ("AC/DC" vs "AC DC") equal.
    spaced = "".join(c if c.isalnum() else " " for c in stripped)
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


def _extract_primary_artist_name(track: Any) -> str | None:
    """Pull the first non-empty artist name from the library track, or None."""
    if track is None:
        return None
    artists = getattr(track, "artists", None) or []
    for artist in artists:
        name = getattr(artist, "name", None)
        if isinstance(name, str) and name:
            return name
    return None


_LUCENE_SEPARATOR_RE = re.compile(r"[\-:()\[\]/]")


def _flatten_separators_for_lucene(value: str) -> str:
    """Collapse separators MB's tokenizer drops so phrase queries match across variants."""
    # MB's tokenizer drops "-", ":", "(", ")", "[", "]", "/" — without
    # pre-flattening, a user tag "My Love - X" never phrase-matches an MB title
    # stored as "My Love: X".
    return " ".join(_LUCENE_SEPARATOR_RE.sub(" ", value).split())
