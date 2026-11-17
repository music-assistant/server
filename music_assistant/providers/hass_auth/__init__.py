"""Sign in to Music Assistant with a Home Assistant account."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Final, cast
from urllib.parse import urlparse

from hass_client import HomeAssistantClient
from hass_client.exceptions import BaseHassClientError
from hass_client.utils import base_url, get_auth_url, get_token, get_websocket_url
from music_assistant_models.auth import AuthProviderType

from music_assistant.controllers.webserver.helpers.auth_providers import get_ha_user_details
from music_assistant.controllers.webserver.helpers.login_flow import (
    ExternalIdentity,
    LoginFlowError,
)
from music_assistant.helpers.pkce import pkce_challenge
from music_assistant.helpers.util import is_public_url
from music_assistant.models.auth_provider import AuthProvider

if TYPE_CHECKING:
    from collections.abc import Mapping

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.controllers.webserver.helpers.login_flow import PendingLogin
    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant.providers.hass import HomeAssistantProvider

# the id clients start a Home Assistant sign-in with
SIGN_IN_METHOD_ID: Final = "homeassistant"
# the errors keep the generic sign-in failure the clients show for them
SIGN_IN_FAILED: Final = "authentication_failed"
# Seconds the URLs Home Assistant reports for itself are reused, and after a failed lookup
HA_NETWORK_URLS_TTL: Final = 300
HA_NETWORK_URLS_RETRY: Final = 30


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return HomeAssistantAuthProvider(mass, manifest, config)


class HomeAssistantAuthProvider(AuthProvider):
    """Sign in with the Home Assistant the hass plugin is connected to."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """
        Initialize the provider.

        :param mass: MusicAssistant instance.
        :param manifest: The manifest of the provider.
        :param config: The config of the provider instance.
        """
        super().__init__(mass, manifest, config)
        self._network_urls: dict[str, str | None] = {}
        self._network_urls_expires_at = 0.0
        self._network_urls_lock = asyncio.Lock()

    @property
    def sign_in_method_id(self) -> str:
        """Return the id clients use to start a sign-in with Home Assistant."""
        return SIGN_IN_METHOD_ID

    @property
    def uses_nonce(self) -> bool:
        """Return False: Home Assistant does not support a nonce."""
        return False

    @property
    def sign_in_available(self) -> bool:
        """Return whether the hass plugin is available and has a Home Assistant URL."""
        hass_provider = self._hass_provider
        return hass_provider is not None and hass_provider.available and bool(self._ha_url)

    @property
    def supports_remote_app(self) -> bool:
        """Return whether Home Assistant is reachable from outside the home network."""
        if time.monotonic() >= self._network_urls_expires_at:
            self.mass.create_task(
                self._get_network_urls(), task_id=f"{self.instance_id}_network_urls"
            )
        return self._public_ha_url(self._network_urls) is not None

    async def loaded_in_mass(self) -> None:
        """Look up the URLs of Home Assistant for the first sign-in listing."""
        await super().loaded_in_mass()
        self.mass.create_task(self._get_network_urls(), task_id=f"{self.instance_id}_network_urls")

    async def build_authorization_url(self, pending: PendingLogin) -> str:
        """
        Return the Home Assistant URL the browser opens to sign in.

        :param pending: The started sign-in.
        :raises LoginFlowError: If Home Assistant is not reachable from where the browser
            returns to.
        """
        if pending.redirect_target == "app":
            # the remote browser opens this URL, so it must reach Home Assistant from anywhere
            ha_url = await self._get_public_ha_url()
        else:
            ha_url = await self._get_server_target_ha_url()
        if not ha_url:
            raise LoginFlowError(SIGN_IN_FAILED, "Home Assistant is not reachable")
        # Use base_url of callback as client_id (same as HA provider does)
        return cast(
            "str",
            get_auth_url(
                ha_url,
                pending.redirect_uri,
                client_id=base_url(pending.redirect_uri),
                state=pending.state,
                code_challenge=pkce_challenge(pending.idp_code_verifier),
            ),
        )

    async def complete_authorization(
        self, pending: PendingLogin, params: Mapping[str, str]
    ) -> ExternalIdentity:
        """
        Exchange the Home Assistant code of a sign-in and return the account it signs in.

        :param pending: The started sign-in.
        :param params: The query parameters of the callback, with the authorization code.
        :raises LoginFlowError: If the sign-in can not be completed.
        """
        if not (code := params.get("code")):
            raise LoginFlowError(SIGN_IN_FAILED, "No authorization code received")
        ha_url = await self._get_external_ha_url()
        if not ha_url:
            raise LoginFlowError(SIGN_IN_FAILED, "Home Assistant URL not configured")

        try:
            # Use base_url of callback as client_id (same as HA provider does)
            client_id = base_url(pending.redirect_uri)

            # Use hass_client's get_token utility - no client_secret needed!
            try:
                token_details = await get_token(
                    ha_url, code, client_id=client_id, code_verifier=pending.idp_code_verifier
                )
            except Exception as token_error:
                self.logger.error(
                    "Failed to get token from HA: %s (client_id: %s, ha_url: %s)",
                    token_error,
                    client_id,
                    ha_url,
                )
                raise LoginFlowError(
                    SIGN_IN_FAILED, "Home Assistant did not accept the sign-in"
                ) from token_error

            access_token = token_details.get("access_token")
            if not access_token:
                raise LoginFlowError(SIGN_IN_FAILED, "No access token received from HA")  # noqa: TRY301 - passed through below

            # Get the HA user ID from the OAuth token via WebSocket
            ha_user_id = await self._fetch_ha_user_id_via_websocket(ha_url, access_token)
            if not ha_user_id:
                raise LoginFlowError(SIGN_IN_FAILED, "Failed to get user ID from Home Assistant")  # noqa: TRY301 - passed through below

            # Get username, display name and avatar from HA provider (has admin access)
            username, display_name, avatar_url = await get_ha_user_details(self.mass, ha_user_id)
        except LoginFlowError:
            raise
        except Exception as err:
            self.logger.exception("Error during Home Assistant OAuth callback")
            raise LoginFlowError(
                SIGN_IN_FAILED, "The Home Assistant sign-in could not be completed"
            ) from err

        # Fall back to HA user ID as username if not found
        if not username:
            self.logger.warning("Could not get username from HA, using user ID as fallback")
            username = ha_user_id
        return ExternalIdentity(
            provider_type=AuthProviderType.HOME_ASSISTANT,
            issuer=ha_url,
            subject=ha_user_id,
            username=username,
            display_name=display_name,
            avatar_url=avatar_url,
        )

    @property
    def _hass_provider(self) -> HomeAssistantProvider | None:
        """Return the hass plugin this provider signs in with, if it is loaded."""
        return cast("HomeAssistantProvider | None", self.mass.get_provider("hass"))

    @property
    def _ha_url(self) -> str | None:
        """Return the Home Assistant URL the hass provider is configured with."""
        hass_provider = self._hass_provider
        ha_url = hass_provider.url if hass_provider else None
        return ha_url.strip() if ha_url else None

    async def _get_server_target_ha_url(self) -> str | None:
        """Return the Home Assistant URL for a sign-in that returns to this server."""
        # Get the correct HA URL (external URL if running as add-on)
        ha_url = await self._get_external_ha_url()
        if not ha_url:
            return None

        # If HA URL is still the internal supervisor URL (no external_url in HA config),
        # infer from MA's base URL (the add-on runs on the HA host)
        if "supervisor" in ha_url.lower():
            # Extract scheme and host from the base URL to build external HA URL
            parsed = urlparse(self.mass.webserver.base_url)
            # HA typically runs on port 8123, but use default ports for HTTPS (443) or HTTP (80)
            if parsed.scheme == "https":
                # HTTPS - use default port 443 (no port in URL)
                inferred_ha_url = f"{parsed.scheme}://{parsed.hostname}"
            else:
                # HTTP - assume HA runs on default port 8123
                inferred_ha_url = f"{parsed.scheme}://{parsed.hostname}:8123"

            self.logger.debug(
                "HA external_url not configured, inferring from base URL: %s",
                inferred_ha_url,
            )
            ha_url = inferred_ha_url
        return ha_url

    async def _get_public_ha_url(self) -> str | None:
        """Return a Home Assistant URL that is reachable from the internet, if any."""
        return self._public_ha_url(await self._get_network_urls())

    def _public_ha_url(self, network_urls: dict[str, str | None]) -> str | None:
        """
        Return the first of the given Home Assistant URLs that is public, if any.

        :param network_urls: The URLs Home Assistant reports for itself.
        """
        candidates = (network_urls.get("external"), network_urls.get("cloud"), self._ha_url)
        return next((url for url in candidates if url and is_public_url(url)), None)

    async def _get_external_ha_url(self) -> str | None:
        """
        Get the external URL for Home Assistant from the config API.

        This is needed when MA runs as HA add-on and connects via internal docker network
        (http://supervisor/api) but needs the external URL for OAuth redirects.

        :return: External URL if available, otherwise None.
        """
        ha_url = self._ha_url
        if not ha_url:
            return None

        # Check if we're using the internal supervisor URL
        if "supervisor" not in ha_url.lower():
            # Not using internal URL, return as-is
            return ha_url

        # External goes before cloud, cloud before internal
        # External is the manually configured external URL
        # Cloud is the Nabu Casa cloud URL
        # Internal is the local network URL
        network_urls = await self._get_network_urls()
        if final_url := (
            network_urls.get("external")
            or network_urls.get("cloud")
            or network_urls.get("internal")
        ):
            self.logger.debug(
                "Using HA URL for OAuth: %s (from network/url, configured: %s)",
                final_url,
                ha_url,
            )
            return final_url

        # Fallback to configured URL
        return ha_url

    async def _get_network_urls(self) -> dict[str, str | None]:
        """Return the internal, external and cloud URLs Home Assistant reports for itself."""
        if time.monotonic() < self._network_urls_expires_at:
            return self._network_urls
        ha_provider = self._hass_provider
        if not ha_provider:
            return {}
        # the hass provider only creates its client during setup
        hass_client = getattr(ha_provider, "hass", None)
        if not hass_client or not hass_client.connected:
            return {}
        # auth/providers is public, so concurrent calls share one lookup
        async with self._network_urls_lock:
            if time.monotonic() < self._network_urls_expires_at:
                return self._network_urls
            try:
                # Get network URLs from Home Assistant using WebSocket API
                network_urls = await hass_client.send_command("network/url") or {}
            except Exception as err:
                self.logger.warning("Failed to fetch HA network URLs: %s", err, exc_info=True)
                self._network_urls = {}
                self._network_urls_expires_at = time.monotonic() + HA_NETWORK_URLS_RETRY
                return {}
            self._network_urls = {
                key: url.strip() if isinstance(url, str) and url.strip() else None
                for key, url in network_urls.items()
            }
            self._network_urls_expires_at = time.monotonic() + HA_NETWORK_URLS_TTL
            return self._network_urls

    async def _fetch_ha_user_id_via_websocket(self, ha_url: str, access_token: str) -> str | None:
        """
        Fetch the HA user ID from Home Assistant via WebSocket using OAuth token.

        :param ha_url: Home Assistant URL.
        :param access_token: Access token for WebSocket authentication.
        :return: The HA user ID or None if fetch fails.
        """
        ws_url = get_websocket_url(ha_url)

        try:
            # Use context manager to automatically handle connect/disconnect
            async with HomeAssistantClient(ws_url, access_token, self.mass.http_session) as client:
                # Use the auth/current_user command to get user ID
                result = await client.send_command("auth/current_user")
                if result and (user_id := result.get("id")):
                    return str(user_id)
                self.logger.warning("auth/current_user returned no user data or missing id")
                return None
        except BaseHassClientError as ws_error:
            self.logger.error("Failed to fetch HA user via WebSocket: %s", ws_error)
            return None
