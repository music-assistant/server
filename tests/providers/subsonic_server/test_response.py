"""Tests for the Subsonic response envelope."""

from __future__ import annotations

import json

from music_assistant.providers.subsonic_server.constants import API_VERSION, ErrorCode
from music_assistant.providers.subsonic_server.response import (
    SubsonicError,
    build_error_response,
    build_response,
    to_xml,
)


def test_json_envelope() -> None:
    """A JSON response carries the OpenSubsonic envelope around the payload."""
    response = build_response({"license": {"valid": True}}, "2.11.0", "json")
    assert response.text is not None
    body = json.loads(response.text)["subsonic-response"]
    assert body == {
        "status": "ok",
        "version": API_VERSION,
        "type": "music-assistant",
        "serverVersion": "2.11.0",
        "openSubsonic": True,
        "license": {"valid": True},
    }


def test_json_error() -> None:
    """An error response has status failed and the error code and message."""
    error = SubsonicError(ErrorCode.WRONG_CREDENTIALS, "Wrong username or password")
    response = build_error_response(error, "2.11.0", "json")
    assert response.status == 200
    assert response.text is not None
    body = json.loads(response.text)["subsonic-response"]
    assert body["status"] == "failed"
    assert body["error"] == {"code": 40, "message": "Wrong username or password"}


def test_jsonp_wraps_in_callback() -> None:
    """JSONP wraps the JSON body in the given callback."""
    response = build_response({}, "2.11.0", "jsonp", callback="handle")
    assert response.text is not None
    assert response.text.startswith('handle({"subsonic-response": ')
    assert response.content_type == "application/javascript"


def test_jsonp_rejects_unsafe_callback() -> None:
    """A callback that is not a plain identifier falls back to XML."""
    response = build_response({}, "2.11.0", "jsonp", callback="alert(1);x")
    assert response.content_type == "text/xml"


def test_xml_is_the_default_format() -> None:
    """Without the ``f`` parameter the response is XML."""
    response = build_response({}, "2.11.0", None)
    assert response.content_type == "text/xml"
    assert response.text is not None
    assert 'status="ok"' in response.text


def test_xml_serialization() -> None:
    """Scalars become attributes, lists repeated elements and ``value`` the element text."""
    xml = to_xml(
        {
            "status": "ok",
            "openSubsonic": True,
            "openSubsonicExtensions": [{"name": "formPost", "versions": [1, 2]}],
            "lyrics": {"artist": "A", "value": "la la"},
            "skipped": None,
        }
    )
    assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?><subsonic-response')
    assert 'openSubsonic="true"' in xml
    assert (
        '<openSubsonicExtensions name="formPost"><versions>1</versions>'
        "<versions>2</versions></openSubsonicExtensions>"
    ) in xml
    assert '<lyrics artist="A">la la</lyrics>' in xml
    assert "skipped" not in xml
