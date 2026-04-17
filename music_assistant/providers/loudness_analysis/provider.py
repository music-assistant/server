"""Loudness Analysis provider - measures EBU R128 integrated loudness via FFmpeg ebur128."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.enums import (
    MediaType,
    ProviderType,
    StreamType,
    VolumeNormalizationMode,
)

from music_assistant.constants import (
    DB_TABLE_AUDIO_ANALYSIS,
    DB_TABLE_PROVIDER_MAPPINGS,
    LOUDNESS_MEASUREMENT_MIN_LUFS,
)
from music_assistant.helpers.datetime import local_clock_time_to_utc
from music_assistant.helpers.ffmpeg import FFMpeg
from music_assistant.helpers.tags import write_replaygain_track_gain
from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.models.audio_analysis_provider import AudioAnalysisProvider
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant

MAX_DURATION_SECONDS = 600
MIN_DURATION_SECONDS = 10

CONF_ANALYZE_LOCAL_FILES_BACKGROUND = "analyze_local_files_background"
CONF_WRITE_REPLAYGAIN_TAGS = "write_replaygain_tags"

BACKGROUND_TASK_ID = "loudness_analysis_background"
BACKGROUND_BATCH_SIZE = 250
BACKGROUND_SLEEP_BETWEEN_TRACKS = 2.0
FILESYSTEM_PROVIDER_DOMAINS: tuple[str, ...] = (
    "filesystem_local",
    "filesystem_smb",
    "filesystem_nfs",
)


@dataclass
class LoudnessSessionData:
    """Per-session state for a loudness analysis job."""

    ffmpeg: FFMpeg
    chunks_received: int = 0
    eof_sent: bool = False


class LoudnessAnalysisProvider(AudioAnalysisProvider):
    """Audio analysis provider that measures EBU R128 integrated loudness."""

    analysis_version: int = 1

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature],
    ) -> None:
        """Initialize the provider."""
        super().__init__(mass, manifest, config, supported_features)
        self._data: dict[str, LoudnessSessionData] = {}

    async def loaded_in_mass(self) -> None:
        """Register the scheduled background analysis task (if enabled)."""
        await super().loaded_in_mass()
        if self.config.get_value(CONF_ANALYZE_LOCAL_FILES_BACKGROUND):
            utc_hour, utc_minute = local_clock_time_to_utc(0, 0)
            self.mass.tasks.register_scheduled_task(
                task_id=BACKGROUND_TASK_ID,
                name="Loudness analysis for local files",
                handler=self._analyze_local_files_background,
                schedule=TaskSchedule.daily(hour=utc_hour, minute=utc_minute),
                metadata={"task_domain": "loudness_analysis"},
            )

    async def process_pcm_chunk(
        self,
        session_id: str,
        pcm_chunk: bytes,
    ) -> None:
        """Feed a PCM chunk to the session's ebur128 ffmpeg process."""
        data = self._data.get(session_id)
        if not data or data.eof_sent:
            return
        data.chunks_received += 1
        await data.ffmpeg.write(pcm_chunk)
        if data.chunks_received >= MAX_DURATION_SECONDS:
            # cap the analysis window for very long streams
            await self._send_eof(data)

    async def cancel(self, session_id: str) -> None:
        """Abort an in-progress loudness analysis session."""
        data = self._data.pop(session_id, None)
        if data:
            with contextlib.suppress(Exception):
                await data.ffmpeg.close()
        await super().cancel(session_id)

    async def unload(self, is_removed: bool = False) -> None:
        """Unregister the scheduled background task on provider unload."""
        self.mass.tasks.unregister_scheduled_task(BACKGROUND_TASK_ID)
        await super().unload(is_removed)

    async def _start_analysis(
        self,
        session_id: str,
        streamdetails: StreamDetails,
        audio_format: AudioFormat,
    ) -> bool:
        """Prepare provider state for a new analysis session."""
        # skip when the requesting player has explicitly opted out of normalization;
        # the nightly background job will pick up the measurement if ever needed
        if streamdetails.volume_normalization_mode == VolumeNormalizationMode.DISABLED:
            return False
        ffmpeg = FFMpeg(
            audio_input="-",
            input_format=audio_format,
            output_format=audio_format,
            audio_output="NULL",
            filter_params=["ebur128=framelog=verbose"],
            collect_log_history=True,
            loglevel="info",
        )
        await ffmpeg.start()
        self._data[session_id] = LoudnessSessionData(ffmpeg=ffmpeg)
        return True

    async def _finalize(self, session_id: str) -> None:
        """Persist the final loudness measurement for the session."""
        data = self._data.pop(session_id, None)
        if not data:
            return

        await self._send_eof(data)
        try:
            await data.ffmpeg.wait()
        except Exception as err:
            self.logger.debug("Loudness analysis ffmpeg failed: %s", err)
            await data.ffmpeg.close()
            return

        loudness = _parse_integrated_loudness(data.ffmpeg.log_history)
        await data.ffmpeg.close()

        session = self._sessions.get(session_id)
        if session is None:
            return

        if data.chunks_received < MIN_DURATION_SECONDS:
            self.logger.debug(
                "Loudness analysis for %s skipped: "
                "insufficient audio data (%s/%s seconds analyzed)",
                session.streamdetails.uri,
                data.chunks_received,
                MIN_DURATION_SECONDS,
            )
            return

        if loudness is None:
            self.logger.debug(
                "Could not determine loudness of %s from buffer analysis",
                session.streamdetails.uri,
            )
            return

        if loudness <= LOUDNESS_MEASUREMENT_MIN_LUFS:
            # ebur128 reports ~-70 LUFS on near-silence / cancelled streams,
            # which would cause huge gain corrections on subsequent plays.
            self.logger.debug(
                "Loudness measurement for %s discarded: "
                "%s LUFS is below the reliability threshold (%s LUFS)",
                session.streamdetails.uri,
                loudness,
                LOUDNESS_MEASUREMENT_MIN_LUFS,
            )
            return

        analysis = AudioAnalysisData(loudness_integrated=loudness)
        await self.mass.streams.audio_analysis.set_audio_analysis(
            item_id=session.streamdetails.item_id,
            provider_instance_id_or_domain=session.streamdetails.provider,
            aa_provider_domain=self.domain,
            analysis=analysis,
            analysis_version=self.analysis_version,
            media_type=session.streamdetails.media_type,
        )
        # update in-memory streamdetails so subsequent seeks use the measurement
        # instead of dynamic normalization
        session.streamdetails.loudness = round(loudness, 2)
        self.logger.debug(
            "Loudness measurement for %s: %s LUFS",
            session.streamdetails.uri,
            loudness,
        )

    async def _send_eof(self, data: LoudnessSessionData) -> None:
        """Signal end-of-input to the session's ffmpeg process (idempotent)."""
        if data.eof_sent:
            return
        data.eof_sent = True
        with contextlib.suppress(Exception):
            await data.ffmpeg.write_eof()

    async def _analyze_local_files_background(self) -> None:
        """Run one background batch of loudness analysis for local file tracks."""
        if not self.config.get_value(CONF_ANALYZE_LOCAL_FILES_BACKGROUND):
            return

        rows = await self._find_local_tracks_missing_loudness(BACKGROUND_BATCH_SIZE)
        if not rows:
            self.logger.debug("Background loudness: no local tracks pending analysis")
            return

        write_tags = bool(self.config.get_value(CONF_WRITE_REPLAYGAIN_TAGS))
        self.logger.info(
            "Background loudness: analyzing %d local track(s), write_tags=%s",
            len(rows),
            write_tags,
        )

        processed = 0
        for row in rows:
            item_id = str(row["item_id"])
            provider_instance = str(row["provider_instance"])
            music_prov = self.mass.get_provider(provider_instance, provider_type=MusicProvider)
            if music_prov is None or not music_prov.available:
                # storage may be offline right now (e.g. NAS asleep) — stop the batch
                # rather than churning through failures for the remaining tracks
                self.logger.debug(
                    "Background loudness: provider %s unavailable, aborting batch",
                    provider_instance,
                )
                break

            if await self._analyze_single_file(music_prov, item_id, write_tags):
                processed += 1
            await asyncio.sleep(BACKGROUND_SLEEP_BETWEEN_TRACKS)

        self.logger.info("Background loudness: analyzed %d/%d track(s)", processed, len(rows))

    async def _analyze_single_file(
        self,
        music_prov: MusicProvider,
        item_id: str,
        write_tags: bool,
    ) -> bool:
        """Analyze a single local file and persist the measurement."""
        try:
            streamdetails = await music_prov.get_stream_details(item_id, MediaType.TRACK)
        except Exception as err:
            self.logger.debug(
                "Background loudness: skipping %s (stream details failed: %s)",
                item_id,
                err,
            )
            return False

        if streamdetails.stream_type != StreamType.LOCAL_FILE:
            return False
        if not isinstance(streamdetails.path, str) or not streamdetails.path:
            return False

        loudness = await _run_ebur128_on_file(streamdetails.path, streamdetails.audio_format)
        if loudness is None or loudness <= LOUDNESS_MEASUREMENT_MIN_LUFS:
            self.logger.debug(
                "Background loudness: could not measure %s (result=%s)", item_id, loudness
            )
            return False

        await self.mass.streams.audio_analysis.set_track_loudness(
            item_id=item_id,
            provider_instance_id_or_domain=music_prov.instance_id,
            loudness=loudness,
        )
        self.logger.debug("Background loudness: %s = %.2f LUFS", item_id, loudness)

        if write_tags:
            # ReplayGain 2.0: track_gain_db = -18 - loudness_lufs
            track_gain_db = -18.0 - loudness
            ok = await write_replaygain_track_gain(streamdetails.path, track_gain_db)
            if ok:
                self.logger.debug(
                    "Background loudness: wrote ReplayGain tag to %s", streamdetails.path
                )
        return True

    async def _find_local_tracks_missing_loudness(self, limit: int) -> list[dict[str, object]]:
        """Return up to N tracks from filesystem providers without a loudness measurement."""
        filesystem_domains = tuple(
            domain
            for domain in FILESYSTEM_PROVIDER_DOMAINS
            if any(
                p.domain == domain and p.available
                for p in self.mass.get_providers(ProviderType.MUSIC)
            )
        )
        if not filesystem_domains:
            return []

        domains_sql = ", ".join(f"'{d}'" for d in filesystem_domains)
        query = (
            f"SELECT pm.item_id AS item_id, pm.provider_instance AS provider_instance "
            f"FROM {DB_TABLE_PROVIDER_MAPPINGS} pm "
            f"LEFT JOIN {DB_TABLE_AUDIO_ANALYSIS} aa "
            f"  ON aa.item_id = pm.item_id "
            f"  AND aa.provider = pm.provider_instance "
            f"  AND aa.aa_provider_domain = 'loudness_analysis' "
            f"  AND aa.media_type = 'track' "
            f"WHERE pm.media_type = 'track' "
            f"  AND pm.provider_domain IN ({domains_sql}) "
            f"  AND aa.id IS NULL"
        )
        rows = await self.mass.music.database.get_rows_from_query(query, limit=limit)
        return [dict(r) for r in rows]


def _parse_integrated_loudness(log_lines: Iterable[str]) -> float | None:
    """Extract the Integrated loudness value (LUFS) from an ebur128 ffmpeg log."""
    log = "\n".join(log_lines)
    try:
        loudness_str = log.split("Integrated loudness")[1].split("I:")[1].split("LUFS")[0]
        return float(loudness_str.strip())
    except (IndexError, ValueError, AttributeError):
        return None


async def _run_ebur128_on_file(file_path: str, audio_format: AudioFormat) -> float | None:
    """Run ebur128 on a local audio file and return the integrated loudness (LUFS)."""
    try:
        async with FFMpeg(
            audio_input=file_path,
            input_format=audio_format,
            output_format=audio_format,
            audio_output="NULL",
            filter_params=["ebur128=framelog=verbose"],
            collect_log_history=True,
            loglevel="info",
        ) as ffmpeg:
            await ffmpeg.wait()
            return _parse_integrated_loudness(ffmpeg.log_history)
    except Exception:
        return None
