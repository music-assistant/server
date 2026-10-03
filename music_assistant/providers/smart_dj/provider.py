"""Smart DJ queue intelligence backed by Music Assistant audio analysis and Musicae."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, Scope

from music_assistant.models.plugin import PluginProvider

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
        handlers = (
            ("smart_dj/analyze", self.analyze),
            ("smart_dj/rank_queue", self.rank_queue),
            ("smart_dj/status", self.status),
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

        if provider != "spotify":
            return None
        now = asyncio.get_running_loop().time()
        cached = self._cache.get(item_id)
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
        self._cache[item_id] = (now, normalized)
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
        for item in items:
            analysis = await self._analysis(item["item_id"], item["provider"])
            analyzed.append({**item, "analysis": analysis})
        current = analyzed[0]["analysis"] if analyzed and analyzed[0].get("analysis") else None
        return {"queue_id": queue_id, "tracks": analyzed, "current": current}

    async def rank_queue(
        self,
        queue_id: str,
        bpm_tolerance: float = 0.08,
        prefer_keys: bool = True,
        preserve_variety: bool = True,
    ) -> dict[str, Any]:
        """Rank upcoming queue items and apply the ordering through MA queue primitives."""
        snapshot = await self.analyze(queue_id)
        tracks = snapshot["tracks"]
        if len(tracks) < 2:
            return snapshot

        settings = {
            "bpm_tolerance": bpm_tolerance,
            "prefer_keys": prefer_keys,
            "preserve_variety": preserve_variety,
        }
        current = snapshot["current"]
        current_id = tracks[0]["queue_item_id"]
        candidates = []
        for track in tracks[1:]:
            candidates.append({
                **track,
                "score": self._compatibility(current, track.get("analysis"), settings),
            })
        candidates.sort(key=lambda x: x["score"], reverse=True)

        # Rebuild only the upcoming portion, never the currently playing item.
        for track in candidates:
            await self.mass.player_queues.move_item_end(queue_id, track["queue_item_id"])
        return {"queue_id": queue_id, "current_item_id": current_id, "tracks": candidates, "settings": settings}

    async def status(self) -> dict[str, Any]:
        """Return provider readiness without revealing credentials."""
        return {
            "configured": bool(self._key()),
            "musicae_host": MUSICAE_HOST,
            "cached_tracks": len(self._cache),
        }
