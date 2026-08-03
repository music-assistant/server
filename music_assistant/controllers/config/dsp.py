"""DSP configuration handling for the ConfigController."""

from __future__ import annotations

import asyncio
import base64
import binascii
import os
from contextlib import suppress
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Final

import aiofiles
import shortuuid
from music_assistant_models.auth import Scope
from music_assistant_models.dsp import ConvolutionFilter, DSPConfig, DSPConfigPreset
from music_assistant_models.enums import EventType
from music_assistant_models.errors import InvalidDataError

from music_assistant.constants import (
    CONF_PLAYER_DSP,
    CONF_PLAYER_DSP_IRS,
    CONF_PLAYER_DSP_PRESETS,
    DSP_IR_ID_RE,
    DSP_IRS_DIRNAME,
)
from music_assistant.helpers.api import api_command
from music_assistant.helpers.process import check_output
from music_assistant.helpers.security import is_safe_path
from music_assistant.helpers.tags import async_parse_tags

if TYPE_CHECKING:
    from music_assistant import MusicAssistant
# cap the accepted upload so a caller cannot exhaust memory or disk; IRs are small
MAX_IR_BYTES: Final = 10 * 1024 * 1024
# afir rejects anything over 30 seconds at playback time, so cap it at upload
MAX_IR_SECONDS: Final = 10.0
# bound the transcode so a crafted upload cannot leave ffmpeg running forever
IR_TRANSCODE_TIMEOUT: Final = 60.0
# a mono IR is applied to both channels and a stereo one channel per side, but afir
# would silently downmix anything wider (e.g. a four channel true stereo IR) to match
# the stream, blending the measurements instead of applying them
MAX_IR_CHANNELS: Final = 2


