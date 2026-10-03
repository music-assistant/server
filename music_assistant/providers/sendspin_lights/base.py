"""Base bridge for light plugins driven by the Sendspin visualizer."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, cast

from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from music_assistant_models.enums import PlayerType

from music_assistant.providers.sendspin.bridge_role import (
    COLOR_BRIDGE_ROLE_ID,
    VISUALIZER_BRIDGE_ROLE_ID,
    BridgeColorRole,
    BridgeVisualizerRole,
)

if TYPE_CHECKING:
    import asyncio

    from aiosendspin.models.core import ServerStatePayload
    from aiosendspin.models.visualizer import BeatTiming, ClientHelloVisualizerSupport
    from aiosendspin.server import ExternalStreamStartRequest, SendspinClient
    from aiosendspin.server.roles.visualizer.features import ExtractedFrame

    from music_assistant.models.plugin import PluginProvider
    from music_assistant.providers.sendspin.provider import SendspinProvider


class SendspinLightBridge(ABC):
    """Registers a light player with Sendspin and drives a fixed-rate render loop."""

    def __init__(
        self,
        provider: PluginProvider,
        sendspin_provider: SendspinProvider,
        client_id: str,
        name: str,
        manufacturer: str,
        product_name: str,
        support: ClientHelloVisualizerSupport,
        render_rate_hz: int,
        with_color_role: bool = False,
    ) -> None:
        """
        Initialize the bridge.

        :param provider: The owning plugin provider.
        :param sendspin_provider: The loaded Sendspin provider to register with.
        :param client_id: Sendspin client id of the light player.
        :param name: Display name of the light player.
        :param manufacturer: Manufacturer shown in the device info.
        :param product_name: Product name shown in the device info.
        :param support: Visualizer features to request from the extraction pipeline.
        :param render_rate_hz: Rate of the render loop.
        :param with_color_role: Also receive color palette updates.
        """
        self.provider = provider
        self.mass = provider.mass
        self.logger = provider.logger.getChild(f"bridge.{client_id}")
        self.sendspin_provider = sendspin_provider
        self.sendspin_server = sendspin_provider.server_api
        self.client_id = client_id
        self._name = name
        self._manufacturer = manufacturer
        self._product_name = product_name
        self._support = support
        self._with_color_role = with_color_role
        self._render_period_s = 1.0 / render_rate_hz
        self._sendspin_client: SendspinClient | None = None
        self._render_handle: asyncio.TimerHandle | None = None
        self._is_streaming = False

    def register_client(self) -> None:
        """Register as an in-process Sendspin client and attach the visualizer role."""
        self.sendspin_provider.register_bridge_player_type(self.client_id, PlayerType.LIGHT)
        roles = [VISUALIZER_BRIDGE_ROLE_ID]
        if self._with_color_role:
            roles.append(COLOR_BRIDGE_ROLE_ID)
        hello = ClientHelloPayload(
            client_id=self.client_id,
            name=self._name,
            version=1,
            supported_roles=roles,
            device_info=SendspinDeviceInfo(
                manufacturer=self._manufacturer, product_name=self._product_name
            ),
            visualizer_support=self._support,
        )
        client = self.sendspin_server.register_external_player(
            hello, on_stream_start=self._on_external_stream_start
        )
        self._sendspin_client = client
        if viz_roles := client.roles_by_family("visualizer"):
            viz_role = cast("BridgeVisualizerRole", viz_roles[0])
            viz_role.set_callbacks(
                on_frame=self._on_frame,
                on_beats=self._on_beats,
                on_beats_clear=self._on_beats_clear,
                on_stream_start=self._on_stream_start,
                on_stream_clear=self._on_stream_clear,
                on_stream_end=self._on_stream_end,
            )
            viz_role.setup_visualizer(self._support)
        if self._with_color_role and (color_roles := client.roles_by_family("color")):
            cast("BridgeColorRole", color_roles[0]).set_callbacks(on_color=self._on_color)
        client.attach_preinitialized_roles()

    async def unregister_client(self) -> None:
        """Stop the render loop and remove the Sendspin client."""
        self._cancel_render_loop()
        self._is_streaming = False
        if self._sendspin_client:
            await self.sendspin_server.remove_client(self._sendspin_client.client_id)
            self._sendspin_client = None

    @abstractmethod
    def _on_frame(self, frame: ExtractedFrame) -> None:
        """Handle an extracted feature frame."""

    @abstractmethod
    def _render(self) -> None:
        """Render and send one update."""

    def _on_external_stream_start(self, request: ExternalStreamStartRequest) -> None:
        """Handle playback dialing this client."""
        self.logger.debug("Sendspin stream start request (%s)", request.connection_reason)

    def _on_stream_start(self) -> None:  # noqa: B027
        """Handle stream start."""

    def _on_stream_clear(self) -> None:  # noqa: B027
        """Handle a seek."""

    def _on_stream_end(self) -> None:  # noqa: B027
        """Handle stream end."""

    def _on_beats(self, beats: list[BeatTiming]) -> None:  # noqa: B027
        """Handle a beat schedule segment."""

    def _on_beats_clear(self) -> None:  # noqa: B027
        """Handle the beat schedule being dropped."""

    def _on_color(self, payload: ServerStatePayload) -> None:  # noqa: B027
        """Handle a color palette update."""

    def _start_render_loop(self) -> None:
        """Begin the fixed-rate render loop."""
        if self._render_handle is None:
            self._render_handle = self.mass.loop.call_later(
                self._render_period_s, self._render_tick
            )

    def _cancel_render_loop(self) -> None:
        """Cancel the render loop."""
        if self._render_handle is not None:
            self._render_handle.cancel()
            self._render_handle = None

    def _render_tick(self) -> None:
        """Render one update and reschedule while streaming."""
        self._render_handle = None
        if not self._is_streaming:
            return
        try:
            self._render()
        except Exception:
            # One bad tick must not stop the loop: log and reschedule below.
            self.logger.exception("Render tick failed")
        finally:
            if self._is_streaming:
                self._render_handle = self.mass.loop.call_later(
                    self._render_period_s, self._render_tick
                )


__all__ = ["SendspinLightBridge"]
