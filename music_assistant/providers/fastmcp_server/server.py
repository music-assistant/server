"""MCPServerRuntime — composes FastMCP, mounts it into MA's webserver."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .constants import (
    CONF_ENFORCE_AUDIENCE,
    CONF_EXTRA_ALLOWED_ORIGINS,
    CONF_MOUNT_PATH,
    CONF_REQUIRE_AUTH,
    CONF_REQUIRE_CONFIRMATION,
    DEFAULT_MOUNT_PATH,
)
from .tags import enabled_tags

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant_models.config_entries import ProviderConfig

    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(__name__)


class MCPServerRuntime:
    """Build and manage a FastMCP server mounted into MA's webserver.

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
        """Hold the shared dependencies; nothing is started here.

        :param mass: MusicAssistant instance.
        :param config: Provider config.
        :param logger: Provider-scoped logger.
        """
        self._mass = mass
        self._config = config
        self._logger = logger
        self._mount_path: str = str(config.get_value(CONF_MOUNT_PATH) or DEFAULT_MOUNT_PATH)
        self._mcp: Any = None
        self._unmount: Callable[[], None] | None = None
        self._unmount_well_known: Callable[[], None] | None = None
        self._unmount_connect: Callable[[], None] | None = None
        # Mutable so apply_permission_change can hot-swap the allowed-tag set
        # without re-instantiating the TagFilterMiddleware closure.
        self._allowed_tags: set[str] = set()

    @property
    def public_url(self) -> str:
        """Return the externally visible MCP endpoint URL."""
        base = str(getattr(self._mass.webserver, "base_url", "")).rstrip("/")
        return f"{base}{self._mount_path}"

    async def start(self) -> None:
        """Build the FastMCP server and mount it into the MA webserver."""
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
        base_url = str(getattr(self._mass.webserver, "base_url", "") or "").rstrip("/")
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
        mcp.mount(build_library_server(self._mass), namespace="library")
        mcp.mount(
            build_queue_server(self._mass, require_confirmation=require_confirmation),
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
            from . import __version__  # noqa: PLC0415
            from .connect import mount_connect_wizard  # noqa: PLC0415

            self._unmount_connect = await mount_connect_wizard(
                self._mass,
                self._mount_path,
                version=__version__,
                enabled_tags_provider=lambda: [str(t) for t in enabled_tags(self._config)],
                extra_origins_csv=extra_origins,
            )
        except Exception:
            self._logger.warning("Connect Wizard: mount failed", exc_info=True)

        self._logger.debug(
            "MCP runtime started: mount=%s, auth=%s, tags=%d",
            self._mount_path,
            bool(verifier),
            len(enabled_tags(self._config)),
        )

    async def stop(self) -> None:
        """Unregister the HTTP route and drop references."""
        if self._unmount is not None:
            try:
                self._unmount()
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

    async def apply_permission_change(self, new_config: ProviderConfig) -> None:
        """Hot-swap the allowed-tag set without rebuilding FastMCP / remounting.

        Resource toggles (``CONF_RES_*``) require a rebuild because resource
        registration is decided at ``register_resources`` time; permission flags
        flip the tag set in the closure read by :class:`TagFilterMiddleware` and
        take effect on the next request.
        """
        from .constants import PERMISSION_KEYS  # noqa: PLC0415

        permission_only = {
            key for key in self._diff_keys(self._config, new_config) if key in PERMISSION_KEYS
        } == set(self._diff_keys(self._config, new_config))

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

    @staticmethod
    def _diff_keys(old: ProviderConfig, new: ProviderConfig) -> set[str]:
        """Return the set of config keys whose values differ between two configs."""
        try:
            old_values = old.values if hasattr(old, "values") else {}
            new_values = new.values if hasattr(new, "values") else {}
        except (AttributeError, TypeError):
            return set()
        keys = set(old_values) | set(new_values)
        return {k for k in keys if old_values.get(k) != new_values.get(k)}

    def _apply_tag_filter(self, mcp: Any, allowed: set[Any]) -> None:
        """Install the tag-filter middleware on the given FastMCP server."""
        from .middleware import TagFilterMiddleware  # noqa: PLC0415

        # Snapshot tags into the closure-captured set declared in __init__.
        # apply_permission_change mutates the same set later, so the
        # middleware sees the new permissions without rebuilding FastMCP.
        self._allowed_tags = {str(t) for t in allowed}

        async def lookup(kind: str, key: str) -> set[str] | None:
            """Resolve component name/URI back to its tag set via FastMCP public API.

            Returns ``None`` if the component is unknown — middleware then blocks
            the call with NotFoundError, preventing a client that cached a name
            from a prior permission set from invoking a now-hidden tool.
            """
            try:
                if kind == "tool":
                    obj = await mcp.get_tool(key)
                elif kind == "resource":
                    obj = await mcp.get_resource(key)
                elif kind == "prompt":
                    obj = await mcp.get_prompt(key)
                else:  # pragma: no cover - kind is Literal-typed at the caller
                    return None
            except Exception:
                return None
            if obj is None:
                return None
            return {str(t) for t in (getattr(obj, "tags", None) or set())}

        mcp.add_middleware(TagFilterMiddleware(lambda: self._allowed_tags, lookup))