class DSPConfigMixin:
    """Mixin providing DSP configuration handling for the ConfigController."""

    # Type hints for attributes/methods provided by the class this mixin is used with
    if TYPE_CHECKING:
        mass: MusicAssistant

        def get(self, key: str, default: Any = None) -> Any: ...  # noqa: D102

        def set(self, key: str, value: Any) -> None: ...  # noqa: D102

        def remove(self, key: str) -> None: ...  # noqa: D102

    @api_command("config/players/dsp/get", required_scope=Scope.CONFIG_PLAYERS_READ)
    def get_player_dsp_config(self, player_id: str) -> DSPConfig:
        """
        Return the DSP Configuration for a player.

        In case the player does not have a DSP configuration, a default one is returned.
        """
        if raw_conf := self.get(f"{CONF_PLAYER_DSP}/{player_id}"):
            return DSPConfig.from_dict(raw_conf)
        # return default DSP config
        dsp_config = DSPConfig()
        # The DSP config does not do anything by default, so we disable it
        dsp_config.enabled = False
        return dsp_config

    @api_command("config/players/dsp/save", required_scope=Scope.CONFIG_PLAYERS_WRITE)
    async def save_dsp_config(self, player_id: str, config: DSPConfig) -> DSPConfig:
        """
        Save/update DSPConfig for a player.

        This method will validate the config and apply it to the player.
        """
        config = deepcopy(config)
        config.preset_id = None
        return await self._save_dsp_config(player_id, config)

    @api_command("config/players/dsp/apply_preset", required_scope=Scope.CONFIG_PLAYERS_WRITE)
    async def apply_dsp_preset(self, player_id: str, preset_id: str) -> DSPConfig:
        """
        Apply a persisted DSP preset to a player.

        :param player_id: Player that should use the preset.
        :param preset_id: Preset identifier to apply.
        """
        if (preset := self._get_dsp_preset(preset_id)) is None:
            msg = f"DSP preset {preset_id} not found"
            raise KeyError(msg)
        config = deepcopy(preset.config)
        config.preset_id = preset_id
        return await self._save_dsp_config(player_id, config)

    @api_command("config/dsp_presets/get", required_scope=Scope.CONFIG_PLAYERS_READ)
    async def get_dsp_presets(self) -> list[DSPConfigPreset]:
        """Return all user-defined DSP presets."""
        raw_presets = self.get(CONF_PLAYER_DSP_PRESETS, {})
        return [DSPConfigPreset.from_dict(preset) for preset in raw_presets.values()]

    @api_command("config/dsp_presets/save", required_scope=Scope.CONFIG_PLAYERS_WRITE)
    async def save_dsp_presets(self, preset: DSPConfigPreset) -> DSPConfigPreset:
        """
        Save/update a user-defined DSP presets.

        This method will validate the config before saving it to the persistent storage.
        """
        preset = deepcopy(preset)
        preset.config.preset_id = None
        preset.validate()

        previous = self._get_dsp_preset(preset.preset_id) if preset.preset_id else None
        if preset.preset_id is None:
            # Generate a new preset_id if it does not exist
            preset.preset_id = shortuuid.random(8).lower()

        # Save the preset to the persistent storage
        self.set(f"{CONF_PLAYER_DSP_PRESETS}/preset_{preset.preset_id}", preset.to_dict())
        if previous:
            previous_config = deepcopy(previous.config)
            previous_config.preset_id = None
            if previous_config != preset.config:
                self._clear_dsp_preset_assignments(preset.preset_id)

        all_presets = await self.get_dsp_presets()

        self.mass.signal_event(
            EventType.DSP_PRESETS_UPDATED,
            data=all_presets,
        )

        return preset

    @api_command("config/dsp_presets/remove", required_scope=Scope.CONFIG_PLAYERS_WRITE)
    async def remove_dsp_preset(self, preset_id: str) -> None:
        """Remove a user-defined DSP preset."""
        self.remove(f"{CONF_PLAYER_DSP_PRESETS}/preset_{preset_id}")
        self._clear_dsp_preset_assignments(preset_id)

        all_presets = await self.get_dsp_presets()

        self.mass.signal_event(
            EventType.DSP_PRESETS_UPDATED,
            data=all_presets,
        )

    @api_command("config/dsp_irs/list", required_scope=Scope.CONFIG_PLAYERS_READ)
    def get_dsp_irs(self) -> list[dict[str, Any]]:
        """Return the metadata for all stored convolution impulse responses."""
        stored: dict[str, dict[str, Any]] = self.get(CONF_PLAYER_DSP_IRS, {})
        return list(stored.values())

    @api_command("config/dsp_irs/upload", required_scope=Scope.CONFIG_PLAYERS_WRITE)
    async def upload_dsp_ir(self, name: str, data: str) -> dict[str, Any]:
        """
        Store a convolution impulse response and return its metadata record.

        :param name: Display name for the impulse response.
        :param data: Base64 encoded contents of the audio file to store.
        """
        # base64 inflates by roughly 4/3, so bound the encoded input before decoding
        # to cap memory, then confirm the decoded size against the real limit
        if len(data) > MAX_IR_BYTES // 3 * 4 + 8:
            raise InvalidDataError("Impulse response file exceeds the size limit")
        try:
            raw = base64.b64decode(data, validate=True)
        except binascii.Error as err:
            raise InvalidDataError("Impulse response data is not valid base64") from err
        if len(raw) > MAX_IR_BYTES:
            raise InvalidDataError("Impulse response file exceeds the size limit")

        ir_dir = self._dsp_irs_dir()
        await asyncio.to_thread(os.makedirs, ir_dir, exist_ok=True)
        ir_id = shortuuid.random(8).lower()
        upload_path = os.path.join(ir_dir, f".{ir_id}.upload")
        ir_path = self._dsp_ir_path(ir_id)

        async with aiofiles.open(upload_path, "wb") as ir_file:
            await ir_file.write(raw)
        try:
            # transcoding to wav both validates the upload is decodable audio and
            # gives afir a format it can always read back at playback time
            returncode, output = await check_output(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                # restrict to the file protocol so a crafted upload (e.g. a playlist
                # or concat script) cannot make ffmpeg reach out over the network
                "-protocol_whitelist",
                "file",
                "-i",
                upload_path,
                # bound the decoded output so a small but very long input cannot fill
                # the disk before the duration check below runs. the extra second keeps
                # an over-long response above the cap, so that check still rejects it
                "-t",
                str(MAX_IR_SECONDS + 1),
                "-c:a",
                "pcm_f32le",
                ir_path,
                timeout=IR_TRANSCODE_TIMEOUT,
            )
            if returncode != 0:
                await self._remove_file(ir_path)
                raise InvalidDataError(
                    f"Uploaded file is not valid audio: {output.decode().strip()}"
                )
            tags = await async_parse_tags(ir_path)
            if tags.channels > MAX_IR_CHANNELS:
                await self._remove_file(ir_path)
                raise InvalidDataError(
                    f"Impulse response has {tags.channels} channels: "
                    "only mono and stereo files are supported"
                )
            if tags.duration is not None and tags.duration > MAX_IR_SECONDS:
                await self._remove_file(ir_path)
                raise InvalidDataError(
                    f"Impulse response is {tags.duration:.1f} seconds long: "
                    f"the limit is {MAX_IR_SECONDS:.0f} seconds"
                )
        except TimeoutError:
            await self._remove_file(ir_path)
            raise InvalidDataError("Impulse response took too long to convert") from None
        finally:
            await self._remove_file(upload_path)

        record = {
            "ir_id": ir_id,
            "name": name,
            "sample_rate": tags.sample_rate,
            "channels": tags.channels,
            "duration": tags.duration,
        }
        stored: dict[str, dict[str, Any]] = self.get(CONF_PLAYER_DSP_IRS, {})
        stored[ir_id] = record
        self.set(CONF_PLAYER_DSP_IRS, stored)

        self.mass.signal_event(
            EventType.DSP_IRS_UPDATED,
            data=self.get_dsp_irs(),
        )

        return record

    @api_command("config/dsp_irs/remove", required_scope=Scope.CONFIG_PLAYERS_WRITE)
    async def remove_dsp_ir(self, ir_id: str) -> None:
        """Remove a stored convolution impulse response by its identifier."""
        stored: dict[str, dict[str, Any]] = self.get(CONF_PLAYER_DSP_IRS, {})
        known = stored.pop(ir_id, None) is not None
        if known:
            self.set(CONF_PLAYER_DSP_IRS, stored)
        try:
            ir_path = self._dsp_ir_path(ir_id)
        except InvalidDataError:
            # an unusable stored id has no file to remove, but the record is gone
            if not known:
                raise
        else:
            await self._remove_file(ir_path)
        await self._clear_dsp_ir_assignments(ir_id)

        self.mass.signal_event(
            EventType.DSP_IRS_UPDATED,
            data=self.get_dsp_irs(),
        )

    def _get_dsp_preset(self, preset_id: str | None) -> DSPConfigPreset | None:
        """Return a DSP preset by identifier."""
        if preset_id is None:
            return None
        if raw_preset := self.get(f"{CONF_PLAYER_DSP_PRESETS}/preset_{preset_id}"):
            return DSPConfigPreset.from_dict(raw_preset)
        return None

    async def _save_dsp_config(self, player_id: str, config: DSPConfig) -> DSPConfig:
        """Persist and apply a validated DSP configuration."""
        config.validate()
        previous = self.get_player_dsp_config(player_id)
        self.set(f"{CONF_PLAYER_DSP}/{player_id}", config.to_dict())
        if previous.enabled or config.enabled:
            await self.mass.players.on_player_dsp_change(player_id)
        elif previous.preset_id != config.preset_id:
            self.mass.streams.audio_processing.update_player_dsp_preset(
                player_id,
                config.preset_id,
            )
        self.mass.signal_event(
            EventType.PLAYER_DSP_CONFIG_UPDATED,
            object_id=player_id,
            data=config,
        )
        return config

    def _clear_dsp_preset_assignments(self, preset_id: str) -> None:
        """Clear a preset selection without changing player DSP values."""
        raw_configs: dict[str, dict[str, Any]] = self.get(CONF_PLAYER_DSP, {})
        for player_id, raw_config in tuple(raw_configs.items()):
            config = DSPConfig.from_dict(raw_config)
            if config.preset_id != preset_id:
                continue
            config.preset_id = None
            self.set(f"{CONF_PLAYER_DSP}/{player_id}", config.to_dict())
            self.mass.streams.audio_processing.update_player_dsp_preset(player_id, None)
            self.mass.signal_event(
                EventType.PLAYER_DSP_CONFIG_UPDATED,
                object_id=player_id,
                data=config,
            )

    async def _clear_dsp_ir_assignments(self, ir_id: str) -> None:
        """Blank a removed impulse response from any player config or preset using it."""
        raw_configs: dict[str, dict[str, Any]] = self.get(CONF_PLAYER_DSP, {})
        for player_id, raw_config in tuple(raw_configs.items()):
            config = DSPConfig.from_dict(raw_config)
            if not _blank_convolution_ir(config, ir_id):
                continue
            self.set(f"{CONF_PLAYER_DSP}/{player_id}", config.to_dict())
            if config.enabled:
                # dropping the convolution changes what the player should hear, so the
                # stream has to be rebuilt the same way a saved config change does it
                await self.mass.players.on_player_dsp_change(player_id)
            self.mass.signal_event(
                EventType.PLAYER_DSP_CONFIG_UPDATED,
                object_id=player_id,
                data=config,
            )
        raw_presets: dict[str, dict[str, Any]] = self.get(CONF_PLAYER_DSP_PRESETS, {})
        for preset_key, raw_preset in tuple(raw_presets.items()):
            preset = DSPConfigPreset.from_dict(raw_preset)
            if not _blank_convolution_ir(preset.config, ir_id):
                continue
            self.set(f"{CONF_PLAYER_DSP_PRESETS}/{preset_key}", preset.to_dict())

    def _dsp_irs_dir(self) -> str:
        """Return the directory holding convolution impulse response files."""
        return os.path.join(self.mass.storage_path, DSP_IRS_DIRNAME)

    def _dsp_ir_path(self, ir_id: str) -> str:
        """
        Return the on-disk path for an impulse response, rejecting unsafe ids.

        :param ir_id: The impulse response identifier to resolve.
        """
        ir_dir = self._dsp_irs_dir()
        ir_path = os.path.join(ir_dir, f"{ir_id}.wav")
        if not DSP_IR_ID_RE.match(ir_id) or not is_safe_path(ir_path, ir_dir):
            raise InvalidDataError(f"Invalid impulse response id: {ir_id!r}")
        return ir_path

    async def _remove_file(self, path: str) -> None:
        """Delete a file if it exists, ignoring a missing file."""
        with suppress(FileNotFoundError):
            await asyncio.to_thread(os.remove, path)


def _blank_convolution_ir(config: DSPConfig, ir_id: str) -> bool:
    """
    Blank any convolution filter in the config that references ir_id, in place.

    :param config: The DSP configuration to update.
    :param ir_id: The impulse response identifier to clear.
    :return: True if the config was changed.
    """
    cleared = False
    for dsp_filter in config.filters:
        if isinstance(dsp_filter, ConvolutionFilter) and dsp_filter.ir_id == ir_id:
            dsp_filter.ir_id = ""
            cleared = True
    return cleared
