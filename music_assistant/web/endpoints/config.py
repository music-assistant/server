"""Config API endpoints."""

import orjson
from aiohttp import web
from aiohttp_jwt import login_required
from music_assistant.constants import (
    CONF_KEY_BASE,
    CONF_KEY_METADATA_PROVIDERS,
    CONF_KEY_MUSIC_PROVIDERS,
    CONF_KEY_PLAYER_PROVIDERS,
    CONF_KEY_PLAYER_SETTINGS,
    CONF_KEY_PLUGINS,
)
from music_assistant.helpers.util import json_serializer

routes = web.RouteTableDef()


@routes.get("/api/config")
@login_required
async def async_get_config(request: web.Request):
    """Get the full config."""
    language = request.rel_url.query.get("lang", "en")
    conf = {
        CONF_KEY_BASE: request.app["mass"].config.base.to_dict(language),
        CONF_KEY_MUSIC_PROVIDERS: request.app["mass"].config.music_providers.to_dict(
            language
        ),
        CONF_KEY_PLAYER_PROVIDERS: request.app["mass"].config.player_providers.to_dict(
            language
        ),
        CONF_KEY_METADATA_PROVIDERS: request.app[
            "mass"
        ].config.metadata_providers.to_dict(language),
        CONF_KEY_PLUGINS: request.app["mass"].config.plugins.to_dict(language),
        CONF_KEY_PLAYER_SETTINGS: request.app["mass"].config.player_settings.to_dict(
            language
        ),
    }
    return web.Response(body=json_serializer(conf), content_type="application/json")


@routes.get("/api/config/{base}")
@login_required
async def async_get_config_item(request: web.Request):
    """Get the config by base type."""
    language = request.rel_url.query.get("lang", "en")
    conf_base = request.match_info.get("base")
    conf = request.app["mass"].config[conf_base]
    return web.Response(
        body=json_serializer(conf.to_dict(language)), content_type="application/json"
    )


@routes.put("/api/config/{base}/{key}/{entry_key}")
@login_required
async def async_put_config(request: web.Request):
    """Save the given config item."""
    conf_key = request.match_info.get("key")
    conf_base = request.match_info.get("base")
    entry_key = request.match_info.get("entry_key")
    try:
        new_value = await request.json(loads=orjson.loads)
    except orjson.decoder.JSONDecodeError:
        new_value = (
            request.app["mass"]
            .config[conf_base][conf_key]
            .get_entry(entry_key)
            .default_value
        )
    request.app["mass"].config[conf_base][conf_key][entry_key] = new_value
    return web.json_response(True)
