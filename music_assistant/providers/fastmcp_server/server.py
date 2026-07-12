"""MCPServerRuntime — composes FastMCP, mounts it into MA's webserver."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from .constants import (
    CONF_DEBUG_EVENT_BUFFER_CAPACITY,
    CONF_DEBUG_EVENTS,
    CONF_ENFORCE_AUDIENCE,
    CONF_EXTRA_ALLOWED_ORIGINS,
    CONF_LEAN_ADMIN_SCHEMA,
    CONF_MOUNT_PATH,
    CONF_REQUIRE_AUTH,
    CONF_REQUIRE_CONFIRMATION,
    CONF_TRUST_FORWARDED_PROTO,
    DEFAULT_MOUNT_PATH,
)
from .tags import enabled_tags

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from music_assistant_models.config_entries import ProviderConfig

    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(__name__)


class MCPServerRuntime:
    """
    Build and manage a FastMCP server mounted into MA's webserver.

    The lifecycle is intentionally simple:

    * :meth:`start` builds the FastMCP root, mounts namespaced sub-servers
      for each tool category, registers resources and prompts, applies the
      tag-filter middleware, and exposes the streamable-HTTP ASGI app on
      MA's webserver under :pyattr:`mount_path`.
    * :meth:`stop` unregisters the dynamic route.
    * :meth:`apply_permission_change` rebuilds the runtime in place when
      only permission flags / resource toggles changed (no port collision
      since we reuse MA's webserver).
    """

    def __init__(
        self,
        mass: MusicAssistant,
        config: ProviderConfig,
        logger: logging.Logger,
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
        raw_path = str(config.get_value(CONF_MOUNT_PATH) or DEFAULT_MOUNT_PATH)
        self._mount_path: str = "/" + raw_path.strip("/")
        self._mcp: Any = None
        self._unmount: Callable[[], Awaitable[None]] | None = None
        self._unmount_well_known: Callable[[], None] | None = None
        self._unmount_connect: Callable[[], None] | None = None
        # Mutable so apply_permission_change can hot-swap the allowed-tag set
        # without re-instantiating the TagFilterMiddleware closure.
        self._allowed_tags: set[str] = set()
        self._event_buffer: Any = None  # provider.debug.event_buffer.EventBuffer | None
        self._reload_lock: asyncio.Lock = asyncio.Lock()

    @property
    def public_url(self) -> str:
        """Return the externally visible MCP endpoint URL."""
        base = str(self._mass.webserver.base_url).rstrip("/")
        return f"{base}{self._mount_path}"

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
            with contextlib.suppress(Exception):
                await self.stop()
            raise

    async def stop(self) -> None:
        """Unregister the HTTP route and drop references."""
        if self._event_buffer is not None:
            try:
                self._event_buffer.stop()
            finally:
                self._event_buffer = None
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

    async def apply_permission_change(
        self, new_config: ProviderConfig, changed_keys: set[str]
    ) -> None:
        """
        Hot-swap the allowed-tag set, or restart when resources are involved.

        Resource toggles (``CONF_RES_*``) require a rebuild because resource
        registration is decided at :meth:`start` time; permission flags flip the
        tag set in the closure read by :class:`TagFilterMiddleware` and take
        effect on the next request without a restart.

        :param new_config: the new provider config; assigned to ``self._config``
            before any restart so ``start`` reads the updated values.
        :param changed_keys: keys that changed (already stripped of any
            ``values/`` prefix by the caller). MA mutates ``ProviderConfig``
            in place during ``config.update(values)``, so re-diffing ``old`` vs
            ``new`` here would always be empty — the caller's set is the only
            reliable signal.
        """
        from .constants import PERMISSION_KEYS  # noqa: PLC0415

        # ``set().issubset(...)`` is True, so an empty ``changed_keys`` (no-op
        # call) classifies as permission-only and skips a pointless restart.
        permission_only = changed_keys.issubset(PERMISSION_KEYS)

        self._config = new_config
        if permission_only and hasattr(self, "_allowed_tags"):
            self._allowed_tags = {str(t) for t in enabled_tags(new_config)}
            self._logger.debug(
                "MCP runtime: hot-swapped tag filter to %d tags",
                len(self._allowed_tags),
            )
            return

        await self.stop()
        await self.start()

    async def _start_impl(self) -> None:
        """Mount the runtime; see :meth:`start` for the public-facing wrapper."""
        from fastmcp import FastMCP  # noqa: PLC0415

        from .auth import MASTokenVerifier  # noqa: PLC0415
        from .http_bridge import mount_into_mass  # noqa: PLC0415
        from .prompts import register_prompts  # noqa: PLC0415
        from .resources import register_resources  # noqa: PLC0415
        from .tools import (  # noqa: PLC0415
            build_library_server,
            build_media_server,
            build_metadata_server,
            build_playback_server,
            build_players_server,
            build_playlists_server,
            build_queue_server,
            build_volume_server,
        )

        require_auth = bool(self._config.get_value(CONF_REQUIRE_AUTH))
        base_url = str(self._mass.webserver.base_url or "").rstrip("/")
        public_resource_uri = f"{base_url}{self._mount_path}" if base_url else None
        enforce_audience = bool(self._config.get_value(CONF_ENFORCE_AUDIENCE))
        verifier = (
            MASTokenVerifier(
                self._mass,
                base_url=base_url or None,
                public_resource_uri=public_resource_uri,
                enforce_audience=enforce_audience,
            )
            if require_auth
            else None
        )

        mcp = FastMCP(
            name="music-assistant",
            instructions=(
                "Music Assistant MCP server: control playback, browse the library, "
                "manage queues, and inspect players. Tools are namespaced by category "
                "(library_, queue_, playback_, players_, playlists_, volume_, media_, "
                "metadata_). Resources expose URI-addressable views: library://artist/{id}, "
                "library://album/{id}, library://track/{id}, library://playlist/{id}, "
                "player://{id}, queue://{id}."
            ),
            auth=verifier,
        )

        require_confirmation = bool(self._config.get_value(CONF_REQUIRE_CONFIRMATION) or False)
        lean_admin_schema = bool(self._config.get_value(CONF_LEAN_ADMIN_SCHEMA) or False)
        from .tags import Tag  # noqa: PLC0415

        mcp.mount(build_library_server(self._mass), namespace="library")
        mcp.mount(
            build_queue_server(
                self._mass,
                require_confirmation=require_confirmation,
                delete_queue_enabled=Tag.DELETE_QUEUE in enabled_tags(self._config),
            ),
            namespace="queue",
        )
        mcp.mount(build_playback_server(self._mass), namespace="playback")
        mcp.mount(build_players_server(self._mass), namespace="players")
        mcp.mount(
            build_playlists_server(self._mass, require_confirmation=require_confirmation),
            namespace="playlists",
        )
        mcp.mount(build_volume_server(self._mass), namespace="volume")
        mcp.mount(
            build_media_server(self._mass, require_confirmation=require_confirmation),
            namespace="media",
        )
        mcp.mount(build_metadata_server(self._mass), namespace="metadata")

        from .debug.event_buffer import EventBuffer  # noqa: PLC0415
        from .tools import build_debug_server  # noqa: PLC0415

        if bool(self._config.get_value(CONF_DEBUG_EVENTS)):
            cap_value = self._config.get_value(CONF_DEBUG_EVENT_BUFFER_CAPACITY)
            capacity = int(cap_value) if isinstance(cap_value, int | float | str) else 500
            self._event_buffer = EventBuffer(self._mass, capacity=capacity)
            self._event_buffer.start()

        mcp.mount(
            build_debug_server(
                self._mass,
                require_confirmation=require_confirmation,
                event_buffer=self._event_buffer,
                logs_enabled=Tag.DEBUG_LOGS in enabled_tags(self._config),
                reload_lock=self._reload_lock,
                lean_schema=lean_admin_schema,
            ),
            namespace="debug",
        )

        from .constants import CONF_CONFIG_WRITE_SECRET  # noqa: PLC0415
        from .tools import build_config_server  # noqa: PLC0415

        mcp.mount(
            build_config_server(
                self._mass,
                require_confirmation=require_confirmation,
                secret_writes_enabled=lambda: bool(
                    self._config.get_value(CONF_CONFIG_WRITE_SECRET)
                ),
                lean_schema=lean_admin_schema,
            ),
            namespace="config",
        )

        register_resources(mcp, self._mass, self._config)
        register_prompts(mcp, self._config)

        self._apply_tag_filter(mcp, enabled_tags(self._config))

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
                scopes_supported=lambda: [str(t) for t in enabled_tags(self._config)],
                resource_name="Music Assistant MCP",
            )

        # Mount the Connect Wizard. Failure here is non-fatal — the MCP server
        # itself is unaffected; the user just falls back to manual onboarding.
        try:
            from .connect import mount_connect_wizard  # noqa: PLC0415

            self._unmount_connect = await mount_connect_wizard(
                self._mass,
                self._mount_path,
                enabled_tags_provider=lambda: [str(t) for t in enabled_tags(self._config)],
                extra_origins_csv=extra_origins,
                trust_forwarded_proto=bool(self._config.get_value(CONF_TRUST_FORWARDED_PROTO)),
            )
        except Exception:
            self._logger.warning("Connect Wizard: mount failed", exc_info=True)

        self._logger.debug(
            "MCP runtime started: mount=%s, auth=%s, tags=%d",
            self._mount_path,
            bool(verifier),
            len(enabled_tags(self._config)),
        )

    def _apply_tag_filter(self, mcp: Any, allowed: set[Any]) -> None:
        """Install the tag-filter middleware on the given FastMCP server."""
        from .middleware import TagFilterMiddleware  # noqa: PLC0415

        # Snapshot tags into the closure-captured set declared in __init__.
        # apply_permission_change mutates the same set later, so the
        # middleware sees the new permissions without rebuilding FastMCP.
        self._allowed_tags = {str(t) for t in allowed}
        mcp.add_middleware(TagFilterMiddleware(lambda: self._allowed_tags, build_tag_lookup(mcp)))


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
    misreported as not-found.
    """
    try:
        if kind == "tool":
            obj = await mcp.get_tool(key)
        elif kind == "resource":
            obj = await mcp.get_resource(key)
            if obj is None:
                obj = await mcp.get_resource_template(key)
        elif kind == "prompt":
            obj = await mcp.get_prompt(key)
        else:  # pragma: no cover - kind is Literal-typed at the caller
            return None
    except Exception:
        return None
    if obj is None:
        return None
    return {str(t) for t in (getattr(obj, "tags", None) or set())}


def build_tag_lookup(mcp: Any) -> Callable[[str, str], Awaitable[set[str] | None]]:
    """Return a closure suitable for :class:`TagFilterMiddleware`'s ``lookup``."""

    async def lookup(kind: str, key: str) -> set[str] | None:
        return await _tag_lookup(mcp, kind, key)

    return lookup
