"""
Local Audio Source plugin for Music Assistant

Captures raw PCM from a user-selected ALSA input and forwards it
to a Music Assistant player through an ultra-low-latency CUSTOM stream.

Author: (@Torrax)
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from typing import TYPE_CHECKING, cast
from collections.abc import AsyncGenerator

from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigEntryType,
    ConfigValueOption,
)
from music_assistant_models.enums import (
    ContentType,
    ProviderFeature,
    StreamType,
    PlayerState,
)
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import CONF_ENTRY_WARN_PREVIEW
from music_assistant.helpers.process import AsyncProcess, check_output
from music_assistant.models.plugin import PluginProvider, PluginSource

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest
    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

# ------------------------------------------------------------------
# CONFIG KEYS
# ------------------------------------------------------------------

CONF_INPUT_DEVICE = "input_device"             # e.g. "alsa:hw:1,0"
CONF_FRIENDLY_NAME = "friendly_name"           # UI label
CONF_THUMBNAIL_IMAGE = "thumbnail_image"       # URL only (for now)

CHANNELS = 2                 # 1=Mono, 2=Stereo
SAMPLE_RATE_HZ = 44100       # arecord -r
PERIOD_US = 10000            # arecord -F (ALSA period)
BUFFER_US = 20000            # arecord -B (small multiple of PERIOD_US)

PAUSE_DEBOUNCE_S = 0.5
RESUME_DEBOUNCE_S = 0.5

# ------------------------------------------------------------------
# PROVIDER SET-UP / CONFIG DIALOG
# ------------------------------------------------------------------


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Create plugin instance."""
    return LocalAudioSourceProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,     # noqa: ARG001
    action: str | None = None,          # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Config wizard for the plugin."""
    device_options = await _get_available_input_devices()

    return (
        CONF_ENTRY_WARN_PREVIEW,
        ConfigEntry(
            key=CONF_FRIENDLY_NAME,
            type=ConfigEntryType.STRING,
            label="Display Name",
            default_value="Local Audio Source",
            required=True,
        ),
        ConfigEntry(
            key=CONF_THUMBNAIL_IMAGE,
            type=ConfigEntryType.STRING,
            label="Thumbnail image",
            description="Direct URL to an SVG/PNG/JPG, e.g. https://example.com/icon.svg",
            default_value="",
            required=False,
        ),
        ConfigEntry(
            key=CONF_INPUT_DEVICE,
            type=ConfigEntryType.STRING,
            label="Audio Input Device",
            description="Select an ALSA capture device (arecord -l).",
            options=device_options,
            default_value="alsa:hw:1,0",
            required=True,
        ),
    )


async def _get_available_input_devices() -> list[ConfigValueOption]:
    """Scan for available ALSA capture devices using arecord -l.

    Labels are formatted as: 'hw X,Y - <last [] desc>'.
    """
    devices: list[ConfigValueOption] = []
    try:
        rc, out = await check_output("arecord", "-l")
        if rc == 0:
            for line in out.decode("utf-8", "ignore").strip().splitlines():
                # Example: "card 1: USB [USB Audio], device 0: USB Audio [USB Audio]"
                if not line.startswith("card ") or "device " not in line:
                    continue
                try:
                    after = line.split("card ", 1)[1]
                    card = after.split(":", 1)[0].strip()
                    dev = after.split("device ", 1)[1].split(":", 1)[0].strip()
                    last_desc = line.rsplit("[", 1)[-1].rstrip("]") if "[" in line else f"Card {card} Device {dev}"
                    label = f"hw {card},{dev} - {last_desc}"
                    devices.append(ConfigValueOption(label, f"alsa:hw:{card},{dev}"))
                except Exception:
                    continue
    except Exception:
        pass

    if not devices:
        devices = [ConfigValueOption("Manual Entry (alsa:hw:X,Y)", "alsa:")]
    return devices


# ------------------------------------------------------------------
# PROVIDER IMPLEMENTATION
# ------------------------------------------------------------------


