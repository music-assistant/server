"""Constants for the nicovideo provider in Music Assistant."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from music_assistant_models.enums import ContentType

if TYPE_CHECKING:
    from typing import Literal


class ApiPriority(Enum):
    """Priority levels for nicovideo API calls."""

    HIGH = "high"
    LOW = "low"


# Network constants
NICOVIDEO_USER_AGENT = "Music Assistant/1.0"

# Note: "Domand" is the actual spelling used in niconico's API (not a typo).
# This appears in API endpoints like https://asset.domand.nicovideo.jp/ and throughout
# their media delivery system (WatchMediaDomand, WatchMediaDomandVideo, WatchMediaDomandAudio, etc.)
DOMAND_BID_COOKIE_NAME = "domand_bid"

# Audio format constants based on niconico official specifications
# Sources:
# - https://qa.nicovideo.jp/faq/show/21908
# - https://qa.nicovideo.jp/faq/show/5685
NICOVIDEO_CONTENT_TYPE = ContentType.MP4
NICOVIDEO_CODEC_TYPE = ContentType.AAC
NICOVIDEO_AUDIO_CHANNELS = 2  # Stereo (2ch)
NICOVIDEO_AUDIO_BIT_DEPTH = 16  # 16-bit (confirmed from downloaded video analysis)

# Content filtering constants
# Default behavior for sensitive content handling
SENSITIVE_CONTENTS: Literal["mask", "filter"] = "mask"
