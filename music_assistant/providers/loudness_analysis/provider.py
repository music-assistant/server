"""Loudness Analysis provider - measures EBU R128 integrated loudness via FFmpeg ebur128."""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, VolumeNormalizationMode

from music_assistant.constants import LOUDNESS_MEASUREMENT_MIN_LUFS
from music_assistant.helpers.ffmpeg import FFMpeg
from music_assistant.helpers.tags import write_replaygain_track_gain
from music_assistant.models.audio_analysis import AudioAnalysisData, AudioAnalysisError
from music_assistant.models.audio_analysis_provider import AudioAnalysisProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant

MAX_DURATION_SECONDS = 600
MIN_DURATION_SECONDS = 10

CONF_WRITE_REPLAYGAIN_TAGS = "write_replaygain_tags"

_INTEGRATED_RE = re.compile(r"Integrated loudness:.*?I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", re.DOTALL)
_LRA_RE = re.compile(r"Loudness range:.*?LRA:\s*(-?\d+(?:\.\d+)?)\s*LU", re.DOTALL)
_TRUE_PEAK_RE = re.compile(r"True peak:.*?Peak:\s*(-?\d+(?:\.\d+)?)\s*dBFS", re.DOTALL)


@dataclass
class LoudnessSessionData:
    """Per-session state for a loudness analysis job."""

    ffmpeg: FFMpeg
    chunks_received: int = 0
    eof_sent: bool = False


class LoudnessAnalysisProvider(AudioAnalysisProvider):
    """Audio analysis provider that measures EBU R128 integrated loudness."""

    analysis_version: int = 2

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

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return config entries for this provider."""
        return (
            ConfigEntry(
                key=CONF_WRITE_REPLAYGAIN_TAGS,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                required=False,
            ),
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
            with contextlib.suppress(OSError):
                await data.ffmpeg.close()
        await super().cancel(session_id)

    async def post_analysis(
        self,
        streamdetails: StreamDetails,
        analysis: AudioAnalysisData,
    ) -> None:
        """Write the ReplayGain track-gain tag back to the source file when configured."""
        if not isinstance(streamdetails.path, str) or not streamdetails.path:
            return
        if not self.config.get_value(CONF_WRITE_REPLAYGAIN_TAGS):
            return
        if analysis.loudness_integrated is None:
            return
        # ReplayGain 2.0: track_gain_db = -18 - loudness_lufs
        track_gain_db = -18.0 - analysis.loudness_integrated
        ok = await write_replaygain_track_gain(streamdetails.path, track_gain_db)
        if ok:
            self.logger.debug(
                "Wrote ReplayGain tag to %s (gain=%.2f dB)",
                streamdetails.path,
                track_gain_db,
            )

    async def _start_analysis(
        self,
        session_id: str,
        streamdetails: StreamDetails,
        audio_format: AudioFormat,
    ) -> bool:
        """Prepare provider state for a new analysis session."""
        # skip when nothing here normalizes on our side: the player opted out, or the
        # source levelled the audio itself and measuring its output would store that
        # level as the track's own. The nightly background job picks the measurement
        # up if it is ever needed
        if streamdetails.volume_normalization_mode in (
            VolumeNormalizationMode.DISABLED,
            VolumeNormalizationMode.SOURCE,
        ):
            return False
        ffmpeg = FFMpeg(
            audio_input="-",
            input_format=audio_format,
            output_format=audio_format,
            audio_output="NULL",
            filter_params=["ebur128=framelog=verbose:peak=true"],
            collect_log_history=True,
            loglevel="info",
        )
        await ffmpeg.start()
        self._data[session_id] = LoudnessSessionData(ffmpeg=ffmpeg)
        return True

    async def _finalize(self, session_id: str) -> AudioAnalysisData | None:
        """Persist the final loudness measurement for the session."""
        data = self._data.pop(session_id, None)
        if not data:
            return None

        await self._send_eof(data)
        try:
            await data.ffmpeg.wait()
        except Exception as err:
            # ffmpeg.wait() can surface process/pipe errors plus anything the ebur128
            # subprocess raises; broad so a failed measurement degrades to "no result"
            # rather than crashing finalize.
            self.logger.debug("Loudness analysis ffmpeg failed: %s", err)
            await data.ffmpeg.close()
            return None

        metrics = _parse_ebur128_metrics(data.ffmpeg.log_history)
        await data.ffmpeg.close()

        session = self._sessions.get(session_id)
        if session is None:
            return None

        if data.chunks_received < MIN_DURATION_SECONDS:
            raise AudioAnalysisError("track too short for loudness measurement")

        loudness, loudness_range, true_peak = metrics
        if loudness is None:
            self.logger.debug(
                "Could not determine loudness of %s from buffer analysis",
                session.streamdetails.uri,
            )
            return None

        if loudness <= LOUDNESS_MEASUREMENT_MIN_LUFS:
            # ebur128 reports ~-70 LUFS on a near-silent track; below the reliability floor
            # it would cause huge gain corrections, and the reading is deterministic per file.
            raise AudioAnalysisError("track too quiet to measure loudness")

        analysis = AudioAnalysisData(
            loudness_integrated=round(loudness, 2),
            loudness_range=round(loudness_range, 2) if loudness_range is not None else None,
            true_peak=round(true_peak, 2) if true_peak is not None else None,
        )
        # update in-memory streamdetails so subsequent seeks use the measurement
        # instead of dynamic normalization; provider-supplied loudness stays authoritative
        if session.streamdetails.loudness is None:
            session.streamdetails.loudness = round(loudness, 2)
        self.logger.debug(
            "Loudness measurement for %s: %s LUFS (LRA=%s LU, peak=%s dBTP)",
            session.streamdetails.uri,
            loudness,
            loudness_range,
            true_peak,
        )
        return analysis

    async def _send_eof(self, data: LoudnessSessionData) -> None:
        """Signal end-of-input to the session's ffmpeg process (idempotent)."""
        if data.eof_sent:
            return
        data.eof_sent = True
        with contextlib.suppress(OSError):
            await data.ffmpeg.write_eof()


def _parse_ebur128_metrics(
    log_lines: Iterable[str],
) -> tuple[float | None, float | None, float | None]:
    """Extract (integrated_loudness, loudness_range, true_peak) from an ebur128 log."""
    log = "\n".join(log_lines)
    integrated = _match_float(_INTEGRATED_RE, log)
    lra = _match_float(_LRA_RE, log)
    true_peak = _match_float(_TRUE_PEAK_RE, log)
    return integrated, lra, true_peak


def _match_float(pattern: re.Pattern[str], text: str) -> float | None:
    match = pattern.search(text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None
