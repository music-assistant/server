"""Helpers for generating API documentation and OpenAPI specifications."""

from __future__ import annotations

import collections.abc
import inspect
from collections.abc import Callable
from dataclasses import MISSING
from datetime import datetime
from enum import Enum
from types import NoneType, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from music_assistant_models.player import Player as PlayerState

from music_assistant.helpers.api import APICommandHandler


def _format_type_name(type_hint: Any) -> str:
    """Format a type hint as a user-friendly string, using JSON types instead of Python types."""
    if type_hint is NoneType or type_hint is type(None):
        return "null"

    # Handle internal Player model - replace with PlayerState
    if hasattr(type_hint, "__name__") and type_hint.__name__ == "Player":
        if (
            hasattr(type_hint, "__module__")
            and type_hint.__module__ == "music_assistant.models.player"
        ):
            return "PlayerState"

    # Map Python types to JSON types
    type_name_mapping = {
        "str": "string",
        "int": "integer",
        "float": "number",
        "bool": "boolean",
        "dict": "object",
        "list": "array",
        "tuple": "array",
        "set": "array",
        "frozenset": "array",
        "Sequence": "array",
        "UniqueList": "array",
        "None": "null",
    }

    if hasattr(type_hint, "__name__"):
        type_name = str(type_hint.__name__)
        return type_name_mapping.get(type_name, type_name)

    type_str = str(type_hint).replace("NoneType", "null")
    # Replace Python types with JSON types in complex type strings
    for python_type, json_type in type_name_mapping.items():
        type_str = type_str.replace(python_type, json_type)
    return type_str


