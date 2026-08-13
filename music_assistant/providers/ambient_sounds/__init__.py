"""
Ambient Sounds provider for Music Assistant.

Serves a small catalog of locally generated ambient sound loops (white, pink and
brown noise, ocean waves) as sound effect items. Loops are synthesized with ffmpeg
on first use — no bundled assets, no network access — and constructed to repeat
seamlessly, which makes them suitable both for direct playback and as source for
the queue audio overlay feature.

Users can extend the catalog with their own ambient sounds by adding a stream URL,
similar to how radio station URLs are added to the builtin provider.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, NotRequired, TypedDict

from aiofiles.os import makedirs, remove, replace
from aiofiles.os import path as aiopath
from music_assistant_models.auth import Scope
from music_assistant_models.enums import ContentType, MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import AudioError, MediaNotFoundError
from music_assistant_models.media_items import AudioFormat, ProviderMapping, SoundEffect
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import CONF_PROVIDERS
from music_assistant.helpers.process import check_output
from music_assistant.helpers.tags import AudioTags, async_parse_tags
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

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


CONF_KEY_CUSTOM_SOUNDS = "custom_sounds"
CACHE_CATEGORY_MEDIA_INFO: Final[int] = 1


class StoredSound(TypedDict):
    """Definition of a user-added ambient sound stored in persistent storage."""

    item_id: str  # the stream url
    name: str
    duration: NotRequired[int]
    content_type: NotRequired[str]


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return AmbientSoundsProvider(mass, manifest, config, SUPPORTED_FEATURES)


class AmbientSoundsProvider(MusicProvider):
    """Music provider serving locally generated ambient sound loops."""

    _render_dir: str
    _render_lock: asyncio.Lock
    _unregister_handles: list[Callable[[], None]]

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to setup this provider (none needed)."""
        return ()

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._render_dir = os.path.join(self.mass.cache_path, self.domain)
        self._render_lock = asyncio.Lock()
        self._unregister_handles = []

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()
        self._unregister_handles.append(
            self.mass.register_api_command(
                "ambient_sounds/add_sound", self.add_sound, required_scope=Scope.LIBRARY_WRITE
            )
        )
        self._unregister_handles.append(
            self.mass.register_api_command(
                "ambient_sounds/remove_sound",
                self.remove_sound,
                required_scope=Scope.LIBRARY_WRITE,
            )
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        for unregister in self._unregister_handles:
            unregister()
        self._unregister_handles.clear()
        await super().unload(is_removed)

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return False

    async def add_sound(self, url: str, name: str) -> SoundEffect:
        """
        Add a custom ambient sound by stream URL.

        :param url: Stream URL of the ambient sound.
        :param name: Display name.
        """
        # probe the url upfront so an invalid/unreachable url is rejected at add time
        media_info = await self._get_media_info(url, force_refresh=True)
        stored_item = StoredSound(item_id=url, name=name)
        if media_info.duration:
            stored_item["duration"] = int(media_info.duration)
        if media_info.format:
            stored_item["content_type"] = media_info.format
        stored_items = [x for x in self._stored_sounds() if x["item_id"] != url]
        stored_items.append(stored_item)
        self.mass.config.set(self._custom_sounds_conf_key, stored_items)
        return self._build_custom_sound_effect(stored_item)

    async def remove_sound(self, url: str) -> None:
        """
        Remove a previously added custom ambient sound.

        :param url: Stream URL of the ambient sound to remove.
        """
        stored_items = [x for x in self._stored_sounds() if x["item_id"] != url]
        self.mass.config.set(self._custom_sounds_conf_key, stored_items)
        await self.mass.cache.delete(
            url, provider=self.instance_id, category=CACHE_CATEGORY_MEDIA_INFO
        )

    async def get_sound_effect(self, prov_sound_effect_id: str) -> SoundEffect:
        """Get full sound effect details by id."""
        if preset := PRESETS.get(prov_sound_effect_id):
            return self._build_sound_effect(prov_sound_effect_id, preset)
        if stored_item := self._get_stored_sound(prov_sound_effect_id):
            return self._build_custom_sound_effect(stored_item)
        raise MediaNotFoundError(f"Unknown sound effect: {prov_sound_effect_id}")

    async def get_sound_effects(self) -> AsyncGenerator[SoundEffect]:
        """Get all sound effect items this provider offers."""
        for preset_id, preset in PRESETS.items():
            yield self._build_sound_effect(preset_id, preset)
        for stored_item in self._stored_sounds():
            yield self._build_custom_sound_effect(stored_item)

    async def get_stream_details(
        self, item_id: str, media_type: MediaType = MediaType.TRACK
    ) -> StreamDetails:
        """Return the streamdetails to stream the given sound effect."""
        if preset := PRESETS.get(item_id):
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
        if not self._get_stored_sound(item_id):
            raise MediaNotFoundError(f"Unknown sound effect: {item_id}")
        media_info = await self._get_media_info(item_id)
        # endless (radio-style) streams have no duration and can not be seeked
        seekable = bool(media_info.duration) and not media_info.get("icyname")
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.try_parse(media_info.format),
                sample_rate=media_info.sample_rate,
                bit_depth=media_info.bits_per_sample,
                channels=media_info.channels,
            ),
            media_type=MediaType.SOUND_EFFECT,
            stream_type=StreamType.HTTP,
            duration=int(media_info.duration) if media_info.duration else None,
            path=item_id,
            allow_seek=seekable,
            can_seek=seekable,
        )

    @property
    def _custom_sounds_conf_key(self) -> str:
        """Return the config storage path for the user-added custom sounds."""
        # stored within the provider's own config (setup_data) so the data is
        # removed together with the provider config when the provider is removed
        return f"{CONF_PROVIDERS}/{self.instance_id}/setup_data/{CONF_KEY_CUSTOM_SOUNDS}"

    def _stored_sounds(self) -> list[StoredSound]:
        """Return all user-added custom sounds from persistent storage."""
        stored_items: list[StoredSound] = self.mass.config.get(self._custom_sounds_conf_key, [])
        return stored_items

    def _get_stored_sound(self, url: str) -> StoredSound | None:
        """Return the stored custom sound for the given url, if present."""
        return next((x for x in self._stored_sounds() if x["item_id"] == url), None)

    def _build_custom_sound_effect(self, stored_item: StoredSound) -> SoundEffect:
        """Create a SoundEffect item for the given user-added custom sound."""
        url = stored_item["item_id"]
        return SoundEffect(
            item_id=url,
            provider=self.instance_id,
            name=stored_item["name"],
            duration=stored_item.get("duration", 0),
            provider_mappings={
                ProviderMapping(
                    item_id=url,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.try_parse(stored_item.get("content_type", url))
                    ),
                )
            },
        )

    async def _get_media_info(self, url: str, force_refresh: bool = False) -> AudioTags:
        """Retrieve (cached) mediainfo for the given custom sound url."""
        cached_info = await self.mass.cache.get(
            url, provider=self.instance_id, category=CACHE_CATEGORY_MEDIA_INFO
        )
        if cached_info and not force_refresh:
            return AudioTags.parse(cached_info)
        # parse info with ffprobe (and store in cache)
        media_info = await async_parse_tags(url)
        await self.mass.cache.set(
            url, media_info.raw, provider=self.instance_id, category=CACHE_CATEGORY_MEDIA_INFO
        )
        return media_info

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
