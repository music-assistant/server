"""DLNA Server Plugin Provider for Music Assistant.

This plugin provider exposes Music Assistant as a DLNA/UPnP Media Server,
allowing DLNA clients on the network to browse and play the MA library.
"""

from __future__ import annotations

import urllib.parse
import uuid
from typing import TYPE_CHECKING, Any

from aiohttp import web
from defusedxml import ElementTree as DefusedET
from music_assistant_models.enums import ContentType, ProviderFeature
from music_assistant_models.errors import (
    MediaNotFoundError,
    MusicAssistantError,
    ProviderUnavailableError,
    SetupFailedError,
)
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import DEFAULT_STREAM_HEADERS, INTERNAL_PCM_FORMAT
from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.plugin import PluginProvider

from .ssdp import SSDPServer

if TYPE_CHECKING:
    from xml.etree.ElementTree import Element

    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.media_items import Album, Artist, MediaItemImage, Track
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES: set[ProviderFeature] = (
    set()
)  # we don't have any special supported features (yet)


# DLNA/UPnP constants
DEVICE_TYPE = "urn:schemas-upnp-org:device:MediaServer:1"
SERVICE_CONTENT_DIRECTORY = "urn:schemas-upnp-org:service:ContentDirectory:1"
SERVICE_CONNECTION_MANAGER = "urn:schemas-upnp-org:service:ConnectionManager:1"

# DLNA object IDs
ROOT_ID = "0"
ARTISTS_CONTAINER_ID = "artists"
ALBUMS_CONTAINER_ID = "albums"
TRACKS_CONTAINER_ID = "tracks"

# TODO: Implement these containers in future versions

