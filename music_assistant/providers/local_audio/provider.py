"""Local Audio Out player provider for Music Assistant."""

from __future__ import annotations

import ctypes
import sys

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import SetupFailedError

from music_assistant.models.player_provider import PlayerProvider

from .constants import (
    AUDIO_BACKEND_ALSA,
    AUDIO_BACKEND_AUTO,
    AUDIO_BACKEND_PULSEAUDIO,
    CONF_AUDIO_BACKEND,
    CONF_PREWARM_STREAMS,
)
from .sendspin_bridge import LocalAudioBridgeManager


class LocalAudioProvider(PlayerProvider):
    """Player provider that exposes locally attached soundcards as Sendspin players."""

    _bridge_manager: LocalAudioBridgeManager

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        entries: list[ConfigEntry] = []

        if sys.platform == "linux":
            configured_backend: str = str(
                self.config.get_value(CONF_AUDIO_BACKEND) or AUDIO_BACKEND_AUTO
            )
            entries.append(
                ConfigEntry(
                    key=CONF_AUDIO_BACKEND,
                    type=ConfigEntryType.STRING,
                    options=[
                        ConfigValueOption(AUDIO_BACKEND_AUTO),
                        ConfigValueOption(AUDIO_BACKEND_PULSEAUDIO),
                        ConfigValueOption(AUDIO_BACKEND_ALSA),
                    ],
                    default_value=AUDIO_BACKEND_AUTO,
                )
            )
            entries.append(
                ConfigEntry(
                    key=CONF_PREWARM_STREAMS,
                    type=ConfigEntryType.BOOLEAN,
                    default_value=True,
                )
            )
            if configured_backend != AUDIO_BACKEND_ALSA:
                entries.extend(await self._card_profile_entries())

        return tuple(entries)

    async def _card_profile_entries(self) -> list[ConfigEntry]:
        """
        Build one profile dropdown per PulseAudio card currently present.

        Generated live at settings-dialog time (get_config_entries is
        async), so the option list is exactly what PA offers right now —
        machine profile names as values, PA's human descriptions as
        titles. Cards offering no real choice (only one output profile)
        are skipped to keep the page clean; absent cards simply produce
        no entry, and any stored value for them sits harmlessly until
        they return. Returns no entries when card introspection is
        unavailable (no PA at runtime, initial setup flow, etc.) —
        profile selection then just isn't offered.
        """
        try:
            from .card_profiles import (  # noqa: PLC0415
                PROFILE_AUTO,
                PROFILE_OFF,
                card_config_label,
                conf_card_profile_key,
            )
            from .pa_simple import enumerate_pa_cards  # noqa: PLC0415
        except ImportError as err:
            self.logger.warning(
                "Card profile config entries unavailable — module out of date "
                "(deploy matching card_profiles.py/pa_simple.py): %s",
                err,
            )
            return []

        try:
            cards = await self.mass.loop.run_in_executor(None, enumerate_pa_cards)
        except (FileNotFoundError, RuntimeError, OSError) as err:
            self.logger.debug("Card profile config entries skipped: %s", err)
            return []

        entries: list[ConfigEntry] = []
        for card in cards:
            options = [
                ConfigValueOption(
                    PROFILE_AUTO, "Auto (most output channels, duplex preferred)"
                )
            ]
            for profile in card.profiles:
                if profile.name == PROFILE_OFF or profile.n_sinks <= 0:
                    continue
                title = profile.description
                if not profile.available:
                    title = f"{title} (currently unavailable)"
                options.append(ConfigValueOption(profile.name, title))
            if len(options) <= 2:
                # "auto" plus a single real profile is not a choice.
                continue
            entries.append(
                ConfigEntry(
                    key=conf_card_profile_key(card.name),
                    type=ConfigEntryType.STRING,
                    label=f"Sound card profile: {card_config_label(card)}",
                    description=(
                        "Which of this card's profiles to activate. Auto picks the "
                        "profile with the most output channels, preferring one that "
                        "also keeps the card's input available (for capture "
                        "providers). The profile decides which outputs of the card "
                        "exist at all — changing it briefly interrupts any active "
                        "streams on the card and rebuilds its players."
                    ),
                    options=options,
                    default_value=PROFILE_AUTO,
                    advanced=True,
                )
            )
        self.logger.debug("Generated %d card profile config entries", len(entries))
        return entries

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        if sys.platform == "linux":
            configured_backend: str = str(
                self.config.get_value(CONF_AUDIO_BACKEND) or AUDIO_BACKEND_AUTO
            )
            needs_pulse = configured_backend in (AUDIO_BACKEND_PULSEAUDIO, AUDIO_BACKEND_AUTO)
            if needs_pulse:
                # Verify libpulse-simple is present before attempting PA output.
                # On AUTO we probe but don't hard-fail — bridge manager will fall
                # back to ALSA if pactl returns no sinks at enumeration time.
                try:
                    ctypes.CDLL("libpulse-simple.so.0")
                except OSError:
                    if configured_backend == AUDIO_BACKEND_PULSEAUDIO:
                        raise SetupFailedError(
                            "libpulse-simple.so.0 not found — is PulseAudio installed?"
                        ) from None
                    # AUTO: libpulse absent, bridge manager will use ALSA

        self._bridge_manager = LocalAudioBridgeManager(self)

    async def discover_players(self) -> None:
        """Discover local audio output devices and register their players."""
        await self._bridge_manager.discover_and_register()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/removal of the provider."""
        if bridge_manager := getattr(self, "_bridge_manager", None):
            await bridge_manager.close()
