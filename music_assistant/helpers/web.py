"""Various helpers for web requests."""

import ipaddress
from functools import wraps
from typing import AsyncGenerator

from aiohttp import web
from music_assistant.helpers.typing import MusicAssistantType
from music_assistant.helpers.util import json_serializer
from music_assistant.models.media_types import MediaType


async def async_stream_json(request: web.Request, generator: AsyncGenerator):
    """Stream items from async generator as json object."""
    resp = web.StreamResponse(
        status=200, reason="OK", headers={"Content-Type": "application/json"}
    )
    await resp.prepare(request)
    # write json open tag
    await resp.write(b'{ "items": [')
    count = 0
    async for item in generator:
        # write each item into the items object of the json
        if count:
            json_response = b"," + json_serializer(item)
        else:
            json_response = json_serializer(item)
        await resp.write(json_response)
        count += 1
    # write json close tag
    msg = '], "count": %s }' % count
    await resp.write(msg.encode())
    await resp.write_eof()
    return resp


async def async_media_items_from_body(mass: MusicAssistantType, data: dict):
    """Convert posted body data into media items."""
    if not isinstance(data, list):
        data = [data]
    media_items = []
    for item in data:
        media_item = await mass.music.async_get_item(
            item["item_id"],
            item["provider"],
            MediaType.from_string(item["media_type"]),
            lazy=True,
        )
        media_items.append(media_item)
    return media_items


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