# PLAYLISTS_CONTAINER_ID = "playlists"
# RADIO_CONTAINER_ID = "radio"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    provider = DLNAServerProvider(mass, manifest, config, SUPPORTED_FEATURES)
    await provider.handle_async_init()
    return provider


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, str] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries for this provider."""
    # No configuration needed for DLNA server
    return ()


class DLNAServerProvider(PluginProvider):
    """DLNA Server Plugin Provider for Music Assistant."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the DLNA server provider."""
        super().__init__(*args, **kwargs)
        self._ssdp_server: SSDPServer | None = None
        self._server_uuid: str = str(uuid.uuid4())
        self._friendly_name = "Music Assistant"
        self._routes_registered = False

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded into Music Assistant."""
        try:
            # Register HTTP endpoints with the streams controller
            await self._register_http_routes()

            # Start SSDP server for device discovery
            await self._start_ssdp_server()

            self.logger.info(
                "DLNA Server started successfully - "
                "Music Assistant is now discoverable as a DLNA Media Server"
            )
        except OSError as err:  # Socket/network errors
            self.logger.exception("Failed to start DLNA server due to network error")
            raise SetupFailedError(f"Failed to start DLNA server: {err}") from err
        except Exception as err:  # Unexpected errors
            self.logger.exception("Failed to start DLNA server")
            raise SetupFailedError("Failed to start DLNA server") from err

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        # Unregister HTTP routes first
        if self._routes_registered:
            self.mass.streams.unregister_dynamic_route("/dlna/description.xml")
            self.mass.streams.unregister_dynamic_route("/dlna/ContentDirectory.xml")
            self.mass.streams.unregister_dynamic_route("/dlna/ConnectionManager.xml")
            self.mass.streams.unregister_dynamic_route("/dlna/control/ContentDirectory")
            self.mass.streams.unregister_dynamic_route("/dlna/control/ConnectionManager")
            self.mass.streams.unregister_dynamic_route("/dlna/track/*")

        # Then stop SSDP server
        if self._ssdp_server:
            await self._ssdp_server.stop()

        self.logger.info("DLNA Server stopped")

    async def _register_http_routes(self) -> None:
        """Register HTTP routes with the streams controller."""
        # Device description
        self.mass.streams.register_dynamic_route(
            "/dlna/description.xml",
            self._handle_device_description,
            "GET",
        )

        # Service descriptions
        self.mass.streams.register_dynamic_route(
            "/dlna/ContentDirectory.xml",
            self._handle_content_directory_scpd,
            "GET",
        )
        self.mass.streams.register_dynamic_route(
            "/dlna/ConnectionManager.xml",
            self._handle_connection_manager_scpd,
            "GET",
        )

        # Control endpoints (SOAP)
        self.mass.streams.register_dynamic_route(
            "/dlna/control/ContentDirectory",
            self._handle_content_directory_control,
            "POST",
        )
        self.mass.streams.register_dynamic_route(
            "/dlna/control/ConnectionManager",
            self._handle_connection_manager_control,
            "POST",
        )

        # Media streaming endpoint
        self.mass.streams.register_dynamic_route(
            "/dlna/track/*",
            self._handle_track_stream,
            "GET",
        )
        self.mass.streams.register_dynamic_route(
            "/dlna/track/*",
            self._handle_track_stream,
            "HEAD",
        )
        # Event subscription endpoint (return not implemented)
        self.mass.streams.register_dynamic_route(
            "/dlna/event/ContentDirectory",
            self._handle_event_subscription,
            "SUBSCRIBE",
        )
        self._routes_registered = True
        self.logger.debug("DLNA HTTP routes registered")

    async def _handle_event_subscription(self, request: web.Request) -> web.Response:
        """Handle event subscription requests (not implemented)."""
        self.logger.debug("Event subscription requested but not implemented")
        return web.Response(status=501, text="Event subscription not implemented")

    async def _start_ssdp_server(self) -> None:
        """Start the SSDP server for device discovery."""
        base_url = self.mass.streams.base_url
        location = f"{base_url}/dlna/description.xml"

        self._ssdp_server = SSDPServer(
            location=location,
            server_uuid=self._server_uuid,
            friendly_name=self._friendly_name,
            logger=self.logger,
        )

        await self._ssdp_server.start()
        self.logger.info("SSDP advertisement started - devices can now discover this server")

    # ==================== HTTP Handlers ====================

    async def _handle_device_description(self, request: web.Request) -> web.Response:
        """Handle device description request."""
        base_url = self.mass.streams.base_url

        device_xml = f"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
    <specVersion>
        <major>1</major>
        <minor>0</minor>
    </specVersion>
    <device>
        <deviceType>{DEVICE_TYPE}</deviceType>
        <friendlyName>{self._friendly_name}</friendlyName>
        <manufacturer>Music Assistant</manufacturer>
        <manufacturerURL>https://music-assistant.io</manufacturerURL>
        <modelDescription>Music Assistant DLNA Media Server</modelDescription>
        <modelName>Music Assistant</modelName>
        <modelNumber>1.0</modelNumber>
        <modelURL>https://music-assistant.io</modelURL>
        <serialNumber>1</serialNumber>
        <UDN>uuid:{self._server_uuid}</UDN>
        <presentationURL>{base_url}</presentationURL>
        <serviceList>
            <service>
                <serviceType>{SERVICE_CONTENT_DIRECTORY}</serviceType>
                <serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>
                <SCPDURL>/dlna/ContentDirectory.xml</SCPDURL>
                <controlURL>/dlna/control/ContentDirectory</controlURL>
                <eventSubURL>/dlna/event/ContentDirectory</eventSubURL>
            </service>
            <service>
                <serviceType>{SERVICE_CONNECTION_MANAGER}</serviceType>
                <serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>
                <SCPDURL>/dlna/ConnectionManager.xml</SCPDURL>
                <controlURL>/dlna/control/ConnectionManager</controlURL>
                <eventSubURL>/dlna/event/ConnectionManager</eventSubURL>
            </service>
        </serviceList>
    </device>
</root>"""

        return web.Response(
            text=device_xml,
            content_type="text/xml",
            charset="utf-8",
        )

    async def _handle_content_directory_scpd(self, request: web.Request) -> web.Response:
        """Handle ContentDirectory service description request."""
        scpd_xml = """<?xml version="1.0"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
    <specVersion>
        <major>1</major>
        <minor>0</minor>
    </specVersion>
    <actionList>
        <action>
            <name>Browse</name>
            <argumentList>
                <argument>
                    <name>ObjectID</name>
                    <direction>in</direction>
                    <relatedStateVariable>A_ARG_TYPE_ObjectID</relatedStateVariable>
                </argument>
                <argument>
                    <name>BrowseFlag</name>
                    <direction>in</direction>
                    <relatedStateVariable>A_ARG_TYPE_BrowseFlag</relatedStateVariable>
                </argument>
                <argument>
                    <name>Filter</name>
                    <direction>in</direction>
                    <relatedStateVariable>A_ARG_TYPE_Filter</relatedStateVariable>
                </argument>
                <argument>
                    <name>StartingIndex</name>
                    <direction>in</direction>
                    <relatedStateVariable>A_ARG_TYPE_Index</relatedStateVariable>
                </argument>
                <argument>
                    <name>RequestedCount</name>
                    <direction>in</direction>
                    <relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable>
                </argument>
                <argument>
                    <name>SortCriteria</name>
                    <direction>in</direction>
                    <relatedStateVariable>A_ARG_TYPE_SortCriteria</relatedStateVariable>
                </argument>
                <argument>
                    <name>Result</name>
                    <direction>out</direction>
                    <relatedStateVariable>A_ARG_TYPE_Result</relatedStateVariable>
                </argument>
                <argument>
                    <name>NumberReturned</name>
                    <direction>out</direction>
                    <relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable>
                </argument>
                <argument>
                    <name>TotalMatches</name>
                    <direction>out</direction>
                    <relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable>
                </argument>
                <argument>
                    <name>UpdateID</name>
                    <direction>out</direction>
                    <relatedStateVariable>A_ARG_TYPE_UpdateID</relatedStateVariable>
                </argument>
            </argumentList>
        </action>
        <action>
            <name>GetSystemUpdateID</name>
            <argumentList>
                <argument>
                    <name>Id</name>
                    <direction>out</direction>
                    <relatedStateVariable>SystemUpdateID</relatedStateVariable>
                </argument>
            </argumentList>
        </action>
    </actionList>
    <serviceStateTable>
        <stateVariable sendEvents="no">
            <name>A_ARG_TYPE_ObjectID</name>
            <dataType>string</dataType>
        </stateVariable>
        <stateVariable sendEvents="no">
            <name>A_ARG_TYPE_Result</name>
            <dataType>string</dataType>
        </stateVariable>
        <stateVariable sendEvents="no">
            <name>A_ARG_TYPE_BrowseFlag</name>
            <dataType>string</dataType>
            <allowedValueList>
                <allowedValue>BrowseMetadata</allowedValue>
                <allowedValue>BrowseDirectChildren</allowedValue>
            </allowedValueList>
        </stateVariable>
        <stateVariable sendEvents="no">
            <name>A_ARG_TYPE_Filter</name>
            <dataType>string</dataType>
        </stateVariable>
        <stateVariable sendEvents="no">
            <name>A_ARG_TYPE_SortCriteria</name>
            <dataType>string</dataType>
        </stateVariable>
        <stateVariable sendEvents="no">
            <name>A_ARG_TYPE_Index</name>
            <dataType>ui4</dataType>
        </stateVariable>
        <stateVariable sendEvents="no">
            <name>A_ARG_TYPE_Count</name>
            <dataType>ui4</dataType>
        </stateVariable>
        <stateVariable sendEvents="no">
            <name>A_ARG_TYPE_UpdateID</name>
            <dataType>ui4</dataType>
        </stateVariable>
        <stateVariable sendEvents="yes">
            <name>SystemUpdateID</name>
            <dataType>ui4</dataType>
        </stateVariable>
    </serviceStateTable>
</scpd>"""

        return web.Response(
            text=scpd_xml,
            content_type="text/xml",
            charset="utf-8",
        )

    async def _handle_connection_manager_scpd(self, request: web.Request) -> web.Response:
        """Handle ConnectionManager service description request."""
        scpd_xml = """<?xml version="1.0"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
    <specVersion>
        <major>1</major>
        <minor>0</minor>
    </specVersion>
    <actionList>
        <action>
            <name>GetProtocolInfo</name>
            <argumentList>
                <argument>
                    <name>Source</name>
                    <direction>out</direction>
                    <relatedStateVariable>SourceProtocolInfo</relatedStateVariable>
                </argument>
                <argument>
                    <name>Sink</name>
                    <direction>out</direction>
                    <relatedStateVariable>SinkProtocolInfo</relatedStateVariable>
                </argument>
            </argumentList>
        </action>
    </actionList>
    <serviceStateTable>
        <stateVariable sendEvents="yes">
            <name>SourceProtocolInfo</name>
            <dataType>string</dataType>
        </stateVariable>
        <stateVariable sendEvents="yes">
            <name>SinkProtocolInfo</name>
            <dataType>string</dataType>
        </stateVariable>
    </serviceStateTable>
</scpd>"""

        return web.Response(
            text=scpd_xml,
            content_type="text/xml",
            charset="utf-8",
        )

    async def _handle_content_directory_control(self, request: web.Request) -> web.Response:
        """Handle ContentDirectory SOAP control requests."""
        try:
            body = await request.text()
            # Parse SOAP request using defusedxml
            root = DefusedET.fromstring(body)

            # Find the action
            action_elem = None
            action = None
            for elem in root.iter():
                if elem.tag.endswith("Browse"):
                    action_elem = elem
                    action = "Browse"
                    break
                if elem.tag.endswith("GetSystemUpdateID"):
                    action = "GetSystemUpdateID"
                    action_elem = elem
                    break

            if action_elem is None or len(action_elem) == 0:
                return self._soap_error(401, "Invalid Action")

            # Handle the action
            if action == "Browse":
                return await self._handle_browse_action(action_elem)
            if action == "GetSystemUpdateID":
                return await self._handle_get_system_update_id()

            return self._soap_error(401, "Invalid Action")

        except DefusedET.ParseError as err:
            self.logger.warning("Invalid XML in SOAP request: %s", err)
            return self._soap_error(400, "Invalid XML")
        except Exception:
            self.logger.exception("Error handling ContentDirectory control request")
            return self._soap_error(500, "Internal server error")

    async def _handle_connection_manager_control(self, request: web.Request) -> web.Response:
        """Handle ConnectionManager SOAP control requests."""
        try:
            body = await request.text()

            # Parse SOAP request
            root = DefusedET.fromstring(body)

            # Check for GetProtocolInfo action
            for elem in root.iter():
                if elem.tag.endswith("GetProtocolInfo"):
                    return await self._handle_get_protocol_info()

            return self._soap_error(401, "Invalid Action")

        except DefusedET.ParseError as err:
            self.logger.warning("Invalid XML in SOAP request: %s", err)
            return self._soap_error(400, "Invalid XML")
        except Exception:
            self.logger.exception("Error handling ConnectionManager control request")
            return self._soap_error(500, "Internal server error")

    async def _handle_browse_action(self, action_elem: Element) -> web.Response:
        """Handle Browse SOAP action."""
        # Extract parameters
        object_id = self._get_soap_param(action_elem, "ObjectID") or ROOT_ID
        browse_flag = self._get_soap_param(action_elem, "BrowseFlag") or "BrowseDirectChildren"
        starting_index = int(self._get_soap_param(action_elem, "StartingIndex") or "0")
        requested_count = int(self._get_soap_param(action_elem, "RequestedCount") or "0")

        self.logger.info(
            "Browse: ObjectID=%s, BrowseFlag=%s, StartingIndex=%d, RequestedCount=%d",
            object_id,
            browse_flag,
            starting_index,
            requested_count,
        )

        # Generate DIDL response
        if browse_flag == "BrowseMetadata":
            didl_xml, number_returned, total_matches = await self._get_object_metadata(object_id)
        else:  # BrowseDirectChildren
            didl_xml, number_returned, total_matches = await self._get_children(
                object_id, starting_index, requested_count
            )

        # Log response details for debugging
        self.logger.info(
            "Browse response: NumberReturned=%d, TotalMatches=%d",
            number_returned,
            total_matches,
        )

        # Build SOAP response
        response_xml = f"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
    <s:Body>
        <u:BrowseResponse xmlns:u="{SERVICE_CONTENT_DIRECTORY}">
            <Result>{self._escape_xml(didl_xml)}</Result>
            <NumberReturned>{number_returned}</NumberReturned>
            <TotalMatches>{total_matches}</TotalMatches>
            <UpdateID>0</UpdateID>
        </u:BrowseResponse>
    </s:Body>
