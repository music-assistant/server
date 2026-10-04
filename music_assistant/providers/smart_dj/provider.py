"""Smart DJ queue intelligence backed by Music Assistant audio analysis and Musicae."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.plugin import PluginProvider

from .engine import (
    MODES,
    DJControls,
    DJMode,
    SignalControl,
    _merge_track,
    beam_optimize,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

CONF_RAPIDAPI_KEY = "rapidapi_key"
MUSICAE_HOST = "dj-track-audio-analysis-api.p.rapidapi.com"
MUSICAE_BASE = f"https://{MUSICAE_HOST}"
CACHE_TTL = 86400.0


def _camelot_from_key(key: str | None, mode: str | None) -> str | None:
    """Convert Music Assistant key/mode to Camelot notation."""
    if not key or not mode:
        return None
    minor = mode.lower() in {"minor", "min", "m"}
    minor_keys = {
        "Ab": "1A",
        "Eb": "2A",
        "Bb": "3A",
        "F": "4A",
        "C": "5A",
        "G": "6A",
        "D": "7A",
        "A": "8A",
        "E": "9A",
        "B": "10A",
        "F#": "11A",
        "C#": "12A",
    }
    major_keys = {
        "B": "1B",
        "F#": "2B",
        "C#": "3B",
        "Ab": "4B",
        "Eb": "5B",
        "Bb": "6B",
        "F": "7B",
        "C": "8B",
        "G": "9B",
        "D": "10B",
        "A": "11B",
        "E": "12B",
    }
    normalized = key.replace("♭", "b").replace("♯", "#")
    return (minor_keys if minor else major_keys).get(normalized)


class SmartDJProvider(PluginProvider):
    """Native Smart DJ controller and Musicae enrichment client."""

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[Any],
    ) -> None:
        """Set up the provider and its analysis cache and API handles."""
        super().__init__(mass, manifest, config, supported_features)
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._session: aiohttp.ClientSession | None = None
        self._handles: list[Any] = []
        self._cache_file = Path(self.mass.storage_path) / "smart_dj" / "analysis_cache.json"

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Smart DJ configuration."""
        return (
            ConfigEntry(
                key=CONF_RAPIDAPI_KEY,
                type=ConfigEntryType.SECURE_STRING,
                label="RapidAPI key",
                required=False,
                advanced=True,
            ),
        )

    async def loaded_in_mass(self) -> None:
        """Register the Smart DJ API."""
        self._session = aiohttp.ClientSession()
        await self._load_cache()
        handlers = (
            ("smart_dj/analyze", self.analyze),
            ("smart_dj/rank_queue", self.rank_queue),
            ("smart_dj/status", self.status),
            ("smart_dj/capabilities", self.capabilities),
        )
        for command, handler in handlers:
            # this server's API layer has no scope system (register_api_command takes
            # required_role, not required_scope). rank_queue mutates the queue, so it
            # requires the "user" role, which excludes guest accounts; the read-only
            # commands stay available to any authenticated user.
            role = "user" if command == "smart_dj/rank_queue" else None
            self._handles.append(
                self.mass.register_api_command(command, handler, required_role=role)
            )

    async def unload(self, is_removed: bool = False) -> None:
        """Close the HTTP client and unregister commands."""
        for handle in self._handles:
            handle()
        self._handles.clear()
        if self._session:
            await self._session.close()
            self._session = None
        await super().unload(is_removed)

    async def _load_cache(self) -> None:
        """Load the persistent analysis cache."""
        try:
            if self._cache_file.exists():
                raw = await asyncio.to_thread(self._cache_file.read_text)
                data = json.loads(raw)
                now = asyncio.get_running_loop().time()
                if isinstance(data, dict):
                    loaded: dict[str, tuple[float, dict[str, Any]]] = {}
                    for key, entry in data.items():
                        if isinstance(entry, dict) and isinstance(entry.get("value"), dict):
                            age = float(entry.get("saved_at", 0.0))
                            timestamp = now - max(0.0, time.time() - age) if age else now
                            if now - timestamp < CACHE_TTL:
                                loaded[key] = (timestamp, entry["value"])
                        elif isinstance(entry, dict):
                            loaded[key] = (now, entry)
                    self._cache = loaded
        except (OSError, ValueError, TypeError) as err:
            self.logger.warning("Could not load Smart DJ cache: %s", err)

    async def _save_cache(self) -> None:
        """Persist analysis cache atomically."""
        try:
            await asyncio.to_thread(self._cache_file.parent.mkdir, parents=True, exist_ok=True)
            wall_now = time.time()
            loop_now = asyncio.get_running_loop().time()
            payload = {
                key: {
                    "saved_at": wall_now - max(0.0, loop_now - timestamp),
                    "value": value,
                }
                for key, (timestamp, value) in self._cache.items()
            }
            tmp = self._cache_file.with_suffix(".tmp")
            await asyncio.to_thread(tmp.write_text, json.dumps(payload))
            await asyncio.to_thread(tmp.replace, self._cache_file)
        except OSError as err:
            self.logger.warning("Could not save Smart DJ cache: %s", err)

    def _key(self) -> str | None:
        value = self.config.get_value(CONF_RAPIDAPI_KEY)
        return str(value) if value else None

    async def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """Call Musicae without exposing credentials to clients."""
        key = self._key()
        if not key:
            raise RuntimeError("Smart DJ requires a Musicae RapidAPI key")
        if self._session is None:
            self._session = aiohttp.ClientSession()
        headers = {"X-RapidAPI-Key": key, "X-RapidAPI-Host": MUSICAE_HOST}
        async with self._session.get(
            f"{MUSICAE_BASE}{path}", params=params, headers=headers
        ) as response:
            if response.status >= 400:
                body = await response.text()
                raise RuntimeError(f"Musicae request failed ({response.status}): {body[:300]}")
            data = await response.json()
            if not isinstance(data, dict):
                raise TypeError("Musicae returned an invalid response")
            return data

    async def _analysis(
        self,
        item_id: str,
        provider: str,
        metadata: dict[str, Any] | None = None,
        analysis_provider: str = "auto",
    ) -> dict[str, Any] | None:
        """Get analysis using the selected provider policy."""
        if analysis_provider not in {"auto", "music_assistant", "musicae"}:
            raise TypeError(f"Unknown analysis provider: {analysis_provider}")
        try:
            analysis = None
            if analysis_provider != "musicae":
                analysis = await self.mass.streams.audio_analysis.get_audio_analysis(
                    item_id, provider
                )
            if analysis:
                return {
                    "bpm": analysis.bpm,
                    "key": analysis.key,
                    "mode": analysis.mode,
                    "energy": analysis.energy,
                    "danceability": analysis.danceability,
                    "valence": analysis.valence,
                    "arousal": analysis.arousal,
                    "loudness": analysis.loudness_integrated,
                    "beats_per_bar": analysis.beats_per_bar,
                    "beats": analysis.beats,
                    "downbeats": analysis.downbeats,
                    "rms_energy": analysis.rms_energy,
                    "spectral_centroid": analysis.spectral_centroid,
                    "instrumental": (
                        None
                        if analysis.instrumentalness is None
                        else analysis.instrumentalness >= 0.5
                    ),
                    "instrumentalness": analysis.instrumentalness,
                    "camelot": _camelot_from_key(analysis.key, analysis.mode),
                    "source": "music_assistant",
                    "sources": dict.fromkeys(
                        (
                            "bpm",
                            "key",
                            "camelot",
                            "energy",
                            "danceability",
                            "loudness",
                            "beats_per_bar",
                            "beats",
                            "downbeats",
                            "instrumental",
                        ),
                        "music_assistant",
                    ),
                    **(metadata or {}),
                }
        except Exception as err:
            self.logger.debug("MA audio analysis unavailable for %s/%s: %s", provider, item_id, err)

        if analysis_provider == "music_assistant":
            return None

        provider_obj = self.mass.get_provider(provider)
        provider_domain = getattr(provider_obj, "domain", provider)
        if provider_domain != "spotify":
            return None
        now = asyncio.get_running_loop().time()
        cache_key = f"{provider}:{item_id}"
        cached = self._cache.get(cache_key)
        if cached and now - cached[0] < CACHE_TTL:
            return cached[1]

        data = await self._request(f"/v2/audio-analysis/{item_id}", {})
        result = data.get("data") if isinstance(data.get("data"), dict) else data
        if not isinstance(result, dict):
            return None
        instrumental_raw = result.get("instrumental")
        instrumentalness_raw = result.get("instrumentalness")
        instrumental_flag = (
            instrumental_raw
            if isinstance(instrumental_raw, bool)
            else (
                instrumentalness_raw >= 0.5
                if isinstance(instrumentalness_raw, (int, float))
                else None
            )
        )
        normalized = {
            "bpm": result.get("bpm"),
            "key": result.get("key") or result.get("musical_key"),
            "mode": result.get("mode"),
            "camelot": result.get("camelot"),
            "energy": result.get("energy"),
            "danceability": result.get("danceability"),
            "valence": result.get("valence"),
            "arousal": result.get("arousal"),
            "loudness": result.get("loudness") or result.get("loudness_integrated"),
            "beats_per_bar": result.get("beats_per_bar") or result.get("time_signature"),
            "beats": result.get("beats"),
            "downbeats": result.get("downbeats"),
            "rms_energy": result.get("rms_energy") or result.get("waveform"),
            "spectral_centroid": result.get("spectral_centroid"),
            "instrumental": instrumental_flag,
            "instrumentalness": result.get("instrumentalness"),
            "source": "musicae",
            "sources": dict.fromkeys(
                (
                    "bpm",
                    "key",
                    "camelot",
                    "energy",
                    "danceability",
                    "loudness",
                    "beats_per_bar",
                    "beats",
                    "downbeats",
                    "instrumental",
                ),
                "musicae",
            ),
            "raw": result,
            **(metadata or {}),
        }
        self._cache[cache_key] = (now, normalized)
        await self._save_cache()
        return normalized

    async def _queue_snapshot(self, queue_id: str) -> list[dict[str, Any]]:
        """Return a compact queue snapshot."""
        items = self.mass.player_queues.items(queue_id, limit=1000, offset=0)
        result: list[dict[str, Any]] = []
        for item in items:
            media = item.media_item
            provider = getattr(media, "provider", None) or getattr(media, "provider_instance", None)
            item_id = getattr(media, "item_id", None)
            if not provider or not item_id:
                continue
            metadata_obj = getattr(media, "metadata", None)
            genres = getattr(metadata_obj, "genres", None) if metadata_obj else None
            result.append(
                {
                    "queue_item_id": item.queue_item_id,
                    "name": item.name,
                    "artist": getattr(media, "artist_str", None) or getattr(media, "artist", None),
                    "item_id": item_id,
                    "provider": provider,
                    "genre": genres[0] if genres else None,
                    "genres": list(genres or []),
                    "explicit": getattr(metadata_obj, "explicit", None) if metadata_obj else None,
                }
            )
        return result

    async def analyze(
        self, queue_id: str, limit: int = 40, analysis_provider: str = "auto"
    ) -> dict[str, Any]:
        """Analyze the active queue and return DJ-ready metadata."""
        items = (await self._queue_snapshot(queue_id))[: max(1, min(limit, 100))]
        analyzed = []
        tasks = [
            asyncio.create_task(
                self._analysis(
                    item["item_id"],
                    item["provider"],
                    {k: item[k] for k in ("genre", "genres", "explicit") if k in item},
                    analysis_provider,
                )
            )
            for item in items
        ]
        analyses = await asyncio.gather(*tasks, return_exceptions=True)
        for item, result in zip(items, analyses, strict=True):
            analysis = None if isinstance(result, Exception) else result
            if isinstance(result, Exception):
                self.logger.debug("Smart DJ analysis failed for %s: %s", item["item_id"], result)
            analyzed.append({**item, "analysis": analysis})
        current = analyzed[0]["analysis"] if analyzed and analyzed[0].get("analysis") else None
        return {"queue_id": queue_id, "tracks": analyzed, "current": current}

    async def rank_queue(
        self,
        queue_id: str,
        bpm_tolerance: float = 0.08,
        prefer_keys: bool = True,
        preserve_variety: bool = True,
        mode: str = "ai_dj",
        controls: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Optimize the upcoming queue under an explicit user control contract."""
        raw = dict(controls or {})
        apply = bool(raw.pop("apply", False))
        analysis_provider = str(raw.get("analysis_provider", "auto"))
        snapshot = await self.analyze(queue_id, analysis_provider=analysis_provider)
        tracks = snapshot["tracks"]
        if len(tracks) < 2:
            return snapshot

        def signal(name: str, fallback: str = "soft") -> SignalControl:
            value = raw.get(name, fallback)
            if isinstance(value, dict):
                return SignalControl(
                    state=str(value.get("state", fallback)),
                    weight=max(0.0, float(value.get("weight", 1.0))),
                )
            return SignalControl(state=str(value), weight=1.0)

        transition_bars = int(raw.get("transition_bars", 8))
        if transition_bars not in {4, 8, 16, 32}:
            raise RuntimeError("transition_bars must be one of 4, 8, 16, or 32")
        control = DJControls(
            bpm=signal("bpm"),
            key=signal("key", "soft" if prefer_keys else "disabled"),
            energy=signal("energy"),
            danceability=signal("danceability"),
            loudness=signal("loudness"),
            genre=signal("genre"),
            artist_spacing=signal("artist_spacing", "soft" if preserve_variety else "disabled"),
            momentum=signal("momentum"),
            bpm_min=float(raw["bpm_min"]) if raw.get("bpm_min") is not None else None,
            bpm_max=float(raw["bpm_max"]) if raw.get("bpm_max") is not None else None,
            max_bpm_jump=float(raw["max_bpm_jump"])
            if raw.get("max_bpm_jump") is not None
            else None,
            key_relation=str(raw.get("key_relation", "compatible")),
            max_artist_repeat=max(0, int(raw.get("max_artist_repeat", 1))),
            instrumental=str(raw.get("instrumental", "any")),
            explicit=str(raw.get("explicit", "allow")),
            transition_bars=transition_bars,
            automix_enabled=bool(raw.get("automix_enabled", False)),
            smart_reorder_enabled=bool(raw.get("smart_reorder_enabled", True)),
            lookahead=max(1, min(32, int(raw.get("lookahead", 4)))),
            transition_aggressiveness=max(
                0.0, min(1.0, float(raw.get("transition_aggressiveness", 0.5)))
            ),
            required_ids=frozenset(str(x) for x in raw.get("required_ids", [])),
            excluded_ids=frozenset(str(x) for x in raw.get("excluded_ids", [])),
            fixed_ids=frozenset(str(x) for x in raw.get("fixed_ids", [])),
            end_track_id=str(raw["end_track_id"]) if raw.get("end_track_id") else None,
        )
        if not control.smart_reorder_enabled:
            if apply:
                # crossfade is a per-player config setting on this server version;
                # there is no queue-level crossfade API to call
                self.logger.debug(
                    "Smart DJ: automix crossfade preference (%s) noted for %s; "
                    "configure crossfade on the player itself",
                    control.automix_enabled,
                    queue_id,
                )
            return {
                "queue_id": queue_id,
                "tracks": tracks[1:],
                "applied": apply,
                "settings": {"controls": raw, "smart_reorder_enabled": False},
            }

        selected_mode = MODES.get(mode, MODES["ai_dj"])
        selected_mode = DJMode(
            selected_mode.name,
            max(0.01, min(1.0, float(bpm_tolerance))),
            selected_mode.energy_direction,
            selected_mode.variety,
            selected_mode.weights,
        )
        optimized = beam_optimize(
            tracks[1:],
            _merge_track(tracks[0]) if tracks else snapshot["current"],
            selected_mode,
            controls=control,
            beam_width=max(4, min(32, control.lookahead * 3)),
        )
        if not optimized and tracks[1:]:
            raise RuntimeError("Hard requirements are impossible with the current queue")

        if apply:
            queue = self.mass.player_queues.get(queue_id)
            if queue is None:
                raise RuntimeError(f"Queue not found: {queue_id}")
            items = self.mass.player_queues.items(queue_id, limit=1000, offset=0)
            by_id = {item.queue_item_id: item for item in items}
            prefix_len = (queue.current_index or 0) + 1
            prefix = items[:prefix_len]
            ranked_ids = [track["queue_item_id"] for track in optimized]
            ranked = [by_id[item_id] for item_id in ranked_ids if item_id in by_id]
            if len(ranked) != len(ranked_ids):
                raise RuntimeError("Queue changed while Smart DJ was planning")
            ranked_set = set(ranked_ids)
            remainder = [
                item for item in items[prefix_len:] if item.queue_item_id not in ranked_set
            ]
            if len(prefix) + len(ranked) + len(remainder) != len(items):
                raise RuntimeError("Queue integrity check failed")
            self.mass.player_queues.update_items(queue_id, prefix + ranked + remainder)
        return {
            "queue_id": queue_id,
            "current_item_id": tracks[0]["queue_item_id"],
            "tracks": optimized,
            "applied": apply,
            "settings": {
                "mode": mode,
                "bpm_tolerance": bpm_tolerance,
                "preserve_variety": preserve_variety,
                "controls": raw,
            },
        }

    async def capabilities(self) -> dict[str, Any]:
        """Describe available Smart DJ analysis and mixing capabilities."""
        analysis_controller = getattr(self.mass.streams, "audio_analysis", None)
        return {
            "analysis": {
                "music_assistant": analysis_controller is not None,
                "musicae": bool(self._key()),
            },
            "mixing": {
                "smart_fades": True,
                "transition_planner": False,
                "vocal_protection": True,
                "bass_eq_management": True,
                "tempo_planning": True,
            },
            "analysis_providers": {
                "music_assistant": analysis_controller is not None,
                "musicae": bool(self._key()),
                "fallback_policy": ["music_assistant", "musicae"],
            },
            "user_control": {
                "states": ["hard", "soft", "disabled"],
                "transition_bars": [4, 8, 16, 32],
                "lookahead": [1, 2, 4, 8, 16, 32],
            },
        }

    async def status(self) -> dict[str, Any]:
        """Return provider readiness without revealing credentials."""
        return {
            "configured": bool(self._key()),
            "musicae_host": MUSICAE_HOST,
            "cached_tracks": len(self._cache),
        }
