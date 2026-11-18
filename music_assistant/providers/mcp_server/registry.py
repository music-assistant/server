"""Registry of the tools the builtin MCP server offers, with their input schemas."""

from __future__ import annotations

import inspect
import re
import types
from annotationlib import ForwardRef
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Union, get_args, get_origin

if TYPE_CHECKING:
    from music_assistant_models.auth import Scope

    from music_assistant.mass import MusicAssistant

_JSON_TYPES: dict[Any, str] = {str: "string", int: "integer", float: "number", bool: "boolean"}
_PARAM_RE = re.compile(r"^:param (\w+):(.*?)(?=^:\w|\Z)", re.MULTILINE | re.DOTALL)


class ToolError(Exception):
    """An error to report back to the model as the result of a tool call."""


@dataclass(frozen=True)
class MCPTool:
    """A tool the MCP server offers, with the scopes a user needs to call it."""

    name: str
    description: str
    scopes: tuple[Scope, ...]
    input_schema: dict[str, Any]
    handler: Callable[..., Awaitable[Any]]

    def to_dict(self) -> dict[str, Any]:
        """Return the tool as listed by tools/list."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }

    async def call(self, mass: MusicAssistant, arguments: dict[str, Any]) -> Any:
        """
        Validate the arguments and run the tool.

        :param mass: The Music Assistant instance.
        :param arguments: The arguments the model passed.
        :raises ToolError: When the arguments are invalid or the tool fails.
        """
        properties = self.input_schema["properties"]
        if unknown := set(arguments) - set(properties):
            raise ToolError(f"Unknown argument(s): {', '.join(sorted(unknown))}")
        if missing := set(self.input_schema["required"]) - set(arguments):
            raise ToolError(f"Missing argument(s): {', '.join(sorted(missing))}")
        for key, value in arguments.items():
            schema = properties[key]
            # null is only valid for an optional argument that defaults to null
            nullable = key not in self.input_schema["required"] and "default" not in schema
            if not (value is None and nullable) and not _matches_schema(value, schema):
                raise ToolError(f"Invalid value for argument {key}: {value!r}")
        return await self.handler(mass, **arguments)


TOOLS: dict[str, MCPTool] = {}


def mcp_tool(
    name: str, scope: Scope | tuple[Scope, ...]
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """
    Register an async function as an MCP tool.

    The function takes the Music Assistant instance as its first argument, followed by the
    tool arguments. The input schema is derived from its signature, the descriptions from
    its docstring.

    :param name: The name of the tool.
    :param scope: The scope a user needs to call the tool, or a tuple of scopes that are all
        needed.
    """

    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        description, param_docs = _parse_docstring(inspect.getdoc(func) or "")
        TOOLS[name] = MCPTool(
            name=name,
            description=description,
            scopes=scope if isinstance(scope, tuple) else (scope,),
            input_schema=_input_schema(func, param_docs),
            handler=func,
        )
        return func

    return decorator


def _parse_docstring(docstring: str) -> tuple[str, dict[str, str]]:
    """Return the description and the parameter descriptions of a docstring."""
    description = docstring.split("\n:", 1)[0]
    params = {name: " ".join(text.split()) for name, text in _PARAM_RE.findall(docstring)}
    return " ".join(description.split()), params


def _input_schema(
    func: Callable[..., Awaitable[Any]], param_docs: dict[str, str]
) -> dict[str, Any]:
    """Return the JSON schema of the arguments of a tool function (all but the first)."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param in list(inspect.signature(func).parameters.values())[1:]:
        hint = ForwardRef(param.annotation).evaluate(globals=func.__globals__)
        schema = _type_schema(hint)
        if description := param_docs.get(param.name):
            schema["description"] = description
        if param.default is inspect.Parameter.empty:
            required.append(param.name)
        elif param.default is not None:
            schema["default"] = param.default
        properties[param.name] = schema
    return {"type": "object", "properties": properties, "required": required}


def _type_schema(hint: Any) -> dict[str, Any]:
    """Return the JSON schema of a type hint."""
    origin = get_origin(hint)
    if origin in (Union, types.UnionType):
        # an optional argument: X | None
        (inner,) = (arg for arg in get_args(hint) if arg is not type(None))
        return _type_schema(inner)
    if origin is Literal:
        return {"type": "string", "enum": list(get_args(hint))}
    if origin is list:
        return {"type": "array", "items": _type_schema(get_args(hint)[0])}
    return {"type": _JSON_TYPES[hint]}


def _matches_schema(value: Any, schema: dict[str, Any]) -> bool:
    """Return whether a value matches the (simple) JSON schema of an argument."""
    match schema["type"]:
        case "string":
            return isinstance(value, str) and value in schema.get("enum", [value])
        case "boolean":
            return isinstance(value, bool)
        case "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        case "number":
            return isinstance(value, int | float) and not isinstance(value, bool)
        case "array":
            return isinstance(value, list) and all(
                _matches_schema(entry, schema["items"]) for entry in value
            )
    return False
