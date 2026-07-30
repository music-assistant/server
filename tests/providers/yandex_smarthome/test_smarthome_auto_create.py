"""Tests for provider/_smarthome_auto_create.py — URL derivations + helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from music_assistant.providers.yandex_smarthome._smarthome_auto_create import (
    SmartHomeUrls,
    derive_smart_home_urls,
    resolve_base_url,
)
from music_assistant.providers.yandex_smarthome.constants import (
    CONNECTION_TYPE_CLOUD,
    CONNECTION_TYPE_CLOUD_PLUS,
    CONNECTION_TYPE_DIRECT,
)


class TestResolveBaseUrl:
    """resolve_base_url picks override over mass.webserver.base_url."""

    def test_override_wins(self) -> None:
        """Explicit override is preferred over MA's global base_url."""
        mass = MagicMock()
        mass.webserver.base_url = "https://internal.example.local"
        assert resolve_base_url(mass, "https://public.example.com") == "https://public.example.com"

    def test_override_strips_trailing_slash(self) -> None:
        """Trailing slash + whitespace are stripped before use."""
        mass = MagicMock()
        mass.webserver.base_url = "https://x"
        assert resolve_base_url(mass, "https://ma.example.com/  ") == "https://ma.example.com"

    def test_no_override_uses_mass_base_url(self) -> None:
        """Empty override falls back to MA's webserver base URL."""
        mass = MagicMock()
        mass.webserver.base_url = "https://ma.example.com/"
        assert resolve_base_url(mass, None) == "https://ma.example.com"

    def test_empty_override_treated_as_none(self) -> None:
        """Whitespace-only override is treated as no override."""
        mass = MagicMock()
        mass.webserver.base_url = "https://ma.example.com"
        assert resolve_base_url(mass, "   ") == "https://ma.example.com"


class TestDeriveSmartHomeUrlsCloudPlus:
    """cloud_plus mode → all five values come from yaha-cloud constants."""

    def test_happy_path(self) -> None:
        """All five URLs are pre-computed from constants + instance_id."""
        urls = derive_smart_home_urls(
            connection_type=CONNECTION_TYPE_CLOUD_PLUS,
            base_url="https://ma.example.com",  # ignored for cloud_plus
            cloud_instance_id="inst-abc-123",
            direct_client_secret="ignored",
        )
        assert isinstance(urls, SmartHomeUrls)
        assert urls.backend_uri == "https://yaha-cloud.ru/api/yandex_smart_home"
        assert urls.oauth_authorize_url == "https://yaha-cloud.ru/oauth/authorize"
        assert urls.oauth_token_url == "https://yaha-cloud.ru/oauth/token"
        assert urls.oauth_client_id == "yandex_smart_home:inst-abc-123"
        assert urls.oauth_client_secret == "secret"  # literal yaha-cloud protocol value

    def test_missing_instance_id_raises(self) -> None:
        """cloud_plus without a registered instance is rejected."""
        with pytest.raises(ValueError, match="yaha-cloud instance"):
            derive_smart_home_urls(
                connection_type=CONNECTION_TYPE_CLOUD_PLUS,
                base_url="https://x",
                cloud_instance_id="",
                direct_client_secret="",
            )


class TestDeriveSmartHomeUrlsDirect:
    """direct mode → URLs computed from base_url + per-install secret."""

    def test_happy_path(self) -> None:
        """All five URLs match the direct-mode endpoint structure."""
        urls = derive_smart_home_urls(
            connection_type=CONNECTION_TYPE_DIRECT,
            base_url="https://ma.example.com",
            cloud_instance_id="ignored",
            direct_client_secret="abc-deadbeef",
        )
        assert urls.backend_uri == "https://ma.example.com/api/yandex_smarthome"
        assert (
            urls.oauth_authorize_url == "https://ma.example.com/api/yandex_smarthome/auth/authorize"
        )
        assert urls.oauth_token_url == "https://ma.example.com/api/yandex_smarthome/auth/token"
        assert urls.oauth_client_id == "https://social.yandex.net/"
        assert urls.oauth_client_secret == "abc-deadbeef"

    def test_missing_secret_raises(self) -> None:
        """Direct mode without per-install client secret is rejected."""
        with pytest.raises(ValueError, match="Client Secret"):
            derive_smart_home_urls(
                connection_type=CONNECTION_TYPE_DIRECT,
                base_url="https://x",
                cloud_instance_id="",
                direct_client_secret="",
            )

    def test_non_https_base_url_raises(self) -> None:
        """Yandex rejects non-HTTPS backends; we surface this client-side."""
        with pytest.raises(ValueError, match="HTTPS"):
            derive_smart_home_urls(
                connection_type=CONNECTION_TYPE_DIRECT,
                base_url="http://insecure.example.com",
                cloud_instance_id="",
                direct_client_secret="abc",
            )

    def test_empty_base_url_raises(self) -> None:
        """Empty base_url fails the HTTPS check."""
        with pytest.raises(ValueError, match="HTTPS"):
            derive_smart_home_urls(
                connection_type=CONNECTION_TYPE_DIRECT,
                base_url="",
                cloud_instance_id="",
                direct_client_secret="abc",
            )


class TestDeriveSmartHomeUrlsUnsupported:
    """Plain 'cloud' or any other connection_type is rejected."""

    def test_cloud_rejected(self) -> None:
        """Plain 'cloud' (public Yaha Cloud) doesn't go through auto-create."""
        with pytest.raises(ValueError, match="connection_type"):
            derive_smart_home_urls(
                connection_type=CONNECTION_TYPE_CLOUD,
                base_url="https://x",
                cloud_instance_id="i",
                direct_client_secret="",
            )

    def test_unknown_rejected(self) -> None:
        """Unrecognised connection_type produces a descriptive error."""
        with pytest.raises(ValueError, match="connection_type"):
            derive_smart_home_urls(
                connection_type="bogus",
                base_url="https://x",
                cloud_instance_id="i",
                direct_client_secret="",
            )
