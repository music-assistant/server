"""FullyKiosk Player implementation."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from aiohttp import ClientError, ClientSession, Fingerprint
from fullykiosk import FullyKiosk
from fullykiosk.exceptions import FullyKioskError
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    IdentifierType,
    PlaybackState,
    PlayerFeature,
)
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.constants import (
    CONF_ENTRY_OUTPUT_CODEC_DEFAULT_MP3,
    CONF_PASSWORD,
    CONF_SSL_FINGERPRINT,
    CONF_USE_SSL,
    CONF_VERIFY_SSL,
)
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia
from music_assistant.models.setup_flow import SetupFlowError

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

    from .provider import FullyKioskProvider

AUDIOMANAGER_STREAM_MUSIC = 3
DEFAULT_PORT = 2323


@dataclass
class _FingerprintSessionWrapper:
    """Proxy ClientSession that enforces a TLS fingerprint."""

    session: ClientSession
    fingerprint: Fingerprint

    def get(self, *args: Any, **kwargs: Any) -> Any:
        """Call the wrapped session.get while injecting the fingerprint."""
        kwargs.setdefault("ssl", self.fingerprint)
        return self.session.get(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        """Delegate attribute access to the wrapped session."""
        return getattr(self.session, name)


def _build_fingerprint(value: str) -> Fingerprint:
    """Parse a fingerprint string (sha256 hex) into an aiohttp Fingerprint."""
    normalized = re.sub(r"[^0-9a-fA-F]", "", value).lower()
    if not normalized:
        msg = "Empty fingerprint provided."
        raise ValueError(msg)
    if len(normalized) % 2 != 0:
        msg = "Fingerprint must contain an even number of hex characters."
        raise ValueError(msg)
    digest = bytes.fromhex(normalized)
    return Fingerprint(digest)


class FullyKioskPlayer(Player):
    """FullyKiosk Player implementation."""

    _attr_poll_interval = 10

    def __init__(
        self,
        provider: FullyKioskProvider,
        player_id: str,
        host: str,
        port: int = DEFAULT_PORT,
    ) -> None:
        """Initialize the FullyKiosk Player."""
        super().__init__(provider, player_id)
        self.host = host
        self.port = port
        self.fully_kiosk: FullyKiosk | None = None
        self._attr_needs_setup = True
        self._attr_setup_reason = "password_required"
        self._attr_available = False
        self._attr_name = f"Fully Kiosk ({host})"
        self._attr_supported_features = {PlayerFeature.PLAY_MEDIA, PlayerFeature.VOLUME_SET}

    @property
    def needs_poll(self) -> bool:
        """Poll only once a password has been configured."""
        return not self._attr_needs_setup

    @property
    def requires_flow_mode(self) -> bool:
        """Return if the player requires flow mode."""
        return True

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        return [
            ConfigEntry(
                key=CONF_USE_SSL,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_VERIFY_SSL,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_SSL_FINGERPRINT,
                type=ConfigEntryType.STRING,
                required=False,
                advanced=True,
            ),
            CONF_ENTRY_OUTPUT_CODEC_DEFAULT_MP3,
        ]

    async def on_config_updated(self) -> None:
        """Reconnect to the Fully Kiosk device when player configuration changes."""
        password = cast("str | None", self.get_setup_value(CONF_PASSWORD) or None)
        if not password:
            self.fully_kiosk = None
            self._attr_needs_setup = True
            self._attr_setup_reason = "password_required"
            self._attr_available = False
            self.update_state()
            return
        await self._connect()

    async def run_setup_flow(self, session: SetupSession) -> None:
        """Run the setup flow: collect the Fully Kiosk API password."""
        entries = [
            ConfigEntry(key=CONF_PASSWORD, type=ConfigEntryType.SECURE_STRING, required=True)
        ]
        errors: dict[str, str] | None = None
        while True:
            values = await session.form(entries, step_id="user", errors=errors, last_step=True)
            try:
                await session.finish({CONF_PASSWORD: str(values[CONF_PASSWORD])})
                return
            except SetupFlowError as err:
                errors = {"base": err.translation_key or str(err)}

    async def poll(self) -> None:
        """Poll player for state updates."""
        if self.fully_kiosk is None:
            await self._connect()
            return
        try:
            async with asyncio.timeout(15):
                await self.fully_kiosk.getDeviceInfo()
        except (TimeoutError, ClientError, FullyKioskError) as err:
            # do not log err: its repr may include the request URL with the password in the query string
            self.logger.warning(
                "Fully Kiosk device %s unavailable (%s) — will retry",
                self.host,
                type(err).__name__,
            )
            self._attr_available = False
            self.fully_kiosk = None
            self.update_state()
            return
        self._sync_state()
        self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        if self.fully_kiosk is None:
            return
        await self.fully_kiosk.setAudioVolume(volume_level, AUDIOMANAGER_STREAM_MUSIC)
        self._attr_volume_level = volume_level
        self.update_state()

    async def stop(self) -> None:
        """Send STOP command to given player."""
        if self.fully_kiosk is None:
            return
        await self.fully_kiosk.stopSound()
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_current_media = None
        self.update_state()

    async def play(self) -> None:
        """Handle PLAY command on the player."""
        raise PlayerCommandFailed("Playback can not be resumed.")

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on given player."""
        if self.fully_kiosk is None:
            raise PlayerCommandFailed("Fully Kiosk device is not connected.")
        url = await self.mass.streams.resolve_stream_url(self.player_id, media)
        await self.fully_kiosk.playSound(url, AUDIOMANAGER_STREAM_MUSIC)
        self._attr_current_media = media
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def _connect(self) -> None:
        """Establish a connection to the Fully Kiosk device."""
        password = cast("str | None", self.get_setup_value(CONF_PASSWORD) or None)
        if not password:
            self._attr_needs_setup = True
            self._attr_setup_reason = "password_required"
            self._attr_available = False
            self.fully_kiosk = None
            self.update_state()
            return

        use_ssl = bool(self.config.get_value(CONF_USE_SSL))
        fingerprint_value = self.config.get_value(CONF_SSL_FINGERPRINT)
        fingerprint_raw = fingerprint_value.strip() if isinstance(fingerprint_value, str) else ""
        if fingerprint_raw and not use_ssl:
            self.logger.warning(
                "Fully Kiosk %s: fingerprint validation requires HTTPS to be enabled",
                self.host,
            )
            self._attr_available = False
            self.update_state()
            return

        verify_ssl = bool(self.config.get_value(CONF_VERIFY_SSL)) if use_ssl else False
        http_session: ClientSession | _FingerprintSessionWrapper
        if use_ssl:
            if fingerprint_raw:
                try:
                    fingerprint = _build_fingerprint(fingerprint_raw)
                except ValueError as err:
                    self.logger.warning(
                        "Fully Kiosk %s: invalid TLS fingerprint configured: %s", self.host, err
                    )
                    self._attr_available = False
                    self.update_state()
                    return
                http_session = _FingerprintSessionWrapper(self.mass.http_session, fingerprint)
                verify_ssl = True
            else:
                http_session = (
                    self.mass.http_session if verify_ssl else self.mass.http_session_no_ssl
                )
        else:
            http_session = self.mass.http_session_no_ssl

        client = FullyKiosk(
            http_session,
            self.host,
            self.port,
            password,
            use_ssl=use_ssl,
            verify_ssl=verify_ssl,
        )
        try:
            async with asyncio.timeout(15):
                await client.getDeviceInfo()
        except (TimeoutError, ClientError, FullyKioskError) as err:
            # do not log err: its repr may include the request URL with the password in the query string
            self.logger.warning(
                "Failed to connect to Fully Kiosk at %s:%s (%s)",
                self.host,
                self.port,
                type(err).__name__,
            )
            self.fully_kiosk = None
            self._attr_available = False
            self.update_state()
            return

        self.fully_kiosk = client
        scheme = "https" if use_ssl else "http"
        address = f"{scheme}://{self.host}:{self.port}"
        self._attr_device_info = DeviceInfo(
            model=client.deviceInfo.get("deviceModel", ""),
            manufacturer=client.deviceInfo.get("deviceManufacturer", ""),
        )
        self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, address)
        self._attr_needs_setup = False
        self._attr_setup_reason = None
        self._attr_available = True
        self._sync_state()
        self.update_state()

    def _sync_state(self) -> None:
        """Refresh player attributes from the latest deviceInfo."""
        if self.fully_kiosk is None:
            return
        device_info = self.fully_kiosk.deviceInfo
        if name := device_info.get("deviceName"):
            self._attr_name = name
        for volume_dict in device_info.get("audioVolumes", []):
            if str(AUDIOMANAGER_STREAM_MUSIC) in volume_dict:
                self._attr_volume_level = volume_dict[str(AUDIOMANAGER_STREAM_MUSIC)]
                break
        if not device_info.get("soundUrlPlaying"):
            self._attr_playback_state = PlaybackState.IDLE
