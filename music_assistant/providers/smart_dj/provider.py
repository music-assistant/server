"""Smart DJ queue intelligence backed by Music Assistant audio analysis and Musicae."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.auth import Scope

from music_assistant.models.plugin import PluginProvider

from .engine import DJControls, MODES, SignalControl, beam_optimize

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

CONF_RAPIDAPI_KEY = "rapidapi_key"
MUSICAE_HOST = "dj-track-audio-analysis-api.p.rapidapi.com"
MUSICAE_BASE = f"https://{MUSICAE_HOST}"
CACHE_TTL = 86400.0

class SmartDJProvider(PluginProvider):
    """Native Smart DJ controller and Musicae enrichment client."""

    def __init__(self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig, supported_features: set[Any]) -> None:
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
            scope = Scope.QUEUES_CONTROL if command == "smart_dj/rank_queue" else Scope.QUEUES_READ
            self._handles.append(self.mass.register_api_command(command, handler, required_scope=scope))

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
                    self._cache = {key: (now, value) for key, value in data.items() if isinstance(value, dict)}
        except (OSError, ValueError, TypeError) as err:
            self.logger.warning("Could not load Smart DJ cache: %s", err)

    async def _save_cache(self) -> None:
        """Persist analysis cache atomically."""
        try:
            await asyncio.to_thread(self._cache_file.parent.mkdir, parents=True, exist_ok=True)
            payload = {key: value for key, (_timestamp, value) in self._cache.items()}
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
        async with self._session.get(f"{MUSICAE_BASE}{path}", params=params, headers=headers) as response:
            if response.status >= 400:
                body = await response.text()
                raise RuntimeError(f"Musicae request failed ({response.status}): {body[:300]}")
            data = await response.json()
            if not isinstance(data, dict):
                raise RuntimeError("Musicae returned an invalid response")
            return data

    async def _analysis(self, item_id: str, provider: str) -> dict[str, Any] | None:
        """Get cached MA analysis first, then Musicae for Spotify-compatible tracks."""
        try:
            analysis = await self.mass.streams.audio_analysis.get_audio_analysis(item_id, provider)
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
                    "source": "music_assistant",
                }
        except Exception as err:
            self.logger.debug("MA audio analysis unavailable for %s/%s: %s", provider, item_id, err)

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
            "source": "musicae",
            "raw": result,
        }
        self._cache[cache_key] = (now, normalized)
        await self._save_cache()
        return normalized

    @staticmethod
    def _compatibility(current: dict[str, Any] | None, candidate: dict[str, Any] | None, settings: dict[str, Any]) -> float:
        """Score a candidate from 0..1 using mix-safe musical dimensions."""
        if not candidate:
            return 0.0
        if not current:
            return 0.5
        score = 0.0
        weight = 0.0

        bpm_a, bpm_b = current.get("bpm"), candidate.get("bpm")
        if isinstance(bpm_a, (int, float)) and isinstance(bpm_b, (int, float)) and bpm_a:
            tolerance = max(0.02, float(settings.get("bpm_tolerance", 0.08)))
            delta = abs(float(bpm_b) - float(bpm_a)) / float(bpm_a)
            score += max(0.0, 1.0 - delta / tolerance) * 0.30
            weight += 0.30

        key_a, key_b = current.get("camelot"), candidate.get("camelot")
        if key_a and key_b:
            if key_a == key_b:
                key_score = 1.0
            else:
                try:
                    na, nb = int(str(key_a)[:-1]), int(str(key_b)[:-1])
                    ma, mb = str(key_a)[-1], str(key_b)[-1]
                    key_score = 0.75 if ma == mb and ((na - nb) % 12 in (1, 11)) else 0.65 if na == nb else 0.0
                except (ValueError, TypeError):
                    key_score = 0.0
            score += key_score * 0.25
            weight += 0.25

        for field, field_weight in (("energy", 0.20), ("danceability", 0.10), ("valence", 0.05), ("arousal", 0.10)):
            a, b = current.get(field), candidate.get(field)
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                target_delta = 0.22 if field in ("energy", "arousal") else 0.35
                score += max(0.0, 1.0 - abs(float(b) - float(a)) / target_delta) * field_weight
                weight += field_weight

        return round(score / weight, 4) if weight else 0.5

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
            result.append({
                "queue_item_id": item.queue_item_id,
                "name": item.name,
                "artist": getattr(media, "artist", None),
                "item_id": item_id,
                "provider": provider,
            })
        return result

    async def analyze(self, queue_id: str, limit: int = 40) -> dict[str, Any]:
        """Analyze the active queue and return DJ-ready metadata."""
        items = (await self._queue_snapshot(queue_id))[:max(1, min(limit, 100))]
        analyzed = []
        tasks = [
            asyncio.create_task(self._analysis(item["item_id"], item["provider"]))
            for item in items
        ]
        analyses = await asyncio.gather(*tasks, return_exceptions=True)
        for item, analysis in zip(items, analyses, strict=True):
            if isinstance(analysis, Exception):
                self.logger.debug("Smart DJ analysis failed for %s: %s", item["item_id"], analysis)
                analysis = None
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
        snapshot = await self.analyze(queue_id)
        tracks = snapshot["tracks"]
        if len(tracks) < 2:
            return snapshot
        raw = controls or {}

        def signal(name: str, fallback: str = "soft") -> SignalControl:
            value = raw.get(name, fallback)
            if isinstance(value, dict):
                return SignalControl(
                    state=str(value.get("state", fallback)),
                    weight=max(0.0, float(value.get("weight", 1.0))),
                )
            return SignalControl(state=str(value), weight=1.0)

        control = DJControls(
            bpm=signal("bpm"),
            key=signal("key", "soft" if prefer_keys else "disabled"),
            energy=signal("energy"),
            danceability=signal("danceability"),
            loudness=signal("loudness"),
            genre=signal("genre"),
            artist_spacing=signal("artist_spacing"),
            momentum=signal("momentum"),
            bpm_min=float(raw["bpm_min"]) if raw.get("bpm_min") is not None else None,
            bpm_max=float(raw["bpm_max"]) if raw.get("bpm_max") is not None else None,
            max_bpm_jump=float(raw["max_bpm_jump"]) if raw.get("max_bpm_jump") is not None else None,
            key_relation=str(raw.get("key_relation", "compatible")),
            max_artist_repeat=max(0, int(raw.get("max_artist_repeat", 1))),
            instrumental=str(raw.get("instrumental", "any")),
            explicit=str(raw.get("explicit", "allow")),
            transition_bars=int(raw.get("transition_bars", 8)),
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
            return {
                "queue_id": queue_id,
                "tracks": tracks[1:],
                "settings": {"controls": raw, "smart_reorder_enabled": False},
            }

        optimized = beam_optimize(
            tracks[1:],
            snapshot["current"],
            MODES.get(mode, MODES["ai_dj"]),
            controls=control,
            beam_width=max(4, min(32, control.lookahead * 3)),
        )
        if not optimized and control.required_ids:
            raise RuntimeError("Hard requirements are impossible with the current queue")

        queue = self.mass.player_queues.get(queue_id)
        if queue is None:
            raise RuntimeError(f"Queue not found: {queue_id}")
        items = self.mass.player_queues.items(queue_id, limit=1000, offset=0)
        by_id = {item.queue_item_id: item for item in items}
        prefix_len = (queue.current_index or 0) + 1
        prefix = items[:prefix_len]
        ranked_ids = [track["queue_item_id"] for track in optimized]
        ranked = [by_id[item_id] for item_id in ranked_ids if item_id in by_id]
        ranked_set = set(ranked_ids)
        remainder = [item for item in items[prefix_len:] if item.queue_item_id not in ranked_set]
        self.mass.player_queues.update_items(queue_id, prefix + ranked + remainder)
        return {
            "queue_id": queue_id,
            "current_item_id": tracks[0]["queue_item_id"],
            "tracks": optimized,
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
                "transition_planner": True,
                "vocal_protection": True,
                "bass_eq_management": True,
                "tempo_planning": True,
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
