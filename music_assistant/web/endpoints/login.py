"""Login API endpoints."""

import datetime

import jwt
from aiohttp.web import HTTPUnauthorized, Request, Response, RouteTableDef
from music_assistant.helpers.typing import MusicAssistantType
from music_assistant.helpers.util import json_serializer

routes = RouteTableDef()


@routes.post("/login")
@routes.post("/api/login")
async def async_login(request: Request):
    """Handle the retrieval of a JWT token."""
    form = await request.json()
    username = form.get("username")
    password = form.get("password")
    token_info = await async_get_token(request.app["mass"], username, password)
    if token_info:
        return Response(
            body=json_serializer(token_info), content_type="application/json"
        )
    return HTTPUnauthorized(body="Invalid username and/or password provided!")


async def async_get_token(
    mass: MusicAssistantType, username: str, password: str
) -> dict:
    """Validate given credentials and return JWT token."""
    verified = mass.config.validate_credentials(username, password)
    if verified:
        token_expires = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        scopes = ["user:admin"]  # scopes not yet implemented
        token = jwt.encode(
            {"username": username, "scopes": scopes, "exp": token_expires},
            mass.web.device_id,
        )
        return {
            "user": username,
            "token": token.decode(),
            "expires": token_expires,
            "scopes": scopes,
        }
    return None
