"""Ordered browse routing and URI handling for Yandex Music."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature
from music_assistant_models.media_items import BrowseFolder, ItemMapping, MediaItemType

from music_assistant.models.music_provider import MusicProvider

from .constants import (
    COLLECTION_FOLDER_ID,
    FOR_YOU_FOLDER_ID,
    LIKED_TRACKS_PLAYLIST_ID,
    LISTENING_HISTORY_FOLDER_ID,
    MY_WAVE_MODES_FOLDER_ID,
    MY_WAVE_PLAYLIST_ID,
    MY_WAVE_PRESETS_FOLDER_ID,
    MY_WAVES_FOLDER_ID,
    MY_WAVES_SET_FOLDER_ID,
    PINNED_ITEMS_FOLDER_ID,
    RADIO_FOLDER_ID,
    ROTOR_STATION_MY_WAVE,
    WAVE_MODE_PRESETS,
    WAVE_MODE_SEP,
    WAVES_FOLDER_ID,
    WAVES_LANDING_FOLDER_ID,
)

if TYPE_CHECKING:
    from .provider import YandexMusicProvider


type _BrowseResult = Sequence[MediaItemType | ItemMapping | BrowseFolder]

_LIBRARY_FOLDERS = frozenset({"tracks", "artists", "albums", "playlists", "audiobooks", "podcasts"})
_KNOWN_FOLDERS = _LIBRARY_FOLDERS | {
    LIKED_TRACKS_PLAYLIST_ID,
    WAVES_FOLDER_ID,
    RADIO_FOLDER_ID,
    MY_WAVES_FOLDER_ID,
    MY_WAVES_SET_FOLDER_ID,
    WAVES_LANDING_FOLDER_ID,
    FOR_YOU_FOLDER_ID,
    COLLECTION_FOLDER_ID,
    PINNED_ITEMS_FOLDER_ID,
    LISTENING_HISTORY_FOLDER_ID,
}


@dataclass(frozen=True)
class _BrowsePath:
    """A browse URI and its parsed route components."""

    path: str
    path_parts: tuple[str, ...]
    subpath: str | None
    sub_subpath: str | None

    @classmethod
    def parse(cls, path: str) -> _BrowsePath:
        """Parse the provider's existing URI forms without changing empty segments."""
        parts = tuple(path.split("://")[1].split("/")) if "://" in path else ()
        return cls(path, parts, parts[0] if parts else None, parts[1] if len(parts) > 1 else None)


@dataclass(frozen=True)
class _BrowseRoute:
    """A pure route predicate and its asynchronous handler."""

    matches: Callable[[_BrowsePath], bool]
    handler: Callable[[_BrowsePath], Awaitable[_BrowseResult]]


