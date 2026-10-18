"""Read deployment-managed OIDC settings without copying secrets into server storage."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from music_assistant_models.auth import UserRole
from yarl import URL

from music_assistant.helpers.json import json_dumps, json_loads

from .oidc import DEFAULT_OIDC_SCOPES, TOKEN_AUTH_METHODS, validate_oidc_scopes, validate_oidc_url
from .oidc_provider import OIDCProviderConfig, parse_role_mapping

OIDC_CONFIG_FILE_ENV: Final = "MASS_OIDC_CONFIG_FILE"
MAX_CONFIG_BYTES: Final = 65536


@dataclass(frozen=True)
class OIDCDeploymentConfig:
    """OIDC settings owned by the deployment rather than the settings UI."""

    base_url: str
    provider: OIDCProviderConfig
    bootstrap_admin: bool


async def load_oidc_deployment() -> OIDCDeploymentConfig | None:
    """Load an explicitly configured file, rejecting invalid or unreadable configuration."""
    path = os.environ.get(OIDC_CONFIG_FILE_ENV)
    if path is None:
        return None
    if not path.strip():
        raise ValueError(f"{OIDC_CONFIG_FILE_ENV} must name a configuration file")
    return await asyncio.to_thread(_read_config, path)


def _read_config(path: str) -> OIDCDeploymentConfig:
    """Read and validate deployment settings without logging their contents."""
    try:
        with Path(path).open("rb") as file:
            raw = file.read(MAX_CONFIG_BYTES + 1)
    except OSError:
        raise ValueError(f"Cannot read {OIDC_CONFIG_FILE_ENV}") from None
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("OIDC deployment configuration exceeds size limit")
    try:
        data = json_loads(raw)
    except ValueError:
        # Parser errors can include credential values from the input.
        raise ValueError(f"Cannot read a valid JSON object from {OIDC_CONFIG_FILE_ENV}") from None
    allowed = {
        "base_url",
        "issuer",
        "client_id",
        "client_secret",
        "token_auth_method",
        "name",
        "allow_self_registration",
        "backchannel_logout",
        "groups_claim",
        "scopes",
        "role_mapping",
        "bootstrap_admin",
    }
    if not isinstance(data, dict) or data.keys() - allowed:
        raise ValueError("OIDC deployment configuration contains unknown settings")
    for key in ("base_url", "issuer", "client_id"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"OIDC deployment configuration requires {key}")
    for key in ("client_secret", "token_auth_method", "name", "groups_claim", "scopes"):
        if key in data and not isinstance(data[key], str):
            raise ValueError(f"OIDC deployment setting {key} must be a string")
    for key in ("bootstrap_admin", "allow_self_registration", "backchannel_logout"):
        if key in data and not isinstance(data[key], bool):
            raise ValueError(f"OIDC deployment setting {key} must be a boolean")
    base_url = data["base_url"].strip()
    if not validate_oidc_url(base_url, issuer=True) or URL(base_url).path not in ("", "/"):
        raise ValueError("OIDC deployment base_url must be an HTTPS origin")
    issuer = data["issuer"].strip()
    if not validate_oidc_url(issuer, issuer=True):
        raise ValueError("OIDC deployment issuer must use HTTPS")
    method = data.get("token_auth_method", "client_secret_basic")
    secret = data.get("client_secret", "")
    if method not in TOKEN_AUTH_METHODS or (method != "none" and not secret):
        raise ValueError("OIDC deployment client authentication is incomplete")
    groups_claim = data.get("groups_claim", "groups")
    scopes = data.get("scopes", DEFAULT_OIDC_SCOPES)
    if not validate_oidc_scopes(scopes):
        raise ValueError("OIDC deployment scopes are invalid")
    name = data.get("name", "OpenID Connect")
    if not 0 < len(groups_claim) <= 255 or not 0 < len(name.strip()) <= 255:
        raise ValueError("OIDC deployment name or groups claim is invalid")
    mapping = parse_role_mapping(json_dumps(data.get("role_mapping", {})))
    bootstrap = data.get("bootstrap_admin", False)
    registration = data.get("allow_self_registration", False)
    if bootstrap and (not registration or UserRole.ADMIN not in mapping.values()):
        raise ValueError("OIDC bootstrap requires self-registration and an explicit admin mapping")
    return OIDCDeploymentConfig(
        base_url=base_url.rstrip("/"),
        bootstrap_admin=bootstrap,
        provider=OIDCProviderConfig(
            issuer=issuer,
            client_id=data["client_id"].strip(),
            client_secret=secret,
            token_auth_method=method,
            name=name.strip(),
            allow_self_registration=registration,
            backchannel_logout=data.get("backchannel_logout", False),
            groups_claim=groups_claim,
            scopes=scopes,
            role_mapping=mapping,
        ),
    )
