"""Various helpers for web requests."""

import asyncio
import ipaddress
from datetime import datetime
from functools import wraps
from typing import Any

import ujson
from aiohttp import web
from mashumaro.exceptions import MissingField
from music_assistant.helpers.typing import MusicAssistantType
from music_assistant.models.media_types import (
    Album,
    Artist,
    FullAlbum,
    FullTrack,
    Playlist,
    Radio,
    Track,
)


async def async_media_items_from_body(mass: MusicAssistantType, data: dict):
    """Convert posted body data into media items."""
    if not isinstance(data, list):
        data = [data]

    def media_item_from_dict(media_item):
        if media_item["media_type"] == "artist":
            return Artist.from_dict(media_item)
        if media_item["media_type"] == "album":
            try:
                return FullAlbum.from_dict(media_item)
            except MissingField:
                return Album.from_dict(media_item)
        if media_item["media_type"] == "track":
            try:
                return FullTrack.from_dict(media_item)
            except MissingField:
                return Track.from_dict(media_item)
        if media_item["media_type"] == "playlist":
            return Playlist.from_dict(media_item)
        if media_item["media_type"] == "radio":
            return Radio.from_dict(media_item)

    return [media_item_from_dict(x) for x in data]


def require_local_subnet(func):
    """Return decorator to specify web method as available locally only."""

    @wraps(func)
    async def wrapped(*args, **kwargs):
        request = args[-1]

        if isinstance(request, web.View):
            request = request.request

        if not isinstance(request, web.BaseRequest):  # pragma: no cover
            raise RuntimeError(
                "Incorrect usage of decorator." "Expect web.BaseRequest as an argument"
            )

        if not ipaddress.ip_address(request.remote).is_private:
            raise web.HTTPUnauthorized(reason="Not remote available")

        return await func(*args, **kwargs)

    return wrapped


def serialize_values(obj):
    """Recursively create serializable values for (custom) data types."""

    def get_val(val):
        if hasattr(val, "to_dict"):
            return val.to_dict()
        if isinstance(val, (list, set, filter)):
            return [get_val(x) for x in val]
        if isinstance(val, datetime):
            return val.isoformat()
        if isinstance(val, dict):
            return {key: get_val(value) for key, value in val.items()}
        return val

    return get_val(obj)


def json_serializer(obj):
    """Json serializer to recursively create serializable values for custom data types."""
    return ujson.dumps(serialize_values(obj))


def json_response(data: Any, status: int = 200):
    """Return json in web request."""
    # return web.json_response(data, dumps=json_serializer)
    return web.Response(
        body=json_serializer(data), status=200, content_type="application/json"
    )


async def async_json_response(data: Any, status: int = 200):
    """Return json in web request."""
    if isinstance(data, list):
        # we could potentially receive a large list of objects to serialize
        # which is blocking IO so run it in executor to be safe
        return await asyncio.get_running_loop().run_in_executor(
            None, json_response, data
        )
    return json_response(data)
