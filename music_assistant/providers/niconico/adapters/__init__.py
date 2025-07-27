"""NicoNico adapters package."""

from music_assistant.providers.niconico.adapters.auth import NiconicoAuthAdapter
from music_assistant.providers.niconico.adapters.base import NiconicoBaseAdapter
from music_assistant.providers.niconico.adapters.mylist import NiconicoMylistAdapter
from music_assistant.providers.niconico.adapters.search import NiconicoSearchAdapter
from music_assistant.providers.niconico.adapters.series import NiconicoSeriesAdapter
from music_assistant.providers.niconico.adapters.user import NicoNicoUserAdapter
from music_assistant.providers.niconico.adapters.video import NiconicoVideoAdapter

__all__ = [
    "NicoNicoUserAdapter",
    "NiconicoAuthAdapter",
    "NiconicoBaseAdapter",
    "NiconicoMylistAdapter",
    "NiconicoSearchAdapter",
    "NiconicoSeriesAdapter",
    "NiconicoVideoAdapter",
]