</s:Envelope>"""

        return web.Response(
            text=response_xml,
            content_type="text/xml",
            charset="utf-8",
            headers={
                "EXT": "",  # Required empty header for DLNA
            },
        )

    async def _handle_get_system_update_id(self) -> web.Response:
        """Handle GetSystemUpdateID action."""
        response_xml = f"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
    <s:Body>
        <u:GetSystemUpdateIDResponse xmlns:u="{SERVICE_CONTENT_DIRECTORY}">
            <Id>0</Id>
        </u:GetSystemUpdateIDResponse>
    </s:Body>
</s:Envelope>"""

        return web.Response(
            text=response_xml,
            content_type="text/xml",
            charset="utf-8",
        )

    async def _handle_get_protocol_info(self) -> web.Response:
        """Handle GetProtocolInfo action."""
        # Advertise supported formats
        protocol_info = (
            "http-get:*:audio/mpeg:*,"
            "http-get:*:audio/mp4:*,"
            "http-get:*:audio/flac:*,"
            "http-get:*:audio/x-flac:*"
        )

        response_xml = f"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
    <s:Body>
        <u:GetProtocolInfoResponse xmlns:u="{SERVICE_CONNECTION_MANAGER}">
            <Source>{protocol_info}</Source>
            <Sink></Sink>
        </u:GetProtocolInfoResponse>
    </s:Body>
