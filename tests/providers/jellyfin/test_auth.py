"""Tests for Jellyfin authentication module."""

from unittest import mock

import aiohttp
import pytest
from aiojellyfin.testing import FixtureBuilder
from music_assistant_models.errors import LoginFailed

from music_assistant.providers.jellyfin.auth import (
    _create_session_config,
    _generate_device_id,
    authenticate,
)


class TestGenerateDeviceId:
    """Tests for device ID generation."""

    def test_stable_device_id(self) -> None:
        """Test that device ID is stable for same inputs."""
        server_id = "test-server-123"
        username = "testuser"

        device_id_1 = _generate_device_id(server_id, username)
        device_id_2 = _generate_device_id(server_id, username)

        assert device_id_1 == device_id_2

    def test_different_device_ids_for_different_inputs(self) -> None:
        """Test that different inputs produce different device IDs."""
        server_id = "test-server-123"

        device_id_1 = _generate_device_id(server_id, "user1")
        device_id_2 = _generate_device_id(server_id, "user2")
        device_id_3 = _generate_device_id("server-456", "user1")

        assert device_id_1 != device_id_2
        assert device_id_1 != device_id_3

    def test_device_id_format(self) -> None:
        """Test that device ID is a valid hex string."""
        device_id = _generate_device_id("server", "user")

        assert isinstance(device_id, str)
        assert len(device_id) == 64  # SHA256 hex digest length
        assert all(c in "0123456789abcdef" for c in device_id)


class TestCreateSessionConfig:
    """Tests for session configuration creation."""

    @pytest.mark.asyncio
    async def test_creates_valid_session_config(self) -> None:
        """Test that a valid session configuration is created."""
        async with aiohttp.ClientSession() as session:
            config = _create_session_config(
                device_id="test-device-id",
                url="http://localhost:8096",
                verify_ssl=True,
                http_session=session,
                app_version="1.0.0",
            )

            assert config.url == "http://localhost:8096"
            assert config.verify_ssl is True
            assert config.device_id == "test-device-id"
            assert config.app_name == "Music Assistant"
            assert config.app_version == "1.0.0"
            assert config.session == session

    @pytest.mark.asyncio
    async def test_session_config_with_ssl_disabled(self) -> None:
        """Test session config with SSL verification disabled."""
        async with aiohttp.ClientSession() as session:
            config = _create_session_config(
                device_id="test-device-id",
                url="http://localhost:8096",
                verify_ssl=True,
                http_session=session,
                app_version="1.0.0",
            )

            assert config.verify_ssl is True


class TestAuthenticate:
    """Tests for the authenticate function."""

    @pytest.mark.asyncio
    async def test_successful_authentication(self) -> None:
        """Test successful authentication with Jellyfin server."""
        # Create a mock fixture that simulates Jellyfin
        fixture_builder = FixtureBuilder()
        authenticate_by_name_mock = fixture_builder.to_authenticate_by_name()

        mock_logger = mock.MagicMock()

        with mock.patch(
            "music_assistant.providers.jellyfin.auth.authenticate_by_name",
            authenticate_by_name_mock,
        ):
            async with aiohttp.ClientSession() as session:
                client = await authenticate(
                    server_id="test-server",
                    username="testuser",
                    password="testpass",
                    url="http://localhost:8096",
                    verify_ssl=True,
                    http_session=session,
                    app_version="1.0.0",
                    logger=mock_logger,
                )

                assert client is not None
                mock_logger.debug.assert_called_with(
                    "Successfully authenticated with Jellyfin server"
                )

    @pytest.mark.asyncio
    async def test_authentication_failure(self) -> None:
        """Test authentication failure handling."""
        mock_logger = mock.MagicMock()

        # Mock authenticate_by_name to raise an exception
        with mock.patch(
            "music_assistant.providers.jellyfin.auth.authenticate_by_name",
            side_effect=RuntimeError("Invalid credentials"),
        ):
            async with aiohttp.ClientSession() as session:
                with pytest.raises(LoginFailed, match="Authentication failed"):
                    await authenticate(
                        server_id="test-server",
                        username="testuser",
                        password="wrongpass",
                        url="http://localhost:8096",
                        verify_ssl=True,
                        http_session=session,
                        app_version="1.0.0",
                        logger=mock_logger,
                    )

                mock_logger.error.assert_called_once()
                assert "authentication failed" in mock_logger.error.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_authentication_with_different_urls(self) -> None:
        """Test authentication with different server URLs."""
        fixture_builder = FixtureBuilder()
        authenticate_by_name_mock = fixture_builder.to_authenticate_by_name()

        mock_logger = mock.MagicMock()

        urls = [
            "http://localhost:8096",
            "https://jellyfin.example.com",
            "http://192.168.1.100:8096",
        ]

        for url in urls:
            with mock.patch(
                "music_assistant.providers.jellyfin.auth.authenticate_by_name",
                authenticate_by_name_mock,
            ):
                async with aiohttp.ClientSession() as session:
                    client = await authenticate(
                        server_id="test-server",
                        username="testuser",
                        password="testpass",
                        url=url,
                        verify_ssl=True,
                        http_session=session,
                        app_version="1.0.0",
                        logger=mock_logger,
                    )

                    assert client is not None