class LocalAudioSourceProvider(PluginProvider):
    """Realtime audio-capture provider."""

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
    ) -> None:
        super().__init__(mass, manifest, config)

        # Resolve config
        self.device: str = cast(str, self.config.get_value(CONF_INPUT_DEVICE))
        self.friendly_name: str = cast(str, self.config.get_value(CONF_FRIENDLY_NAME))
        self.thumbnail_image: str = cast(str, self.config.get_value(CONF_THUMBNAIL_IMAGE) or "")

        # Fixed audio params
        self.sample_rate: int = SAMPLE_RATE_HZ
        self.period_us: int = PERIOD_US
        self.buffer_us: int = BUFFER_US
        self.channels: int = CHANNELS

        # Parse device string for ALSA (arecord)
        self.alsa_device = self._parse_device_string(self.device)

        # Runtime helpers
        self._capture_proc: AsyncProcess | None = None
        self._paused = False
        self._stream_active = False
        self._current_player_id: str | None = None
        self._monitor_task: asyncio.Task | None = None          # type: ignore[type-arg]
        self._capture_lock = asyncio.Lock()
        self._last_state = None
        self._state_since = time.monotonic()
        self._active_stream_id: int | None = None

        # Codec management for WAV output
        self._original_codec: str | None = None
        self._codec_changed: bool = False

        # Static plugin-wide audio source definition
        metadata = PlayerMedia("Local Audio Source")
        if self.thumbnail_image and self.thumbnail_image.startswith(("http://", "https://")):
            metadata.image_url = self.thumbnail_image
        elif self.thumbnail_image:
            self.logger.warning("Only URLs are supported for thumbnail images. Ignoring: %s", self.thumbnail_image)

        self._source_details = PluginSource(
            id=self.instance_id,
            name=self.friendly_name,
            passive=False,
            can_play_pause=True,
            can_seek=False,
            can_next_previous=False,
            audio_format=AudioFormat(
                content_type=ContentType.PCM_S16LE,
                codec_type=ContentType.PCM_S16LE,
                sample_rate=self.sample_rate,
                bit_depth=16,
                channels=self.channels,
            ),
            metadata=metadata,
            stream_type=StreamType.CUSTOM,
            path="",
        )

    # ---------------- Provider API ----------------

    @property
    def supported_features(self) -> set[ProviderFeature]:
        return {ProviderFeature.AUDIO_SOURCE}

    async def handle_async_init(self) -> None:
        """Called when MA is ready."""
        original_get_plugin_source_url = self.mass.streams.get_plugin_source_url

        async def patched_get_plugin_source_url(plugin_source: str, player_id: str) -> str:
            # Ensure WAV before generating URL for this plugin
            if plugin_source == self.instance_id:
                await self._save_and_set_wav_codec(player_id)
            return await original_get_plugin_source_url(plugin_source, player_id)

        self.mass.streams.get_plugin_source_url = patched_get_plugin_source_url

    async def _save_and_set_wav_codec(self, player_id: str) -> None:
        """Save current codec and set player to WAV format."""
        try:
            current_codec = await self.mass.config.get_player_config_value(player_id, "output_codec")
            if current_codec != "wav":
                self._original_codec = current_codec
                self._codec_changed = True
                await self.mass.config.save_player_config(player_id=player_id, values={"output_codec": "wav"})
                self.mass.config._value_cache.clear()
                await asyncio.sleep(0.5)
                self.mass.players.update(player_id, force_update=True)
                await asyncio.sleep(0.2)
        except Exception as err:
            self.logger.error("Failed to set WAV codec for player %s: %s", player_id, err)

    async def _restore_original_codec(self, player_id: str) -> None:
        """Restore the original codec setting."""
        if not self._codec_changed or not self._original_codec:
            return
        try:
            await self.mass.config.save_player_config(player_id=player_id, values={"output_codec": self._original_codec})
            self.logger.info("Restored player %s codec to '%s'", player_id, self._original_codec)
        except Exception as err:
            self.logger.error("Failed to restore codec for player %s: %s", player_id, err)
        finally:
            self._original_codec = None
            self._codec_changed = False

    async def _monitor_player_state(self, player_id: str) -> None:
        """Monitor player state with debounce to avoid flapping."""
        self._current_player_id = player_id
        self._last_state = None
        self._state_since = time.monotonic()

        while self._stream_active:
            try:
                player = self.mass.players.get(player_id)
                if not player:
                    break
                now = time.monotonic()
                current_state = player.state
                if current_state != self._last_state:
                    self._last_state = current_state
                    self._state_since = now
                stable_for = now - self._state_since

                if current_state == PlayerState.PAUSED and stable_for >= PAUSE_DEBOUNCE_S and not self._paused:
                    self._paused = True
                elif current_state == PlayerState.PLAYING and stable_for >= RESUME_DEBOUNCE_S and self._paused:
                    self._paused = False
                elif current_state == PlayerState.IDLE and stable_for >= PAUSE_DEBOUNCE_S and not self._paused:
                    self._paused = True

                await asyncio.sleep(0.2)
            except Exception:
                await asyncio.sleep(1)

    async def unload(self, is_removed: bool = False) -> None:
        """Tear down."""
        self._stream_active = False

        if self._current_player_id and self._codec_changed:
            await self._restore_original_codec(self._current_player_id)

        async with self._capture_lock:
            if self._capture_proc and not self._capture_proc.closed:
                with suppress(Exception):
                    await self._capture_proc.close(True)
                self._capture_proc = None

        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._monitor_task
        self._monitor_task = None

        # Refresh player source lists
        for player in self.mass.players.all():
            try:
                player.source_list = [s for s in player.source_list if s.id != self.instance_id]
                self.mass.players.update(player.player_id, force_update=True)
            except Exception:
                continue

    # ---------------- PluginProvider hooks ----------------

    def get_source(self) -> PluginSource:
        """Expose this input as a PlayerSource (CUSTOM stream)."""
        return self._source_details

    async def cmd_pause(self, player_id: str) -> None:
        """Pause: stop arecord but keep stream alive."""
        self._paused = True
        async with self._capture_lock:
            if self._capture_proc and not self._capture_proc.closed:
                with suppress(Exception):
                    await self._capture_proc.close(True)
                self._capture_proc = None

    async def cmd_play(self, player_id: str) -> None:
        """Resume: clear pause flag; loop will restart arecord."""
        self._paused = False

    async def cmd_stop(self, player_id: str) -> None:
        """Stop stream and restore codec."""
        self._paused = False
        self._stream_active = False
        await self._restore_original_codec(player_id)
        async with self._capture_lock:
            if self._capture_proc and not self._capture_proc.closed:
                with suppress(Exception):
                    await self._capture_proc.close(True)
                self._capture_proc = None

    async def get_audio_stream(self, player_id: str) -> AsyncGenerator[bytes, None]:
        """Yield raw PCM from arecord directly to MA (low-latency CUSTOM stream)."""
        self._stream_active = True
        self._current_player_id = player_id
        self._active_stream_id = (self._active_stream_id or 0) + 1
        my_stream_id = self._active_stream_id

        # ensure WAV for URL generation fallback
        current_codec = await self.mass.config.get_player_config_value(player_id, "output_codec")
        if current_codec != "wav":
            await self._save_and_set_wav_codec(player_id)

        # start player-state monitor
        if not self._monitor_task or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor_player_state(player_id))

        bytes_per_sec = self.sample_rate * self.channels * 2  # 16-bit PCM
        period_s = max(1, self.period_us) / 1_000_000
        chunk_size = max(256, int(bytes_per_sec * period_s))

        # Single arecord command (no fallbacks/retries)
        arecord_cmd: list[str] = [
            "arecord",
            "-q",
            "-D", self.alsa_device,
            "-f", "S16_LE",
            "-c", str(self.channels),
            "-r", str(self.sample_rate),
            "-t", "raw",
            "-M",
            "-F", str(self.period_us),
            "-B", str(self.buffer_us),
            "-"
        ]

        async def start_arecord_once() -> AsyncProcess | None:
            self.logger.info(
                "Starting capture for %s (dev=%s sr=%d ch=%d F=%dµs B=%dµs chunk=%dB)",
                self.friendly_name, self.alsa_device, self.sample_rate, self.channels,
                self.period_us, self.buffer_us, chunk_size
            )
            proc = AsyncProcess(arecord_cmd, stdout=True, stderr=True, name=f"audio-capture[{self.friendly_name}]")
            try:
                await proc.start()
                return proc
            except Exception as err:
                self.logger.error("arecord failed to start: %s", err)
                return None

        early_restored = False

        try:
            while self._stream_active and my_stream_id == self._active_stream_id:
                if self._paused:
                    async with self._capture_lock:
                        if self._capture_proc and not self._capture_proc.closed:
                            with suppress(Exception):
                                await self._capture_proc.close(True)
                            self._capture_proc = None
                    await asyncio.sleep(period_s)
                    continue

                async with self._capture_lock:
                    if not self._capture_proc or self._capture_proc.closed:
                        self._capture_proc = await start_arecord_once()

                if not self._capture_proc:
                    await asyncio.sleep(period_s)
                    continue

                try:
                    chunk = await asyncio.wait_for(self._capture_proc.read(chunk_size), timeout=period_s * 2)
                    if not chunk:
                        async with self._capture_lock:
                            self._capture_proc = None
                        await asyncio.sleep(0.05)
                        continue

                    # On first successful audio, restore original codec in background.
                    if not early_restored and self._codec_changed:
                        early_restored = True
                        self.logger.info("First audio chunk received; restoring original codec…")
                        asyncio.create_task(self._restore_original_codec(player_id))

                    yield chunk
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    async with self._capture_lock:
                        if self._capture_proc:
                            with suppress(Exception):
                                await self._capture_proc.close(True)
                            self._capture_proc = None
                await asyncio.sleep(0.001)

        except Exception as err:
            self.logger.error("Error in audio stream for %s: %s", self.friendly_name, err)

        finally:
            # Safety net: if not already restored, do it now.
            if my_stream_id == self._active_stream_id:
                await self._restore_original_codec(player_id)

            if self._monitor_task and not self._monitor_task.done():
                self._monitor_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._monitor_task
            self._monitor_task = None

            async with self._capture_lock:
                if self._capture_proc and not self._capture_proc.closed:
                    with suppress(Exception):
                        await self._capture_proc.close(True)
                    self._capture_proc = None

            if my_stream_id == self._active_stream_id:
                self._stream_active = False

    # ---------------- Internals ----------------

    def _parse_device_string(self, device: str) -> str:
        """Normalize device string for arecord."""
        if device.startswith("alsa:"):
            return device[5:] or "hw:1,0"
        if device in ("default", ""):
            return "hw:1,0"
        return device
