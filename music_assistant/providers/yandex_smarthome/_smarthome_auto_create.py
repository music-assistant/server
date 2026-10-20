"""
Smart-home auto-create-skill helpers — URL derivation + preconditions.

Equivalent of the deleted ``derive_backend_uri`` / ``derive_auth_urls`` /
``derive_client_id`` / ``_resolve_base_url`` / ``check_preconditions``
that lived inside the old ``provider/auto_skill.py``. Narrower scope:
smart-home only — drops the ``skill_type="dialog"`` branch (that's alice's
concern, lives in trudenboy/ma-provider-yandex-alice).

These derivations live in the provider, not in the lib, because
``connection_type`` and the cloud-relay/direct-mode protocols are
Music-Assistant-specific concepts. ``ya-dialogs-api`` accepts the
already-computed URLs as plain string parameters.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .constants import (
    CLOUD_OAUTH_AUTHORIZE_URL,
    CLOUD_OAUTH_TOKEN_URL,
    CLOUD_SKILL_CLIENT_ID_TEMPLATE,
    CLOUD_SKILL_CLIENT_SECRET,
    CLOUD_SKILL_WEBHOOK_TEMPLATE,
    CONNECTION_TYPE_CLOUD_PLUS,
    CONNECTION_TYPE_DIRECT,
    DIRECT_AUTH_BASE_PATH,
    DIRECT_BACKEND_URI_PATH,
    DIRECT_OAUTH_CLIENT_ID,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


@dataclass(frozen=True, slots=True)
class SmartHomeUrls:
    """
    The five pre-computed values ya_dialogs_api.auto_create_skill needs.

    Pass these straight into ``auto_create_skill`` — they correspond to its
    ``backend_uri`` / ``oauth_authorize_url`` / ``oauth_token_url`` /
    ``oauth_client_id`` / ``oauth_client_secret`` keyword arguments.
    """

    backend_uri: str
    oauth_authorize_url: str
    oauth_token_url: str
    oauth_client_id: str
    oauth_client_secret: str


def resolve_base_url(mass: MusicAssistant, override: str | None) -> str:
    """
    Pick override over ``mass.webserver.base_url``; strip trailing slashes.

    ``override`` is the user-set ``CONF_EXTERNAL_BASE_URL``. Lets users keep
    MA's global Base URL pointing at the local address (so HA Ingress / local
    UI keep working) while exposing a public HTTPS URL only to Yandex via a
    reverse proxy.
    """
    if override and override.strip():
        return override.strip().rstrip("/")
    fallback = ""
    with contextlib.suppress(Exception):
        fallback = str(mass.webserver.base_url)
    return fallback.strip().rstrip("/")


def derive_smart_home_urls(
    *,
    connection_type: str,
    base_url: str,
    cloud_instance_id: str,
    direct_client_secret: str,
) -> SmartHomeUrls:
    """
    Compute the five pre-computed values for ``auto_create_skill``.

    cloud_plus
        Backend uses :data:`CLOUD_SKILL_WEBHOOK_TEMPLATE` (the yaha-cloud
        relay). OAuth uses yaha-cloud's own authorize/token endpoints.
        ``client_id`` is ``yandex_smart_home:{instance_id}``;
        ``client_secret`` is the literal ``"secret"`` required by the
        relay protocol.

    direct
        Backend = ``base_url + DIRECT_BACKEND_URI_PATH``. OAuth uses MA's
        own ``/api/yandex_smarthome/auth/{authorize,token}`` endpoints
        served by ``provider/direct.py``. ``client_id`` is the social
        redirect base (Yandex's existing OAuth client expects this exact
        value); ``client_secret`` is the per-install random UUID minted on
        first save.

    :raises ValueError: ``cloud_plus`` without a registered cloud
        instance id; ``direct`` without a client secret or with a
        non-HTTPS base URL; any other ``connection_type``.
    """
    if connection_type == CONNECTION_TYPE_CLOUD_PLUS:
        if not cloud_instance_id:
            msg = (
                "Cloud Plus requires a registered yaha-cloud instance first. "
                "Use the 'Register with cloud' action."
            )
            raise ValueError(msg)
        return SmartHomeUrls(
            backend_uri=CLOUD_SKILL_WEBHOOK_TEMPLATE,
            oauth_authorize_url=CLOUD_OAUTH_AUTHORIZE_URL,
            oauth_token_url=CLOUD_OAUTH_TOKEN_URL,
            oauth_client_id=CLOUD_SKILL_CLIENT_ID_TEMPLATE.format(instance_id=cloud_instance_id),
            oauth_client_secret=CLOUD_SKILL_CLIENT_SECRET,
        )
    if connection_type == CONNECTION_TYPE_DIRECT:
        if not direct_client_secret:
            msg = "Direct mode requires a generated Client Secret"
            raise ValueError(msg)
        if not base_url.startswith("https://"):
            msg = (
                "Direct mode requires MA to be reachable over HTTPS from "
                f"the public internet (got base_url={base_url!r}). Yandex "
                "rejects skills with non-HTTPS backends."
            )
            raise ValueError(msg)
        return SmartHomeUrls(
            backend_uri=f"{base_url}{DIRECT_BACKEND_URI_PATH}",
            oauth_authorize_url=f"{base_url}{DIRECT_AUTH_BASE_PATH}/authorize",
            oauth_token_url=f"{base_url}{DIRECT_AUTH_BASE_PATH}/token",
            oauth_client_id=DIRECT_OAUTH_CLIENT_ID,
            oauth_client_secret=direct_client_secret,
        )
    msg = (
        f"auto-create is not supported for connection_type={connection_type!r}; "
        "use cloud_plus or direct."
    )
    raise ValueError(msg)
