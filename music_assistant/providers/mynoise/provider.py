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
browser. The layer files are staged in a private temp dir for the duration of
playback (so the endless ffmpeg loop replays local data instead of re-fetching
from the CDN on every pass) and deleted again when playback stops.

myNoise grants lifetime privileged access to donors, validated by posting the
donor email address (no password). This provider requires that access: the
setup flow collects the email address of a myNoise donation, verifies it with
myNoise, and no machine (patron-only or free) is offered until it is accepted.

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
import shutil
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
from music_assistant_models.errors import (
    AuthenticationFailed,
    AuthenticationRequired,
    MediaNotFoundError,
    ProviderUnavailableError,
)
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
    AUDIO_EXTENSION,
    CONF_MASTER_GAIN_DB,
    CONF_PATRON_EMAIL,
    CONF_REFRESH_CATALOG,
    CONF_USE_VARIANT_B,
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


def _write_layer_to_file(path: str, data: bytes) -> None:
    """Write layer bytes to the given path atomically (never leaves a truncated file)."""
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
        # (and a rejected one surfaces as an error on the options page)
        self._patron_logged_in = None
        await self._require_patron()
        await fetch_catalog(self.mass, force_refresh=True)

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
        await self._require_patron()
        catalog = await fetch_catalog(self.mass)
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
        await self._require_patron()
        catalog = await fetch_catalog(self.mass)
        machine = catalog.machines.get(prov_radio_id)
        if not machine:
            raise MediaNotFoundError(f"Unknown myNoise machine: {prov_radio_id}")
        return self._radio_from_machine(machine)

    # -------------------------------------------------------------- browse

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse the myNoise catalog: categories and their machines."""
        await self._require_patron()
        catalog = await fetch_catalog(self.mass)
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
        await self._require_patron()
        catalog = await fetch_catalog(self.mass)
        machine = catalog.machines.get(item_id)
        if not machine:
            raise MediaNotFoundError(f"Unknown myNoise machine: {item_id}")
        machine = await fetch_machine_details(self.mass, machine)
        if not machine.layers:
            raise MediaNotFoundError(f"Machine {machine.name} has no audio layers")

        master_gain_db = float(self.get_config_value(CONF_MASTER_GAIN_DB, 0.0) or 0.0)
        use_variant_b = bool(self.get_config_value(CONF_USE_VARIANT_B, False))
        layer_gains = resolve_layer_gains(machine)

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
                # both file variants travel to the streamer, which stages the
                # selected one as a local file for the life of the playback
                "layers": [
                    {"url_a": layer.url_a, "url_b": layer.url_b} for layer in machine.layers
                ],
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
            MachineLayer(url_a=str(layer["url_a"]), url_b=str(layer["url_b"]))
            for layer in data.get("layers", [])
        ]
        gains = [float(gain) for gain in data.get("gains", [])]
        if not layers or not gains:
            raise MediaNotFoundError("Invalid stream data for myNoise machine")
        use_variant_b = bool(data.get("use_variant_b", False))
        machine = Machine(
            machine_id=str(data.get("machine_id", "mynoise")),
            name=str(data.get("machine_name", "myNoise")),
            layers=layers,
        )
        # stage the layer audio in a private temp dir (never in the persistent
        # cache) so the endless ffmpeg loop replays local files without
        # re-fetching from the CDN, while no copy of the sample library
        # outlives playback: the dir is removed when the stream ends
        staging_dir = await asyncio.to_thread(
            tempfile.mkdtemp, prefix="mynoise-", dir=self.mass.runtime_path
        )
        try:
            machine.layers = [
                await self._stage_layer(layer, gain, use_variant_b, staging_dir)
                for layer, gain in zip(layers, gains, strict=True)
            ]
            async for chunk in stream_machine(
                machine,
                gains,
                master_gain_db=float(data.get("master_gain_db", 0.0)),
                use_variant_b=use_variant_b,
                logger=self.logger,
            ):
                yield chunk
        finally:
            await asyncio.to_thread(shutil.rmtree, staging_dir, ignore_errors=True)

    # --------------------------------------------------------------- images

    async def resolve_image(self, path: str) -> str | bytes:
        """Resolve an image path (we only use remotely accessible urls)."""
        return path

    # -------------------------------------------------------------- helpers

    async def _require_patron(self) -> None:
        """
        Raise unless the configured patron email has been accepted by myNoise.

        myNoise content is licensed "FOR PERSONAL USE ONLY" to its donors, so
        nothing (free machines included) is offered before the patron login
        succeeded. A rejected email is cached until the refresh action retries
        it; a transient failure is not cached and is retried on the next call.
        """
        email = str(self.get_setup_value(CONF_PATRON_EMAIL, "") or "").strip()
        if not email:
            raise AuthenticationRequired(
                "myNoise requires the email address of your donation "
                "(re-run the provider setup and enter the 'Patron email')"
            )
        if self._patron_logged_in is None:
            try:
                self._patron_logged_in = await patron_login(self.mass, email)
            except Exception as err:
                # a transient failure (network hiccup) is not an answer from
                # myNoise: do not latch it, retry on the next call
                self.logger.warning("myNoise patron login failed", exc_info=True)
                raise ProviderUnavailableError(
                    "Could not reach myNoise to verify the patron email"
                ) from err
            if not self._patron_logged_in:
                self.logger.warning(
                    "myNoise rejected the configured patron email (the email must "
                    "match the one of your donation)"
                )
        if not self._patron_logged_in:
            raise AuthenticationFailed(
                "myNoise rejected the configured patron email: the machines require "
                "a myNoise donation and the email must match the one of your donation"
            )

    async def _stage_layer(
        self, layer: MachineLayer, gain: float, use_variant_b: bool, staging_dir: str
    ) -> MachineLayer:
        """Return the layer with its selected source downloaded into the staging dir."""
        url = layer.url_b if use_variant_b else layer.url_a
        if gain <= 0:
            # muted layers are skipped by the mixer, so no need to download them
            return layer
        digest = hashlib.sha1(url.encode()).hexdigest()[:16]
        ext = PurePath(urlparse(url).path).suffix or AUDIO_EXTENSION
        path = os.path.join(staging_dir, f"{digest}{ext}")
        try:
            async with self.mass.http_session.get(url) as resp:
                resp.raise_for_status()
                content = await resp.read()
            await asyncio.to_thread(_write_layer_to_file, path, content)
        except aiohttp.ClientError, TimeoutError, OSError:
            self.logger.warning(
                "Failed to download myNoise layer %s, streaming it remotely instead", url
            )
            return layer
        # both variants point at the staged file: the A/B choice is already made
        return MachineLayer(url_a=path, url_b=path, default_level=layer.default_level)

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
