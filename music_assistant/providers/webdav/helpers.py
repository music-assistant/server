"""WebDAV helper functions for Music Assistant."""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from urllib.parse import quote, unquote, urljoin

import aiohttp
from defusedxml import ElementTree
from music_assistant_models.errors import LoginFailed, ProviderUnavailableError, SetupFailedError

LOGGER = logging.getLogger(__name__)

DAV_NAMESPACE = {"d": "DAV:"}

PROPFIND_BODY = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:">
    <d:prop>
        <d:resourcetype/>
        <d:getcontentlength/>
        <d:getlastmodified/>
        <d:displayname/>
        <d:getetag/>
    </d:prop>
</d:propfind>"""


@dataclass
class WebDAVItem:
    """Representation of a WebDAV resource."""

    href: str
    name: str
    is_dir: bool
    size: int | None = None
    last_modified: str | None = None
    etag: str | None = None


async def webdav_propfind(
    session: aiohttp.ClientSession,
    url: str,
    depth: int = 1,
    timeout: int = 30,
    auth_header: str | None = None,
) -> list[WebDAVItem]:
    """
    Execute a PROPFIND request on a WebDAV resource.

    :param session: Active HTTP session.
    :param url: WebDAV URL to query.
    :param depth: Depth level (0=properties only, 1=immediate children).
    :param timeout: Request timeout in seconds.
    :param auth_header: Optional pre-encoded Authorization header value (e.g. "Basic ...").
    :returns: List of WebDAVItem objects.
    :raises LoginFailed: Authentication failed (401/403).
    :raises SetupFailedError: Server error during setup.
    :raises ProviderUnavailableError: Connection or timeout error.
    """
    headers = {"Depth": str(depth), "Content-Type": "application/xml; charset=utf-8"}
    if auth_header:
        headers["Authorization"] = auth_header

    try:
        async with session.request(
            "PROPFIND",
            url,
            headers=headers,
            data=PROPFIND_BODY,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status == 401:
                raise LoginFailed("Authentication failed for WebDAV server")
            if resp.status == 403:
                raise LoginFailed("Access forbidden for WebDAV server")
            if resp.status == 404:
                return []
            if resp.status >= 400:
                raise SetupFailedError(f"WebDAV PROPFIND failed with status {resp.status}")

            response_text = await resp.text()
            return _parse_propfind_response(response_text, url)

    except TimeoutError as err:
        raise ProviderUnavailableError(
            f"WebDAV connection timeout: {url}",
            translation_key="connection_timeout",
            translation_args=[url],
        ) from err
    except aiohttp.ClientError as err:
        raise ProviderUnavailableError(f"WebDAV connection error: {err}") from err


def _find_prop(props: list[ElementTree.Element], tag: str) -> ElementTree.Element | None:
    """Return the first match for tag across a response's merged propstat prop elements."""
    for prop in props:
        if (elem := prop.find(tag, DAV_NAMESPACE)) is not None:
            return elem
    return None


def _parse_propfind_response(response_text: str, base_url: str) -> list[WebDAVItem]:
    """Parse WebDAV PROPFIND XML response."""
    try:
        root = ElementTree.fromstring(response_text)
    except ElementTree.ParseError as err:
        LOGGER.warning("Failed to parse WebDAV PROPFIND response: %s", err)
        return []

    items: list[WebDAVItem] = []
    base_url_normalized = base_url.rstrip("/")

    for response_elem in root.findall("d:response", DAV_NAMESPACE):
        href_elem = response_elem.find("d:href", DAV_NAMESPACE)
        if href_elem is None or not href_elem.text:
            continue

        href = unquote(href_elem.text.rstrip("/"))

        # Skip the base directory itself
        if href.rstrip("/") == base_url_normalized:
            continue

        # a server may split properties it cannot satisfy (e.g. an unsupported getetag) into
        # a separate propstat with a non-2xx status; merge every successful block's props so a
        # 404 block returned first does not shadow resourcetype/getlastmodified from a later 200
        props: list[ElementTree.Element] = []
        for propstat in response_elem.findall("d:propstat", DAV_NAMESPACE):
            status_elem = propstat.find("d:status", DAV_NAMESPACE)
            if status_elem is not None and status_elem.text and " 200 " not in status_elem.text:
                continue
            if (prop := propstat.find("d:prop", DAV_NAMESPACE)) is not None:
                props.append(prop)
        if not props:
            continue

        # Check if it's a directory
        resourcetype = _find_prop(props, "d:resourcetype")
        is_collection = (
            resourcetype is not None
            and resourcetype.find("d:collection", DAV_NAMESPACE) is not None
        )

        # Get size (only for files)
        size = None
        if not is_collection:
            contentlength = _find_prop(props, "d:getcontentlength")
            if contentlength is not None and contentlength.text:
                with contextlib.suppress(ValueError):
                    size = int(contentlength.text)

        # Get last modified
        lastmodified = _find_prop(props, "d:getlastmodified")
        last_modified = lastmodified.text if lastmodified is not None else None

        # Get etag (used only as a higher-precision metadata-file change token)
        etagelem = _find_prop(props, "d:getetag")
        etag = None
        if etagelem is not None and etagelem.text:
            etag = etagelem.text.strip().removeprefix("W/").strip('"') or None

        # Get display name or extract from href
        displayname = _find_prop(props, "d:displayname")
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
                etag=etag,
            )
        )

    return items


async def webdav_test_connection(
    session: aiohttp.ClientSession,
    base_url: str,
    username: str | None,
    password: str | None,
    timeout: int = 10,
) -> None:
    """
    Test WebDAV connection and authentication.

    :param session: Active HTTP session.
    :param base_url: WebDAV server URL.
    :param username: Optional username.
    :param password: Optional password.
    :param timeout: Connection timeout in seconds.
    :raises LoginFailed: Authentication failed.
    :raises SetupFailedError: Connection or configuration error.
    """
    auth_header = aiohttp.encode_basic_auth(username, password or "") if username else None

    try:
        await webdav_propfind(session, base_url, depth=0, timeout=timeout, auth_header=auth_header)
    except ProviderUnavailableError as err:
        # During setup, connection errors should be SetupFailedError
        raise SetupFailedError(str(err)) from err


def build_webdav_url(base_url: str, path: str) -> str:
    """
    Build a WebDAV URL by joining the base URL with a relative resource path.

    :param base_url: The WebDAV base URL.
    :param path: A relative resource path, or an absolute URL which is returned as-is.
    """
    if path.startswith(("http://", "https://")):
        return path
    normalized_base = base_url if base_url.endswith("/") else f"{base_url}/"
    # Percent-encode the path so reserved characters (e.g. ; ? # :) survive intact;
    # left unencoded they would be misread as URL params/query/fragment/scheme.
    quoted_path = quote(path.removeprefix("/"), safe="/")
    return urljoin(normalized_base, quoted_path)