def _get_type_schema(  # noqa: PLR0911, PLR0915
    type_hint: Any, definitions: dict[str, Any]
) -> dict[str, Any]:
    """Convert a Python type hint to an OpenAPI schema."""
    # Handle string type hints from __future__ annotations
    if isinstance(type_hint, str):
        # Handle simple primitive type names
        if type_hint in ("str", "string"):
            return {"type": "string"}
        if type_hint in ("int", "integer"):
            return {"type": "integer"}
        if type_hint in ("float", "number"):
            return {"type": "number"}
        if type_hint in ("bool", "boolean"):
            return {"type": "boolean"}

        # Check if it looks like a simple class name (no special chars, starts with uppercase)
        # Examples: "PlayerType", "DeviceInfo", "PlaybackState"
        if type_hint.isidentifier() and type_hint[0].isupper():
            # Create a schema reference for this type
            if type_hint not in definitions:
                definitions[type_hint] = {"type": "object"}
            return {"$ref": f"#/components/schemas/{type_hint}"}

        # For complex type expressions like "str | None", "list[str]", return generic object
        return {"type": "object"}

    # Handle None type
    if type_hint is NoneType or type_hint is type(None):
        return {"type": "null"}

    # Handle internal Player model - replace with external PlayerState
    if hasattr(type_hint, "__name__") and type_hint.__name__ == "Player":
        # Check if this is the internal Player (from music_assistant.models.player)
        if (
            hasattr(type_hint, "__module__")
            and type_hint.__module__ == "music_assistant.models.player"
        ):
            # Replace with PlayerState from music_assistant_models
            return _get_type_schema(PlayerState, definitions)

    # Handle Union types (including Optional)
    origin = get_origin(type_hint)
    if origin is Union or origin is UnionType:
        args = get_args(type_hint)
        # Check if it's Optional (Union with None)
        non_none_args = [arg for arg in args if arg not in (NoneType, type(None))]
        if (len(non_none_args) == 1 and NoneType in args) or type(None) in args:
            # It's Optional[T], make it nullable
            schema = _get_type_schema(non_none_args[0], definitions)
            schema["nullable"] = True
            return schema
        # It's a union of multiple types
        return {"oneOf": [_get_type_schema(arg, definitions) for arg in args]}

    # Handle UniqueList (treat as array)
    if hasattr(type_hint, "__name__") and type_hint.__name__ == "UniqueList":
        args = get_args(type_hint)
        if args:
            return {"type": "array", "items": _get_type_schema(args[0], definitions)}
        return {"type": "array", "items": {}}

    # Handle Sequence types (from collections.abc or typing)
    if origin is collections.abc.Sequence or (
        hasattr(origin, "__name__") and origin.__name__ == "Sequence"
    ):
        args = get_args(type_hint)
        if args:
            return {"type": "array", "items": _get_type_schema(args[0], definitions)}
        return {"type": "array", "items": {}}

    # Handle set/frozenset types
    if origin in (set, frozenset):
        args = get_args(type_hint)
        if args:
            return {"type": "array", "items": _get_type_schema(args[0], definitions)}
        return {"type": "array", "items": {}}

    # Handle list/tuple types
    if origin in (list, tuple):
        args = get_args(type_hint)
        if args:
            return {"type": "array", "items": _get_type_schema(args[0], definitions)}
        return {"type": "array", "items": {}}

    # Handle dict types
    if origin is dict:
        args = get_args(type_hint)
        if len(args) == 2:
            return {
                "type": "object",
                "additionalProperties": _get_type_schema(args[1], definitions),
            }
        return {"type": "object", "additionalProperties": True}

    # Handle Enum types - add them to definitions as explorable objects
    if inspect.isclass(type_hint) and issubclass(type_hint, Enum):
        enum_name = type_hint.__name__
        if enum_name not in definitions:
            enum_values = [item.value for item in type_hint]
            enum_type = type(enum_values[0]).__name__ if enum_values else "string"
            openapi_type = {
                "str": "string",
                "int": "integer",
                "float": "number",
                "bool": "boolean",
            }.get(enum_type, "string")

            # Create a detailed enum definition with descriptions
            enum_values_str = ", ".join(str(v) for v in enum_values)
            definitions[enum_name] = {
                "type": openapi_type,
                "enum": enum_values,
                "description": f"Enum: {enum_name}. Possible values: {enum_values_str}",
            }
        return {"$ref": f"#/components/schemas/{enum_name}"}

    # Handle datetime
    if type_hint is datetime:
        return {"type": "string", "format": "date-time"}

    # Handle primitive types - check both exact type and type name
    if type_hint is str or (hasattr(type_hint, "__name__") and type_hint.__name__ == "str"):
        return {"type": "string"}
    if type_hint is int or (hasattr(type_hint, "__name__") and type_hint.__name__ == "int"):
        return {"type": "integer"}
    if type_hint is float or (hasattr(type_hint, "__name__") and type_hint.__name__ == "float"):
        return {"type": "number"}
    if type_hint is bool or (hasattr(type_hint, "__name__") and type_hint.__name__ == "bool"):
        return {"type": "boolean"}

    # Handle complex types (dataclasses, models)
    # Check for __annotations__ or if it's a class (not already handled above)
    if hasattr(type_hint, "__annotations__") or (
        inspect.isclass(type_hint) and not issubclass(type_hint, (str, int, float, bool, Enum))
    ):
        type_name = getattr(type_hint, "__name__", str(type_hint))
        # Add to definitions if not already there
        if type_name not in definitions:
            properties = {}
            required = []

            # Check if this is a dataclass with fields
            if hasattr(type_hint, "__dataclass_fields__"):
                # Resolve type hints to handle forward references from __future__ annotations
                try:
                    resolved_hints = get_type_hints(type_hint)
                except Exception:
                    resolved_hints = {}

                # Use dataclass fields to get proper info including defaults and metadata
                for field_name, field_info in type_hint.__dataclass_fields__.items():
                    # Skip fields marked with serialize="omit" in metadata
                    if field_info.metadata:
                        # Check for mashumaro field_options
                        if "serialize" in field_info.metadata:
                            if field_info.metadata["serialize"] == "omit":
                                continue

                    # Use resolved type hint if available, otherwise fall back to field type
                    field_type = resolved_hints.get(field_name, field_info.type)
                    field_schema = _get_type_schema(field_type, definitions)

                    # Add default value if present
                    if field_info.default is not MISSING:
                        field_schema["default"] = field_info.default
                    elif (
                        hasattr(field_info, "default_factory")
                        and field_info.default_factory is not MISSING
                    ):
                        # Has a default factory - don't add anything, just skip
                        pass

                    properties[field_name] = field_schema

                    # Check if field is required (not Optional and no default)
                    has_default = field_info.default is not MISSING or (
                        hasattr(field_info, "default_factory")
                        and field_info.default_factory is not MISSING
                    )
                    is_optional = get_origin(field_type) in (
                        Union,
                        UnionType,
                    ) and NoneType in get_args(field_type)
                    if not has_default and not is_optional:
                        required.append(field_name)
            elif hasattr(type_hint, "__annotations__"):
                # Fallback for non-dataclass types with annotations
                for field_name, field_type in type_hint.__annotations__.items():
                    properties[field_name] = _get_type_schema(field_type, definitions)
                    # Check if field is required (not Optional)
                    if not (
                        get_origin(field_type) in (Union, UnionType)
                        and NoneType in get_args(field_type)
                    ):
                        required.append(field_name)
            else:
                # Class without dataclass fields or annotations - treat as generic object
                pass  # Will create empty properties

            definitions[type_name] = {
                "type": "object",
                "properties": properties,
            }
            if required:
                definitions[type_name]["required"] = required

        return {"$ref": f"#/components/schemas/{type_name}"}

    # Handle Any
    if type_hint is Any:
        return {"type": "object"}

    # Fallback - for types we don't recognize, at least return a generic object type
    return {"type": "object"}


