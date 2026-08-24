"""MCPServerRuntime — composes FastMCP, mounts it into MA's webserver."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from .audit import NO_TOKEN_CLIENT_ID
from .auth import LEGACY_TOKEN_CLIENT_ID, LOOKUP_FAILURE_CLIENT_ID
from .capabilities import Capability
from .constants import (
    CONF_ENABLE_MCP_APP,
    CONF_ENFORCE_AUDIENCE,
    CONF_EXTRA_ALLOWED_ORIGINS,
    CONF_MOUNT_PATH,
    CONF_REQUIRE_AUTH,
    CONF_RES_PROMPTS,
    CONF_TRUST_FORWARDED_PROTO,
    DEFAULT_MOUNT_PATH,
    is_policy_key,
)
from .policy import POLICY_SCHEMA_VERSION
from .policy_config import build_policy_resolver
from .token_identity import AuthenticatedPolicyResolver, TokenIdentityRegistry

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from music_assistant_models.config_entries import ProviderConfig

    from music_assistant.mass import MusicAssistant

    from .policy import PolicyResolver, PolicySnapshot

LOGGER = logging.getLogger(__name__)


class MCPServerRuntime:
    """
    Build and manage a FastMCP server mounted into MA's webserver.

    The lifecycle is intentionally simple:

    * :meth:`start` builds the FastMCP root, registers resources, prompts, and
      the three meta-tools, applies tag filtering, and exposes the
      streamable-HTTP ASGI app on MA's webserver under :pyattr:`mount_path`.
    * :meth:`stop` unregisters the dynamic route.
    * :meth:`apply_config_change` rebuilds the runtime in place when
      only permission flags / resource toggles changed (no port collision
      since we reuse MA's webserver).
    """

    def __init__(
        self,
        mass: MusicAssistant,
        config: ProviderConfig,
        logger: logging.Logger,
        policy_change_callback: Callable[[frozenset[str]], None] | None = None,
    ) -> None:
        """
        Hold the shared dependencies; nothing is started here.

        :param mass: MusicAssistant instance.
        :param config: Provider config.
        :param logger: Provider-scoped logger.
        """
        self._mass = mass
        self._config = config
        self._logger = logger
        self._policy_change_callback = policy_change_callback
        raw_path = str(config.get_value(CONF_MOUNT_PATH) or DEFAULT_MOUNT_PATH)
        self._mount_path: str = "/" + raw_path.strip("/")
        self._mcp: Any = None
        self._unmount: Callable[[], Awaitable[None]] | None = None
        self._unmount_well_known: Callable[[], None] | None = None
        self._unmount_connect: Callable[[], None] | None = None
        self._dynamic_adapter: Any = None
        self._token_identities = TokenIdentityRegistry(on_change=self._refresh_policy_resolver)
        self._request_policies = AuthenticatedPolicyResolver(
            self._token_identities,
            build_policy_resolver(config, raw_value_provider=self._raw_policy_value),
        )

    @property
    def public_url(self) -> str:
        """Return the externally visible MCP endpoint URL."""
        base = str(self._mass.webserver.base_url).rstrip("/")
        return f"{base}{self._mount_path}"

    @property
    def policy_resolver(self) -> PolicyResolver:
        """Return the immutable token-ID resolver used by future requests."""
        return self._request_policies.policies

    def resolve_policy(self, bearer_token: str) -> PolicySnapshot:
        """Resolve one authenticated bearer through its bounded MA identity binding."""
        return self._request_policies.resolve(bearer_token)

    def resolve_request_policy(self, bearer_token: str | None) -> PolicySnapshot:
        """Resolve an exact bearer or the configured auth-off global default."""
        if bearer_token is None:
            return self.policy_resolver.resolve(None)
        return self.resolve_policy(bearer_token)

    def audit_client_id(self, bearer_token: str | None) -> str:
        """Return an exact token ID or a safe non-authoritative client label."""
        if bearer_token is None:
            return NO_TOKEN_CLIENT_ID
        identity = self._token_identities.lookup(bearer_token)
        if identity is None:
            return LOOKUP_FAILURE_CLIENT_ID
        return identity.token_id or LEGACY_TOKEN_CLIENT_ID

    async def start(self) -> None:
        """
        Build the FastMCP server and mount it into the MA webserver.

        On any partial-mount failure, the in-progress state is rolled back
        via :meth:`stop` before the exception propagates — so a retry (or a
        permission-rebuild) starts from a clean slate instead of accumulating
        orphan well-known routes or zombie ASGI lifespans.
        """
        try:
            await self._start_impl()
        except BaseException:
            with contextlib.suppress(BaseException):
                await self.stop()
            raise

    async def stop(self) -> None:
        """Unregister the HTTP route and drop references."""
        if self._unmount is not None:
            try:
                await self._unmount()
            except Exception:
                self._logger.exception("Failed to unregister MCP route")
            self._unmount = None
        if getattr(self, "_unmount_well_known", None) is not None:
            try:
                self._unmount_well_known()  # type: ignore[misc, unused-ignore]
            except Exception:
                self._logger.exception("Failed to unregister well-known route")
            self._unmount_well_known = None
        if getattr(self, "_unmount_connect", None) is not None:
            try:
                self._unmount_connect()  # type: ignore[misc, unused-ignore]
            except Exception:
                self._logger.exception("Failed to unregister Connect Wizard route")
            self._unmount_connect = None
        self._mcp = None

    async def apply_config_change(self, new_config: ProviderConfig, changed_keys: set[str]) -> None:
        """
        Hot-swap the resolver for policy-only changes, or restart the surface.

        Resource toggles (``CONF_RES_*``) require a rebuild because resource
        registration is decided at :meth:`start` time. Policy-only changes replace
        the immutable resolver and take effect on the next request without a restart.

        :param new_config: the new provider config; assigned to ``self._config``
            before any restart so ``start`` reads the updated values.
        :param changed_keys: keys that changed (already stripped of any
            ``values/`` prefix by the caller). MA mutates ``ProviderConfig``
            in place during ``config.update(values)``, so re-diffing ``old`` vs
            ``new`` here would always be empty — the caller's set is the only
            reliable signal.
        """
        policy_only = all(is_policy_key(key) for key in changed_keys)

        self._config = new_config
        if policy_only:
            self._refresh_policy_resolver()
            self._logger.debug("MCP runtime: hot-swapped policy resolver")
            return

        await self.stop()
        await self.start()

    def dynamic_diagnostics(self) -> dict[str, Any]:
        """Return a public snapshot of dynamic-command health without exposing its adapter."""
        diagnostics = (
            {"available": False, "last_error": "catalog not initialized"}
            if self._dynamic_adapter is None
            else dict(self._dynamic_adapter.diagnostics())
        )
        diagnostics.update(
            policy_schema_version=POLICY_SCHEMA_VERSION,
            token_resolution_failures=self._token_identities.token_resolution_failures,
        )
        if self._dynamic_adapter is not None:
            performance = self._dynamic_adapter.performance()
            if isinstance(performance, Mapping):
                diagnostics["performance"] = dict(performance)
        return diagnostics

    async def _start_impl(self) -> None:
        """Mount the runtime; see :meth:`start` for the public-facing wrapper."""
        from fastmcp import FastMCP  # noqa: PLC0415

        from .auth import MASTokenVerifier  # noqa: PLC0415
        from .http_bridge import mount_into_mass  # noqa: PLC0415
        from .prompts import register_prompts  # noqa: PLC0415
        from .resources import register_resources  # noqa: PLC0415

        require_auth_value = self._config.get_value(CONF_REQUIRE_AUTH)
        require_auth = True if require_auth_value is None else bool(require_auth_value)
        base_url = str(self._mass.webserver.base_url or "").rstrip("/")
        public_resource_uri = f"{base_url}{self._mount_path}" if base_url else None
        enforce_audience = bool(self._config.get_value(CONF_ENFORCE_AUDIENCE))
        verifier = (
            MASTokenVerifier(
                self._mass,
                base_url=base_url or None,
                public_resource_uri=public_resource_uri,
                enforce_audience=enforce_audience,
                identity_registry=self._token_identities,
            )
            if require_auth
            else None
        )

        mcp = FastMCP(
            name="music-assistant",
            instructions=(
                "Music Assistant MCP server with on-demand discovery. Use search_tools with a short "
                "query, then get_tool_schema for one canonical ma_api:* command, then execute it "
                "through call_tool. Use an empty search_tools query or catalog://commands for "
                "paginated alphabetical browsing. Follow next_cursor/next_uri; responses default "
                "to compact mode. Resources also expose library://, player:// and queue:// views."
            ),
            auth=verifier,
        )

        register_resources(mcp, self._mass, self._config)
        register_prompts(mcp, self._config)

        self._apply_tag_filter(mcp)
        self._register_meta_discovery(mcp)

        self._mcp = mcp
        extra_origins = str(self._config.get_value(CONF_EXTRA_ALLOWED_ORIGINS) or "")
        self._unmount = await mount_into_mass(
            self._mass, mcp, self._mount_path, extra_origins_csv=extra_origins
        )

        # Publish RFC 9728 protected-resource metadata at the well-known URL
        # advertised by FastMCP in WWW-Authenticate. Skipped when require_auth
        # is off (no metadata to serve) or base_url is missing (no canonical URI).
        if require_auth and public_resource_uri:
            from .http_bridge import mount_well_known  # noqa: PLC0415

            self._unmount_well_known = await mount_well_known(
                self._mass,
                mount_path=self._mount_path,
                resource_uri=public_resource_uri,
                authorization_servers=[base_url],
                # Lazy provider so hot-swapped permissions update the
                # advertised `scopes_supported` immediately, without
                # rebuilding the runtime.
                scopes_supported=lambda: [str(capability) for capability in Capability],
                resource_name="Music Assistant MCP",
            )

        # Mount the Connect Wizard. Failure here is non-fatal — the MCP server
        # itself is unaffected; the user just falls back to manual onboarding.
        try:
            from .connect import mount_connect_wizard  # noqa: PLC0415

            self._unmount_connect = await mount_connect_wizard(
                self._mass,
                self._mount_path,
                default_profile_provider=lambda: self.policy_resolver.resolve(None).profile.value,
                extra_origins_csv=extra_origins,
                trust_forwarded_proto=bool(self._config.get_value(CONF_TRUST_FORWARDED_PROTO)),
            )
        except Exception:
            self._logger.warning("Connect Wizard: mount failed", exc_info=True)

        self._logger.debug(
            "MCP runtime started: mount=%s, auth=%s",
            self._mount_path,
            bool(verifier),
        )

    def _register_meta_discovery(self, mcp: Any) -> None:
        """Install the permanent dynamic command discovery layer."""
        from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

        from .execution import DynamicAPIAdapter  # noqa: PLC0415
        from .meta_discovery import register_meta_discovery  # noqa: PLC0415

        def config_bool(key: str, *, default: bool = False) -> bool:
            """Read booleans while preserving defaults for older installations."""
            value = self._config.get_value(key)
            return default if value is None else bool(value)

        adapter = DynamicAPIAdapter(
            self._mass,
            auth_required_provider=lambda: config_bool(CONF_REQUIRE_AUTH, default=True),
            token_provider=get_access_token,
            policy_provider=self.resolve_policy,
            default_policy_provider=lambda: self.policy_resolver.resolve(None),
            identity_provider=self._token_identities.lookup,
        )
        self._dynamic_adapter = adapter
        additional_public_tools: frozenset[str] = frozenset()
        if config_bool(CONF_ENABLE_MCP_APP):
            # Keep Prefab/FastMCP App imports out of the default runtime path.
            from .app_music_assistant import (  # noqa: PLC0415
                APP_TOOL_NAME,
                register_music_assistant_app,
            )

            register_music_assistant_app(mcp, adapter)
            additional_public_tools = frozenset({APP_TOOL_NAME})
        register_meta_discovery(
            mcp,
            dynamic_adapter=adapter,
            additional_public_tools=additional_public_tools,
        )

    def _apply_tag_filter(self, mcp: Any) -> None:
        """Install request-policy component visibility on FastMCP."""
        from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

        from .middleware import TagFilterMiddleware  # noqa: PLC0415
        from .resource_authorization import ResourceAuthorizer  # noqa: PLC0415

        def config_bool(key: str, *, default: bool = False) -> bool:
            """Read booleans while preserving defaults for older installations."""
            value = self._config.get_value(key)
            return default if value is None else bool(value)

        def request_policy() -> PolicySnapshot:
            token = get_access_token()
            if token is None:
                return self.policy_resolver.resolve(None)
            return self.resolve_policy(token.token)

        mcp.add_middleware(
            TagFilterMiddleware(
                build_tag_lookup(mcp),
                policy_provider=request_policy,
                resource_authorizer=ResourceAuthorizer(
                    self._mass,
                    auth_required_provider=lambda: config_bool(CONF_REQUIRE_AUTH, default=True),
                    token_provider=get_access_token,
                    identity_provider=self._token_identities.lookup,
                    policy_provider=self.resolve_policy,
                    default_policy_provider=lambda: self.policy_resolver.resolve(None),
                ),
                prompts_enabled_provider=lambda: config_bool(CONF_RES_PROMPTS, default=True),
            )
        )

    def _refresh_policy_resolver(self) -> None:
        """Compile and atomically install a resolver for known and manual token IDs."""
        resolver = build_policy_resolver(
            self._config,
            active_token_ids=self._token_identities.token_ids(),
            raw_value_provider=self._raw_policy_value,
        )
        if hasattr(self, "_request_policies"):
            self._request_policies.replace(resolver)
            if self._policy_change_callback is not None:
                self._policy_change_callback(self._token_identities.token_ids())

    def _raw_policy_value(self, key: str) -> object:
        """Read one preserved policy value through MA's sanctioned raw API."""
        instance_id = str(getattr(self._config, "instance_id", ""))
        config_controller = getattr(self._mass, "config", None)
        getter = getattr(config_controller, "get_raw_provider_config_value", None)
        if not instance_id or not callable(getter):
            return None
        return getter(instance_id, key, None)


async def _tag_lookup(mcp: Any, kind: str, key: str) -> set[str] | None:
    """
    Resolve component name/URI back to its tag set via FastMCP public API.

    Returns ``None`` if the component is unknown — middleware then blocks
    the call with NotFoundError, preventing a client that cached a name
    from a prior permission set from invoking a now-hidden tool. For
    resources the concrete-URI lookup falls back to template matching:
    ``FastMCP.get_resource`` only finds statically-registered resources,
    so a request for a concrete URI backed by a
    ``@mcp.resource("scheme://{x}")`` template would otherwise be
    misreported as not-found. Prefab renderers are synthesized only by
    ``list_resources`` and ``read_resource``; an exact match against the
    middleware-free listing keeps those virtual resources addressable without
    treating arbitrary Prefab-looking URIs as infrastructure.
    """
    try:
        if kind == "tool":
            obj = await mcp.get_tool(key)
        elif kind == "resource":
            obj = await mcp.get_resource(key)
            if obj is None:
                obj = await mcp.get_resource_template(key)
            if obj is None:
                obj = await _listed_prefab_resource(mcp, key)
        elif kind == "prompt":
            obj = await mcp.get_prompt(key)
        else:  # pragma: no cover - kind is Literal-typed at the caller
            return None
    except Exception:
        return None
    if obj is None:
        return None
    return {str(t) for t in (getattr(obj, "tags", None) or set())}


async def _listed_prefab_resource(mcp: Any, uri: str) -> Any | None:
    """Return an exact synthetic Prefab listing match without running middleware."""
    if not uri.startswith("ui://prefab/tool/"):
        return None
    resources = await mcp.list_resources(run_middleware=False)
    return next((resource for resource in resources if str(resource.uri) == uri), None)


def build_tag_lookup(mcp: Any) -> Callable[[str, str], Awaitable[set[str] | None]]:
    """Return a closure suitable for :class:`TagFilterMiddleware`'s ``lookup``."""

    async def lookup(kind: str, key: str) -> set[str] | None:
        return await _tag_lookup(mcp, kind, key)

    return lookup
