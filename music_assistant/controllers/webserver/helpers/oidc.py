"""OpenID Connect authorization code client using the shared aiohttp session and PyJWT."""

from __future__ import annotations

import asyncio
import base64
import hmac
import math
import re
import time
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote_plus

import jwt
from aiohttp import ClientTimeout, encode_basic_auth
from yarl import URL

from music_assistant.helpers.json import json_loads
from music_assistant.helpers.pkce import pkce_challenge

if TYPE_CHECKING:
    from aiohttp import ClientSession

    from .login_flow import PendingLogin

CACHE_TTL: Final = 3600
KEY_REFRESH_INTERVAL: Final = 30
MAX_RESPONSE_SIZE: Final = 1024 * 1024
CLOCK_LEEWAY: Final = 30
LOGOUT_MAX_AGE: Final = 300
BACKCHANNEL_LOGOUT_EVENT: Final = "http://schemas.openid.net/event/backchannel-logout"
# Discovery cannot enable symmetric or unsigned tokens.
SIGNING_ALGORITHMS: Final = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"}
)
TOKEN_AUTH_METHODS: Final = ("client_secret_basic", "client_secret_post", "none")
# RFC 6749 Appendix A.11 allows visible ASCII, including spaces and base64 padding.
OIDC_AUTH_CODE_RE: Final = re.compile(r"[\x20-\x7e]{1,4096}")


def validate_oidc_url(value: str, *, issuer: bool = False) -> bool:
    """Return whether a value is an HTTPS OIDC endpoint, or an issuer without a query."""
    try:
        url = URL(value)
        return bool(
            url.scheme == "https"
            and url.host
            and url.user is None
            and url.password is None
            and not url.fragment
            and (not issuer or not url.query_string)
        )
    except ValueError:
        return False