def _parse_docstring(  # noqa: PLR0915
    func: Callable[..., Any],
) -> tuple[str, str, dict[str, str]]:
    """Parse docstring to extract summary, description and parameter descriptions.

    Returns:
        Tuple of (short_summary, full_description, param_descriptions)

    Handles multiple docstring formats:
    - reStructuredText (:param name: description)
    - Google style (Args: section)
    - NumPy style (Parameters section)
    """
    docstring = inspect.getdoc(func)
    if not docstring:
        return "", "", {}

    lines = docstring.split("\n")
    description_lines = []
    param_descriptions = {}
    current_section = "description"
    current_param = None

    for line in lines:
        stripped = line.strip()

        # Check for section headers
        if stripped.lower() in ("args:", "arguments:", "parameters:", "params:"):
            current_section = "params"
            current_param = None
            continue
        if stripped.lower() in (
            "returns:",
            "return:",
            "yields:",
            "raises:",
            "raises",
            "examples:",
            "example:",
            "note:",
            "notes:",
            "see also:",
            "warning:",
            "warnings:",
        ):
            current_section = "other"
            current_param = None
            continue

        # Parse :param style
        if stripped.startswith(":param "):
            current_section = "params"
            parts = stripped[7:].split(":", 1)
            if len(parts) == 2:
                current_param = parts[0].strip()
                desc = parts[1].strip()
                if desc:
                    param_descriptions[current_param] = desc
            continue

        if stripped.startswith((":type ", ":rtype", ":return")):
            current_section = "other"
            current_param = None
            continue

        # In params section, detect param lines (indented or starting with name)
        if current_section == "params" and stripped:
            # Google/NumPy style: "param_name: description" or "param_name (type): description"
            if ":" in stripped and not stripped.startswith(" "):
                # Likely a parameter definition
                if "(" in stripped and ")" in stripped:
                    # Format: param_name (type): description
                    param_part = stripped.split(":")[0]
                    param_name = param_part.split("(")[0].strip()
                    desc_part = ":".join(stripped.split(":")[1:]).strip()
                else:
                    # Format: param_name: description
                    parts = stripped.split(":", 1)
                    param_name = parts[0].strip()
                    desc_part = parts[1].strip() if len(parts) > 1 else ""

                if param_name and not param_name.startswith(("return", "yield", "raise")):
                    current_param = param_name
                    if desc_part:
                        param_descriptions[current_param] = desc_part
            elif current_param and stripped:
                # Continuation of previous parameter description
                param_descriptions[current_param] = (
                    param_descriptions.get(current_param, "") + " " + stripped
                ).strip()
            continue

        # Collect description lines (only before params/returns sections)
        if current_section == "description" and stripped:
            description_lines.append(stripped)
        elif current_section == "description" and not stripped and description_lines:
            # Empty line in description - keep it for paragraph breaks
            description_lines.append("")

    # Join description lines, removing excessive empty lines
    description = "\n".join(description_lines).strip()
    # Collapse multiple empty lines into one
    while "\n\n\n" in description:
        description = description.replace("\n\n\n", "\n\n")

    # Extract first sentence/line as summary
    summary = ""
    if description:
        # Get first line or first sentence (whichever is shorter)
        first_line = description.split("\n")[0]
        # Try to get first sentence (ending with .)
        summary = first_line.split(".")[0] + "." if "." in first_line else first_line

    return summary, description, param_descriptions


