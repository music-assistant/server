"""
myNoise provider for Music Assistant.

Streams the noise machines from mynoise.net (https://mynoise.net) as endless,
looping audio sources.

myNoise machines are interactive soundscapes built from a set of short,
seamlessly loopable audio samples (one per frequency band). The website plays
them through the WebAudio API with looping enabled and a mixer per band.
This provider recreates that model server-side: the layer files are fetched
from the myNoise CDN and looped/mixed on the fly by ffmpeg, producing one
endless stream per machine - so a machine plays forever, just like in the
browser, without any (re)download.

myNoise grants lifetime privileged access to donors, validated by posting the
donor email address (no password). Setting the optional 'Patron email' option
logs in with that address and also unlocks the patron-only 'Magic' machines.

No official API exists, so the catalog is scraped from the public index page
(with a bundled fallback) and each machine's layer files are parsed from the
CFG object embedded in its generator page. Results are cached aggressively.

Note: myNoise content is (c) Stephane Pigeon / myNoise BV, "FOR PERSONAL USE
ONLY". This provider streams the same public files the website serves and is
intended for personal use. Consider donating to myNoise.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from pathlib import Path, PurePath
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import aiohttp
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    ProviderMapping,
    Radio,
    SearchResults,
)
from music_assistant_models.streamdetails import StreamDetails
from music_assistant_models.unique_list import UniqueList

from music_assistant.models.music_provider import MusicProvider

from .catalog import (
    Machine,
    MachineLayer,
    fetch_catalog,
    fetch_machine_details,
    patron_login,
)
from .constants import (
    AUDIO_CACHE_SUBDIR,
    AUDIO_EXTENSION,
    CONF_MASTER_GAIN_DB,
    CONF_PATRON_EMAIL,
    CONF_REFRESH_CATALOG,
    CONF_USE_VARIANT_B,
    USER_AGENT,
    WEBSITE_URL,
)
from .streaming import pcm_output_format, resolve_layer_gains, stream_machine

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
}

BROWSE_ROOT = "mynoise://root"


def _write_layer_to_cache(path: str, data: bytes) -> None:
    """Write layer bytes to the cache atomically (never leaves a truncated file)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=target.parent, suffix=".part")
    with os.fdopen(fd, "wb") as file:
        file.write(data)
    Path(tmp_path).replace(target)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return MyNoiseProvider(mass, manifest, config, SUPPORTED_FEATURES)


