"""Subsonic response envelope in the JSON, JSONP and XML formats."""

from __future__ import annotations

import json
import re
from typing import Any
from xml.etree import ElementTree as ET

from aiohttp import web

from .constants import API_VERSION, SERVER_TYPE, XML_NAMESPACE, ErrorCode

JSONP_CALLBACK_PATTERN = re.compile(r"^[A-Za-z_$][\w$.]*$")
# Subsonic credentials travel in the request parameters, never in cookies, so any origin may call
# the API; browser clients such as Feishin web are served from another origin than the server.
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Max-Age": "86400",
}


class SubsonicError(Exception):
    """An error reported to the client inside a Subsonic response."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        """
        Initialize the error.

        :param code: The Subsonic error code.
        :param message: Human readable explanation.
        """
        super().__init__(message)
        self.code = code
        self.message = message


def build_response(
    payload: dict[str, Any],
    server_version: str,
    response_format: str | None,
    callback: str | None = None,
) -> web.Response:
    """
    Wrap a payload in a successful Subsonic response.

    :param payload: The response body (e.g. ``{"license": {"valid": True}}``).
    :param server_version: The Music Assistant version reported as ``serverVersion``.
    :param response_format: The ``f`` request parameter; XML when omitted.
    :param callback: The ``callback`` request parameter, used by the JSONP format.
    """
    return _render(_envelope("ok", server_version) | payload, response_format, callback)


def build_error_response(
    error: SubsonicError,
    server_version: str,
    response_format: str | None,
    callback: str | None = None,
) -> web.Response:
    """
    Wrap an error in a failed Subsonic response.

    :param error: The error to report.
    :param server_version: The Music Assistant version reported as ``serverVersion``.
    :param response_format: The ``f`` request parameter; XML when omitted.
    :param callback: The ``callback`` request parameter, used by the JSONP format.
    """
    body = _envelope("failed", server_version) | {
        "error": {"code": int(error.code), "message": error.message}
    }
    return _render(body, response_format, callback)


def build_preflight_response(requested_headers: str | None) -> web.Response:
    """
    Answer a CORS preflight request.

    :param requested_headers: The ``Access-Control-Request-Headers`` header of the request.
    """
    headers = CORS_HEADERS | {"Access-Control-Allow-Headers": requested_headers or "*"}
    return web.Response(status=204, headers=headers)


def to_xml(body: dict[str, Any]) -> str:
    """
    Serialize a response body to Subsonic XML.

    Scalars become attributes, nested objects become child elements, lists become repeated
    child elements and a ``value`` key becomes the element text.

    :param body: The response body, without the ``subsonic-response`` wrapper.
    """
    root = ET.Element("subsonic-response", {"xmlns": XML_NAMESPACE})
    _fill_element(root, body)
    return '<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(root, encoding="unicode")


def _envelope(status: str, server_version: str) -> dict[str, Any]:
    return {
        "status": status,
        "version": API_VERSION,
        "type": SERVER_TYPE,
        "serverVersion": server_version,
        "openSubsonic": True,
    }


def _render(
    body: dict[str, Any], response_format: str | None, callback: str | None
) -> web.Response:
    if response_format == "json":
        return web.json_response({"subsonic-response": body})
    if response_format == "jsonp" and callback and JSONP_CALLBACK_PATTERN.match(callback):
        script = f"{callback}({json.dumps({'subsonic-response': body})});"
        return web.Response(text=script, content_type="application/javascript")
    return web.Response(text=to_xml(body), content_type="text/xml")


def _fill_element(element: ET.Element, data: dict[str, Any]) -> None:
    for key, value in data.items():
        if value is None:
            continue
        if key == "value" and not isinstance(value, (dict, list)):
            element.text = _xml_scalar(value)
        elif isinstance(value, dict):
            _fill_element(ET.SubElement(element, key), value)
        elif isinstance(value, list):
            for item in value:
                child = ET.SubElement(element, key)
                if isinstance(item, dict):
                    _fill_element(child, item)
                else:
                    child.text = _xml_scalar(item)
        else:
            element.set(key, _xml_scalar(value))


def _xml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
