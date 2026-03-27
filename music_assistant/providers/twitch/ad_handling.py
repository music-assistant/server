"""Ad handling for Twitch streams via Streamlink monkey-patching."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Module-level flag — GIL makes bool read/write atomic.
# Set by Streamlink writer (runs in thread), read by provider.
ad_break_active: bool = False


def patch_ad_handling(reader_cls: type | None = None) -> None:
    """Patch TwitchHLSStreamReader.__writer__ to pass through ads with logging.

    Args:
        reader_cls: The actual TwitchHLSStreamReader class to patch. If None,
            patches the imported class (which may differ from the class Streamlink's
            plugin system uses at runtime due to fresh module loading).
            Callers should pass the reader class from the resolved stream object
            to ensure the correct class is patched.

    """
    from streamlink.plugins.twitch import (  # noqa: PLC0415
        TwitchHLSSegment,
        TwitchHLSStreamReader,
        TwitchHLSStreamWriter,
    )

    target_reader = reader_cls or TwitchHLSStreamReader

    class PassthroughTwitchWriter(TwitchHLSStreamWriter):
        """Writer that logs ad segments and tracks ad break state."""

        def should_filter_segment(self, segment: TwitchHLSSegment) -> bool:  # type: ignore[override]
            """Never filter — let all segments through."""
            global ad_break_active  # noqa: PLW0603
            if segment.ad:
                ad_break_active = True
                logger.debug(
                    "Ad segment %d (%.1fs): passing through as audio",
                    segment.num,
                    segment.duration,
                )
            else:
                if ad_break_active:
                    logger.debug(
                        "Content segment %d: ad block ended, audio resuming",
                        segment.num,
                    )
                ad_break_active = False
            return False

    target_reader.__writer__ = PassthroughTwitchWriter  # type: ignore[attr-defined]
    logger.info("Twitch ad handling: passthrough (ads play as audio)")
