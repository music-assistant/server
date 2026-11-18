"""Model/base for an Auth Provider implementation."""

from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING, final

from music_assistant_models.auth import SignInMethod

from .provider import Provider

if TYPE_CHECKING:
    from collections.abc import Mapping

    from music_assistant.controllers.webserver.helpers.login_flow import (
        ExternalIdentity,
        PendingLogin,
    )

DEFAULT_SIGN_IN_ICON = "mdi-login"


class AuthProvider(Provider):
    """
    Base representation of a provider that offers a way to sign in to Music Assistant.

    Account provisioning and user resolution arrive with the federated identity table.
    """

    @property
    def sign_in_method_id(self) -> str:
        """Return the id clients use to start a sign-in with this provider."""
        return self.instance_id

    @property
    def sign_in_label(self) -> str:
        """Return the label of the sign-in button."""
        return self.name

    @property
    def icon(self) -> str:
        """Return the icon of the sign-in button: an icon name or a preset slug."""
        return self.manifest.icon or DEFAULT_SIGN_IN_ICON

    @property
    def requires_redirect(self) -> bool:
        """Return whether signing in sends the browser to the identity provider."""
        return True

    @property
    def uses_pkce(self) -> bool:
        """Return whether a PKCE S256 code challenge is sent to the identity provider."""
        return True

    @property
    def uses_nonce(self) -> bool:
        """Return whether a nonce is sent to the identity provider and checked on return."""
        return True

    @property
    def sign_in_available(self) -> bool:
        """Return whether the sign-in method can be used right now."""
        return True

    @property
    def unavailable_reason(self) -> str | None:
        """Return the translation key that tells why signing in is not available right now."""
        return None

    @property
    def supports_remote_app(self) -> bool:
        """Return whether the sign-in method also works for the remote app."""
        return False

    @abstractmethod
    async def build_authorization_url(self, pending: PendingLogin) -> str:
        """
        Return the URL of the identity provider that the browser opens to sign in.

        :param pending: The sign-in that was started.
        """

    @abstractmethod
    async def complete_authorization(
        self, pending: PendingLogin, params: Mapping[str, str]
    ) -> ExternalIdentity:
        """
        Turn the parameters the browser returned with into a verified identity.

        :param pending: The sign-in the browser returned for.
        :param params: The query parameters of the callback request.
        :raises LoginFlowError: If the sign-in cannot be completed.
        """

    def logout_url(self) -> str | None:
        """Return the URL that signs the user out at the identity provider, if it has one."""
        return None

    @final
    def get_sign_in_method(self) -> SignInMethod:
        """Return the sign-in method the login page offers for this provider."""
        available = self.sign_in_available
        return SignInMethod(
            provider_id=self.sign_in_method_id,
            provider_type=self.domain,
            name=self.sign_in_label,
            icon=self.icon,
            requires_redirect=self.requires_redirect,
            available=available,
            unavailable_reason=None if available else self.unavailable_reason,
            supports_remote_app=self.supports_remote_app,
        )
