"""Ad handling for Twitch streams via Streamlink monkey-patching."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Module-level flag — GIL makes bool read/write atomic.
# Set by Streamlink writer (runs in thread), read by provider.
ad_break_active: bool = False

_SILENCE_DURATION = 1.0  # seconds — must match generated silence.ts


def patch_ad_handling(mode: str) -> None:
    """Patch TwitchHLSStreamReader.__writer__ based on the ad handling mode.

    Must be called once before any session.streams() call.

    Args:
        mode: "silence" or "passthrough"

    """
    from streamlink.plugins.twitch import (  # noqa: PLC0415
        TwitchHLSSegment,
        TwitchHLSStreamReader,
        TwitchHLSStreamWriter,
    )

    if mode == "passthrough":

        class PassthroughTwitchWriter(TwitchHLSStreamWriter):
            """Writer that logs ad segments but passes them through."""

            def should_filter_segment(self, segment: TwitchHLSSegment) -> bool:  # type: ignore[override]
                """Never filter — let all segments through."""
                if segment.ad:
                    logger.debug(
                        "Ad segment %d (%.1fs): passing through as audio",
                        segment.num,
                        segment.duration,
                    )
                return False

        TwitchHLSStreamReader.__writer__ = PassthroughTwitchWriter
        logger.info("Twitch ad handling: passthrough (ads play as audio)")

    else:
        silence_path = Path(__file__).parent / "data" / "silence.ts"
        silence_data = silence_path.read_bytes()
        logger.debug(
            "Twitch silence injection: loaded %d-byte clip from %s",
            len(silence_data),
            silence_path,
        )

        class SilenceInjectingTwitchWriter(TwitchHLSStreamWriter):
            """Writer that replaces ad segments with silence."""

            def should_filter_segment(self, segment: TwitchHLSSegment) -> bool:  # type: ignore[override]
                """Never filter — we handle ad segments in write() with silence injection."""
                return False

            def write(  # type: ignore[override]
                self,
                segment: TwitchHLSSegment,
                result: Any,
                *data: Any,
            ) -> None:
                """Write segment, replacing ads with silence."""
                global ad_break_active  # noqa: PLW0603
                if segment.ad:
                    ad_break_active = True
                    # segment.duration is 0.0 for some ad segment types;
                    # fall back to standard Twitch HLS segment length (2s).
                    effective_duration = segment.duration if segment.duration > 0 else 2.0
                    copies = max(1, round(effective_duration / _SILENCE_DURATION))
                    logger.debug(
                        "Ad segment %d (%.1fs): injecting silence (%d copies)",
                        segment.num,
                        segment.duration,
                        copies,
                    )
                    # Discard ad bytes without buffering
                    result.raw.drain_conn()
                    self.reader.buffer.write(silence_data * copies)  # type: ignore[no-untyped-call]
                    # Resume reader if paused — the base HLSStreamWriter pauses the
                    # reader when filtering segments, and resumes after writing data.
                    # Since we bypass the base write() for ad segments, we must resume
                    # explicitly so fd.read() isn't blocked.
                    if self.reader.is_paused():
                        logger.debug("Resuming reader after silence injection")
                        self.reader.resume()
                    self._prev_was_ad = True
                else:
                    ad_break_active = False
                    if getattr(self, "_prev_was_ad", False):
                        logger.debug(
                            "Content segment %d: ad block ended, audio resuming",
                            segment.num,
                        )
                    self._prev_was_ad = False
                    super().write(segment, result, *data)

        TwitchHLSStreamReader.__writer__ = SilenceInjectingTwitchWriter
        logger.info("Twitch ad handling: silence injection enabled")
