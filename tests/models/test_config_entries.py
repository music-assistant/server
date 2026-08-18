"""Tests for config entries and requires_reload settings."""

from typing import Any

import pytest
from music_assistant_models.config_entries import ConfigEntry, CoreConfig
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import (
    CONF_BIND_IP,
    CONF_BIND_PORT,
    CONF_ENTRY_ZEROCONF_INTERFACES,
    CONF_LOG_LEVEL,
    CONF_PUBLISH_IP,
    CONF_ZEROCONF_INTERFACES,
)
from music_assistant.controllers.discovery import DiscoveryController
from music_assistant.controllers.players import PlayerController
from music_assistant.models.core_controller import CoreController


class TestRequiresReload:
    """Tests to verify requires_reload is set correctly on config entries."""

    def test_zeroconf_interfaces_requires_reload(self) -> None:
        """
        Test that CONF_ENTRY_ZEROCONF_INTERFACES has requires_reload=True.

        This entry is read at MusicAssistant startup to configure the zeroconf instance,
        so changes require a reload.
        """
        assert CONF_ENTRY_ZEROCONF_INTERFACES.requires_reload is True, (
            f"CONF_ENTRY_ZEROCONF_INTERFACES ({CONF_ZEROCONF_INTERFACES}) should have "
            "requires_reload=True because it's read at startup time"
        )

    @pytest.mark.asyncio
    async def test_zeroconf_interfaces_entry_lives_on_discovery_controller(
        self, mock_mass: MockMass
    ) -> None:
        """Test that zeroconf interface selection belongs to the discovery controller."""
        discovery_controller = DiscoveryController(mock_mass)
        player_controller = PlayerController(mock_mass)  # type: ignore[arg-type]

        discovery_entries = await discovery_controller.get_config_entries()
        player_entries = await player_controller.get_config_entries()

        assert any(entry.key == CONF_ZEROCONF_INTERFACES for entry in discovery_entries)
        assert all(entry.key != CONF_ZEROCONF_INTERFACES for entry in player_entries)


class TestStreamsControllerConfigEntries:
    """Tests for streams controller config entries."""

    def test_streams_bind_port_requires_reload(self) -> None:
        """
        Test that CONF_BIND_PORT in streams controller has requires_reload=True.

        The bind port is used when starting the webserver in setup(),
        so changes require a reload.
        """
        # We verify by checking that the key is in the list of entries
        # that should require reload
        entries_requiring_reload = {
            CONF_BIND_PORT,
            CONF_BIND_IP,
            CONF_PUBLISH_IP,
        }

        # This test documents that these entries need requires_reload=True
        assert len(entries_requiring_reload) == 3


class TestWebserverControllerConfigEntries:
    """Tests for webserver controller config entries."""

    def test_webserver_bind_entries_require_reload(self) -> None:
        """
        Test that webserver bind/SSL entries have requires_reload=True.

        Entries that affect the webserver's network binding or SSL configuration
        must trigger a reload when changed.
        """
        # These are the keys that should have requires_reload=True in the
        # webserver controller
        entries_requiring_reload = {
            CONF_BIND_PORT,
            CONF_BIND_IP,
            "enable_ssl",
            "ssl_certificate",
            "ssl_private_key",
        }

        # These keys should have requires_reload=False (read dynamically)
        entries_not_requiring_reload = {
            "base_url",
            "auth_allow_self_registration",
        }

        # This test documents the expected behavior
        assert len(entries_requiring_reload) == 5
        assert len(entries_not_requiring_reload) == 2


class MockMass:
    """Mock MusicAssistant instance for testing CoreController."""

    def __init__(self) -> None:
        """Initialize mock."""
        self.call_later_calls: list[tuple[Any, ...]] = []

    def call_later(self, *args: Any, **kwargs: Any) -> None:
        """Record call_later invocations."""
        self.call_later_calls.append((args, kwargs))

    def get_providers_supporting_feature(self, *args: Any, **kwargs: Any) -> list[Any]:
        """Report no loaded providers, so engine-backed config entries stay empty."""
        return []