class _BrowseRouter:
    """Resolve provider browse URIs using an ordered registry of handlers."""

    def __init__(
        self, provider: YandexMusicProvider, *, routes: Sequence[_BrowseRoute] | None = None
    ) -> None:
        """
        Build the route registry for one provider instance.

        :param provider: Provider whose browse handlers and wave state are used.
        :param routes: Optional registry override; first matching route wins.
        """
        self.provider = provider
        self._routes = (
            tuple(routes)
            if routes is not None
            else (
                _BrowseRoute(lambda p: p.subpath == MY_WAVE_PLAYLIST_ID, self._my_wave),
                _BrowseRoute(
                    lambda p: (
                        p.subpath == MY_WAVE_MODES_FOLDER_ID
                        or bool(p.subpath and p.subpath.startswith(f"{MY_WAVE_MODES_FOLDER_ID}_"))
                    ),
                    self._wave_modes,
                ),
                _BrowseRoute(
                    lambda p: (
                        p.subpath == MY_WAVE_PRESETS_FOLDER_ID
                        or bool(p.subpath and p.subpath.startswith(f"{MY_WAVE_PRESETS_FOLDER_ID}_"))
                    ),
                    self._user_presets,
                ),
                _BrowseRoute(
                    lambda p: p.subpath == FOR_YOU_FOLDER_ID,
                    lambda p: provider._browse_for_you(p.path, list(p.path_parts)),
                ),
                _BrowseRoute(lambda p: p.subpath == COLLECTION_FOLDER_ID, self._collection),
                _BrowseRoute(
                    lambda p: p.subpath == "picks",
                    lambda p: provider._browse_picks(p.path, list(p.path_parts)),
                ),
                _BrowseRoute(
                    lambda p: p.subpath == "mixes",
                    lambda p: provider._browse_mixes(p.path, list(p.path_parts)),
                ),
                _BrowseRoute(
                    lambda p: p.subpath in (WAVES_FOLDER_ID, RADIO_FOLDER_ID),
                    lambda p: provider._browse_waves(p.path, list(p.path_parts)),
                ),
                _BrowseRoute(
                    lambda p: p.subpath == MY_WAVES_SET_FOLDER_ID,
                    lambda p: provider._browse_vibe_sets(p.path, list(p.path_parts)),
                ),
                _BrowseRoute(
                    lambda p: p.subpath == PINNED_ITEMS_FOLDER_ID,
                    lambda _p: provider._browse_pins(),
                ),
                _BrowseRoute(
                    lambda p: p.subpath == LISTENING_HISTORY_FOLDER_ID,
                    lambda _p: provider._browse_history(),
                ),
                _BrowseRoute(
                    lambda p: p.subpath == WAVES_LANDING_FOLDER_ID,
                    lambda p: provider._browse_waves_landing(p.path, list(p.path_parts)),
                ),
                _BrowseRoute(
                    lambda p: bool(p.subpath and p.subpath not in _KNOWN_FOLDERS), self._direct
                ),
                _BrowseRoute(lambda p: not p.subpath, self._root),
            )
        )

    async def dispatch(self, path: str) -> _BrowseResult:
        """Resolve the first matching route, retaining MA's library fallback."""
        parsed = _BrowsePath.parse(path)
        for route in self._routes:
            if route.matches(parsed):
                return await route.handler(parsed)
        return await self._library(path)

    async def _my_wave(self, path: _BrowsePath) -> _BrowseResult:
        """Browse My Wave while holding its station lock."""
        async with self.provider._get_wave_state(ROTOR_STATION_MY_WAVE).lock:
            return await self.provider._browse_my_wave(path.path, path.sub_subpath)

    async def _wave_modes(self, path: _BrowsePath) -> _BrowseResult:
        """Resolve built-in wave-mode URI forms and pagination."""
        if path.subpath == MY_WAVE_MODES_FOLDER_ID:
            if path.sub_subpath is None:
                return self.provider._browse_my_wave_modes_list(path.path)
            if path.sub_subpath == "next":
                return []
            preset = path.sub_subpath
            load_more = len(path.path_parts) > 2 and path.path_parts[2] == "next"
        else:
            preset = (path.subpath or "")[len(MY_WAVE_MODES_FOLDER_ID) + 1 :]
            load_more = path.sub_subpath == "next"
        if preset not in WAVE_MODE_PRESETS:
            return []
        station = f"{ROTOR_STATION_MY_WAVE}{WAVE_MODE_SEP}{preset}"
        async with self.provider._get_wave_state(station).lock:
            return await self.provider._browse_my_wave_mode(path.path, station, load_more)

    async def _user_presets(self, path: _BrowsePath) -> _BrowseResult:
        """Resolve a saved preset and apply its settings under the station lock."""
        if path.subpath == MY_WAVE_PRESETS_FOLDER_ID:
            if path.sub_subpath is None:
                return self.provider._browse_user_presets_list(
                    path.path, self.provider._get_user_wave_presets()
                )
            index_text = path.sub_subpath
            load_more = len(path.path_parts) > 2 and path.path_parts[2] == "next"
        else:
            index_text = (path.subpath or "")[len(MY_WAVE_PRESETS_FOLDER_ID) + 1 :]
            load_more = path.sub_subpath == "next"
        try:
            index = int(index_text)
        except ValueError:
            return []
        presets = self.provider._get_user_wave_presets()
        if not 0 <= index < len(presets):
            return []
        station = f"{ROTOR_STATION_MY_WAVE}{WAVE_MODE_SEP}preset_{index}"
        wave = self.provider._get_wave_state(station)
        async with wave.lock:
            wave.settings = {
                key: value
                for key, value in presets[index].items()
                if key in ("diversity", "moodEnergy", "language") and value
            }
            return await self.provider._browse_my_wave_mode(path.path, station, load_more)

    async def _collection(self, path: _BrowsePath) -> _BrowseResult:
        """Browse the collection or delegate a nested library folder."""
        if path.sub_subpath in _LIBRARY_FOLDERS:
            return await self._library(f"{self.provider.instance_id}://{path.sub_subpath}")
        return await self.provider._browse_collection(path.path)

    async def _direct(self, path: _BrowsePath) -> _BrowseResult:
        """Resolve a reconstructed station or tag URI before the library fallback."""
        subpath = path.subpath or ""
        if ":" in subpath and not subpath.split(":", 1)[0].isdigit():
            return await self.provider._browse_wave_station(subpath)
        if subpath in await self.provider._get_discovered_tag_slugs():
            return await self.provider._get_tag_playlists_as_browse(subpath)
        return await self._library(path.path)

    async def _library(self, path: str) -> _BrowseResult:
        """Delegate standard and unknown paths to Music Assistant."""
        return await MusicProvider.browse(self.provider, path)

    async def _root(self, path: _BrowsePath) -> _BrowseResult:
        """Build the provider root using the current account and enabled features."""
        # The English name on each folder doubles as the fallback; translation_key localizes
        # it for the connection locale at serialization (the server is the single source).
        items: list[MediaItemType | ItemMapping | BrowseFolder] = []
        base = path.path if path.path.endswith("//") else path.path.rstrip("/") + "/"
        # My Wave is a dynamic playlist so the queue can request refills.
        items.append(await self.provider.get_playlist(MY_WAVE_PLAYLIST_ID))
        # Wave modes folder (P4): discover / calm / active / language presets
        items.append(
            BrowseFolder(
                item_id=MY_WAVE_MODES_FOLDER_ID,
                provider=self.provider.instance_id,
                path=f"{base}{MY_WAVE_MODES_FOLDER_ID}",
                name="Wave Modes",
                translation_key=MY_WAVE_MODES_FOLDER_ID,
                is_playable=False,
            )
        )
        # User-defined wave presets (P8) — shown only when any configured.
        if self.provider._get_user_wave_presets():
            items.append(
                BrowseFolder(
                    item_id=MY_WAVE_PRESETS_FOLDER_ID,
                    provider=self.provider.instance_id,
                    path=f"{base}{MY_WAVE_PRESETS_FOLDER_ID}",
                    name="My Presets",
                    translation_key=MY_WAVE_PRESETS_FOLDER_ID,
                    is_playable=False,
                )
            )
        # For You folder — Picks + Mixes (Яндекс «Для вас»)
        items.append(
            BrowseFolder(
                item_id=FOR_YOU_FOLDER_ID,
                provider=self.provider.instance_id,
                path=f"{base}{FOR_YOU_FOLDER_ID}",
                name="For You",
                translation_key=FOR_YOU_FOLDER_ID,
                is_playable=False,
            )
        )
        # Collection folder — library items (Яндекс «Коллекция»)
        has_library = any(
            f in self.provider.supported_features
            for f in (
                ProviderFeature.LIBRARY_ARTISTS,
                ProviderFeature.LIBRARY_ALBUMS,
                ProviderFeature.LIBRARY_TRACKS,
                ProviderFeature.LIBRARY_PLAYLISTS,
            )
        )
        if has_library:
            items.append(
                BrowseFolder(
                    item_id=COLLECTION_FOLDER_ID,
                    provider=self.provider.instance_id,
                    path=f"{base}{COLLECTION_FOLDER_ID}",
                    name="Collection",
                    translation_key=COLLECTION_FOLDER_ID,
                    is_playable=False,
                )
            )
        # Radio folder — rotor stations (Яндекс волны, shown as Radio)
        items.append(
            BrowseFolder(
                item_id=RADIO_FOLDER_ID,
                provider=self.provider.instance_id,
                path=f"{base}{RADIO_FOLDER_ID}",
                name="Radio",
                translation_key=RADIO_FOLDER_ID,
                is_playable=False,
            )
        )
        # AI Wave Sets — parametric stations from /landing-blocks/mixes-waves
        items.append(
            BrowseFolder(
                item_id=MY_WAVES_SET_FOLDER_ID,
                provider=self.provider.instance_id,
                path=f"{base}{MY_WAVES_SET_FOLDER_ID}",
                name="AI Wave Sets",
                translation_key=MY_WAVES_SET_FOLDER_ID,
                is_playable=False,
            )
        )
        # Pinned items — user-pinned artists/albums/playlists/waves
        items.append(
            BrowseFolder(
                item_id=PINNED_ITEMS_FOLDER_ID,
                provider=self.provider.instance_id,
                path=f"{base}{PINNED_ITEMS_FOLDER_ID}",
                name="Pinned",
                translation_key=PINNED_ITEMS_FOLDER_ID,
                is_playable=False,
            )
        )
        # Listening history — recently played tracks/albums
        items.append(
            BrowseFolder(
                item_id=LISTENING_HISTORY_FOLDER_ID,
                provider=self.provider.instance_id,
                path=f"{base}{LISTENING_HISTORY_FOLDER_ID}",
                name="Listening History",
                translation_key=LISTENING_HISTORY_FOLDER_ID,
                is_playable=False,
            )
        )
        if len(items) == 1 and isinstance(items[0], BrowseFolder):
            return await self.provider.browse(items[0].path)
        return items
