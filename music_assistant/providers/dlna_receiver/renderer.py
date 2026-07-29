"""
DLNA Receiver — UPnP MediaRenderer implementation.

This module contains the HTTP server that serves UPnP device/service XML
descriptions and processes incoming SOAP control actions from DLNA
control points. Includes GENA eventing for state change notifications.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from xml.etree.ElementTree import Element, ParseError, SubElement, fromstring, tostring
from xml.sax.saxutils import escape

import aiohttp
from aiohttp import web

from .constants import (
    DEFAULT_HTTP_PORT,
    SUPPORTED_MIME_TYPES,
    TRANSPORT_STATE_NO_MEDIA,
    TRANSPORT_STATE_PAUSED,
    TRANSPORT_STATE_PLAYING,
    TRANSPORT_STATE_STOPPED,
    UPNP_DEVICE_TYPE,
    UPNP_SERVICE_AV_TRANSPORT,
    UPNP_SERVICE_CONNECTION_MANAGER,
    UPNP_SERVICE_RENDERING_CONTROL,
)
from .eventing import EventingManager

LOGGER = logging.getLogger(__name__)

SCPD_DIR = Path(__file__).parent / "scpd"

SoapCallback = Callable[..., Awaitable[None]]
PlayCallback = Callable[[str], Awaitable[None]]
PositionCallback = Callable[[], tuple[int, int]]

# Extra entity mapping for XML attribute values (default escape() handles only &, <, >).
_ATTR_ENTITIES = {'"': "&quot;"}

# Upper bound on SOAP body size we agree to parse (normal UPnP bodies are < 4 KiB).
_MAX_SOAP_BODY_CHARS = 64 * 1024

# Strip a leading XML declaration so we can safely wrap the remaining body in a
# synthetic root for ElementTree parsing.
_XML_DECLARATION_RE = re.compile(r"^\s*<\?xml[^?]*\?>\s*", re.IGNORECASE)


def _format_upnp_time(seconds: int) -> str:
    """Format a non-negative duration as an UPnP ``HH:MM:SS`` value."""
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class UPnPRenderer:
    """Virtual UPnP MediaRenderer with SOAP action handling."""

    def __init__(
        self,
        friendly_name: str,
        bind_ip: str,
        http_port: int = DEFAULT_HTTP_PORT,
        udn: str | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """
        Create a renderer bound to the given IP/port with a stable UDN.

        ``session`` — optional shared aiohttp session. When supplied (the
        typical provider path passes ``mass.http_session``), all three
        GENA eventing managers reuse the same connector/DNS cache instead
        of each spinning up its own. In multi-player mode this collapses
        ``3 * N`` sessions down to one.
        """
        self.friendly_name = friendly_name
        self.bind_ip = bind_ip
        self.http_port = http_port
        self.udn = udn or f"uuid:{uuid.uuid4()}"

        # Transport state
        self.transport_state: str = TRANSPORT_STATE_NO_MEDIA
        self.current_uri: str = ""
        self.current_uri_metadata: str = ""
        self.volume: int = 50
        self.mute: bool = False

        # HTTP server
        self._app = web.Application()
        self._runner: web.AppRunner | None = None
        # Pre-load SCPD XML bytes once: each request should not do sync
        # file I/O on the event loop.
        self._scpd_cache: dict[str, bytes] = {
            name: (SCPD_DIR / name).read_bytes()
            for name in ("AVTransport.xml", "RenderingControl.xml", "ConnectionManager.xml")
        }
        self._setup_routes()

        # GENA eventing managers (one per service). Share the provided
        # aiohttp session across all three so NOTIFY traffic reuses a
        # single connector instead of spawning one per service per renderer.
        self._evt_av_transport = EventingManager(session=session)
        self._evt_rendering_control = EventingManager(session=session)
        self._evt_connection_manager = EventingManager(session=session)

        # Callbacks (set by provider)
        self.on_set_av_transport_uri: SoapCallback | None = None
        self.on_play: PlayCallback | None = None
        self.on_pause: SoapCallback | None = None
        self.on_stop: SoapCallback | None = None
        self.on_get_position: PositionCallback | None = None
        self.on_set_volume: SoapCallback | None = None
        self.on_set_mute: SoapCallback | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the UPnP HTTP server and eventing managers."""
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.bind_ip, self.http_port)
        await site.start()
        # If the caller requested an ephemeral port (http_port == 0), learn
        # the actual bound port from the runner so description_url and the
        # SSDP LOCATION header advertise a routable port instead of ":0".
        if self.http_port == 0:
            for address in self._runner.addresses:
                if isinstance(address, tuple) and len(address) >= 2:
                    self.http_port = int(address[1])
                    break
        await self._evt_av_transport.start()
        await self._evt_rendering_control.start()
        await self._evt_connection_manager.start()
        LOGGER.info(
            "UPnP renderer HTTP server listening on %s:%s",
            self.bind_ip,
            self.http_port,
        )

    async def stop(self) -> None:
        """Stop the UPnP HTTP server and eventing managers."""
        await self._evt_av_transport.stop()
        await self._evt_rendering_control.stop()
        await self._evt_connection_manager.stop()
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        LOGGER.info("UPnP renderer HTTP server stopped")

    @property
    def description_url(self) -> str:
        """
        Return the device description URL.

        IPv6 literals need square brackets in URL host components
        (RFC 3986 §3.2.2); without them the resulting URL would be
        unparsable by strict control points consuming SSDP LOCATION.
        """
        host = f"[{self.bind_ip}]" if ":" in self.bind_ip else self.bind_ip
        return f"http://{host}:{self.http_port}/description.xml"

    def _setup_routes(self) -> None:
        """Register HTTP routes for UPnP description, control, and eventing."""
        self._app.router.add_get("/description.xml", self._handle_description)
        # SCPD routes
        self._app.router.add_get(
            "/AVTransport/description.xml",
            self._handle_av_transport_scpd,
        )
        self._app.router.add_get(
            "/RenderingControl/description.xml",
            self._handle_rendering_control_scpd,
        )
        self._app.router.add_get(
            "/ConnectionManager/description.xml",
            self._handle_connection_manager_scpd,
        )
        # SOAP control routes
        self._app.router.add_post("/AVTransport/control", self._handle_av_transport)
        self._app.router.add_post(
            "/RenderingControl/control",
            self._handle_rendering_control,
        )
        self._app.router.add_post(
            "/ConnectionManager/control",
            self._handle_connection_manager,
        )
        # GENA event subscription routes
        self._app.router.add_route(
            "SUBSCRIBE",
            "/AVTransport/event",
            self._handle_subscribe_av_transport,
        )
        self._app.router.add_route(
            "UNSUBSCRIBE",
            "/AVTransport/event",
            self._handle_unsubscribe_av_transport,
        )
        self._app.router.add_route(
            "SUBSCRIBE",
            "/RenderingControl/event",
            self._handle_subscribe_rendering_control,
        )
        self._app.router.add_route(
            "UNSUBSCRIBE",
            "/RenderingControl/event",
            self._handle_unsubscribe_rendering_control,
        )
        self._app.router.add_route(
            "SUBSCRIBE",
            "/ConnectionManager/event",
            self._handle_subscribe_connection_manager,
        )
        self._app.router.add_route(
            "UNSUBSCRIBE",
            "/ConnectionManager/event",
            self._handle_unsubscribe_connection_manager,
        )

    # ------------------------------------------------------------------
    # UPnP Device Description
    # ------------------------------------------------------------------

    async def _handle_description(self, _request: web.Request) -> web.Response:
        """Return the root UPnP device description XML."""
        root = Element("root", xmlns="urn:schemas-upnp-org:device-1-0")
        spec = SubElement(root, "specVersion")
        SubElement(spec, "major").text = "1"
        SubElement(spec, "minor").text = "0"

        device = SubElement(root, "device")
        SubElement(device, "deviceType").text = UPNP_DEVICE_TYPE
        SubElement(device, "friendlyName").text = self.friendly_name
        SubElement(device, "manufacturer").text = "Music Assistant"
        SubElement(device, "modelName").text = "DLNA Receiver"
        SubElement(device, "modelDescription").text = "Music Assistant DLNA Receiver Bridge"
        SubElement(device, "UDN").text = self.udn

        service_list = SubElement(device, "serviceList")
        for svc_type, svc_id, scpd_url, ctrl_url, event_url in [
            (
                UPNP_SERVICE_AV_TRANSPORT,
                "urn:upnp-org:serviceId:AVTransport",
                "/AVTransport/description.xml",
                "/AVTransport/control",
                "/AVTransport/event",
            ),
            (
                UPNP_SERVICE_RENDERING_CONTROL,
                "urn:upnp-org:serviceId:RenderingControl",
                "/RenderingControl/description.xml",
                "/RenderingControl/control",
                "/RenderingControl/event",
            ),
            (
                UPNP_SERVICE_CONNECTION_MANAGER,
                "urn:upnp-org:serviceId:ConnectionManager",
                "/ConnectionManager/description.xml",
                "/ConnectionManager/control",
                "/ConnectionManager/event",
            ),
        ]:
            svc = SubElement(service_list, "service")
            SubElement(svc, "serviceType").text = svc_type
            SubElement(svc, "serviceId").text = svc_id
            SubElement(svc, "SCPDURL").text = scpd_url
            SubElement(svc, "controlURL").text = ctrl_url
            SubElement(svc, "eventSubURL").text = event_url

        xml_bytes = b'<?xml version="1.0"?>' + tostring(root, encoding="unicode").encode()
        return web.Response(body=xml_bytes, content_type="text/xml")

    # ------------------------------------------------------------------
    # Service SCPDs (served from static XML files)
    # ------------------------------------------------------------------

    async def _handle_av_transport_scpd(self, _request: web.Request) -> web.Response:
        """Return AVTransport service description."""
        return self._serve_scpd("AVTransport.xml")

    async def _handle_rendering_control_scpd(
        self,
        _request: web.Request,
    ) -> web.Response:
        """Return RenderingControl service description."""
        return self._serve_scpd("RenderingControl.xml")

    async def _handle_connection_manager_scpd(
        self,
        _request: web.Request,
    ) -> web.Response:
        """Return ConnectionManager service description."""
        return self._serve_scpd("ConnectionManager.xml")

    def _serve_scpd(self, filename: str) -> web.Response:
        """Serve a SCPD XML file from the startup-populated cache."""
        return web.Response(body=self._scpd_cache[filename], content_type="text/xml")

    # ------------------------------------------------------------------
    # SOAP Action Handlers
    # ------------------------------------------------------------------

    async def _handle_av_transport(self, request: web.Request) -> web.Response:
        """Handle AVTransport SOAP actions."""
        body = await request.text()
        soap_action = request.headers.get("SOAPACTION", "").strip('"')
        action_name = soap_action.rsplit("#", 1)[-1] if "#" in soap_action else ""
        LOGGER.debug("AVTransport action: %s", action_name)

        if action_name == "SetAVTransportURI":
            uri = self._extract_xml_value(body, "CurrentURI") or ""
            metadata = self._extract_xml_value(body, "CurrentURIMetaData")
            LOGGER.debug("SetAVTransportURI raw metadata (first 500): %s", (metadata or "")[:500])
            # Validate before mutating state: if the callback rejects the URI
            # (raises ValueError), keep the prior transport state intact and
            # surface a SOAP fault so the control point knows it was refused,
            # instead of returning 200 OK and silently ignoring the request.
            if self.on_set_av_transport_uri:
                try:
                    await self.on_set_av_transport_uri(uri, metadata)
                except ValueError as exc:
                    LOGGER.info("SetAVTransportURI rejected: %s", exc)
                    return self._soap_error(716, "Illegal URI")
            self.current_uri = uri
            self.current_uri_metadata = metadata or ""
            self.transport_state = TRANSPORT_STATE_STOPPED
            await self._notify_av_transport_change()
            return self._soap_response(action_name, UPNP_SERVICE_AV_TRANSPORT)

        if action_name == "Play":
            previous_state = self.transport_state
            if self.on_play:
                await self.on_play(previous_state)
            self.transport_state = TRANSPORT_STATE_PLAYING
            await self._notify_av_transport_change()
            return self._soap_response(action_name, UPNP_SERVICE_AV_TRANSPORT)

        if action_name == "Pause":
            self.transport_state = TRANSPORT_STATE_PAUSED
            if self.on_pause:
                await self.on_pause()
            await self._notify_av_transport_change()
            return self._soap_response(action_name, UPNP_SERVICE_AV_TRANSPORT)

        if action_name == "Stop":
            self.transport_state = TRANSPORT_STATE_STOPPED
            if self.on_stop:
                await self.on_stop()
            await self._notify_av_transport_change()
            return self._soap_response(action_name, UPNP_SERVICE_AV_TRANSPORT)

        if action_name == "Seek":
            unit = self._extract_xml_value(body, "Unit") or ""
            target = self._extract_xml_value(body, "Target") or ""
            LOGGER.info("Seek requested: Unit=%s, Target=%s", unit, target)
            return self._soap_error(710, "Seek mode not supported")

        if action_name == "GetTransportInfo":
            return self._soap_response(
                action_name,
                UPNP_SERVICE_AV_TRANSPORT,
                {
                    "CurrentTransportState": self.transport_state,
                    "CurrentTransportStatus": "OK",
                    "CurrentSpeed": "1",
                },
            )

        if action_name == "GetPositionInfo":
            elapsed, duration = self.on_get_position() if self.on_get_position else (0, 0)
            elapsed_value = _format_upnp_time(elapsed)
            return self._soap_response(
                action_name,
                UPNP_SERVICE_AV_TRANSPORT,
                {
                    "Track": "1",
                    "TrackDuration": _format_upnp_time(duration),
                    "TrackMetaData": self.current_uri_metadata,
                    "TrackURI": self.current_uri,
                    "RelTime": elapsed_value,
                    "AbsTime": elapsed_value,
                    "RelCount": "0",
                    "AbsCount": "0",
                },
            )

        if action_name == "GetMediaInfo":
            return self._soap_response(
                action_name,
                UPNP_SERVICE_AV_TRANSPORT,
                {
                    "NrTracks": "1",
                    "MediaDuration": "00:00:00",
                    "CurrentURI": self.current_uri,
                    "CurrentURIMetaData": self.current_uri_metadata,
                    "NextURI": "",
                    "NextURIMetaData": "",
                    "PlayMedium": "NETWORK",
                    "RecordMedium": "NOT_IMPLEMENTED",
                    "WriteStatus": "NOT_IMPLEMENTED",
                },
            )

        LOGGER.warning("Unhandled AVTransport action: %s", action_name)
        return self._soap_error(401, "Invalid Action")

    async def _handle_rendering_control(self, request: web.Request) -> web.Response:
        """Handle RenderingControl SOAP actions."""
        body = await request.text()
        soap_action = request.headers.get("SOAPACTION", "").strip('"')
        action_name = soap_action.rsplit("#", 1)[-1] if "#" in soap_action else ""
        LOGGER.debug("RenderingControl action: %s", action_name)

        if action_name == "GetVolume":
            return self._soap_response(
                action_name,
                UPNP_SERVICE_RENDERING_CONTROL,
                {"CurrentVolume": str(self.volume)},
            )

        if action_name == "SetVolume":
            vol_str = self._extract_xml_value(body, "DesiredVolume")
            if vol_str is not None:
                try:
                    vol = int(vol_str.strip())
                except ValueError, TypeError:
                    LOGGER.warning("Invalid DesiredVolume value: %r", vol_str)
                    return self._soap_error(402, "Invalid Args")
                self.volume = max(0, min(100, vol))
                if self.on_set_volume:
                    await self.on_set_volume(self.volume)
                await self._notify_rendering_control_change()
            return self._soap_response(
                action_name,
                UPNP_SERVICE_RENDERING_CONTROL,
            )

        if action_name == "GetMute":
            return self._soap_response(
                action_name,
                UPNP_SERVICE_RENDERING_CONTROL,
                {"CurrentMute": "1" if self.mute else "0"},
            )

        if action_name == "SetMute":
            mute_str = self._extract_xml_value(body, "DesiredMute")
            if mute_str is not None:
                self.mute = mute_str.strip().lower() in {"1", "true", "yes"}
                if self.on_set_mute:
                    await self.on_set_mute(self.mute)
                await self._notify_rendering_control_change()
            return self._soap_response(
                action_name,
                UPNP_SERVICE_RENDERING_CONTROL,
            )

        LOGGER.warning("Unhandled RenderingControl action: %s", action_name)
        return self._soap_error(401, "Invalid Action")

    async def _handle_connection_manager(self, request: web.Request) -> web.Response:
        """Handle ConnectionManager SOAP actions."""
        soap_action = request.headers.get("SOAPACTION", "").strip('"')
        action_name = soap_action.rsplit("#", 1)[-1] if "#" in soap_action else ""
        LOGGER.debug("ConnectionManager action: %s", action_name)

        if action_name == "GetProtocolInfo":
            sink_protocols = ",".join(f"http-get:*:{mime}:*" for mime in SUPPORTED_MIME_TYPES)
            return self._soap_response(
                action_name,
                UPNP_SERVICE_CONNECTION_MANAGER,
                {"Source": "", "Sink": sink_protocols},
            )

        if action_name == "GetCurrentConnectionIDs":
            return self._soap_response(
                action_name,
                UPNP_SERVICE_CONNECTION_MANAGER,
                {"ConnectionIDs": "0"},
            )

        if action_name == "GetCurrentConnectionInfo":
            sink_protocols = ",".join(f"http-get:*:{mime}:*" for mime in SUPPORTED_MIME_TYPES)
            return self._soap_response(
                action_name,
                UPNP_SERVICE_CONNECTION_MANAGER,
                {
                    "RcsID": "0",
                    "AVTransportID": "0",
                    "ProtocolInfo": sink_protocols,
                    "PeerConnectionManager": "",
                    "PeerConnectionID": "-1",
                    "Direction": "Input",
                    "Status": "OK",
                },
            )

        LOGGER.warning("Unhandled ConnectionManager action: %s", action_name)
        return self._soap_error(401, "Invalid Action")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_xml_value(xml_str: str, tag: str) -> str | None:
        """
        Extract a value from a SOAP XML body by tag name.

        Accepts fragments (tests) or full envelopes: strips a leading
        ``<?xml ... ?>`` declaration, wraps the remainder in a synthetic
        root so ElementTree can always parse it, and searches
        namespace-agnostically via the ``{*}tag`` wildcard.

        Defence-in-depth: rejects oversized bodies and anything carrying a
        DOCTYPE/ENTITY declaration, since we parse untrusted LAN input with
        the stdlib parser (defusedxml is not yet a dependency).
        """
        if len(xml_str) > _MAX_SOAP_BODY_CHARS:
            return None
        lowered = xml_str.lower()
        if "<!doctype" in lowered or "<!entity" in lowered:
            return None
        body = _XML_DECLARATION_RE.sub("", xml_str, count=1)
        try:
            root = fromstring(f"<r>{body}</r>")  # noqa: S314
        except ParseError:
            return None
        elem = root.find(f".//{{*}}{tag}")
        if elem is None:
            return None
        return elem.text or ""

    @staticmethod
    def _soap_response(
        action_name: str,
        service_type: str,
        values: dict[str, str] | None = None,
    ) -> web.Response:
        """Build a UPnP SOAP response envelope."""
        body = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
            s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:{escape(action_name)}Response xmlns:u="{escape(service_type, _ATTR_ENTITIES)}">"""
        if values:
            for key, val in values.items():
                body += f"\n      <{key}>{escape(val)}</{key}>"
        body += f"""
    </u:{escape(action_name)}Response>
  </s:Body>