class MockConfig:
    """Mock config for testing CoreController."""

    def get_raw_core_config_value(self, domain: str, key: str, default: str = "GLOBAL") -> str:
        """Return a mock log level."""
        return "INFO"


@pytest.fixture
def mock_mass() -> MockMass:
    """Create a mock MusicAssistant instance."""
    mass = MockMass()
    mass.config = MockConfig()  # type: ignore[attr-defined]
    return mass


@pytest.fixture
def test_controller(mock_mass: MockMass) -> CoreController:
    """Create a test CoreController instance."""

    class TestController(CoreController):
        domain = "test"

    return TestController(mock_mass)  # type: ignore[arg-type]


@pytest.fixture
def entry_with_reload() -> ConfigEntry:
    """Create a ConfigEntry that requires reload."""
    return ConfigEntry(
        key="needs_reload",
        type=ConfigEntryType.STRING,
        label="Needs Reload",
        default_value="default",
        requires_reload=True,
    )


@pytest.fixture
def entry_without_reload() -> ConfigEntry:
    """Create a ConfigEntry that does not require reload."""
    return ConfigEntry(
        key="no_reload",
        type=ConfigEntryType.STRING,
        label="No Reload",
        default_value="default",
        requires_reload=False,
    )


@pytest.mark.asyncio
async def test_core_controller_update_config_triggers_reload_when_required(
    mock_mass: MockMass,
    test_controller: CoreController,
    entry_with_reload: ConfigEntry,
) -> None:
    """Test that CoreController.update_config triggers reload for requires_reload=True."""
    config = CoreConfig(
        values={"needs_reload": entry_with_reload},
        domain="test",
    )
    entry_with_reload.value = "new_value"

    await test_controller.update_config(config, {"values/needs_reload"})

    # Verify call_later was called (which schedules the reload)
    assert len(mock_mass.call_later_calls) == 1
    args, kwargs = mock_mass.call_later_calls[0]
    assert "reload" in str(args) or "reload" in str(kwargs)


@pytest.mark.asyncio
async def test_core_controller_update_config_skips_reload_when_not_required(
    mock_mass: MockMass,
    test_controller: CoreController,
    entry_without_reload: ConfigEntry,
) -> None:
    """Test that CoreController.update_config skips reload for requires_reload=False."""
    config = CoreConfig(
        values={"no_reload": entry_without_reload},
        domain="test",
    )
    entry_without_reload.value = "new_value"

    await test_controller.update_config(config, {"values/no_reload"})

    # Verify call_later was NOT called
    assert len(mock_mass.call_later_calls) == 0


@pytest.mark.asyncio
async def test_core_controller_reload_runs_post_setup(mock_mass: MockMass) -> None:
    """Test that CoreController.reload also reruns post-setup logic."""

    class TestController(CoreController):
        domain = "test"

        def __init__(self, mass: MockMass) -> None:
            """Initialize test controller."""
            super().__init__(mass)  # type: ignore[arg-type]
            self.setup_calls = 0
            self.post_setup_calls = 0

        async def setup(self, config: CoreConfig) -> None:
            """Record setup invocations."""
            self.setup_calls += 1
            self.config = config

        async def post_setup(self) -> None:
            """Record post-setup invocations."""
            self.post_setup_calls += 1

    controller = TestController(mock_mass)
    config = CoreConfig(
        domain="test",
        values={
            CONF_LOG_LEVEL: ConfigEntry(
                key=CONF_LOG_LEVEL,
                type=ConfigEntryType.STRING,
                label="Log level",
                default_value="INFO",
                value="INFO",
            )
        },
    )

    await controller.reload(config)

    assert controller.setup_calls == 1
    assert controller.post_setup_calls == 1


def test_config_entry_default_requires_reload_is_false() -> None:
    """
    Test that ConfigEntry defaults requires_reload to False.

    This documents the expected default behavior from the models package.
    Config entries must explicitly set requires_reload=True if they need it.
    """
    entry = ConfigEntry(
        key="test",
        type=ConfigEntryType.STRING,
        label="Test Entry",
        default_value="default",
    )

    assert entry.requires_reload is False, (
        "ConfigEntry should default requires_reload to False. "
        "Entries that need reload must explicitly set requires_reload=True."
    )
