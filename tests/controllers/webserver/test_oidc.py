"""OIDC protocol, browser binding and account isolation tests using a local HTTPS IdP."""

from __future__ import annotations

import asyncio
import ipaddress
import ssl
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from aiohttp import BasicAuth, ClientSession, TCPConnector, web
from aiohttp.test_utils import make_mocked_request
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from music_assistant_models.auth import AuthProviderType, UserRole
from music_assistant_models.errors import AuthenticationFailed
from yarl import URL

from music_assistant.controllers.webserver.auth import AuthenticationManager
from music_assistant.controllers.webserver.controller import WebserverController, _oidc_cookie_name
from music_assistant.controllers.webserver.helpers.auth_providers import (
    BuiltinLoginProvider,
    get_or_create_ha_user,
)
from music_assistant.controllers.webserver.helpers.login_flow import (
    AuthTransport,
    PendingLoginStore,
)
from music_assistant.controllers.webserver.helpers.oidc import OIDCClient
from music_assistant.controllers.webserver.helpers.oidc_provider import (
    CONF_OIDC_ALLOW_SELF_REGISTRATION,
    CONF_OIDC_CLIENT_ID,
    CONF_OIDC_CLIENT_SECRET,
    CONF_OIDC_ENABLED,
    CONF_OIDC_ISSUER,
    CONF_OIDC_NAME,
    CONF_OIDC_ROLE_MAPPING,
    OIDCLoginProvider,
    oidc_identity_id,
)
from music_assistant.helpers.datetime import utc
from music_assistant.helpers.json import json_dumps, json_loads
from music_assistant.helpers.pkce import pkce_challenge

if TYPE_CHECKING:
    from pathlib import Path

    from aiohttp.pytest_plugin import AiohttpServer
    from music_assistant_models.auth import User

    from music_assistant.controllers.webserver.helpers.login_flow import PendingLogin
    from music_assistant.mass import MusicAssistant

MA_URL = "https://music.example"
CLIENT_ID = "music-assistant"
CLIENT_SECRET = "oidc-test-client-credential"


@dataclass
class IdentityProvider:
    """Controllable OIDC provider served over locally trusted HTTPS."""

    session: ClientSession
    key: rsa.RSAPrivateKey
    pending: PendingLogin
    issuer: str = ""
    kid: str = "first-key"
    overrides: dict[str, Any] = field(default_factory=dict)
    missing: set[str] = field(default_factory=set)
    metadata_overrides: dict[str, Any] = field(default_factory=dict)
    jwks_requests: int = 0
    discovery_requests: int = 0
    token_requests: int = 0
    token_status: int = 200
    userinfo_subject: str = "subject-1"
    userinfo_overrides: dict[str, Any] = field(default_factory=dict)
    signing_algorithm: str = "RS256"
    signing_key: Any = None
    jwks_override: dict[str, Any] | None = None
    last_token_form: dict[str, str] = field(default_factory=dict)
    last_auth_header: str = ""
    groups_scope: str | None = None
    granted_scopes: set[str] = field(default_factory=set)
    token_body: bytes | None = None

    def token(self) -> str:
        """Sign the configured identity using the current key."""
        now = utc().timestamp()
        claims = {
            "iss": self.issuer,
            "aud": CLIENT_ID,
            "sub": "subject-1",
            "iat": now,
            "exp": now + 300,
            "nonce": self.pending.idp_nonce,
            "preferred_username": "listener",
            "name": "A Listener",
            **self.overrides,
        }
        for name in self.missing:
            claims.pop(name, None)
        return jwt.encode(
            claims,
            self.key if self.signing_key is None else self.signing_key,
            algorithm=self.signing_algorithm,
            headers={"kid": self.kid},
        )

    async def authorize(self, request: web.Request) -> web.Response:
        """Record scopes granted for the subsequent code exchange."""
        self.granted_scopes = set(request.query.get("scope", "").split())
        return web.Response(status=204)

    async def discovery(self, request: web.Request) -> web.Response:
        """Serve standards-based discovery with optional fault injection."""
        self.discovery_requests += 1
        return web.json_response(
            {
                "issuer": self.issuer,
                "authorization_endpoint": f"{self.issuer}authorize",
                "token_endpoint": f"{self.issuer}token",
                "jwks_uri": f"{self.issuer}jwks",
                "userinfo_endpoint": f"{self.issuer}userinfo",
                "response_types_supported": ["code"],
                "id_token_signing_alg_values_supported": ["RS256", "HS256", "none"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": [
                    "client_secret_basic",
                    "client_secret_post",
                    "none",
                ],
                **self.metadata_overrides,
            }
        )

    async def jwks(self, request: web.Request) -> web.Response:
        """Serve public signing keys, including rotations."""
        self.jwks_requests += 1
        key = json_loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        return web.json_response(
            self.jwks_override or {"keys": [{**key, "kid": self.kid, "alg": "RS256", "use": "sig"}]}
        )

    async def exchange(self, request: web.Request) -> web.Response:
        """Accept a code only with the correct verifier and exact redirect URI."""
        self.token_requests += 1
        self.last_token_form = {key: str(value) for key, value in (await request.post()).items()}
        self.last_auth_header = request.headers.get("Authorization", "")
        if (
            self.last_token_form.get("code_verifier") != self.pending.idp_code_verifier
            or self.last_token_form.get("redirect_uri") != self.pending.redirect_uri
        ):
            return web.json_response({"error": "invalid_grant"}, status=400)
        if self.token_body is not None:
            return web.Response(body=self.token_body, content_type="application/json")
        return web.json_response(
            {"id_token": self.token(), "access_token": "oidc-test-access", "token_type": "Bearer"},
            status=self.token_status,
        )

    async def userinfo(self, request: web.Request) -> web.Response:
        """Return profile claims separately from the signed ID token."""
        assert request.headers["Authorization"] == "Bearer oidc-test-access"
        claims = {
            "sub": self.userinfo_subject,
            "preferred_username": "profile-listener",
            "name": "Profile Name",
            **self.userinfo_overrides,
        }
        if self.groups_scope and self.groups_scope not in self.granted_scopes:
            claims.pop("groups", None)
        return web.json_response(claims)