</s:Envelope>"""
        return web.Response(body=body, content_type="text/xml", charset="utf-8")

    @staticmethod
    def _soap_error(code: int, description: str) -> web.Response:
        """Build a UPnP SOAP error response."""
        body = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
            s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <s:Fault>
      <faultcode>s:Client</faultcode>
      <faultstring>UPnPError</faultstring>
      <detail>
        <UPnPError xmlns="urn:schemas-upnp-org:control-1-0">
          <errorCode>{code}</errorCode>
          <errorDescription>{escape(description)}</errorDescription>
        </UPnPError>
      </detail>
    </s:Fault>
  </s:Body>
</s:Envelope>"""
        return web.Response(
            body=body,
            status=500,
            content_type="text/xml",
            charset="utf-8",
        )

    # ------------------------------------------------------------------
    # GENA Event Subscription Handlers
    # ------------------------------------------------------------------

    async def _handle_subscribe_av_transport(
        self,
        request: web.Request,
    ) -> web.Response:
        """Handle SUBSCRIBE for AVTransport events."""
        return await self._handle_subscribe(
            request,
            self._evt_av_transport,
            self._get_av_transport_vars(),
        )

    async def _handle_unsubscribe_av_transport(
        self,
        request: web.Request,
    ) -> web.Response:
        """Handle UNSUBSCRIBE for AVTransport events."""
        return self._handle_unsubscribe(request, self._evt_av_transport)

    async def _handle_subscribe_rendering_control(
        self,
        request: web.Request,
    ) -> web.Response:
        """Handle SUBSCRIBE for RenderingControl events."""
        return await self._handle_subscribe(
            request,
            self._evt_rendering_control,
            self._get_rendering_control_vars(),
        )

    async def _handle_unsubscribe_rendering_control(
        self,
        request: web.Request,
    ) -> web.Response:
        """Handle UNSUBSCRIBE for RenderingControl events."""
        return self._handle_unsubscribe(request, self._evt_rendering_control)

    async def _handle_subscribe_connection_manager(
        self,
        request: web.Request,
    ) -> web.Response:
        """Handle SUBSCRIBE for ConnectionManager events."""
        sink_protocols = ",".join(f"http-get:*:{mime}:*" for mime in SUPPORTED_MIME_TYPES)
        initial_vars = {
            "SourceProtocolInfo": "",
            "SinkProtocolInfo": sink_protocols,
            "CurrentConnectionIDs": "0",
        }
        return await self._handle_subscribe(
            request,
            self._evt_connection_manager,
            initial_vars,
        )

    async def _handle_unsubscribe_connection_manager(
        self,
        request: web.Request,
    ) -> web.Response:
        """Handle UNSUBSCRIBE for ConnectionManager events."""
        return self._handle_unsubscribe(request, self._evt_connection_manager)

    async def _handle_subscribe(
        self,
        request: web.Request,
        manager: EventingManager,
        initial_vars: dict[str, str],
    ) -> web.Response:
        """Handle SUBSCRIBE requests for a UPnP service."""
        sid = request.headers.get("SID")

        if sid:
            # Renewal
            try:
                timeout = manager.renew(
                    sid,
                    request.headers.get("TIMEOUT"),
                )
            except KeyError:
                return web.Response(status=412, text="Invalid SID")
            return web.Response(
                status=200,
                headers={
                    "SID": sid,
                    "TIMEOUT": f"Second-{timeout}",
                },
            )

        # New subscription
        callback = request.headers.get("CALLBACK")
        if not callback:
            return web.Response(status=412, text="Missing CALLBACK header")

        try:
            sid, timeout = manager.subscribe(
                callback,
                request.headers.get("TIMEOUT"),
            )
        except ValueError as exc:
            return web.Response(status=412, text=str(exc))

        # Send initial event with current state
        await manager.notify_initial(sid, initial_vars)

        return web.Response(
            status=200,
            headers={
                "SID": sid,
                "TIMEOUT": f"Second-{timeout}",
                "Server": "UPnP/1.0 MusicAssistant/1.0",
            },
        )

    @staticmethod
    def _handle_unsubscribe(
        request: web.Request,
        manager: EventingManager,
    ) -> web.Response:
        """Handle UNSUBSCRIBE requests for a UPnP service."""
        sid = request.headers.get("SID")
        if not sid:
            return web.Response(status=412, text="Missing SID header")
        manager.unsubscribe(sid)
        return web.Response(status=200)

    # ------------------------------------------------------------------
    # Event Notification Helpers
    # ------------------------------------------------------------------

    def _get_av_transport_vars(self) -> dict[str, str]:
        """Get current AVTransport state as a LastChange XML fragment."""
        last_change = self._build_last_change(
            "urn:schemas-upnp-org:service:AVTransport:1",
            {
                "TransportState": self.transport_state,
                "TransportStatus": "OK",
                "TransportPlaySpeed": "1",
                "CurrentTrackURI": self.current_uri,
                "AVTransportURI": self.current_uri,
                "AVTransportURIMetaData": self.current_uri_metadata,
                "CurrentTrackMetaData": self.current_uri_metadata,
            },
        )
        return {"LastChange": last_change}

    def _get_rendering_control_vars(self) -> dict[str, str]:
        """Get current RenderingControl state as a LastChange XML fragment."""
        last_change = self._build_last_change(
            "urn:schemas-upnp-org:service:RenderingControl:1",
            {
                "Volume": str(self.volume),
                "Mute": "1" if self.mute else "0",
            },
            channel="Master",
        )
        return {"LastChange": last_change}

    async def _notify_av_transport_change(self) -> None:
        """Notify AVTransport subscribers of state changes."""
        await self._evt_av_transport.notify(self._get_av_transport_vars())

    async def _notify_rendering_control_change(self) -> None:
        """Notify RenderingControl subscribers of state changes."""
        await self._evt_rendering_control.notify(
            self._get_rendering_control_vars(),
        )

    @staticmethod
    def _build_last_change(
        namespace: str,
        variables: dict[str, str],
        channel: str | None = None,
    ) -> str:
        """
        Build a LastChange XML value for GENA eventing.

        The LastChange event wraps state variable changes in an
        <Event><InstanceID> structure as required by UPnP spec.
        """
        parts: list[str] = []
        for name, value in variables.items():
            attrs = f'val="{escape(value, _ATTR_ENTITIES)}"'
            if channel:
                attrs += f' channel="{channel}"'
            parts.append(f"<{name} {attrs}/>")

        return (
            f'<Event xmlns="{namespace}"><InstanceID val="0">{"".join(parts)}</InstanceID></Event>'
        )