class MyNoiseProvider(MusicProvider):
    """Music provider that streams myNoise.net machines on endless loop."""

    # cached outcome of the patron login (None = not attempted yet)
    _patron_logged_in: bool | None = None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the (options) config entries for this provider instance."""
        return (
            ConfigEntry(
                key=CONF_REFRESH_CATALOG,
                type=ConfigEntryType.ACTION,
                required=False,
                action="refresh_catalog",
            ),
            ConfigEntry(
                key=CONF_PATRON_EMAIL,
                type=ConfigEntryType.STRING,
                value=self.get_config_value(CONF_PATRON_EMAIL, ""),
                required=False,
            ),
            ConfigEntry(
                key=CONF_MASTER_GAIN_DB,
                type=ConfigEntryType.FLOAT,
                default_value=0.0,
                required=False,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_USE_VARIANT_B,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                required=False,
                advanced=True,
            ),
        )

    async def handle_config_action(self, action: str) -> None:
        """Handle a one-shot action button press from the options page."""
        if action != CONF_REFRESH_CATALOG:
            await super().handle_config_action(action)
            return
        # retry the patron login too, so a corrected email takes effect here
        self._patron_logged_in = None
        await fetch_catalog(self.mass, force_refresh=True, patron=await self._patron_enabled())

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return True

    @property
    def supported_media_types(self) -> set[MediaType]:
        """Return the media types this provider can serve."""
        return {MediaType.RADIO}

    # ---------------------------------------------------------------- search

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Search the myNoise machine catalog by name."""
        if MediaType.RADIO not in media_types:
            return SearchResults()
        catalog = await fetch_catalog(self.mass, patron=await self._patron_enabled())
        query = search_query.casefold()
        results = [
            self._radio_from_machine(machine)
            for machine in catalog.machines.values()
            if query in machine.name.casefold()
            or query in machine.category.casefold()
            or query in machine.machine_id.casefold()
        ]
        return SearchResults(radio=results[:limit])

    # ------------------------------------------------------------- lookups

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio (noise machine) details by id."""
        catalog = await fetch_catalog(self.mass, patron=await self._patron_enabled())
        machine = catalog.machines.get(prov_radio_id)
        if not machine:
            raise MediaNotFoundError(f"Unknown myNoise machine: {prov_radio_id}")
        return self._radio_from_machine(machine)

    # -------------------------------------------------------------- browse

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse the myNoise catalog: categories and their machines."""
        catalog = await fetch_catalog(self.mass, patron=await self._patron_enabled())
        path_no_schema = path.split("://", 1)[1] if "://" in path else path
        parts = [part for part in path_no_schema.split("/") if part]
        # strip generic leading segments (mynoise://, root, libraries, radios)
        while parts and parts[0] in ("mynoise", "root", "libraries", "radios"):
            parts = parts[1:]

        if not parts:
            # top level: one folder per category
            return [
                BrowseFolder(
                    item_id=category.id,
                    provider=self.instance_id,
                    name=category.name,
                    path=f"{BROWSE_ROOT}/{category.id}",
                )
                for category in catalog.categories
            ]

        category = next((cat for cat in catalog.categories if cat.id == parts[0]), None)
        if category:
            if len(parts) == 1:
                return [
                    self._radio_from_machine(catalog.machines[machine_id])
                    for machine_id in category.machine_ids
                    if machine_id in catalog.machines
                ]
            # a machine inside a category: return it as single playable item
            machine = catalog.machines.get(parts[1])
            if machine:
                return [self._radio_from_machine(machine)]

        # single machine referenced directly by id
        if machine := catalog.machines.get(parts[0]):
            return [self._radio_from_machine(machine)]

        raise MediaNotFoundError(f"Unknown browse path: {path}")

    # ------------------------------------------------------------- streaming

    async def get_stream_details(
        self, item_id: str, media_type: MediaType = MediaType.RADIO
    ) -> StreamDetails:
        """Return streamdetails for the given machine: an endless custom mix."""
        catalog = await fetch_catalog(self.mass, patron=await self._patron_enabled())
        machine = catalog.machines.get(item_id)
        if not machine:
            raise MediaNotFoundError(f"Unknown myNoise machine: {item_id}")
        machine = await fetch_machine_details(self.mass, machine)
        if not machine.layers:
            raise MediaNotFoundError(f"Machine {machine.name} has no audio layers")

        master_gain_db = float(self.get_config_value(CONF_MASTER_GAIN_DB, 0.0) or 0.0)
        use_variant_b = bool(self.get_config_value(CONF_USE_VARIANT_B, False))
        layer_gains = resolve_layer_gains(machine)
        layer_sources = await self._resolve_layer_sources(
            machine.layers, layer_gains, use_variant_b
        )

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=pcm_output_format(),
            media_type=MediaType.RADIO,
            stream_type=StreamType.CUSTOM,
            # endless mix: no duration, not seekable (handled like a live stream)
            duration=None,
            allow_seek=False,
            can_seek=False,
            is_realtime=True,
            data={
                "machine_id": machine.machine_id,
                "machine_name": machine.name,
                # each layer's source is a cached local file (remote url as fallback),
                # so the endless mix loops local data instead of hitting the CDN
                "layers": [{"source": source} for source in layer_sources],
                "gains": layer_gains,
                "master_gain_db": master_gain_db,
                "use_variant_b": use_variant_b,
            },
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """Yield the endless looped+mixed audio for the machine."""
        data = streamdetails.data or {}
        layers = [
            MachineLayer(
                url_a=str(layer["source"]),
                url_b=str(layer["source"]),
            )
            for layer in data.get("layers", [])
        ]
        gains = [float(gain) for gain in data.get("gains", [])]
        if not layers or not gains:
            raise MediaNotFoundError("Invalid stream data for myNoise machine")
        machine = Machine(
            machine_id=str(data.get("machine_id", "mynoise")),
            name=str(data.get("machine_name", "myNoise")),
            layers=layers,
        )
        async for chunk in stream_machine(
            machine,
            gains,
            master_gain_db=float(data.get("master_gain_db", 0.0)),
            use_variant_b=bool(data.get("use_variant_b", False)),
            logger=self.logger,
        ):
            yield chunk

    # --------------------------------------------------------------- images

    async def resolve_image(self, path: str) -> str | bytes:
        """Resolve an image path (we only use remotely accessible urls)."""
        return path

    # -------------------------------------------------------------- helpers

    async def _patron_enabled(self) -> bool:
        """Return True when a patron email is configured and the login succeeded."""
        email = str(self.get_config_value(CONF_PATRON_EMAIL, "") or "").strip()
        if not email:
            return False
        if self._patron_logged_in is None:
            try:
                self._patron_logged_in = await patron_login(self.mass, email)
            except Exception:
                self.logger.warning("myNoise patron login failed", exc_info=True)
                self._patron_logged_in = False
            if not self._patron_logged_in:
                self.logger.warning(
                    "myNoise rejected the configured patron email; patron-only machines "
                    "stay locked (the email must match the one of your donation)"
                )
        return self._patron_logged_in

    async def _resolve_layer_sources(
        self, layers: list[MachineLayer], gains: list[float], use_variant_b: bool
    ) -> list[str]:
        """Return a playable source per layer: a cached local file, or its url."""
        # single-instance provider: instance_id always equals the domain, so only
        # one folder is needed under the cache path
        cache_dir = os.path.join(self.mass.cache_path, self.domain, AUDIO_CACHE_SUBDIR)
        sources: list[str] = []
        for layer, gain in zip(layers, gains, strict=True):
            url = layer.url_b if use_variant_b else layer.url_a
            if gain <= 0:
                # muted layers are skipped by the mixer, so no need to download them
                sources.append(url)
                continue
            sources.append(await self._cache_layer_file(url, cache_dir))
        return sources

    async def _cache_layer_file(self, url: str, cache_dir: str) -> str:
        """Download a layer file into the provider cache, falling back to its url."""
        digest = hashlib.sha1(url.encode()).hexdigest()[:16]
        ext = PurePath(urlparse(url).path).suffix or AUDIO_EXTENSION
        path = os.path.join(cache_dir, f"{digest}{ext}")
        if Path(path).is_file():
            return path
        try:
            async with self.mass.http_session.get(url, headers={"User-Agent": USER_AGENT}) as resp:
                resp.raise_for_status()
                data = await resp.read()
            await asyncio.to_thread(_write_layer_to_cache, path, data)
        except aiohttp.ClientError, TimeoutError, OSError:
            self.logger.warning(
                "Failed to cache myNoise layer %s, streaming it remotely instead", url
            )
            return url
        return path

    def _radio_from_machine(self, machine: Machine) -> Radio:
        """Build a Radio media item for the given machine."""
        radio = Radio(
            item_id=machine.machine_id,
            provider=self.instance_id,
            name=machine.name,
            media_type=MediaType.RADIO,
            is_endless=True,
            provider_mappings={
                ProviderMapping(
                    item_id=machine.machine_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"{WEBSITE_URL}{machine.generator_path}",
                )
            },
        )
        radio.metadata.description = (
            f"myNoise soundscapes — {machine.category}. Loops endlessly. Streamed from mynoise.net."
        )
        if machine.image_url:
            radio.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.LANDSCAPE,
                        path=machine.image_url,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    ),
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=machine.image_url,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    ),
                ]
            )
        return radio
