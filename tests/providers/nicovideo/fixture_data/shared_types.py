"""
Manually managed shared types for fixture system.

This file contains type definitions that are shared between the fixture
repository and the server repository. Unlike generated files, these are
manually maintained and versioned.
"""

from __future__ import annotations

# Pydantic requires runtime type information, so these imports cannot be in TYPE_CHECKING block
from niconico.objects.video.watch import (  # noqa: TC002
    WatchData,
    WatchLazyData,
    WatchMediaAudio,
)
from pydantic import BaseModel


class StreamFixtureData(BaseModel):
    """
    Fixture data for stream conversion tests.

    This type is stored in fixtures and reconstructed into StreamConversionData
    during test execution with stub values for unstable fields (hls_url, domand_bid,
    hls_playlist_text).

    Attributes:
        watch_data: Video watch page data from niconico
        watch_lazy_data: Lazily loaded watch data (owner, series) from niconico
        selected_audio: Selected audio track information
    """

    watch_data: WatchData
    watch_lazy_data: WatchLazyData
    selected_audio: WatchMediaAudio
