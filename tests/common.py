"""Common test helpers for Music Assistant tests."""

import asyncio
import contextlib
import logging
import pathlib
from collections.abc import AsyncGenerator, Iterator
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import aiofiles.os
from music_assistant_models.enums import EventType, IdentifierType, PlayerFeature, PlayerType
from music_assistant_models.player import DeviceInfo

from music_assistant.controllers.config.providers import ProviderConfigMixin
from music_assistant.mass import MusicAssistant
from music_assistant.models.player import Player

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent


def _get_fixture_folder(provider: str | None = None) -> pathlib.Path:
    tests_base = pathlib.Path(__file__).parent
    if provider:
        return tests_base / "providers" / provider / "fixtures"
    return tests_base / "fixtures"


async def get_fixtures_dir(
    subdir: str, provider: str | None = None
) -> AsyncGenerator[tuple[str, bytes]]:
    """Yield the contents of every fixture in a fixtures folder."""
    dir_path = _get_fixture_folder(provider) / subdir
    for file in await aiofiles.os.listdir(dir_path):
        async with aiofiles.open(dir_path / file, "rb") as fp:
            yield (file, await fp.read())


@contextlib.asynccontextmanager
async def wait_for_sync_completion(mass: MusicAssistant) -> AsyncGenerator[None]:
    """Wait for a sync to finish."""
    flag = asyncio.Event()

    def _event(_event: MassEvent) -> None:
        flag.set()

    release_cb = mass.subscribe(_event, EventType.MUSIC_SYNC_COMPLETED)

    try:
        yield
    finally:
        try:
            if mass.music.active_sync_tasks:
                await flag.wait()
        finally:
            release_cb()


# builtin providers that must not be auto-set-up during a fixture boot: local_audio
# bridges the host machine's sound devices (built-in speakers, bluetooth, ...) as
# sendspin players, which would leak real hardware into the player registry
SUPPRESSED_BUILTIN_PROVIDERS = {"local_audio"}

_orig_create_builtin_provider_config = ProviderConfigMixin.create_builtin_provider_config


@contextlib.contextmanager
def suppress_auto_loaded_providers() -> Iterator[None]:
    """
    Stop a fixture boot from auto-setting-up providers that reach into the host.

    Keeps a booted test instance isolated from the developer's machine: the default
    device providers (airplay/chromecast/dlna/...) are not auto-configured, and neither
    is the builtin local_audio provider, which would otherwise bridge the host's sound
    devices (built-in speakers, bluetooth, ...) into the player registry.
    """
    with (
        patch("music_assistant.mass.DEFAULT_PROVIDERS", ()),
        patch.object(
            ProviderConfigMixin,
            "create_builtin_provider_config",
            _create_builtin_provider_config_hermetic,
        ),
    ):
        yield


async def _create_builtin_provider_config_hermetic(
    self: ProviderConfigMixin, provider_domain: str
) -> None:
    """Create builtin provider configs, skipping providers that discover host hardware."""
    if provider_domain in SUPPRESSED_BUILTIN_PROVIDERS:
        return
    await _orig_create_builtin_provider_config(self, provider_domain)


# Mock classes for testing


def create_mock_config(name: str) -> MagicMock:
    """Create a mock player config with the given name."""
    config = MagicMock()
    config.name = None  # No custom name, use default
    config.default_name = name
    config.get_value = MagicMock(return_value="none")  # Default to no power control
    return config


class MockProvider:
    """Mock player provider for testing."""

    def __init__(
        self, domain: str, instance_id: str = "test_instance", mass: MagicMock | None = None
    ) -> None:
        """Initialize the mock provider."""
        self.domain = domain
        self.instance_id = instance_id
        self.name = f"Mock {domain.title()}"
        self.manifest = MagicMock()
        self.manifest.name = f"Mock {domain} Provider"
        self.mass = mass or MagicMock()
        self.logger = logging.getLogger(f"test.{domain}")


class MockPlayer(Player):
    """Mock player for testing."""

    def __init__(
        self,
        provider: MockProvider,
        player_id: str,
        name: str,
        player_type: PlayerType = PlayerType.PLAYER,
        identifiers: dict[IdentifierType, str] | None = None,
    ) -> None:
        """Initialize the mock player."""
        # Set up the mock config before calling super().__init__
        # because the parent __init__ accesses config
        provider.mass.config.get_base_player_config.return_value = create_mock_config(name)

        super().__init__(provider, player_id)  # type: ignore[arg-type]
        self._attr_name = name
        # Set type as instance attribute (overrides class attribute)
        self._attr_type = player_type
        self._attr_available = True
        self._attr_powered = True
        self._attr_supported_features = {PlayerFeature.VOLUME_SET}
        self._attr_can_group_with = set()
        self._attr_group_members = []

        # Set up device info with identifiers
        self._attr_device_info = DeviceInfo(
            model="Test Model",
            manufacturer="Test Manufacturer",
        )
        if identifiers:
            for conn_type, value in identifiers.items():
                self._attr_device_info.add_identifier(conn_type, value)

        # Clear cached properties after modifying attributes
        self._cache.clear()

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Mock implementation of set_members."""
        current_members = set(self._attr_group_members)

        if player_ids_to_add:
            current_members.update(player_ids_to_add)

        if player_ids_to_remove:
            current_members.difference_update(player_ids_to_remove)

        # Always include self as first member if there are members
        if current_members:
            self._attr_group_members = [self.player_id] + [
                pid for pid in current_members if pid != self.player_id
            ]
        else:
            self._attr_group_members = []

        # Clear cache to reflect changes
        self._cache.clear()

    async def stop(self) -> None:
        """Stop playback - required abstract method."""


class MockMass:
    """Type hint for mocked MusicAssistant instance."""
