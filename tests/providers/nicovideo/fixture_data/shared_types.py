"""Manually managed shared types for fixture system.

This file contains type definitions that are shared between the fixture
repository and the server repository. Unlike generated files, these are
manually maintained and versioned.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# Pydantic requires runtime type information, so these imports cannot be in TYPE_CHECKING block
from niconico.objects.video.watch import WatchData, WatchMediaDomandAudio  # noqa: TC002

if TYPE_CHECKING:

    class _PydanticBaseModel:
        """Typed fallback base for mypy when pydantic is not installed."""

        def __init__(self, **data: object) -> None: ...

else:
    from pydantic import BaseModel as _PydanticBaseModel


class StreamFixtureData(_PydanticBaseModel):
    """Fixture data for stream conversion tests.

    This type is stored in fixtures and reconstructed into StreamConversionData
    during test execution with stub values for unstable fields (hls_url, domand_bid,
    hls_playlist_text).

    Attributes:
        watch_data: Video watch page data from niconico
        selected_audio: Selected audio track information
    """

    watch_data: WatchData
    selected_audio: WatchMediaDomandAudio
