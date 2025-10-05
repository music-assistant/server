"""WebDAV helper functions for Music Assistant."""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from urllib.parse import unquote, urljoin

import aiohttp
from defusedxml import ElementTree
from music_assistant_models.errors import LoginFailed, SetupFailedError

LOGGER = logging.getLogger(__name__)

DAV_NAMESPACE = {"d": "DAV:"}


@dataclass
class WebDAVItem:
    """Representation of a WebDAV resource."""

    href: str
    name: str
    is_dir: bool
    size: int | None = None
    last_modified: str | None = None

    @property
    def ext(self) -> str | None:
        """Return file extension."""
        if self.is_dir:
            return None
        try:
            return self.name.rsplit(".", 1)[1].lower()
        except IndexError:
            return None


async def webdav_propfind(
    session: aiohttp.ClientSession,
    url: str,
    depth: int = 1,
    timeout: int = 30,
) -> list[WebDAVItem]:
    """
    Execute a PROPFIND request on a WebDAV resource.

    Args:
        session: Active HTTP session with auth configured
        url: WebDAV URL to query
        depth: Depth level (0=properties only, 1=immediate children)
        timeout: Request timeout in seconds

    Returns:
        List of WebDAVItem objects

    Raises:
        LoginFailed: Authentication failed
        SetupFailedError: Connection or other setup issues
    """
    headers = {
        "Depth": str(depth),
        "Content-Type": "application/xml; charset=utf-8",
    }

    body = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:">
    <d:prop>
        <d:resourcetype/>
        <d:getcontentlength/>
        <d:getlastmodified/>
        <d:displayname/>
    </d:prop>
</d:propfind>"""

    try:
        async with session.request(
            "PROPFIND",
            url,
            headers=headers,
            data=body,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status == 401:
                raise LoginFailed(f"Authentication failed for WebDAV server: {url}")
            if resp.status == 403:
                raise LoginFailed(f"Access forbidden for WebDAV server: {url}")
            if resp.status == 404:
                LOGGER.debug("WebDAV resource not found: %s", url)
                return []
            elif resp.status >= 400:
                raise SetupFailedError(
                    f"WebDAV PROPFIND failed with status {resp.status} for {url}"
                )

            response_text = await resp.text()
            return _parse_propfind_response(response_text, url)

    except TimeoutError as err:
        raise SetupFailedError(f"WebDAV connection timeout: {url}") from err
    except aiohttp.ClientError as err:
        raise SetupFailedError(f"WebDAV connection error: {err}") from err


def _parse_propfind_response(response_text: str, base_url: str) -> list[WebDAVItem]:
    """Parse WebDAV PROPFIND XML response."""
    try:
        root = ElementTree.fromstring(response_text)
    except ElementTree.ParseError as err:
        LOGGER.warning("Failed to parse WebDAV PROPFIND response: %s", err)
        return []

    items: list[WebDAVItem] = []

    for response_elem in root.findall("d:response", DAV_NAMESPACE):
        href_elem = response_elem.find("d:href", DAV_NAMESPACE)
        if href_elem is None or not href_elem.text:
            continue

        href = unquote(href_elem.text.rstrip("/"))

        # Skip the base directory itself
        if href.rstrip("/") == base_url.rstrip("/"):
            continue

        # Extract properties
        propstat = response_elem.find("d:propstat", DAV_NAMESPACE)
        if propstat is None:
            continue

        prop = propstat.find("d:prop", DAV_NAMESPACE)
        if prop is None:
            continue

        # Check if it's a directory
        resourcetype = prop.find("d:resourcetype", DAV_NAMESPACE)
        is_collection = (
            resourcetype is not None
            and resourcetype.find("d:collection", DAV_NAMESPACE) is not None
        )

        # Get size
        size = None
        if not is_collection:
            contentlength = prop.find("d:getcontentlength", DAV_NAMESPACE)
            if contentlength is not None and contentlength.text:
                with contextlib.suppress(ValueError):
                    size = int(contentlength.text)

        # Get last modified
        lastmodified = prop.find("d:getlastmodified", DAV_NAMESPACE)
        last_modified = lastmodified.text if lastmodified is not None else None

        # Get display name or extract from href
        displayname = prop.find("d:displayname", DAV_NAMESPACE)
        if displayname is not None and displayname.text:
            name = displayname.text
        else:
            name = href.split("/")[-1] or href.split("/")[-2]

        items.append(
            WebDAVItem(
                href=href,
                name=name,
                is_dir=is_collection,
                size=size,
                last_modified=last_modified,
            )
        )

    return items


async def webdav_test_connection(
    url: str,
    username: str | None = None,
    password: str | None = None,
    verify_ssl: bool = True,
    timeout: int = 10,
) -> None:
    """
    Test WebDAV connection and authentication.

    Args:
        url: WebDAV server URL
        username: Optional username
        password: Optional password
        verify_ssl: Whether to verify SSL certificates
        timeout: Connection timeout

    Raises:
        LoginFailed: Authentication or connection failed
        SetupFailedError: Server configuration issues
    """
    auth = None
    if username:
        auth = aiohttp.BasicAuth(username, password or "")

    connector = aiohttp.TCPConnector(ssl=verify_ssl)

    try:
        async with aiohttp.ClientSession(
            auth=auth,
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as session:
            # Try a simple PROPFIND to test connectivity
            await webdav_propfind(session, url, depth=0, timeout=timeout)
    finally:
        if connector:
            await connector.close()


def normalize_webdav_url(url: str) -> str:
    """Normalize WebDAV URL by ensuring it ends with a slash."""
    if not url.endswith("/"):
        return f"{url}/"
    return url


def build_webdav_url(base_url: str, path: str) -> str:
    """Build a WebDAV URL by joining base URL with path."""
    normalized_base = normalize_webdav_url(base_url)
    path = path.removeprefix("/")
    return urljoin(normalized_base, path)
