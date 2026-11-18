"""The tools that providers declare in their manifest, as the builtin MCP server offers them."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant.controllers.webserver.helpers.auth_middleware import current_user

from .registry import MCPTool, ToolError
from .tools import _resolve_player

if TYPE_CHECKING:
    from music_assistant_models.provider import ProviderMCPTool

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


def get_provider_tools(mass: MusicAssistant) -> list[MCPTool]:
    """
    Return the tools of the loaded providers that the authenticated user may use an instance of.

    A tool is named ``<domain>__<id>``. A tool of a provider of which the user may use several
    instances takes the instance to act on as its ``instance`` argument, and a tool that
    requires a player takes the player as its ``player`` argument. A call refuses an instance
    that the user it acts as (such as a Home Assistant user) may not use.

    :param mass: The Music Assistant instance.
    """
    instances: dict[str, list[ProviderInstanceType]] = {}
    for provider in mass.get_providers_for_user(current_user.get()):
        if provider.manifest.mcp_tools and provider.available and not provider.unloading:
            instances.setdefault(provider.domain, []).append(provider)
    return [
        _provider_tool(declared, providers)
        for providers in instances.values()
        for declared in providers[0].manifest.mcp_tools
    ]


def _provider_tool(declared: ProviderMCPTool, providers: list[ProviderInstanceType]) -> MCPTool:
    """
    Return the MCP tool for a tool that the given instances of a provider declare.

    :param declared: The tool as declared in the manifest of the provider.
    :param providers: The instances of the provider the current user may use.
    """
    manifest = providers[0].manifest
    declared_properties: dict[str, Any] = declared.input_schema.get("properties", {})
    properties: dict[str, Any] = {}
    required: list[str] = []
    if len(providers) > 1:
        names = ", ".join(f'"{provider.instance_id}": {provider.name}' for provider in providers)
        properties["instance"] = {
            "type": "string",
            "enum": [provider.instance_id for provider in providers],
            "description": f"The id of the {manifest.name} instance to use: {names}.",
        }
        required.append("instance")
    if declared.requires_player:
        properties["player"] = {"type": "string", "description": "The name or id of the player."}
        required.append("player")
    properties.update(declared_properties)
    required.extend(declared.input_schema.get("required", []))

    # positional-only, so a declared argument may be named "mass"
    async def run(mass: MusicAssistant, /, **arguments: Any) -> dict[str, Any]:
        provider = providers[0]
        if len(providers) > 1:
            instance_id = arguments.pop("instance")
            provider = next(p for p in providers if p.instance_id == instance_id)
        # the tool is built for the user who lists it, a call may act as another user
        if all(p.instance_id != provider.instance_id for p in mass.get_providers()):
            raise ToolError(f"Not allowed: {provider.name} is not available to this user")
        player_id = None
        if declared.requires_player:
            player_id = _resolve_player(mass, arguments.pop("player")).player_id
        values = {
            key: arguments.get(key, spec.get("default"))
            for key, spec in declared_properties.items()
        }
        result: object = await provider.handle_mcp_tool(declared.id, values, player_id)
        if not isinstance(result, dict):
            msg = (
                f"Provider {provider.domain} returned a {type(result).__name__} for MCP tool "
                f"{declared.id}, not a dict"
            )
            raise TypeError(msg)
        return result

    return MCPTool(
        name=f"{manifest.domain}__{declared.id}",
        description=f"{declared.description} (from the {manifest.name} provider)",
        scopes=(declared.scope,),
        input_schema={"type": "object", "properties": properties, "required": required},
        handler=run,
    )