@pytest.fixture
async def idp(aiohttp_server: AiohttpServer, tmp_path: Path) -> AsyncIterator[IdentityProvider]:
    """Start a real HTTPS IdP and trust only its throwaway certificate in the test client."""
    tls_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(tls_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(utc() - timedelta(minutes=1))
        .not_valid_after(utc() + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(tls_key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    cert_path, key_path = tmp_path / "tls.pem", tmp_path / "tls.key"
    cert_path.write_bytes(cert_pem)
    key_path.write_bytes(
        tls_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ssl.load_cert_chain(cert_path, key_path)
    client_ssl = ssl.create_default_context(cadata=cert_pem.decode())
    async with ClientSession(connector=TCPConnector(ssl=client_ssl)) as session:
        pending = PendingLoginStore().start(
            "oidc", AuthTransport.DIRECT, f"{MA_URL}/auth/callback?provider_id=oidc", "v" * 64
        )
        identity_provider = IdentityProvider(
            session, rsa.generate_private_key(public_exponent=65537, key_size=2048), pending
        )
        app = web.Application()
        app.router.add_get("/issuer/.well-known/openid-configuration", identity_provider.discovery)
        app.router.add_get("/issuer/jwks", identity_provider.jwks)
        app.router.add_get("/issuer/authorize", identity_provider.authorize)
        app.router.add_get("/issuer/userinfo", identity_provider.userinfo)
        app.router.add_post("/issuer/token", identity_provider.exchange)
        server = await aiohttp_server(app, ssl=server_ssl)
        identity_provider.issuer = str(server.make_url("/issuer/"))
        yield identity_provider


@pytest.fixture
def client(idp: IdentityProvider) -> OIDCClient:
    """Return a protocol client using the real test HTTPS provider."""
    return OIDCClient(idp.session, idp.issuer, CLIENT_ID, CLIENT_SECRET)


@pytest.fixture
async def auth_manager(
    mass_minimal: MusicAssistant, idp: IdentityProvider, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AuthenticationManager]:
    """Set up a real authentication database and a recovery administrator."""
    monkeypatch.setattr(mass_minimal, "_http_session", idp.session)
    monkeypatch.setattr(WebserverController, "base_url", property(lambda _: MA_URL))
    webserver = WebserverController(mass_minimal)
    mass_minimal.webserver = webserver
    webserver.config = await mass_minimal.config.get_core_config("webserver")
    await webserver.auth.setup()
    builtin = webserver.auth.login_providers["builtin"]
    assert isinstance(builtin, BuiltinLoginProvider)
    await builtin.create_user_with_password("admin", "local-test-password", role=UserRole.ADMIN)
    try:
        yield webserver.auth
    finally:
        await webserver.auth.close()


@pytest.fixture
def provider(auth_manager: AuthenticationManager, idp: IdentityProvider) -> OIDCLoginProvider:
    """Enable the optional provider without bypassing its configuration loader."""
    values: dict[str, str | bool] = {
        CONF_OIDC_ENABLED: True,
        CONF_OIDC_ISSUER: idp.issuer,
        CONF_OIDC_CLIENT_ID: CLIENT_ID,
        CONF_OIDC_CLIENT_SECRET: CLIENT_SECRET,
        CONF_OIDC_NAME: "Authentik",
        CONF_OIDC_ALLOW_SELF_REGISTRATION: True,
    }
    for key, value in values.items():
        auth_manager.webserver.config.values[key].value = value
    auth_manager._setup_oidc_provider()
    result = auth_manager.login_providers["oidc"]
    assert isinstance(result, OIDCLoginProvider)
    return result


async def test_authorization_code_flow_uses_pkce_nonce_and_exact_issuer(
    client: OIDCClient, idp: IdentityProvider
) -> None:
    """Discovery and code exchange use TLS, a nonce and the exact registered redirect URI."""
    url = URL(await client.authorization_url(idp.pending))
    assert url.query["response_type"] == "code"
    assert url.query["scope"] == "openid profile"
    assert url.query["state"] == idp.pending.state
    assert url.query["nonce"] == idp.pending.idp_nonce
    assert url.query["code_challenge"] == pkce_challenge(idp.pending.idp_code_verifier)
    assert url.query["code_challenge_method"] == "S256"
    assert (await client.authenticate(idp.pending, "valid-code"))["sub"] == "subject-1"
    assert BasicAuth.decode(idp.last_auth_header).password == CLIENT_SECRET
    assert "client_secret" not in idp.last_token_form


@pytest.mark.parametrize("method", ["client_secret_post", "none"])
async def test_registered_client_authentication_methods(idp: IdentityProvider, method: str) -> None:
    """Confidential POST clients and public PKCE clients use their registered auth method."""
    client = OIDCClient(idp.session, idp.issuer, CLIENT_ID, CLIENT_SECRET, method)
    await client.authenticate(idp.pending, "valid-code")
    assert idp.last_token_form["client_id"] == CLIENT_ID
    assert ("client_secret" in idp.last_token_form) == (method == "client_secret_post")
    assert not idp.last_auth_header


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("iss", "https://another-issuer.example/"),
        ("aud", "other-client"),
        ("nonce", "other-nonce"),
        ("nonce", "non-ascii-\u00e9"),
        ("sub", ""),
        ("azp", "other-client"),
        ("exp", 1),
        ("iat", 999999999999),
        ("iat", True),
        ("aud", [CLIENT_ID, "another-client"]),
        ("at_hash", "wrong-hash"),
        ("at_hash", "non-ascii-\u00e9"),
        ("c_hash", "wrong-hash"),
        ("c_hash", "non-ascii-\u00e9"),
    ],
)
async def test_untrusted_identity_claims_are_rejected(
    provider: OIDCLoginProvider, idp: IdentityProvider, claim: str, value: Any
) -> None:
    """No account is created when signature-bound OIDC claims do not validate."""
    idp.overrides[claim] = value
    with pytest.raises(AuthenticationFailed, match="OpenID Connect sign-in failed"):
        await provider.complete_authorization(idp.pending, {"code": "valid-code"})
    assert len(await provider.auth_manager.database.get_rows("users")) == 1


@pytest.mark.parametrize("claim", ["iss", "sub", "aud", "exp", "iat", "nonce"])
async def test_required_id_token_claims_are_not_optional(
    provider: OIDCLoginProvider, idp: IdentityProvider, claim: str
) -> None:
    """An otherwise correctly signed token must contain every required OIDC claim."""
    idp.missing.add(claim)
    with pytest.raises(AuthenticationFailed):
        await provider.complete_authorization(idp.pending, {"code": "valid-code"})


@pytest.mark.parametrize("algorithm", ["HS256", "none"])
async def test_advertised_symmetric_and_unsigned_tokens_are_rejected(
    provider: OIDCLoginProvider, idp: IdentityProvider, algorithm: str
) -> None:
    """Discovery cannot enable HMAC key confusion or unsigned ID tokens."""
    idp.signing_algorithm = algorithm
    idp.signing_key = "untrusted-test-key-with-enough-entropy" if algorithm == "HS256" else ""
    with pytest.raises(AuthenticationFailed):
        await provider.complete_authorization(idp.pending, {"code": "valid-code"})


async def test_signature_verification_is_required(
    provider: OIDCLoginProvider, idp: IdentityProvider
) -> None:
    """A matching kid and correct claims cannot substitute for a valid signature."""
    idp.signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(AuthenticationFailed):
        await provider.complete_authorization(idp.pending, {"code": "valid-code"})


@pytest.mark.parametrize(
    "override",
    [
        {"issuer": "https://another-issuer.example/"},
        {"token_endpoint": "http://insecure.example/token"},
        {"jwks_uri": "https://credentials:secret@example.com/jwks"},
        {"code_challenge_methods_supported": ["plain"]},
        {"response_types_supported": ["token"]},
        {"token_endpoint_auth_methods_supported": ["none"]},
    ],
)
async def test_untrusted_or_incompatible_discovery_is_rejected(
    client: OIDCClient, idp: IdentityProvider, override: dict[str, Any]
) -> None:
    """Discovery cannot switch issuers, downgrade TLS/PKCE, or change registered client auth."""
    idp.metadata_overrides.update(override)
    with pytest.raises(ValueError, match="OIDC"):
        await client.authorization_url(idp.pending)
    assert idp.token_requests == 0


async def test_discovery_and_keys_are_cached_and_new_keys_refresh_once(
    client: OIDCClient, idp: IdentityProvider
) -> None:
    """A legitimate key rotation refreshes JWKS while ordinary sign-ins reuse discovery/keys."""
    await client.authenticate(idp.pending, "valid-code")
    await client.authenticate(idp.pending, "valid-code")
    assert (idp.discovery_requests, idp.jwks_requests) == (1, 1)
    idp.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    idp.kid = "rotated-key"
    await client.authenticate(idp.pending, "valid-code")
    assert idp.jwks_requests == 2
    idp.jwks_override = {"keys": []}
    idp.kid = "unpublished-key"
    for _ in range(3):
        with pytest.raises(ValueError, match="No matching OIDC signing key"):
            await client.authenticate(idp.pending, "valid-code")
    assert idp.jwks_requests == 2


async def test_separate_userinfo_requires_the_same_subject(
    client: OIDCClient, idp: IdentityProvider
) -> None:
    """Providers that omit profile claims from ID tokens are supported without identity mixing."""
    idp.missing.update({"preferred_username", "name"})
    claims = await client.authenticate(idp.pending, "valid-code")
    assert claims["preferred_username"] == "profile-listener"
    idp.userinfo_subject = "someone-else"
    with pytest.raises(ValueError, match="subject does not match"):
        await client.authenticate(idp.pending, "valid-code")


async def test_oidc_is_opt_in_and_does_not_contact_the_provider_at_startup(
    auth_manager: AuthenticationManager, idp: IdentityProvider
) -> None:
    """An existing install still offers only built-in login and makes no OIDC requests."""
    assert [p["provider_id"] for p in await auth_manager.get_login_providers()] == ["builtin"]
    assert idp.discovery_requests == 0


async def test_oidc_listing_exposes_only_public_display_metadata(
    provider: OIDCLoginProvider, idp: IdentityProvider
) -> None:
    """The public provider listing adds a label without disclosing client credentials."""
    providers = await provider.auth_manager.get_login_providers()
    assert providers[-1] == {
        "provider_id": "oidc",
        "provider_type": "oidc",
        "requires_redirect": True,
        "supports_remote_app": False,
        "name": "Authentik",
    }
    assert CLIENT_SECRET not in json_dumps(providers)
    assert idp.discovery_requests == 0


async def test_username_collision_cannot_take_over_a_local_administrator(
    provider: OIDCLoginProvider, idp: IdentityProvider
) -> None:
    """A matching admin username/email creates a separate regular user, preserving local login."""
    idp.overrides.update(
        {"preferred_username": "admin", "email": "admin@example.com", "role": "admin"}
    )
    user = await provider.complete_authorization(idp.pending, {"code": "valid-code"})
    assert user.username.startswith("admin-")
    assert user.role == UserRole.USER
    local = await provider.auth_manager.authenticate_with_credentials(
        "builtin", {"username": "admin", "password": "local-test-password"}
    )
    assert local.success
    assert local.user
    assert local.user.role == UserRole.ADMIN
    assert user.user_id != local.user.user_id


async def test_repeated_and_concurrent_sign_ins_keep_one_identity_and_its_permissions(
    provider: OIDCLoginProvider, idp: IdentityProvider
) -> None:
    """Issuer/sub identity survives username changes and concurrent callbacks, preserving filters."""
    user = await provider.complete_authorization(idp.pending, {"code": "valid-code"})
    await provider.auth_manager.database.update(
        "users", {"user_id": user.user_id}, {"player_filter": json_dumps(["living-room"])}
    )
    idp.overrides.update({"preferred_username": "renamed-listener", "role": "admin"})
    results = await asyncio.gather(
        *(provider.complete_authorization(idp.pending, {"code": "valid-code"}) for _ in range(3))
    )
    assert all(result.user_id == user.user_id for result in results)
    assert all(result.player_filter == ["living-room"] for result in results)
    assert all(result.role == UserRole.USER for result in results)
    assert len(await provider.auth_manager.database.get_rows("users")) == 2
    await provider.auth_manager.database.update(
        "users", {"user_id": user.user_id}, {"enabled": False}
    )
    with pytest.raises(AuthenticationFailed, match="disabled"):
        await provider.complete_authorization(idp.pending, {"code": "valid-code"})


async def test_later_home_assistant_login_cannot_merge_with_an_oidc_username(
    provider: OIDCLoginProvider, idp: IdentityProvider
) -> None:
    """HA's legacy username matching must not turn an OIDC identity into the HA user's account."""
    oidc_user = await provider.complete_authorization(idp.pending, {"code": "valid-code"})
    mass = provider.mass
    with patch(
        "music_assistant.controllers.webserver.helpers.auth_providers.get_ha_user_role",
        new=AsyncMock(return_value=UserRole.ADMIN),
    ) as role_lookup:
        ha_user = await get_or_create_ha_user(
            mass, "ha-listener", "listener", "Home Assistant Listener", None, allow_create=True
        )
        assert ha_user is not None
        assert ha_user.user_id != oidc_user.user_id
        assert ha_user.username.startswith("listener-ha-")
        assert ha_user.role == UserRole.ADMIN
        role_lookup.assert_awaited_once_with(mass, "ha-listener")
        repeated = await get_or_create_ha_user(
            mass, "ha-listener", "listener", None, None, allow_create=True
        )
        assert repeated is not None
        assert repeated.user_id == ha_user.user_id
    stored_oidc_user = await provider.auth_manager.get_user(oidc_user.user_id)
    assert stored_oidc_user is not None
    assert stored_oidc_user.role == UserRole.USER
    assert (
        await provider.complete_authorization(idp.pending, {"code": "valid-code"})
    ).user_id == oidc_user.user_id


@pytest.mark.parametrize("first_provider", [AuthProviderType.OIDC, AuthProviderType.HOME_ASSISTANT])
async def test_concurrent_home_assistant_and_oidc_accounts_stay_separate(
    provider: OIDCLoginProvider, idp: IdentityProvider, first_provider: AuthProviderType
) -> None:
    """A concurrent HA sign-in cannot inherit the role of a partially created OIDC account."""
    auth = provider.auth_manager
    auth.webserver.config.values[CONF_OIDC_ROLE_MAPPING].value = '{"admins":"admin"}'
    auth._setup_oidc_provider()
    oidc_provider = auth.login_providers["oidc"]
    assert isinstance(oidc_provider, OIDCLoginProvider)
    provider = oidc_provider
    idp.overrides["groups"] = ["admins"]
    link_started = asyncio.Event()
    finish_link = asyncio.Event()
    create_user = auth.create_user

    async def delayed_creation(*args: Any, **kwargs: Any) -> User:
        user = await create_user(*args, **kwargs)
        provider_type = (
            AuthProviderType.OIDC
            if kwargs.get("provider_link")
            else AuthProviderType.HOME_ASSISTANT
        )
        if provider_type == first_provider:
            link_started.set()
            await finish_link.wait()
        return user

    async def sign_in(provider_type: AuthProviderType) -> User | None:
        if provider_type == AuthProviderType.OIDC:
            return await provider.complete_authorization(idp.pending, {"code": "valid-code"})
        return await get_or_create_ha_user(
            provider.mass, "ha-listener", "listener", None, None, allow_create=True
        )

    second_provider = (
        AuthProviderType.HOME_ASSISTANT
        if first_provider == AuthProviderType.OIDC
        else AuthProviderType.OIDC
    )
    with (
        patch.object(auth, "create_user", side_effect=delayed_creation),
        patch(
            "music_assistant.controllers.webserver.helpers.auth_providers.get_ha_user_role",
            new=AsyncMock(return_value=UserRole.USER),
        ),
    ):
        async with asyncio.TaskGroup() as tasks:
            first = tasks.create_task(sign_in(first_provider))
            try:
                await asyncio.wait_for(link_started.wait(), timeout=5)
                second = tasks.create_task(sign_in(second_provider))
                await asyncio.wait({second}, timeout=0.2)
            finally:
                finish_link.set()
    users = {first_provider: first.result(), second_provider: second.result()}
    oidc_user = users[AuthProviderType.OIDC]
    ha_user = users[AuthProviderType.HOME_ASSISTANT]
    assert oidc_user is not None
    assert ha_user is not None
    assert oidc_user.user_id != ha_user.user_id
    assert oidc_user.role == UserRole.ADMIN
    assert ha_user.role == UserRole.USER
    assert (
        await auth.get_user_by_provider_link(AuthProviderType.HOME_ASSISTANT, "ha-listener")
        == ha_user
    )
    assert (
        await auth.get_user_by_provider_link(
            AuthProviderType.OIDC, oidc_identity_id(idp.issuer, "subject-1")
        )
        == oidc_user
    )


async def test_registration_can_be_disabled_without_locking_out_linked_users(
    provider: OIDCLoginProvider, idp: IdentityProvider
) -> None:
    """Only established issuer/sub links work when self-registration is disabled."""
    user = await provider.complete_authorization(idp.pending, {"code": "valid-code"})
    provider._oidc_config["allow_self_registration"] = False
    assert (
        await provider.complete_authorization(idp.pending, {"code": "valid-code"})
    ).user_id == user.user_id
    idp.overrides["sub"] = "another-subject"
    with pytest.raises(AuthenticationFailed, match="Self-registration is disabled"):
        await provider.complete_authorization(idp.pending, {"code": "valid-code"})


async def test_oidc_links_survive_a_restart_without_a_schema_migration(
    provider: OIDCLoginProvider, idp: IdentityProvider
) -> None:
    """OIDC reuses the existing provider-link table and preserves local users/tokens."""
    auth = provider.auth_manager
    user = await provider.complete_authorization(idp.pending, {"code": "valid-code"})
    await auth.close()
    await auth.setup()
    linked = await auth.get_user_by_provider_link(
        AuthProviderType.OIDC, oidc_identity_id(idp.issuer, "subject-1")
    )
    assert linked
    assert linked.user_id == user.user_id
    admin = await auth.get_user_by_username("admin")
    assert admin is not None
    assert admin.role == UserRole.ADMIN


@pytest.mark.parametrize(
    "code",
    ["valid-code", "opaque+code/with=padding", "c" * 1024],
    ids=["url-safe", "opaque-base64", "long-code"],
)
async def test_browser_start_callback_and_replay_protection(
    provider: OIDCLoginProvider, idp: IdentityProvider, code: str
) -> None:
    """A real HTTPS code exchange succeeds only in the initiating browser, once."""
    auth = provider.auth_manager
    start_url = (await auth.get_auth_url("oidc", return_url=MA_URL))["authorization_url"]
    assert start_url is not None
    assert URL(start_url).path == "/auth/oidc/start"
    assert not auth.pending_logins._pending
    response = await auth.webserver._handle_oidc_start(
        make_mocked_request(
            "GET",
            f"/auth/oidc/start?return_url={MA_URL}",
            headers={"Host": "music.example", "X-Forwarded-Proto": "https"},
        )
    )
    assert response.status == 302
    state = URL(response.headers["Location"]).query["state"]
    pending = auth.pending_logins.get(state)
    assert pending is not None
    idp.pending = pending
    cookie_name = _oidc_cookie_name(state)
    cookie = response.cookies[cookie_name]
    assert cookie["httponly"]
    assert cookie["secure"]
    assert cookie["samesite"] == "Lax"
    assert cookie_name.startswith("__Host-")
    assert cookie["path"] == "/"
    assert not cookie["domain"]
    callback_url = str(URL("/auth/callback").with_query(provider_id="oidc", code=code, state=state))
    # Sharing a callback URL with another browser must not sign that browser in.
    refused = await auth.webserver._handle_auth_callback(make_mocked_request("GET", callback_url))
    assert refused.status == 400
    assert idp.token_requests == 0
    request = make_mocked_request(
        "GET", callback_url, headers={"Cookie": f"{cookie_name}={cookie.value}"}
    )
    accepted = await auth.webserver._handle_auth_callback(request)
    assert accepted.status == 200
    assert accepted.headers["Cache-Control"] == "no-store"
    assert accepted.cookies[cookie_name]["max-age"] == "0"
    assert accepted.cookies[cookie_name]["secure"]
    assert idp.token_requests == 1
    assert idp.last_token_form["code"] == code
    assert len(await auth.database.get_rows("oidc_sessions")) == 1
    replay = await auth.webserver._handle_auth_callback(request)
    assert replay.status == 400
    assert idp.token_requests == 1


@pytest.mark.parametrize("internal_base", [False, True])
async def test_forwarded_host_sets_cookie_on_the_public_callback_origin(
    provider: OIDCLoginProvider,
    monkeypatch: pytest.MonkeyPatch,
    internal_base: bool,
) -> None:
    """A proxy's internal Host cannot loop redirects or choose the wrong callback URL."""
    if internal_base:
        monkeypatch.setattr(
            WebserverController, "base_url", property(lambda _: "http://ma-internal:8095")
        )
        monkeypatch.setattr(WebserverController, "external_url", property(lambda _: MA_URL))
    auth = provider.auth_manager
    response = await auth.webserver._handle_oidc_start(
        make_mocked_request(
            "GET",
            "/auth/oidc/start",
            headers={
                "Host": "ma-internal:8095",
                "X-Forwarded-Host": "music.example",
                "X-Forwarded-Proto": "https",
            },
        )
    )
    assert response.status == 302
    state = URL(response.headers["Location"]).query["state"]
    pending = auth.pending_logins.get(state)
    assert pending is not None
    assert pending.redirect_uri == f"{MA_URL}/auth/callback?provider_id=oidc"
    cookie = response.cookies[_oidc_cookie_name(state)]
    assert cookie["secure"]
    assert cookie["httponly"]
    assert not cookie["domain"]


@pytest.mark.parametrize("code", ["", "non-ascii-\u00e9", "line\nbreak", "c" * 4097])
async def test_invalid_codes_are_rejected_before_contacting_the_provider(
    provider: OIDCLoginProvider, idp: IdentityProvider, code: str
) -> None:
    """Reject control characters, non-ASCII and unbounded authorization codes locally."""
    started = await provider.auth_manager.get_authorization_url("oidc")
    assert started is not None
    result = await provider.auth_manager.handle_oauth_callback("oidc", code, started[1].state)
    assert not result.success
    assert idp.token_requests == 0


async def test_bad_oidc_configuration_or_outage_does_not_disable_local_login(
    provider: OIDCLoginProvider, idp: IdentityProvider
) -> None:
    """A bad issuer or an unavailable identity provider never removes built-in sign-in."""
    auth = provider.auth_manager
    idp.token_status = 503
    with pytest.raises(AuthenticationFailed):
        await provider.complete_authorization(idp.pending, {"code": "valid-code"})
    auth.webserver.config.values[CONF_OIDC_ISSUER].value = "http://insecure.example"
    auth._setup_oidc_provider()
    assert "oidc" not in auth.login_providers
    assert (
        await auth.authenticate_with_credentials(
            "builtin", {"username": "admin", "password": "local-test-password"}
        )
    ).success


def test_issuer_identity_is_exact_and_unambiguous() -> None:
    """An identity from a different issuer or a different subject never reuses the link."""
    assert oidc_identity_id("https://issuer/", "a") != oidc_identity_id("https://issuer", "a")
    assert oidc_identity_id("https://issuer/", "a") != oidc_identity_id("https://issuer/", "A")
    assert oidc_identity_id("https://issuer/a", "bc") != oidc_identity_id("https://issuer/ab", "c")
