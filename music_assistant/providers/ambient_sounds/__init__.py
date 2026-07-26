"""
Ambient Sounds provider for Music Assistant.

Serves a small catalog of locally generated ambient sound loops (white, pink and
brown noise, ocean waves) as sound effect items. Loops are synthesized with ffmpeg
on first use — no bundled assets, no network access — and constructed to repeat
seamlessly, which makes them suitable both for direct playback and as source for
the queue audio overlay feature.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiofiles.os import makedirs, remove, replace
from aiofiles.os import path as aiopath
from music_assistant_models.enums import ContentType, MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import AudioError, MediaNotFoundError
from music_assistant_models.media_items import AudioFormat, ProviderMapping, SoundEffect
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.helpers.process import check_output
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SOUND_EFFECTS,
}

# rendered loop specification - bump RENDER_VERSION whenever the recipe changes
# so previously rendered files are invalidated
RENDER_VERSION = 1
SAMPLE_RATE = 44100
LOOP_DURATION = 30
CROSSFADE_DURATION = 2


@dataclass(frozen=True)
class AmbientPreset:
    """Recipe for a locally generated ambient sound loop."""

    name: str
    description: str
    # noise color fed to ffmpeg's anoisesrc generator
    noise_color: str
    # gain that brings the rendered loop to -14 LUFS integrated loudness,
    # so all presets play equally loud (matching the volume normalization default)
    gain_db: float
    # optional extra filter(s) applied to the finished loop; must be loop-safe,
    # i.e. periodic effects need a whole number of cycles per LOOP_DURATION
    extra_filter: str = ""


PRESETS: dict[str, AmbientPreset] = {
    "white_noise": AmbientPreset(
        name="White noise",
        description="Bright, steady hiss with equal energy across all frequencies.",
        noise_color="white",
        gain_db=-9.3,
    ),
    "pink_noise": AmbientPreset(
        name="Pink noise",
        description="Softer noise with reduced high frequencies, similar to steady rainfall.",
        noise_color="pink",
        gain_db=3.7,
    ),
    "brown_noise": AmbientPreset(
        name="Brown noise",
        description="Deep, low rumble similar to a distant waterfall or heavy surf.",
        noise_color="brown",
        gain_db=4.6,
    ),
    "ocean_waves": AmbientPreset(
        name="Ocean waves",
        description="Deep rolling noise with a slow, wave-like swell.",
        noise_color="brown",
        gain_db=8.0,
        # 0.1 Hz swell = 3 full cycles per 30s loop, keeping the loop seamless
        extra_filter="tremolo=f=0.1:d=0.75",
    ),
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return AmbientSoundsProvider(mass, manifest, config, SUPPORTED_FEATURES)


class AmbientSoundsProvider(MusicProvider):
    """Music provider serving locally generated ambient sound loops."""

    _render_dir: str
    _render_lock: asyncio.Lock

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to setup this provider (none needed)."""
        return ()

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._render_dir = os.path.join(self.mass.cache_path, self.domain)
        self._render_lock = asyncio.Lock()

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return False

    async def get_sound_effect(self, prov_sound_effect_id: str) -> SoundEffect:
        """Get full sound effect details by id."""
        if not (preset := PRESETS.get(prov_sound_effect_id)):
            raise MediaNotFoundError(f"Unknown sound effect: {prov_sound_effect_id}")
        return self._build_sound_effect(prov_sound_effect_id, preset)

    async def get_sound_effects(self) -> AsyncGenerator[SoundEffect]:
        """Get all sound effect items this provider offers."""
        for preset_id, preset in PRESETS.items():
            yield self._build_sound_effect(preset_id, preset)

    async def get_stream_details(
        self, item_id: str, media_type: MediaType = MediaType.TRACK
    ) -> StreamDetails:
        """Return the streamdetails to stream the given sound effect."""
        if not (preset := PRESETS.get(item_id)):
            raise MediaNotFoundError(f"Unknown sound effect: {item_id}")
        file_path = await self._ensure_rendered(item_id, preset)
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=self._loop_audio_format(),
            media_type=MediaType.SOUND_EFFECT,
            stream_type=StreamType.LOCAL_FILE,
            duration=LOOP_DURATION,
            path=file_path,
            allow_seek=True,
            can_seek=True,
        )

    def _build_sound_effect(self, preset_id: str, preset: AmbientPreset) -> SoundEffect:
        """Create a SoundEffect item for the given preset."""
        sound_effect = SoundEffect(
            item_id=preset_id,
            provider=self.instance_id,
            name=preset.name,
            translation_key=preset_id,
            duration=LOOP_DURATION,
            provider_mappings={
                ProviderMapping(
                    item_id=preset_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=self._loop_audio_format(),
                )
            },
        )
        sound_effect.metadata.description = preset.description
        return sound_effect

    async def _ensure_rendered(self, preset_id: str, preset: AmbientPreset) -> str:
        """Return the path to the rendered loop file, rendering it on first use."""
        file_path = os.path.join(self._render_dir, f"{preset_id}_v{RENDER_VERSION}.flac")
        async with self._render_lock:
            if await aiopath.isfile(file_path):
                return file_path
            await makedirs(self._render_dir, exist_ok=True)
            # render to a temp file first so an interrupted render can never
            # leave a partial file behind at the final path
            tmp_path = f"{file_path}.tmp"
            returncode, output = await check_output(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-filter_complex",
                self._build_filter_graph(preset),
                "-map",
                "[out]",
                "-sample_fmt",
                "s16",
                "-f",
                "flac",
                tmp_path,
            )
            if returncode != 0:
                with suppress(OSError):
                    await remove(tmp_path)
                raise AudioError(
                    f"Failed to render ambient sound {preset_id}: {output.decode(errors='replace')}"
                )
            await replace(tmp_path, file_path)
        return file_path

    def _build_filter_graph(self, preset: AmbientPreset) -> str:
        """Build the ffmpeg filter graph that renders the preset as a seamless loop."""
        # anoisesrc output is deterministic per seed, which enables a sample-exact
        # seamless loop: generate CROSSFADE_DURATION extra seconds, trim that amount
        # off the head of the main body and crossfade its tail into a fresh render
        # of exactly that head. The faded-out tail thus ends precisely where the
        # file starts. Two decorrelated seeds (left/right) create wide stereo.
        render_duration = LOOP_DURATION + CROSSFADE_DURATION
        src = f"anoisesrc=r={SAMPLE_RATE}:color={preset.noise_color}:a=0.5"
        extra = f"{preset.extra_filter}," if preset.extra_filter else ""
        return (
            f"{src}:seed=101:d={render_duration}[ml];"
            f"{src}:seed=202:d={render_duration}[mr];"
            f"{src}:seed=101:d={CROSSFADE_DURATION}[hl];"
            f"{src}:seed=202:d={CROSSFADE_DURATION}[hr];"
            "[ml][mr]join=inputs=2:channel_layout=stereo,"
            f"atrim=start={CROSSFADE_DURATION},asetpts=PTS-STARTPTS[main];"
            "[hl][hr]join=inputs=2:channel_layout=stereo[head];"
            f"[main][head]acrossfade=d={CROSSFADE_DURATION}:c1=qsin:c2=qsin,"
            f"{extra}volume={preset.gain_db}dB[out]"
        )

    def _loop_audio_format(self) -> AudioFormat:
        """Return the audio format of the rendered loop files."""
        return AudioFormat(
            content_type=ContentType.FLAC,
            sample_rate=SAMPLE_RATE,
            bit_depth=16,
            channels=2,
        )
