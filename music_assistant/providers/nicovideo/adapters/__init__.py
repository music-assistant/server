"""nicovideo adapters package."""

from __future__ import annotations

from music_assistant.providers.nicovideo.adapters.auth import NicovideoAuthAdapter
from music_assistant.providers.nicovideo.adapters.base import NicovideoBaseAdapter
from music_assistant.providers.nicovideo.adapters.mylist import NicovideoMylistAdapter
from music_assistant.providers.nicovideo.adapters.search import NicovideoSearchAdapter
from music_assistant.providers.nicovideo.adapters.series import NicovideoSeriesAdapter
from music_assistant.providers.nicovideo.adapters.user import NicovideoUserAdapter
from music_assistant.providers.nicovideo.adapters.video import NicovideoVideoAdapter

__all__ = [
    "NicovideoAuthAdapter",
    "NicovideoBaseAdapter",
    "NicovideoMylistAdapter",
    "NicovideoSearchAdapter",
    "NicovideoSeriesAdapter",
    "NicovideoUserAdapter",
    "NicovideoVideoAdapter",
]