def generate_openapi_spec(
    command_handlers: dict[str, APICommandHandler],
    server_url: str = "http://localhost:8095",
    version: str = "1.0.0",
) -> dict[str, Any]:
    """Generate OpenAPI 3.0 specification from API command handlers."""
    definitions: dict[str, Any] = {}
    paths: dict[str, Any] = {}

    for command, handler in sorted(command_handlers.items()):
        # Parse docstring
        summary, description, param_descriptions = _parse_docstring(handler.target)

        # Build request body
        request_body_properties = {}
        request_body_required = []

        for param_name, param in handler.signature.parameters.items():
            if param_name == "self":
                continue

            param_type = handler.type_hints.get(param_name, Any)
            param_schema = _get_type_schema(param_type, definitions)
            param_description = param_descriptions.get(param_name, "")

            # Check if parameter is required
            is_required = param.default is inspect.Parameter.empty

            # Add default value if present
            if not is_required:
                # Try to serialize the default value
                try:
                    if param.default is None:
                        param_schema["default"] = None
                    elif isinstance(param.default, (str, int, float, bool)):
                        param_schema["default"] = param.default
                    elif isinstance(param.default, Enum):
                        param_schema["default"] = param.default.value
                    elif isinstance(param.default, (list, dict)):
                        param_schema["default"] = param.default
                except Exception:  # noqa: S110
                    # If we can't serialize it, just skip the default
                    pass

            # Add to request body properties
            request_body_properties[param_name] = {
                **param_schema,
                "description": param_description,
            }
            if is_required:
                request_body_required.append(param_name)

        # Build response
        return_type = handler.type_hints.get("return", Any)
        response_schema = _get_type_schema(return_type, definitions)

        # Build path item
        path = f"/{command}"
        paths[path] = {
            "post": {
                "summary": summary or command,
                "description": description,
                "operationId": command.replace("/", "_"),
                "tags": [command.split("/")[0]] if "/" in command else ["general"],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": request_body_properties,
                                **(
                                    {"required": request_body_required}
                                    if request_body_required
                                    else {}
                                ),
                            }
                        }
                    },
                }
                if request_body_properties
                else None,
                "responses": {
                    "200": {
                        "description": "Successful response",
                        "content": {"application/json": {"schema": response_schema}},
                    },
                    "400": {"description": "Bad request - invalid parameters"},
                    "500": {"description": "Internal server error"},
                },
            }
        }

        # Remove requestBody if empty
        if not request_body_properties:
            del paths[path]["post"]["requestBody"]

    # Build OpenAPI spec
    return {
        "openapi": "3.0.0",
        "info": {
            "title": "Music Assistant API",
            "version": version,
            "description": """
# Music Assistant API Documentation

Music Assistant provides two ways to interact with the API:

## WebSocket API (Recommended)
- **Endpoint:** `ws://{server}/ws`
- **Features:**
  - Full API access to all commands
  - Real-time event updates
  - Bi-directional communication
  - Best for applications that need live updates

### WebSocket Message Format
Send commands as JSON messages:
```json
{
  "message_id": "unique-id",
  "command": "command/name",
  "args": {
    "param1": "value1",
    "param2": "value2"
  }
}
```

Receive responses:
```json
{
  "message_id": "unique-id",
  "result": { ... }
}
```

## REST API (Simple)
- **Endpoint:** `POST /api`
- **Features:**
  - Simple HTTP POST requests
  - JSON request/response
  - Best for simple, incidental commands

### REST Message Format
Send POST request to `/api` with JSON body:
```json
{
  "command": "command/name",
  "args": {
    "param1": "value1",
    "param2": "value2"
  }
}
```

Receive JSON response with result.

## Authentication
Authentication is not yet implemented but will be added in a future release.

## API Commands
All commands listed below are available via both WebSocket and REST interfaces.
            """.strip(),
            "contact": {
                "name": "Music Assistant",
                "url": "https://music-assistant.io",
            },
        },
        "servers": [{"url": server_url, "description": "Music Assistant Server"}],
        "paths": paths,
        "components": {"schemas": definitions},
    }