class OIDCClient:
    """A discovery-based OIDC code client with PKCE and signed ID token validation."""

    def __init__(
        self,
        session: ClientSession,
        issuer: str,
        client_id: str,
        client_secret: str,
        client_auth_method: str = "client_secret_basic",
    ) -> None:
        """
        Initialize a client without contacting the identity provider.

        :param session: Music Assistant's shared, TLS-verifying HTTP session.
        :param issuer: Exact issuer identifier, including any trailing slash.
        :param client_id: Registered OIDC client identifier.
        :param client_secret: Client secret, empty only for a public client.
        :param client_auth_method: Registered token endpoint authentication method.
        """
        if not validate_oidc_url(issuer, issuer=True) or not client_id:
            raise ValueError("An HTTPS issuer and client ID are required")
        if client_auth_method not in TOKEN_AUTH_METHODS:
            raise ValueError("Unsupported OIDC token endpoint authentication method")
        if client_auth_method != "none" and not client_secret:
            raise ValueError("A client secret is required for a confidential client")
        self.session = session
        self.issuer = issuer
        self.client_id = client_id
        self._client_secret = client_secret
        self._client_auth_method = client_auth_method
        self._metadata: dict[str, Any] | None = None
        self._metadata_expires_at = 0.0
        self._metadata_lock = asyncio.Lock()
        self._jwks: dict[str, Any] | None = None
        self._jwks_expires_at = 0.0
        self._jwks_lock = asyncio.Lock()
        self._last_forced_refresh = float("-inf")

    async def authorization_url(self, pending: PendingLogin) -> str:
        """Build the authorization URL for a pending sign-in."""
        metadata = await self._get_metadata()
        return str(
            URL(metadata["authorization_endpoint"]).update_query(
                response_type="code",
                client_id=self.client_id,
                redirect_uri=pending.redirect_uri,
                scope="openid profile",
                state=pending.state,
                nonce=pending.idp_nonce,
                code_challenge=pkce_challenge(pending.idp_code_verifier),
                code_challenge_method="S256",
            )
        )

    async def authenticate(
        self, pending: PendingLogin, code: str, *, groups_claim: str | None = None
    ) -> dict[str, Any]:
        """Exchange a code for verified identity and profile claims."""
        metadata = await self._get_metadata()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": pending.redirect_uri,
            "code_verifier": pending.idp_code_verifier,
        }
        auth = None
        if self._client_auth_method == "client_secret_basic":
            # OAuth Basic credentials are form encoded before the HTTP Basic encoding.
            auth = encode_basic_auth(
                quote_plus(self.client_id, safe=""), quote_plus(self._client_secret, safe="")
            )
        else:
            data["client_id"] = self.client_id
            if self._client_auth_method == "client_secret_post":
                data["client_secret"] = self._client_secret
        tokens = await self._request_json("POST", metadata["token_endpoint"], data=data, auth=auth)
        id_token = tokens.get("id_token")
        access_token = tokens.get("access_token")
        if (
            not isinstance(id_token, str)
            or not isinstance(access_token, str)
            or not access_token
            or str(tokens.get("token_type", "")).lower() != "bearer"
            or "error" in tokens
        ):
            raise ValueError("Invalid OIDC token response")
        claims = await self._validate_id_token(metadata, id_token, pending, access_token, code)
        if metadata.get("userinfo_endpoint") and (
            not claims.get("preferred_username") or (groups_claim and groups_claim not in claims)
        ):
            userinfo = await self._request_json(
                "GET", metadata["userinfo_endpoint"], bearer_token=access_token
            )
            if userinfo.get("sub") != claims["sub"]:
                raise ValueError("OIDC UserInfo subject does not match the ID token")
            for key in ("preferred_username", "name", "picture"):
                if isinstance(userinfo.get(key), str):
                    claims[key] = userinfo[key]
            if groups_claim and groups_claim not in claims and groups_claim in userinfo:
                claims[groups_claim] = userinfo[groups_claim]
        return claims

    async def validate_logout_token(self, token: str) -> dict[str, Any]:
        """Verify a signed back-channel logout event."""
        metadata = await self._get_metadata()
        claims = await self._decode_signed_token(
            metadata, token, ["iss", "aud", "iat", "exp", "jti", "events"]
        )
        header_type = jwt.get_unverified_header(token).get("typ")
        if header_type not in (None, "JWT", "logout+jwt"):
            raise ValueError("Invalid OIDC logout token type")
        events = claims["events"]
        if (
            not isinstance(events, dict)
            or not isinstance(events.get(BACKCHANNEL_LOGOUT_EVENT), dict)
            or "nonce" in claims
        ):
            raise ValueError("Invalid OIDC logout event")
        if not claims.get("sub") and not claims.get("sid"):
            raise ValueError("OIDC logout requires a subject or session")
        for name in ("sub", "sid", "jti"):
            if name in claims and (
                not isinstance(claims[name], str) or not 0 < len(claims[name]) <= 255
            ):
                raise ValueError("Invalid OIDC logout identifier")
        if claims["iat"] < time.time() - LOGOUT_MAX_AGE - CLOCK_LEEWAY:
            raise ValueError("OIDC logout token is too old")
        if claims["exp"] <= claims["iat"]:
            raise ValueError("Invalid OIDC logout token lifetime")
        return claims

    async def _get_metadata(self) -> dict[str, Any]:
        async with self._metadata_lock:
            if self._metadata is not None and time.monotonic() < self._metadata_expires_at:
                return self._metadata
            metadata = await self._request_json(
                "GET", f"{self.issuer.rstrip('/')}/.well-known/openid-configuration"
            )
            if metadata.get("issuer") != self.issuer:
                raise ValueError("OIDC discovery issuer does not match the configured issuer")
            for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                if not isinstance(metadata.get(key), str) or not validate_oidc_url(metadata[key]):
                    raise ValueError("OIDC discovery contains an invalid endpoint")
            if "userinfo_endpoint" in metadata and not validate_oidc_url(
                metadata["userinfo_endpoint"]
            ):
                raise ValueError("OIDC discovery contains an invalid UserInfo endpoint")
            if "code" not in metadata.get("response_types_supported", []):
                raise ValueError("OIDC provider does not support the authorization code flow")
            methods = metadata.get("token_endpoint_auth_methods_supported", ["client_secret_basic"])
            if self._client_auth_method not in methods:
                raise ValueError(
                    "OIDC provider does not support the configured client authentication"
                )
            if "S256" not in metadata.get("code_challenge_methods_supported", []):
                raise ValueError("OIDC provider must advertise PKCE S256 support")
            algorithms = metadata.get("id_token_signing_alg_values_supported", [])
            if not isinstance(algorithms, list) or not SIGNING_ALGORITHMS.intersection(algorithms):
                raise ValueError("OIDC provider does not advertise supported signing algorithms")
            self._metadata = metadata
            self._metadata_expires_at = time.monotonic() + CACHE_TTL
            return metadata

    async def _get_jwks(self, uri: str, *, force: bool = False) -> dict[str, Any]:
        async with self._jwks_lock:
            now = time.monotonic()
            if self._jwks is not None:
                if force and now - self._last_forced_refresh < KEY_REFRESH_INTERVAL:
                    return self._jwks
                if not force and now < self._jwks_expires_at:
                    return self._jwks
            if force:
                self._last_forced_refresh = now
            jwks = await self._request_json("GET", uri)
            if not isinstance(jwks.get("keys"), list):
                raise TypeError("OIDC provider returned an invalid key set")
            self._jwks = jwks
            self._jwks_expires_at = now + CACHE_TTL
            return jwks

    async def _validate_id_token(
        self,
        metadata: dict[str, Any],
        token: str,
        pending: PendingLogin,
        access_token: str,
        code: str,
    ) -> dict[str, Any]:
        claims = await self._decode_signed_token(
            metadata, token, ["iss", "sub", "aud", "exp", "iat", "nonce"]
        )
        if not isinstance(claims["sub"], str) or not claims["sub"] or len(claims["sub"]) > 255:
            raise ValueError("Invalid OIDC subject")
        if "sid" in claims and (
            not isinstance(claims["sid"], str) or not 0 < len(claims["sid"]) <= 255
        ):
            raise ValueError("Invalid OIDC session identifier")
        nonce = claims["nonce"]
        if not isinstance(nonce, str) or not hmac.compare_digest(nonce, pending.idp_nonce):
            raise ValueError("OIDC nonce does not match this sign-in")
        audience = claims["aud"]
        if isinstance(audience, list) and len(audience) > 1 and "azp" not in claims:
            raise ValueError(
                "OIDC token with multiple audiences must identify its authorized party"
            )
        if "azp" in claims and claims["azp"] != self.client_id:
            raise ValueError("OIDC authorized party does not match this client")
        algorithm = jwt.get_unverified_header(token)["alg"]
        for name, value in (("at_hash", access_token), ("c_hash", code)):
            if name in claims:
                digest = jwt.get_algorithm_by_name(algorithm).compute_hash_digest(
                    value.encode("ascii")
                )
                expected = (
                    base64.urlsafe_b64encode(digest[: len(digest) // 2]).rstrip(b"=").decode()
                )
                if not isinstance(claims[name], str) or not hmac.compare_digest(
                    claims[name], expected
                ):
                    raise ValueError("OIDC token hash does not match")
        return claims

    async def _decode_signed_token(
        self, metadata: dict[str, Any], token: str, required_claims: list[str]
    ) -> dict[str, Any]:
        header = jwt.get_unverified_header(token)
        algorithm = header.get("alg")
        if (
            algorithm not in SIGNING_ALGORITHMS
            or algorithm not in metadata["id_token_signing_alg_values_supported"]
            or header.get("crit")
        ):
            raise ValueError("Unsupported OIDC token signing algorithm or critical header")
        jwks = await self._get_jwks(metadata["jwks_uri"])
        key = _select_signing_key(jwks, header)
        if key is None:
            # Refresh for key rotation, but rate-limit unknown kids to prevent JWKS floods.
            jwks = await self._get_jwks(metadata["jwks_uri"], force=True)
            key = _select_signing_key(jwks, header)
        if key is None:
            raise ValueError("No matching OIDC signing key")
        claims: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=list(
                SIGNING_ALGORITHMS.intersection(metadata["id_token_signing_alg_values_supported"])
            ),
            audience=self.client_id,
            issuer=self.issuer,
            leeway=CLOCK_LEEWAY,
            options={"require": required_claims},
        )
        for name in ("iat", "exp"):
            if (
                isinstance(claims[name], bool)
                or not isinstance(claims[name], (int, float))
                or not math.isfinite(claims[name])
            ):
                raise TypeError("Invalid OIDC token timestamp")
        return claims

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, str] | None = None,
        auth: str | None = None,
        bearer_token: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if auth:
            headers["Authorization"] = auth
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        async with self.session.request(
            method,
            url,
            data=data,
            headers=headers,
            timeout=ClientTimeout(total=15),
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise ValueError("OIDC endpoint did not return a successful response")
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_SIZE:
                    raise ValueError("OIDC endpoint response is too large")
            result = json_loads(bytes(body))
        if not isinstance(result, dict):
            raise TypeError("OIDC endpoint response must be an object")
        return result


def _select_signing_key(jwks: dict[str, Any], header: dict[str, Any]) -> jwt.PyJWK | None:
    """Select one public signing key, ignoring token-supplied key URLs and key material."""
    candidates = [
        key
        for key in jwks["keys"]
        if isinstance(key, dict)
        and key.get("kty") in ("RSA", "EC")
        and key.get("use", "sig") == "sig"
        and "verify" in key.get("key_ops", ["verify"])
        and key.get("alg", header["alg"]) == header["alg"]
        and ("kid" not in header or key.get("kid") == header["kid"])
    ]
    if len(candidates) != 1:
        return None
    return jwt.PyJWK(candidates[0], algorithm=header["alg"])