</s:Envelope>"""

        return web.Response(
            text=response_xml,
            content_type="text/xml",
            charset="utf-8",
        )

    async def _handle_track_stream(self, request: web.Request) -> web.StreamResponse:
        """Handle track streaming request using the streams controller."""
        # Parse path: /dlna/track/{provider}/{item_id}.{fmt}
        path_parts = request.path.split("/")
        if len(path_parts) < 5:
            raise web.HTTPBadRequest(reason="Invalid path")

        filename = path_parts[4]
        item_id, output_format_str = filename.rsplit(".", 1)

        self.logger.info(
            "Stream request: item_id=%s, format=%s, method=%s",
            item_id,
            output_format_str,
            request.method,
        )

        try:
            # Get the track from library
            track = await self.mass.music.tracks.get_library_item(item_id)

            # Get stream details from the best available provider
            # Sort by quality and find the first available provider
            streamdetails = None
            for prov_mapping in sorted(
                track.provider_mappings, key=lambda x: x.quality or 0, reverse=True
            ):
                if not prov_mapping.available:
                    continue
                prov = self.mass.get_provider(prov_mapping.provider_instance)
                if not prov or not isinstance(prov, MusicProvider):
                    continue
                try:
                    streamdetails = await prov.get_stream_details(
                        prov_mapping.item_id, track.media_type
                    )
                    break
                except Exception as err:
                    self.logger.debug(
                        "Failed to get stream details from %s: %s",
                        prov_mapping.provider_instance,
                        err,
                    )
                    continue

            if not streamdetails:
                raise ProviderUnavailableError("No provider available for this track")

            # Determine output format based on request
            output_format = self._get_output_format(output_format_str, streamdetails)

            # Prepare response headers with DLNA-compatible settings
            headers = {
                **DEFAULT_STREAM_HEADERS,
                "Content-Type": f"audio/{output_format.output_format_str}",
                "icy-name": track.name,
                "Accept-Ranges": "none",
            }

            resp = web.StreamResponse(
                status=200,
                reason="OK",
                headers=headers,
            )
            await resp.prepare(request)

            # Return early for HEAD requests
            if request.method != "GET":
                return resp

            # Define PCM format for internal processing
            pcm_format = AudioFormat(
                sample_rate=streamdetails.audio_format.sample_rate,
                content_type=INTERNAL_PCM_FORMAT.content_type,
                bit_depth=INTERNAL_PCM_FORMAT.bit_depth,
                channels=streamdetails.audio_format.channels,
            )

            # Get raw PCM audio stream from the media
            audio_input = self.mass.streams.audio.get_media_stream(
                streamdetails=streamdetails,
                pcm_format=pcm_format,
            )

            # Convert to output format using ffmpeg and stream to client
            async for chunk in get_ffmpeg_stream(
                audio_input=audio_input,
                input_format=pcm_format,
                output_format=output_format,
            ):
                try:
                    await resp.write(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionError):
                    self.logger.debug("DLNA client disconnected during streaming")
                    break

            return resp

        except MediaNotFoundError:
            raise web.HTTPNotFound(reason="Track not found")
        except ProviderUnavailableError as err:
            self.logger.warning("Provider unavailable for track streaming: %s", err)
            raise web.HTTPServiceUnavailable(reason="Provider unavailable")
        except MusicAssistantError as err:
            self.logger.warning("Error streaming track: %s", err)
            raise web.HTTPInternalServerError(reason="Failed to stream track")
        except Exception:
            self.logger.exception("Unexpected error streaming track")
            raise web.HTTPInternalServerError(reason="Failed to stream track")

    def _get_output_format(
        self, output_format_str: str, streamdetails: StreamDetails
    ) -> AudioFormat:
        """Determine output format for DLNA streaming.

        :param output_format_str: Requested format string (e.g., 'mp3', 'flac').
        :param streamdetails: Stream details for the track.
        """
        # Map format strings to content types
        format_map = {
            "mp3": ContentType.MP3,
            "flac": ContentType.FLAC,
            "wav": ContentType.WAV,
            "m4a": ContentType.M4A,
            "aac": ContentType.AAC,
            "ogg": ContentType.OGG,
        }

        content_type = format_map.get(output_format_str.lower(), ContentType.FLAC)

        # For lossy formats, limit quality
        if content_type in (ContentType.MP3, ContentType.AAC, ContentType.OGG):
            sample_rate = min(streamdetails.audio_format.sample_rate, 48000)
            bit_depth = 16
        else:
            sample_rate = streamdetails.audio_format.sample_rate
            bit_depth = streamdetails.audio_format.bit_depth or 16

        return AudioFormat(
            content_type=content_type,
            sample_rate=sample_rate,
            bit_depth=bit_depth,
            channels=streamdetails.audio_format.channels,
        )

    # ==================== DIDL/XML Helpers ====================

    async def _get_object_metadata(self, object_id: str) -> tuple[str, int, int]:
        """Get metadata for a specific object."""
        if object_id == ROOT_ID:
            didl_xml = self._create_root_container()
            return didl_xml, 1, 1

        # Parse object_id to determine type
        if object_id.startswith("artist_"):
            artist_id = object_id[7:]  # Remove "artist_" prefix
            try:
                artist = await self.mass.music.artists.get_library_item(artist_id)
                didl_xml = self._create_artist_container(artist)
                return didl_xml, 1, 1
            except MediaNotFoundError:
                return self._create_empty_didl(), 0, 0

        if object_id.startswith("album_"):
            album_id = object_id[6:]  # Remove "album_" prefix
            try:
                album = await self.mass.music.albums.get_library_item(album_id)
                didl_xml = self._create_album_container(album)
                return didl_xml, 1, 1
            except MediaNotFoundError:
                return self._create_empty_didl(), 0, 0

        if object_id.startswith("track_"):
            track_id = object_id[6:]  # Remove "track_" prefix
            try:
                track = await self.mass.music.tracks.get_library_item(track_id)
                didl_xml = self._create_track_item(track)
                return didl_xml, 1, 1
            except MediaNotFoundError:
                return self._create_empty_didl(), 0, 0

        return self._create_empty_didl(), 0, 0

    async def _get_children(
        self, parent_id: str, starting_index: int, requested_count: int
    ) -> tuple[str, int, int]:
        """Get children of a container."""
        limit = requested_count if requested_count > 0 else 500
        offset = starting_index

        if parent_id == ROOT_ID:
            return self._get_root_children()

        if parent_id == ARTISTS_CONTAINER_ID:
            return await self._get_artists_children(offset, limit)

        if parent_id == ALBUMS_CONTAINER_ID:
            return await self._get_albums_children(offset, limit)

        if parent_id == TRACKS_CONTAINER_ID:
            return await self._get_tracks_children(offset, limit)

        if parent_id.startswith("artist_"):
            return await self._get_artist_children(parent_id[7:], offset, limit)

        if parent_id.startswith("album_"):
            return await self._get_album_children(parent_id[6:], offset, limit)

        return self._create_empty_didl(), 0, 0

    def _get_root_children(self) -> tuple[str, int, int]:
        """Get root container children."""
        containers = [
            self._create_artists_root_container(),
            self._create_albums_root_container(),
            self._create_tracks_root_container(),
        ]
        didl_xml = self._wrap_didl_items(containers)
        return didl_xml, len(containers), len(containers)

    async def _get_artists_children(self, offset: int, limit: int) -> tuple[str, int, int]:
        """Get all artists with pagination."""
        # Use iter_library_items to get all artists with consistent filtering
        # This ensures TotalMatches equals the actual number of items available.
        all_artists = [
            artist
            async for artist in self.mass.music.artists.iter_library_items(order_by="sort_name")
        ]
        total = len(all_artists)
        # Debug: also check what library_count returns for comparison
        raw_count = await self.mass.music.artists.library_count()
        self.logger.debug(
            "Artists: iter_library_items returned %d, library_count returned %d",
            total,
            raw_count,
        )
        paginated = all_artists[offset : offset + limit] if limit > 0 else all_artists[offset:]
        items = [self._create_artist_container(artist) for artist in paginated]
        return self._wrap_didl_items(items), len(items), total

    async def _get_albums_children(self, offset: int, limit: int) -> tuple[str, int, int]:
        """Get all albums with pagination."""
        all_albums = [
            album async for album in self.mass.music.albums.iter_library_items(order_by="sort_name")
        ]
        total = len(all_albums)
        # Debug: also check what library_count returns for comparison
        raw_count = await self.mass.music.albums.library_count()
        self.logger.debug(
            "Albums: iter_library_items returned %d, library_count returned %d",
            total,
            raw_count,
        )
        paginated = all_albums[offset : offset + limit] if limit > 0 else all_albums[offset:]
        items = [self._create_album_container(album, ALBUMS_CONTAINER_ID) for album in paginated]
        return self._wrap_didl_items(items), len(items), total

    async def _get_tracks_children(self, offset: int, limit: int) -> tuple[str, int, int]:
        """Get all tracks with pagination."""
        all_tracks = [
            track async for track in self.mass.music.tracks.iter_library_items(order_by="sort_name")
        ]
        total = len(all_tracks)
        paginated = all_tracks[offset : offset + limit] if limit > 0 else all_tracks[offset:]
        items = [self._create_track_item(track) for track in paginated]
        return self._wrap_didl_items(items), len(items), total

    async def _get_artist_children(
        self, artist_id: str, offset: int, limit: int
    ) -> tuple[str, int, int]:
        """Get albums of an artist."""
        self.logger.debug("Fetching albums for artist_id=%s", artist_id)
        albums = await self.mass.music.artists.albums(artist_id, "library", in_library_only=True)
        albums_list = list(albums)
        self.logger.debug("Artist %s has %d albums", artist_id, len(albums_list))
        paginated = albums_list[offset : offset + limit] if limit > 0 else albums_list[offset:]
        items = [self._create_album_container(album) for album in paginated]
        return self._wrap_didl_items(items), len(items), len(albums_list)

    async def _get_album_children(
        self, album_id: str, offset: int, limit: int
    ) -> tuple[str, int, int]:
        """Get tracks of an album."""
        self.logger.debug("Fetching tracks for album_id=%s", album_id)
        tracks = await self.mass.music.albums.tracks(album_id, "library", in_library_only=True)
        tracks_list = list(tracks)
        self.logger.debug("Album %s has %d tracks", album_id, len(tracks_list))
        paginated = tracks_list[offset : offset + limit] if limit > 0 else tracks_list[offset:]
        items = [self._create_track_item(track) for track in paginated]
        return self._wrap_didl_items(items), len(items), len(tracks_list)

    def _create_root_container(self) -> str:
        """Create DIDL-Lite XML for root container."""
        return (
            '<container id="0" parentID="-1" restricted="1" '
            'childCount="3" searchable="0">\n'
            "    <dc:title>Music Assistant</dc:title>\n"
            "    <upnp:class>object.container</upnp:class>\n"
            "</container>"
        )

    def _create_artists_root_container(self) -> str:
        """Create DIDL-Lite XML for Artists root container."""
        return (
            f'<container id="{ARTISTS_CONTAINER_ID}" parentID="{ROOT_ID}" '
            f'restricted="1" childCount="0" searchable="0">\n'
            f"    <dc:title>Artists</dc:title>\n"
            f"    <upnp:class>object.container</upnp:class>\n"
            f"</container>"
        )

    def _create_albums_root_container(self) -> str:
        """Create DIDL-Lite XML for Albums root container."""
        return (
            f'<container id="{ALBUMS_CONTAINER_ID}" parentID="{ROOT_ID}" '
            f'restricted="1" childCount="0" searchable="0">\n'
            f"    <dc:title>Albums</dc:title>\n"
            f"    <upnp:class>object.container</upnp:class>\n"
            f"</container>"
        )

    def _create_tracks_root_container(self) -> str:
        """Create DIDL-Lite XML for Tracks root container."""
        return (
            f'<container id="{TRACKS_CONTAINER_ID}" parentID="{ROOT_ID}" '
            f'restricted="1" childCount="0" searchable="0">\n'
            f"    <dc:title>Tracks</dc:title>\n"
            f"    <upnp:class>object.container</upnp:class>\n"
            f"</container>"
        )

    def _create_artist_container(self, artist: Artist, child_count: int = 1) -> str:
        """Create DIDL-Lite XML for an artist container.

        :param artist: The artist object.
        :param child_count: Number of child items (albums). Defaults to 1 to show expand arrow.
        """
        artist_id = f"artist_{artist.item_id}"
        title = self._escape_xml(artist.name)

        # Add album art if available
        album_art_xml = ""
        if artist.image and artist.image.path:
            image_url = self._get_image_url(artist.image)
            album_art_xml = (
                f"\n    <upnp:albumArtURI>{self._escape_xml(image_url)}</upnp:albumArtURI>"
            )

        return (
            f'<container id="{artist_id}" parentID="{ARTISTS_CONTAINER_ID}" '
            f'restricted="1" childCount="{child_count}" searchable="0">\n'
            f"    <dc:title>{title}</dc:title>\n"
            f"    <upnp:class>object.container.person.musicArtist</upnp:class>"
            f"{album_art_xml}\n"
            f"</container>"
        )

    def _create_album_container(
        self, album: Album, parent_id: str | None = None, child_count: int = 1
    ) -> str:
        """Create DIDL-Lite XML for an album container.

        :param album: The album object.
        :param parent_id: Parent container ID. Defaults to artist parent.
        :param child_count: Number of child items (tracks). Defaults to 1 to show expand arrow.
        """
        album_id = f"album_{album.item_id}"

        # Use provided parent_id, or default to artist parent
        if parent_id is None:
            parent_id = (
                f"artist_{album.artists[0].item_id}" if album.artists else ARTISTS_CONTAINER_ID
            )
        title = self._escape_xml(album.name)

        # Add album art if available
        album_art_xml = ""
        if album.image and album.image.path:
            image_url = self._get_image_url(album.image)
            album_art_xml = (
                f"\n    <upnp:albumArtURI>{self._escape_xml(image_url)}</upnp:albumArtURI>"
            )

        # Add artist
        artist_xml = ""
        if album.artists:
            artist_name = self._escape_xml(album.artists[0].name)
            artist_xml = f"\n    <upnp:artist>{artist_name}</upnp:artist>"

        return (
            f'<container id="{album_id}" parentID="{parent_id}" '
            f'restricted="1" childCount="{child_count}" searchable="0">\n'
            f"    <dc:title>{title}</dc:title>\n"
            f"    <upnp:class>object.container.album.musicAlbum</upnp:class>"
            f"{artist_xml}{album_art_xml}\n"
            f"</container>"
        )

    def _create_track_item(self, track: Track) -> str:
        """Create DIDL-Lite XML for a track item."""
        track_id = f"track_{track.item_id}"
        parent_id = f"album_{track.album.item_id}" if track.album else ROOT_ID
        title = self._escape_xml(track.name)

        # Use generic extension - actual file type determined during streaming
        # in _handle_track_stream. The extension in the URL is cosmetic; the
        # actual file is served based on stream details
        file_ext = "mp3"
        mime_type = "audio/mpeg"

        # Build stream URL
        stream_url = f"{self.mass.streams.base_url}/dlna/track/library/{track.item_id}.{file_ext}"

        # Build protocol info
        protocol_info = f"http-get:*:{mime_type}:*"

        self.logger.debug("Generated DIDL for track %s:\n%s", track.item_id, stream_url)

        # Get metadata for res attributes
        bitrate = 0
        sample_rate = 44100
        channels = 2
        file_size = 0  # Unknown without stream details

        if track.metadata:
            bitrate = getattr(track.metadata, "bitrate", 0) or 0
            sample_rate = getattr(track.metadata, "sample_rate", 44100) or 44100
            channels = getattr(track.metadata, "channels", 2) or 2

        # Add artist and creator
        artist_xml = ""
        creator_xml = ""
        if track.artists:
            artist_name = self._escape_xml(track.artists[0].name)
            artist_xml = f"<upnp:artist>{artist_name}</upnp:artist>"
            creator_xml = f"<dc:creator>{artist_name}</dc:creator>"

        # Add album
        album_xml = ""
        if track.album:
            album_name = self._escape_xml(track.album.name)
            album_xml = f"<upnp:album>{album_name}</upnp:album>"

        # Add track number
        track_number_xml = ""
        if track.track_number:
            track_number_xml = (
                f"<upnp:originalTrackNumber>{track.track_number}</upnp:originalTrackNumber>"
            )

        # Add release year (optional)
        date_xml = ""
        if track.album:
            album_year = getattr(track.album, "year", None)
            if album_year:
                date_xml = f"<dc:date>{album_year}-01-01</dc:date>"

        # Add album art if available
        album_art_xml = ""
        if track.image and track.image.path:
            self.logger.debug(
                "Track %s image path: %s (remotely_accessible: %s)",
                track.name,
                track.image.path,
                track.image.remotely_accessible,
            )
            image_url = self._get_image_url(track.image)
            if image_url:
                album_art_xml = (
                    f"<upnp:albumArtURI>{self._escape_xml(image_url)}</upnp:albumArtURI>"
                )

        # Duration in H:MM:SS format
        duration_str = self._format_duration(track.duration)

        # Build res element with all attributes
        file_size_attr = f'size="{file_size}" ' if file_size else ""

        res_element = (
            f'<res protocolInfo="{protocol_info}" '
            f"{file_size_attr}"
            f'bitrate="{bitrate}" '
            f'duration="{duration_str}" '
            f'nrAudioChannels="{channels}" '
            f'sampleFrequency="{sample_rate}">'
            f"{self._escape_xml(stream_url)}</res>"
        )

        return f"""<item id="{track_id}" parentID="{parent_id}" restricted="1">
        <dc:title>{title}</dc:title>
        <upnp:class>object.item.audioItem.musicTrack</upnp:class>
        {date_xml}
        {artist_xml}
        {creator_xml}
        {album_xml}
        {track_number_xml}
        {album_art_xml}
        {res_element}
    </item>"""

    def _create_empty_didl(self) -> str:
        """Create empty DIDL-Lite XML."""
        return (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            "</DIDL-Lite>"
        )

    def _get_image_url(self, image: MediaItemImage) -> str:
        """Get the URL for an image.

        :param image: The MediaItemImage to get URL for.
        """
        if image.remotely_accessible:
            return image.path

        # Use the imageproxy endpoint for local images
        # The path must be double-encoded to match the metadata controller's format
        encoded_path = urllib.parse.quote_plus(urllib.parse.quote_plus(image.path))
        return (
            f"{self.mass.webserver.base_url}/imageproxy"
            f"?provider={image.provider}&path={encoded_path}"
        )

    def _format_duration(self, duration_seconds: int) -> str:
        """Format duration in seconds to H:MM:SS format."""
        if not duration_seconds:
            return "0:00:00"
        hours = duration_seconds // 3600
        minutes = (duration_seconds % 3600) // 60
        seconds = duration_seconds % 60
        return f"{hours}:{minutes:02d}:{seconds:02d}"

    def _escape_xml(self, text: str) -> str:
        """Escape XML special characters."""
        if not text:
            return ""
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;")
        )

    def _wrap_didl_items(self, items: list[str]) -> str:
        """Wrap DIDL items in DIDL-Lite container."""
        items_xml = "\n".join(items)
        return (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            f"{items_xml}"
            "</DIDL-Lite>"
        )

    def _get_soap_param(self, action_elem: Element, param_name: str) -> str | None:
        """Extract a parameter from SOAP action element."""
        for elem in action_elem:
            if elem.tag.endswith(param_name):
                return elem.text
        return None

    def _soap_error(self, error_code: int, error_description: str) -> web.Response:
        """Create a SOAP error response."""
        error_xml = f"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
    <s:Body>
        <s:Fault>
            <faultcode>s:Client</faultcode>
            <faultstring>UPnPError</faultstring>
            <detail>
                <UPnPError xmlns="urn:schemas-upnp-org:control-1-0">
                    <errorCode>{error_code}</errorCode>
                    <errorDescription>{error_description}</errorDescription>
                </UPnPError>
            </detail>
        </s:Fault>
    </s:Body>
</s:Envelope>"""

        return web.Response(
            text=error_xml,
            content_type="text/xml",
            charset="utf-8",
            status=500,
        )