def generate_html_docs(  # noqa: PLR0915
    command_handlers: dict[str, APICommandHandler],
    server_url: str = "http://localhost:8095",
    version: str = "1.0.0",
) -> str:
    """Generate HTML documentation from API command handlers."""
    # Group commands by category
    categories: dict[str, list[tuple[str, APICommandHandler]]] = {}
    for command, handler in sorted(command_handlers.items()):
        category = command.split("/")[0] if "/" in command else "general"
        if category not in categories:
            categories[category] = []
        categories[category].append((command, handler))

    # Start building HTML
    html_parts = [
        """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Music Assistant API Documentation</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
                Oxygen, Ubuntu, Cantarell, sans-serif;
            line-height: 1.6;
            color: #333;
            background: #f5f5f5;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }
        .header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 40px 20px;
            text-align: center;
            margin-bottom: 30px;
            border-radius: 8px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }
        .header h1 {
            font-size: 2.5em;
            margin-bottom: 10px;
        }
        .header p {
            font-size: 1.1em;
            opacity: 0.9;
        }
        .intro {
            background: white;
            padding: 30px;
            margin-bottom: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        .intro h2 {
            color: #667eea;
            margin-bottom: 15px;
        }
        .intro h3 {
            color: #764ba2;
            margin: 20px 0 10px 0;
        }
        .intro pre {
            background: #f8f9fa;
            padding: 15px;
            border-radius: 4px;
            overflow-x: auto;
            border-left: 4px solid #667eea;
        }
        .intro code {
            font-family: 'Monaco', 'Courier New', monospace;
            font-size: 0.9em;
        }
        .category {
            background: white;
            margin-bottom: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            overflow: hidden;
        }
        .category-header {
            background: #667eea;
            color: white;
            padding: 20px;
            font-size: 1.5em;
            font-weight: bold;
            text-transform: capitalize;
        }
        .command {
            border-bottom: 1px solid #e0e0e0;
            padding: 20px;
        }
        .command:last-child {
            border-bottom: none;
        }
        .command-name {
            font-size: 1.2em;
            font-weight: bold;
            color: #667eea;
            font-family: 'Monaco', 'Courier New', monospace;
            margin-bottom: 10px;
        }
        .command-description {
            color: #666;
            margin-bottom: 15px;
        }
        .params, .returns {
            margin-top: 15px;
        }
        .params h4, .returns h4 {
            color: #764ba2;
            margin-bottom: 10px;
            font-size: 1em;
        }
        .param {
            background: #f8f9fa;
            padding: 10px;
            margin: 5px 0;
            border-radius: 4px;
            border-left: 3px solid #667eea;
        }
        .param-name {
            font-weight: bold;
            color: #333;
            font-family: 'Monaco', 'Courier New', monospace;
        }
        .param-type {
            color: #764ba2;
            font-style: italic;
            font-size: 0.9em;
        }
        .param-required {
            color: #e74c3c;
            font-size: 0.85em;
            font-weight: bold;
        }
        .param-optional {
            color: #95a5a6;
            font-size: 0.85em;
        }
        .param-description {
            color: #666;
            margin-top: 5px;
        }
        .return-type {
            background: #f8f9fa;
            padding: 10px;
            border-radius: 4px;
            border-left: 3px solid #764ba2;
            font-family: 'Monaco', 'Courier New', monospace;
            color: #764ba2;
        }
        .nav {
            background: white;
            padding: 20px;
            margin-bottom: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        .nav h3 {
            color: #667eea;
            margin-bottom: 15px;
        }
        .nav ul {
            list-style: none;
        }
        .nav li {
            margin: 5px 0;
        }
        .nav a {
            color: #667eea;
            text-decoration: none;
            text-transform: capitalize;
        }
        .nav a:hover {
            text-decoration: underline;
        }
        .download-link {
            display: inline-block;
            background: #667eea;
            color: white;
            padding: 10px 20px;
            border-radius: 4px;
            text-decoration: none;
            margin-top: 10px;
        }
        .download-link:hover {
            background: #764ba2;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Music Assistant API Documentation</h1>
            <p>Version """,
        version,
        """</p>
        </div>

        <div class="intro">
            <h2>Getting Started</h2>
            <p>Music Assistant provides two ways to interact with the API:</p>

            <h3>🔌 WebSocket API (Recommended)</h3>
            <p>
                The WebSocket API provides full access to all commands
                and <strong>real-time event updates</strong>.
            </p>
            <ul style="margin-left: 20px; margin-top: 10px;">
                <li><strong>Endpoint:</strong> <code>ws://""",
        server_url.replace("http://", "").replace("https://", ""),
        """/ws</code></li>
                <li>
                    <strong>Best for:</strong> Applications that need live
                    updates and real-time communication
                </li>
                <li>
                    <strong>Bonus:</strong> When connected, you automatically
                    receive event messages for state changes
                </li>
            </ul>
            <p style="margin-top: 10px;"><strong>Sending commands:</strong></p>
            <pre><code>{
  "message_id": "unique-id-123",
  "command": "players/all",
  "args": {}
}</code></pre>
            <p style="margin-top: 10px;"><strong>Receiving events:</strong></p>
            <p>
                Once connected, you will automatically receive event messages
                whenever something changes:
            </p>
            <pre><code>{
  "event": "player_updated",
  "data": {
    "player_id": "player_123",
    ...player data...
  }
}</code></pre>

            <h3>🌐 REST API (Simple)</h3>
            <p>
                The REST API provides a simple HTTP interface for
                executing commands.
            </p>
            <ul style="margin-left: 20px; margin-top: 10px;">
                <li><strong>Endpoint:</strong> <code>POST """,
        server_url,
        """/api</code></li>
                <li>
                    <strong>Best for:</strong> Simple, incidental commands
                    without need for real-time updates
                </li>
            </ul>
            <p style="margin-top: 10px;"><strong>Example request:</strong></p>
            <pre><code>{
  "command": "players/all",
  "args": {}
}</code></pre>

            <h3>📥 OpenAPI Specification</h3>
            <p>Download the OpenAPI 3.0 specification for automated client generation:</p>
            <a href="/openapi.json" class="download-link">Download openapi.json</a>

            <h3>🚀 Interactive API Explorers</h3>
            <p>
                Try out the API interactively with our API explorers.
                Test endpoints, see live responses, and explore the full API:
            </p>
            <div style="margin-top: 15px;">
                <a href="/api-explorer" class="download-link" style="margin-right: 10px;">
                    Swagger UI Explorer
                </a>
                <a href="/api-docs" class="download-link">
                    ReDoc Documentation
                </a>
            </div>

            <h3>📡 WebSocket Events</h3>
            <p>
                When connected via WebSocket, you automatically receive
                real-time event notifications:
            </p>
            <div style="margin-top: 15px; margin-left: 20px;">
                <strong>Player Events:</strong>
                <ul style="margin-left: 20px;">
                    <li><code>player_added</code> - New player discovered</li>
                    <li><code>player_updated</code> - Player state changed</li>
                    <li><code>player_removed</code> - Player disconnected</li>
                    <li><code>player_config_updated</code> - Player settings changed</li>
                </ul>

                <strong style="margin-top: 10px; display: block;">Queue Events:</strong>
                <ul style="margin-left: 20px;">
                    <li><code>queue_added</code> - New queue created</li>
                    <li><code>queue_updated</code> - Queue state changed</li>
                    <li><code>queue_items_updated</code> - Queue content changed</li>
                    <li><code>queue_time_updated</code> - Playback position updated</li>
                </ul>

                <strong style="margin-top: 10px; display: block;">Library Events:</strong>
                <ul style="margin-left: 20px;">
                    <li><code>media_item_added</code> - New media added to library</li>
                    <li><code>media_item_updated</code> - Media metadata updated</li>
                    <li><code>media_item_deleted</code> - Media removed from library</li>
                    <li><code>media_item_played</code> - Media playback started</li>
                </ul>

                <strong style="margin-top: 10px; display: block;">System Events:</strong>
                <ul style="margin-left: 20px;">
                    <li><code>providers_updated</code> - Provider status changed</li>
                    <li><code>sync_tasks_updated</code> - Sync progress updated</li>
                    <li><code>application_shutdown</code> - Server shutting down</li>
                </ul>
            </div>
        </div>

        <div class="nav">
            <h3>Quick Navigation</h3>
            <ul>
""",
    ]

    # Add navigation links
    for category in sorted(categories.keys()):
        html_parts.append(
            f'                <li><a href="#{category}">{category}</a> '
            f"({len(categories[category])} commands)</li>\n"
        )

    html_parts.append(
        """            </ul>
        </div>
"""
    )

    # Add commands by category
    for category, commands in sorted(categories.items()):
        html_parts.append(f'        <div class="category" id="{category}">\n')
        html_parts.append(f'            <div class="category-header">{category}</div>\n')

        for command, handler in commands:
            _, description, param_descriptions = _parse_docstring(handler.target)

            html_parts.append('            <div class="command">\n')
            html_parts.append(f'                <div class="command-name">{command}</div>\n')

            if description:
                html_parts.append(
                    f'                <div class="command-description">{description}</div>\n'
                )

            # Parameters
            params_html = []
            for param_name, param in handler.signature.parameters.items():
                if param_name == "self":
                    continue

                param_type = handler.type_hints.get(param_name, Any)
                is_required = param.default is inspect.Parameter.empty
                param_desc = param_descriptions.get(param_name, "")

                # Format type name
                type_name = _format_type_name(param_type)
                if get_origin(param_type):
                    origin = get_origin(param_type)
                    args = get_args(param_type)
                    if origin is Union or origin is UnionType:
                        type_name = " | ".join(_format_type_name(arg) for arg in args)
                    elif origin in (list, tuple):
                        if args:
                            inner_type = _format_type_name(args[0])
                            type_name = f"{origin.__name__}[{inner_type}]"
                    elif origin is dict:
                        if len(args) == 2:
                            key_type = _format_type_name(args[0])
                            val_type = _format_type_name(args[1])
                            type_name = f"dict[{key_type}, {val_type}]"

                required_badge = (
                    '<span class="param-required">required</span>'
                    if is_required
                    else '<span class="param-optional">optional</span>'
                )

                # Format default value
                default_str = ""
                if not is_required and param.default is not None:
                    try:
                        if isinstance(param.default, str):
                            default_str = f' = "{param.default}"'
                        elif isinstance(param.default, Enum):
                            default_str = f" = {param.default.value}"
                        elif isinstance(param.default, (int, float, bool, list, dict)):
                            default_str = f" = {param.default}"
                    except Exception:  # noqa: S110
                        pass  # Can't serialize, skip default

                params_html.append(
                    f'                    <div class="param">\n'
                    f'                        <span class="param-name">{param_name}</span>\n'
                    f'                        <span class="param-type">'
                    f"({type_name}{default_str})</span>\n"
                    f"                        {required_badge}\n"
                )
                if param_desc:
                    params_html.append(
                        f'                        <div class="param-description">'
                        f"{param_desc}</div>\n"
                    )
                params_html.append("                    </div>\n")

            if params_html:
                html_parts.append('                <div class="params">\n')
                html_parts.append("                    <h4>Parameters</h4>\n")
                html_parts.extend(params_html)
                html_parts.append("                </div>\n")

            # Return type
            return_type = handler.type_hints.get("return", Any)
            if return_type and return_type is not NoneType:
                type_name = _format_type_name(return_type)
                if get_origin(return_type):
                    origin = get_origin(return_type)
                    args = get_args(return_type)
                    if origin in (list, tuple) and args:
                        inner_type = _format_type_name(args[0])
                        type_name = f"{origin.__name__}[{inner_type}]"
                    elif origin is Union or origin is UnionType:
                        type_name = " | ".join(_format_type_name(arg) for arg in args)

                html_parts.append('                <div class="returns">\n')
                html_parts.append("                    <h4>Returns</h4>\n")
                html_parts.append(
                    f'                    <div class="return-type">{type_name}</div>\n'
                )
                html_parts.append("                </div>\n")

            html_parts.append("            </div>\n")

        html_parts.append("        </div>\n")

    html_parts.append(
        """    </div>
</body>
</html>
"""
    )

    return "".join(html_parts)
