"""DLNA Server Plugin Provider for Music Assistant.

This plugin provider exposes Music Assistant as a DLNA/UPnP Media Server,
allowing DLNA clients on the network to browse and play the MA library.
"""

from __future__ import annotations

import urllib.parse
import uuid
from typing import TYPE_CHECKING, Any, cast

from aiohttp import web
from defusedxml import ElementTree as DefusedET
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import (
    MediaNotFoundError,
    ProviderUnavailableError,
    SetupFailedError,
    UnsupportedFeaturedException,
)

from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.plugin import PluginProvider

from .ssdp import SSDPServer

if TYPE_CHECKING:
    from xml.etree.ElementTree import Element

    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.media_items import Album, Artist, MediaItemImage, Track
    from music_assistant_models.provider import ProviderManifest

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
        self.is_streaming_provider = False

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
        # Stop SSDP server
        if self._ssdp_server:
            try:
                await self._ssdp_server.stop()
            except Exception as err:
                self.logger.warning("Error stopping SSDP server: %s", err)

        # Unregister HTTP routes
        if self._routes_registered:
            try:
                self.mass.streams.unregister_dynamic_route("/dlna/description.xml")
                self.mass.streams.unregister_dynamic_route("/dlna/ContentDirectory.xml")
                self.mass.streams.unregister_dynamic_route("/dlna/ConnectionManager.xml")
                self.mass.streams.unregister_dynamic_route("/dlna/control/ContentDirectory")
                self.mass.streams.unregister_dynamic_route("/dlna/control/ConnectionManager")
                self.mass.streams.unregister_dynamic_route("/dlna/track/*")
                # self.mass.streams.unregister_dynamic_route("/dlna/event/ContentDirectory")
            except Exception as err:
                self.logger.warning("Error unregistering routes: %s", err)

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

            if action_elem is None or len(action_elem) == 0:  # Fixed the deprecation warning too
                return await self._soap_error(401, "Invalid Action")

            # Handle the action
            if action == "Browse":
                return await self._handle_browse_action(action_elem)
            if action == "GetSystemUpdateID":
                return await self._handle_get_system_update_id()

            return await self._soap_error(401, "Invalid Action")

        except DefusedET.ParseError as err:
            self.logger.warning("Invalid XML in SOAP request: %s", err)
            return await self._soap_error(400, "Invalid XML")
        except Exception:
            self.logger.exception("Error handling ContentDirectory control request")
            return await self._soap_error(500, "Internal server error")

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

            return await self._soap_error(401, "Invalid Action")

        except Exception:
            self.logger.exception("Error handling ConnectionManager control request")
            return await self._soap_error(500, "Internal server error")

    async def _handle_browse_action(self, action_elem: Element) -> web.Response:
        """Handle Browse SOAP action."""
        # Extract parameters
        object_id = self._get_soap_param(action_elem, "ObjectID") or ROOT_ID
        browse_flag = self._get_soap_param(action_elem, "BrowseFlag") or "BrowseDirectChildren"
        starting_index = int(self._get_soap_param(action_elem, "StartingIndex") or "0")
        requested_count = int(self._get_soap_param(action_elem, "RequestedCount") or "0")

        self.logger.debug(
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

        # Log DIDL for album browsing
        if object_id.startswith("album_") and browse_flag == "BrowseDirectChildren":
            self.logger.error("=== MA ALBUM DIDL ===\n%s", didl_xml)

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

    async def _handle_track_stream(self, request: web.Request) -> web.Response:
        """Handle track streaming request."""
        # Parse path: /dlna/track/{provider}/{item_id}.{fmt}
        path_parts = request.path.split("/")
        if len(path_parts) < 5:
            return web.Response(status=400, text="Invalid path")

        provider_param = path_parts[3]
        filename = path_parts[4]
        item_id, fmt = filename.rsplit(".", 1)

        self.logger.debug(
            "Stream request: provider=%s, item_id=%s, format=%s", provider_param, item_id, fmt
        )

        try:
            # Get the track
            track = await self.mass.music.tracks.get_library_item(item_id)

            # Get provider mapping
            provider_instance, prov_item_id = await self.mass.music.tracks.get_provider_mapping(
                track
            )

            # Get the provider
            prov = self.mass.get_provider(provider_instance)
            if not prov or not isinstance(prov, MusicProvider):
                raise ProviderUnavailableError(f"Provider {provider_instance} not available")

            # Get stream details
            streamdetails = await prov.get_stream_details(prov_item_id, MediaType.TRACK)

            # Get the absolute path from the FileSystemItem
            if hasattr(streamdetails.data, "absolute_path"):
                file_path = streamdetails.data.absolute_path
            else:
                # Fallback for non-filesystem providers
                raise UnsupportedFeaturedException(
                    "Only local files are supported for DLNA streaming"
                )

            self.logger.debug("Serving file: %s", file_path)

            # Serve the file
            return cast(
                "web.Response", web.FileResponse(path=file_path, headers={"Accept-Ranges": "bytes"})
            )

        except MediaNotFoundError:
            return web.Response(status=404, text="Track not found")
        except Exception:
            self.logger.exception("Error streaming track")
            return web.Response(status=500, text="Internal server error")

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
                didl_xml = await self._create_track_item(track)
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
            # Root level: return Artists, Albums, and Tracks containers
            containers = [
                self._create_artists_root_container(),
                self._create_albums_root_container(),
                self._create_tracks_root_container(),
            ]
            didl_xml = self._wrap_didl_items(containers)
            return didl_xml, len(containers), len(containers)

        if parent_id == ARTISTS_CONTAINER_ID:
            # Return all artists
            artists = await self.mass.music.artists.library_items(
                limit=limit, offset=offset, order_by="sort_name"
            )
            artist_items = [self._create_artist_container(artist) for artist in artists]
            total = await self.mass.music.artists.library_count()
            didl_xml = self._wrap_didl_items(artist_items)
            return didl_xml, len(artist_items), total

        if parent_id == ALBUMS_CONTAINER_ID:
            # Return all albums
            albums = await self.mass.music.albums.library_items(
                limit=limit, offset=offset, order_by="sort_name"
            )
            album_items = [
                self._create_album_container(album, ALBUMS_CONTAINER_ID)  # type: ignore[arg-type]
                for album in albums
            ]
            total = await self.mass.music.albums.library_count()
            didl_xml = self._wrap_didl_items(album_items)
            return didl_xml, len(album_items), total

        if parent_id == TRACKS_CONTAINER_ID:
            # Return all tracks
            tracks = await self.mass.music.tracks.library_items(
                limit=limit, offset=offset, order_by="sort_name"
            )
            track_items = []
            for track in tracks:
                item_xml = await self._create_track_item(track)
                track_items.append(item_xml)
            total = await self.mass.music.tracks.library_count()
            didl_xml = self._wrap_didl_items(track_items)
            return didl_xml, len(track_items), total

        if parent_id.startswith("artist_"):
            # Return albums for this artist
            artist_id = parent_id[7:]
            albums = await self.mass.music.artists.albums(
                artist_id, "library", in_library_only=True
            )  # type: ignore[assignment]
            # Apply pagination manually since albums() doesn't support it
            paginated_albums = (
                list(albums)[offset : offset + limit] if limit > 0 else list(albums)[offset:]
            )
            album_items = [self._create_album_container(album) for album in paginated_albums]  # type: ignore[arg-type]
            didl_xml = self._wrap_didl_items(album_items)
            return didl_xml, len(album_items), len(albums)

        if parent_id.startswith("album_"):
            # Return tracks for this album
            album_id = parent_id[6:]
            tracks = await self.mass.music.albums.tracks(album_id, "library", in_library_only=True)
            # Apply pagination manually
            paginated_tracks = (
                list(tracks)[offset : offset + limit] if limit > 0 else list(tracks)[offset:]
            )
            track_items = []
            for track in paginated_tracks:
                item_xml = await self._create_track_item(track)
                track_items.append(item_xml)
            didl_xml = self._wrap_didl_items(track_items)
            return didl_xml, len(track_items), len(tracks)

        return self._create_empty_didl(), 0, 0

    def _create_root_container(self) -> str:
        """Create DIDL-Lite XML for root container."""
        return """<container id="0" parentID="-1" restricted="1">
    <dc:title>Music Assistant</dc:title>
    <upnp:class>object.container</upnp:class>
</container>"""

    def _create_artists_root_container(self) -> str:
        """Create DIDL-Lite XML for Artists root container."""
        return f"""<container id="{ARTISTS_CONTAINER_ID}" parentID="{ROOT_ID}" restricted="1">
    <dc:title>Artists</dc:title>
    <upnp:class>object.container</upnp:class>
</container>"""

    def _create_albums_root_container(self) -> str:
        """Create DIDL-Lite XML for Albums root container."""
        return f"""<container id="{ALBUMS_CONTAINER_ID}" parentID="{ROOT_ID}" restricted="1">
        <dc:title>Albums</dc:title>
        <upnp:class>object.container</upnp:class>
    </container>"""

    def _create_tracks_root_container(self) -> str:
        """Create DIDL-Lite XML for Tracks root container."""
        return f"""<container id="{TRACKS_CONTAINER_ID}" parentID="{ROOT_ID}" restricted="1">
        <dc:title>Tracks</dc:title>
        <upnp:class>object.container</upnp:class>
    </container>"""

    def _create_artist_container(self, artist: Artist) -> str:
        """Create DIDL-Lite XML for an artist container."""
        artist_id = f"artist_{artist.item_id}"
        title = self._escape_xml(artist.name)

        # Add album art if available
        album_art_xml = ""
        if artist.image and artist.image.path:
            image_url = self._get_image_url(artist.image)
            album_art_xml = f"<upnp:albumArtURI>{self._escape_xml(image_url)}</upnp:albumArtURI>"

        return f"""<container id="{artist_id}" parentID="{ARTISTS_CONTAINER_ID}" restricted="1">
    <dc:title>{title}</dc:title>
    <upnp:class>object.container.person.musicArtist</upnp:class>
    {album_art_xml}
</container>"""

    def _create_album_container(self, album: Album, parent_id: str | None = None) -> str:
        """Create DIDL-Lite XML for an album container."""
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
            album_art_xml = f"<upnp:albumArtURI>{self._escape_xml(image_url)}</upnp:albumArtURI>"

        # Add artist
        artist_xml = ""
        if album.artists:
            artist_name = self._escape_xml(album.artists[0].name)
            artist_xml = f"<upnp:artist>{artist_name}</upnp:artist>"

        return f"""<container id="{album_id}" parentID="{parent_id}" restricted="1">
    <dc:title>{title}</dc:title>
    <upnp:class>object.container.album.musicAlbum</upnp:class>
    {artist_xml}
    {album_art_xml}
</container>"""

    async def _create_track_item(self, track: Track) -> str:  # noqa: PLR0915
        """Create DIDL-Lite XML for a track item."""
        track_id = f"track_{track.item_id}"
        parent_id = f"album_{track.album.item_id}" if track.album else ROOT_ID
        title = self._escape_xml(track.name)

        # Get provider details for file extension and metadata
        provider_instance, prov_item_id = await self.mass.music.tracks.get_provider_mapping(track)
        prov = self.mass.get_provider(provider_instance)

        # Default values
        file_ext = "mp3"
        mime_type = "audio/mpeg"
        file_size = 0

        if prov and isinstance(prov, MusicProvider):
            try:
                streamdetails = await prov.get_stream_details(prov_item_id, MediaType.TRACK)
                if hasattr(streamdetails.data, "filename"):
                    # Extract extension from filename
                    filename = streamdetails.data.filename
                    file_ext = filename.rsplit(".", 1)[-1].lower()
                    # Map extension to mime type
                    mime_type = {
                        "mp3": "audio/mpeg",
                        "m4a": "audio/mp4",
                        "flac": "audio/flac",
                        "wav": "audio/wav",
                        "ogg": "audio/ogg",
                    }.get(file_ext, "audio/mpeg")

                # Get file size
                if hasattr(streamdetails.data, "file_size"):
                    file_size = streamdetails.data.file_size

            except Exception as err:
                self.logger.debug("Could not determine file type, using defaults: %s", err)

        # Build stream URL with correct extension
        stream_url = f"{self.mass.streams.base_url}/dlna/track/library/{track.item_id}.{file_ext}"

        # Build protocol info
        if file_ext == "m4a":
            protocol_info = (
                "http-get:*:audio/mp4:"
                "DLNA.ORG_PN=AAC_ISO_320;DLNA.ORG_OP=01;"
                "DLNA.ORG_FLAGS=01700000000000000000000000000000"
            )
        else:
            protocol_info = f"http-get:*:{mime_type}:*"

        # Get metadata for res attributes
        bitrate = 0
        sample_rate = 44100
        channels = 2

        if track.metadata:
            bitrate = getattr(track.metadata, "bitrate", 0) or 0
            sample_rate = getattr(track.metadata, "sample_rate", 44100) or 44100
            channels = getattr(track.metadata, "channels", 2) or 2

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

        # Add artist
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
                f"<upnp:originalTrackNumber>{track.position}</upnp:originalTrackNumber>"
            )
        else:
            self.logger.debug("Track %s has no position: %s", track.name, track.position)

        # Add release year (optional)
        date_xml = ""
        if track.album:
            album_year = getattr(track.album, "year", None)
            if album_year:
                date_xml = f"<dc:date>{album_year}-01-01</dc:date>"

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

    def _wrap_didl_items(self, items: list[str]) -> str:
        """Wrap DIDL items in DIDL-Lite container."""
        items_xml = "\n".join(items)
        return f"""<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">
{items_xml}
</DIDL-Lite>"""

    def _create_empty_didl(self) -> str:
        """Create empty DIDL-Lite XML."""
        return """<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">
</DIDL-Lite>"""

    def _get_image_url(self, image: MediaItemImage) -> str:
        """Get the URL for an image."""
        if image.remotely_accessible:
            return image.path

        # For local images with relative paths, construct absolute path
        image_path = image.path

        # If it's not already absolute, prepend the filesystem base path
        if not image_path.startswith("/"):
            # Find the filesystem provider to get the base path
            for provider in self.mass.music.providers:
                # Check if this provider has a base_path attribute (filesystem providers do)
                if hasattr(provider, "base_path"):
                    base_path = provider.base_path
                    image_path = f"{base_path}/{image_path}"
                    break

        encoded_path = urllib.parse.quote(image_path)
        return f"{self.mass.webserver.base_url}/imageproxy?path={encoded_path}"

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

    def _get_soap_param(self, action_elem: Element, param_name: str) -> str | None:
        """Extract a parameter from SOAP action element."""
        for elem in action_elem:
            if elem.tag.endswith(param_name):
                return elem.text
        return None

    async def _soap_error(self, error_code: int, error_description: str) -> web.Response:
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
