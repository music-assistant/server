"""Helpers for generating API documentation and OpenAPI specifications."""

from __future__ import annotations

import collections.abc
import inspect
import re
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

    # Handle PluginSource - replace with PlayerSource (parent type)
    if hasattr(type_hint, "__name__") and type_hint.__name__ == "PluginSource":
        if (
            hasattr(type_hint, "__module__")
            and type_hint.__module__ == "music_assistant.models.plugin"
        ):
            return "PlayerSource"

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


def _generate_type_alias_description(type_alias: Any, alias_name: str) -> str:
    """Generate a human-readable description of a type alias from its definition.

    :param type_alias: The type alias to describe (e.g., ConfigValueType)
    :param alias_name: The name of the alias for display
    :return: A human-readable description string
    """
    # Get the union args
    args = get_args(type_alias)
    if not args:
        return f"Type alias for {alias_name}."

    # Convert each type to a readable name
    type_names = []
    for arg in args:
        origin = get_origin(arg)
        if origin in (list, tuple):
            # Handle list types
            inner_args = get_args(arg)
            if inner_args:
                inner_type = inner_args[0]
                if inner_type is bool:
                    type_names.append("array of boolean")
                elif inner_type is int:
                    type_names.append("array of integer")
                elif inner_type is float:
                    type_names.append("array of number")
                elif inner_type is str:
                    type_names.append("array of string")
                else:
                    type_names.append(
                        f"array of {getattr(inner_type, '__name__', str(inner_type))}"
                    )
            else:
                type_names.append("array")
        elif arg is type(None) or arg is NoneType:
            type_names.append("null")
        elif arg is bool:
            type_names.append("boolean")
        elif arg is int:
            type_names.append("integer")
        elif arg is float:
            type_names.append("number")
        elif arg is str:
            type_names.append("string")
        elif hasattr(arg, "__name__"):
            type_names.append(arg.__name__)
        else:
            type_names.append(str(arg))

    # Format the list nicely
    if len(type_names) == 1:
        types_str = type_names[0]
    elif len(type_names) == 2:
        types_str = f"{type_names[0]} or {type_names[1]}"
    else:
        types_str = f"{', '.join(type_names[:-1])}, or {type_names[-1]}"

    return f"Type alias for {alias_name.lower()} types. Can be {types_str}."


def _get_type_schema(  # noqa: PLR0911, PLR0915
    type_hint: Any, definitions: dict[str, Any]
) -> dict[str, Any]:
    """Convert a Python type hint to an OpenAPI schema."""
    # Check if type_hint matches a type alias that was expanded by get_type_hints()
    # Import type aliases to compare against
    from music_assistant_models.config_entries import (  # noqa: PLC0415
        ConfigValueType as config_value_type,  # noqa: N813
    )
    from music_assistant_models.media_items import (  # noqa: PLC0415
        MediaItemType as media_item_type,  # noqa: N813
    )

    if type_hint == config_value_type:
        # This is the expanded ConfigValueType, treat it as the type alias
        return _get_type_schema("ConfigValueType", definitions)
    if type_hint == media_item_type:
        # This is the expanded MediaItemType, treat it as the type alias
        return _get_type_schema("MediaItemType", definitions)

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

        # Special handling for type aliases - create proper schema definitions
        if type_hint == "ConfigValueType":
            if "ConfigValueType" not in definitions:
                from music_assistant_models.config_entries import (  # noqa: PLC0415
                    ConfigValueType as config_value_type,  # noqa: N813
                )

                # Dynamically create oneOf schema with description from the actual type
                cvt_args = get_args(config_value_type)
                definitions["ConfigValueType"] = {
                    "description": _generate_type_alias_description(
                        config_value_type, "configuration value"
                    ),
                    "oneOf": [_get_type_schema(arg, definitions) for arg in cvt_args],
                }
            return {"$ref": "#/components/schemas/ConfigValueType"}

        if type_hint == "MediaItemType":
            if "MediaItemType" not in definitions:
                from music_assistant_models.media_items import (  # noqa: PLC0415
                    MediaItemType as media_item_type,  # noqa: N813
                )

                # Dynamically create oneOf schema with description from the actual type
                mit_origin = get_origin(media_item_type)
                if mit_origin in (Union, UnionType):
                    mit_args = get_args(media_item_type)
                    definitions["MediaItemType"] = {
                        "description": _generate_type_alias_description(
                            media_item_type, "media item"
                        ),
                        "oneOf": [_get_type_schema(arg, definitions) for arg in mit_args],
                    }
                else:
                    definitions["MediaItemType"] = _get_type_schema(media_item_type, definitions)
            return {"$ref": "#/components/schemas/MediaItemType"}

        # Handle PluginSource - replace with PlayerSource (parent type)
        if type_hint == "PluginSource":
            return _get_type_schema("PlayerSource", definitions)

        # Check if it looks like a simple class name (no special chars, starts with uppercase)
        # Examples: "PlayerType", "DeviceInfo", "PlaybackState"
        # Exclude generic types like "Any", "Union", "Optional", etc.
        excluded_types = {"Any", "Union", "Optional", "List", "Dict", "Tuple", "Set"}
        if type_hint.isidentifier() and type_hint[0].isupper() and type_hint not in excluded_types:
            # Create a schema reference for this type
            if type_hint not in definitions:
                definitions[type_hint] = {"type": "object"}
            return {"$ref": f"#/components/schemas/{type_hint}"}

        # If it's "Any", return generic object without creating a schema
        if type_hint == "Any":
            return {"type": "object"}

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

    # Handle PluginSource - replace with PlayerSource (parent type)
    if hasattr(type_hint, "__name__") and type_hint.__name__ == "PluginSource":
        # Check if this is PluginSource from music_assistant.models.plugin
        if (
            hasattr(type_hint, "__module__")
            and type_hint.__module__ == "music_assistant.models.plugin"
        ):
            # Replace with PlayerSource from music_assistant.models.player
            from music_assistant.models.player import PlayerSource  # noqa: PLC0415

            return _get_type_schema(PlayerSource, definitions)

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

        # Detect bullet-style params even without explicit section header
        # Format: "- param_name: description"
        if stripped.startswith("- ") and ":" in stripped:
            # This is likely a bullet-style parameter
            current_section = "params"
            content = stripped[2:]  # Remove "- "
            parts = content.split(":", 1)
            param_name = parts[0].strip()
            desc_part = parts[1].strip() if len(parts) > 1 else ""
            if param_name and not param_name.startswith(("return", "yield", "raise")):
                current_param = param_name
                if desc_part:
                    param_descriptions[current_param] = desc_part
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
    """Generate simplified OpenAPI 3.0 specification focusing on data models.

    This spec documents the single /api endpoint and all data models/schemas.
    For detailed command documentation, see the Commands Reference page.
    """
    definitions: dict[str, Any] = {}

    # Build all schemas from command handlers (this populates definitions)
    for handler in command_handlers.values():
        # Skip aliases - they are for backward compatibility only
        if handler.alias:
            continue
        # Build parameter schemas
        for param_name in handler.signature.parameters:
            if param_name == "self":
                continue
            # Skip return_type parameter (used only for type hints)
            if param_name == "return_type":
                continue
            param_type = handler.type_hints.get(param_name, Any)
            # Skip Any types as they don't provide useful schema information
            if param_type is not Any and str(param_type) != "typing.Any":
                _get_type_schema(param_type, definitions)

        # Build return type schema
        return_type = handler.type_hints.get("return", Any)
        # Skip Any types as they don't provide useful schema information
        if return_type is not Any and str(return_type) != "typing.Any":
            _get_type_schema(return_type, definitions)

    # Build a single /api endpoint with generic request/response
    paths = {
        "/api": {
            "post": {
                "summary": "Execute API command",
                "description": (
                    "Execute any Music Assistant API command.\n\n"
                    "See the **Commands Reference** page for a complete list of available "
                    "commands with examples."
                ),
                "operationId": "execute_command",
                "security": [{"bearerAuth": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["command"],
                                "properties": {
                                    "command": {
                                        "type": "string",
                                        "description": (
                                            "The command to execute (e.g., 'players/all')"
                                        ),
                                        "example": "players/all",
                                    },
                                    "args": {
                                        "type": "object",
                                        "description": "Command arguments (varies by command)",
                                        "additionalProperties": True,
                                        "example": {},
                                    },
                                },
                            },
                            "examples": {
                                "get_players": {
                                    "summary": "Get all players",
                                    "value": {"command": "players/all", "args": {}},
                                },
                                "play_media": {
                                    "summary": "Play media on a player",
                                    "value": {
                                        "command": "players/cmd/play",
                                        "args": {"player_id": "player123"},
                                    },
                                },
                            },
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Successful command execution",
                        "content": {
                            "application/json": {
                                "schema": {"description": "Command result (varies by command)"}
                            }
                        },
                    },
                    "400": {"description": "Bad request - invalid command or parameters"},
                    "401": {"description": "Unauthorized - authentication required"},
                    "403": {"description": "Forbidden - insufficient permissions"},
                    "500": {"description": "Internal server error"},
                },
            }
        },
        "/auth/login": {
            "post": {
                "summary": "Authenticate with credentials",
                "description": "Login with username and password to obtain an access token.",
                "operationId": "auth_login",
                "tags": ["Authentication"],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "provider_id": {
                                        "type": "string",
                                        "description": "Auth provider ID (defaults to 'builtin')",
                                        "example": "builtin",
                                    },
                                    "credentials": {
                                        "type": "object",
                                        "description": "Provider-specific credentials",
                                        "properties": {
                                            "username": {"type": "string"},
                                            "password": {"type": "string"},
                                        },
                                    },
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Login successful",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "success": {"type": "boolean"},
                                        "token": {"type": "string"},
                                        "user": {"type": "object"},
                                    },
                                }
                            }
                        },
                    },
                    "400": {"description": "Invalid credentials"},
                },
            }
        },
        "/auth/providers": {
            "get": {
                "summary": "Get available auth providers",
                "description": "Returns list of configured authentication providers.",
                "operationId": "auth_providers",
                "tags": ["Authentication"],
                "responses": {
                    "200": {
                        "description": "List of auth providers",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "providers": {
                                            "type": "array",
                                            "items": {"type": "object"},
                                        }
                                    },
                                }
                            }
                        },
                    }
                },
            }
        },
        "/setup": {
            "post": {
                "summary": "Initial server setup",
                "description": (
                    "Handle initial setup of the Music Assistant server including creating "
                    "the first admin user. Only accessible when no users exist."
                ),
                "operationId": "setup",
                "tags": ["Server"],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["username", "password"],
                                "properties": {
                                    "username": {"type": "string"},
                                    "password": {"type": "string"},
                                    "display_name": {"type": "string"},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Setup completed successfully",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "success": {"type": "boolean"},
                                        "token": {"type": "string"},
                                        "user": {"type": "object"},
                                    },
                                }
                            }
                        },
                    },
                    "400": {"description": "Setup already completed or invalid request"},
                },
            }
        },
        "/info": {
            "get": {
                "summary": "Get server info",
                "description": (
                    "Returns server information including schema version and authentication status."
                ),
                "operationId": "get_info",
                "tags": ["Server"],
                "responses": {
                    "200": {
                        "description": "Server information",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "schema_version": {"type": "integer"},
                                        "server_version": {"type": "string"},
                                        "onboard_done": {"type": "boolean"},
                                        "homeassistant_addon": {"type": "boolean"},
                                    },
                                }
                            }
                        },
                    }
                },
            }
        },
    }

    # Build OpenAPI spec
    return {
        "openapi": "3.0.0",
        "info": {
            "title": "Music Assistant API",
            "version": version,
            "description": (
                "Music Assistant API provides control over your music library, "
                "players, and playback.\n\n"
                "This specification documents the API structure and data models. "
                "For a complete list of available commands with examples, "
                "see the Commands Reference page."
            ),
            "contact": {
                "name": "Music Assistant",
                "url": "https://music-assistant.io",
            },
        },
        "servers": [{"url": server_url, "description": "Music Assistant Server"}],
        "paths": paths,
        "components": {
            "schemas": definitions,
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "Access token obtained from /auth/login or /auth/setup",
                }
            },
        },
    }


def _split_union_type(type_str: str) -> list[str]:
    """Split a union type on | but respect brackets and parentheses.

    This ensures that list[A | B] and (A | B) are not split at the inner |.
    """
    parts = []
    current_part = ""
    bracket_depth = 0
    paren_depth = 0
    i = 0
    while i < len(type_str):
        char = type_str[i]
        if char == "[":
            bracket_depth += 1
            current_part += char
        elif char == "]":
            bracket_depth -= 1
            current_part += char
        elif char == "(":
            paren_depth += 1
            current_part += char
        elif char == ")":
            paren_depth -= 1
            current_part += char
        elif char == "|" and bracket_depth == 0 and paren_depth == 0:
            # Check if this is a union separator (has space before and after)
            if (
                i > 0
                and i < len(type_str) - 1
                and type_str[i - 1] == " "
                and type_str[i + 1] == " "
            ):
                parts.append(current_part.strip())
                current_part = ""
                i += 1  # Skip the space after |, the loop will handle incrementing i
            else:
                current_part += char
        else:
            current_part += char
        i += 1
    if current_part.strip():
        parts.append(current_part.strip())
    return parts


def _extract_generic_inner_type(type_str: str) -> str | None:
    """Extract inner type from generic type like list[T] or dict[K, V].

    :param type_str: Type string like "list[str]" or "dict[str, int]"
    :return: Inner type string "str" or "str, int", or None if not a complete generic type
    """
    # Find the matching closing bracket
    bracket_count = 0
    start_idx = type_str.index("[") + 1
    end_idx = -1
    for i in range(start_idx, len(type_str)):
        if type_str[i] == "[":
            bracket_count += 1
        elif type_str[i] == "]":
            if bracket_count == 0:
                end_idx = i
                break
            bracket_count -= 1

    # Check if this is a complete generic type (ends with the closing bracket)
    if end_idx == len(type_str) - 1:
        return type_str[start_idx:end_idx].strip()
    return None


def _parse_dict_type_params(inner_type: str) -> tuple[str, str] | None:
    """Parse key and value types from dict inner type string.

    :param inner_type: The content inside dict[...], e.g., "str, ConfigValueType"
    :return: Tuple of (key_type, value_type) or None if parsing fails
    """
    # Split on comma to get key and value types
    # Need to be careful with nested types like dict[str, list[int]]
    parts = []
    current_part = ""
    bracket_depth = 0
    for char in inner_type:
        if char == "[":
            bracket_depth += 1
            current_part += char
        elif char == "]":
            bracket_depth -= 1
            current_part += char
        elif char == "," and bracket_depth == 0:
            parts.append(current_part.strip())
            current_part = ""
        else:
            current_part += char
    if current_part:
        parts.append(current_part.strip())

    if len(parts) == 2:
        return parts[0], parts[1]
    return None


def _python_type_to_json_type(type_str: str, _depth: int = 0) -> str:
    """Convert Python type string to JSON/JavaScript type string.

    Args:
        type_str: The type string to convert
        _depth: Internal recursion depth tracker (do not set manually)
    """
    # Prevent infinite recursion
    if _depth > 50:
        return "any"

    # Remove typing module prefix and class markers
    type_str = type_str.replace("typing.", "").replace("<class '", "").replace("'>", "")

    # Remove module paths from type names (e.g., "music_assistant.models.Artist" -> "Artist")
    type_str = re.sub(r"[\w.]+\.(\w+)", r"\1", type_str)

    # Check for type aliases that should be preserved as-is
    # These will have schema definitions in the API docs
    if type_str in ("ConfigValueType", "MediaItemType"):
        return type_str

    # Map Python types to JSON types
    type_mappings = {
        "str": "string",
        "int": "integer",
        "float": "number",
        "bool": "boolean",
        "dict": "object",
        "Dict": "object",
        "list": "array",
        "tuple": "array",
        "Tuple": "array",
        "None": "null",
        "NoneType": "null",
    }

    # Check for List/list/UniqueList/tuple with type parameter BEFORE checking for union types
    # This is important because list[A | B] contains " | " but should be handled as a list first
    # codespell:ignore
    if type_str.startswith(("list[", "List[", "UniqueList[", "tuple[", "Tuple[")):
        inner_type = _extract_generic_inner_type(type_str)
        if inner_type:
            # Handle variable-length tuple (e.g., tuple[str, ...])
            # The ellipsis means "variable length of this type"
            if inner_type.endswith(", ..."):
                # Remove the ellipsis and just use the type
                inner_type = inner_type[:-5].strip()
            # Recursively convert the inner type
            inner_json_type = _python_type_to_json_type(inner_type, _depth + 1)
            # For list[A | B], wrap in parentheses to keep it as one unit
            # This prevents "Array of A | B" from being split into separate union parts
            if " | " in inner_json_type:
                return f"Array of ({inner_json_type})"
            return f"Array of {inner_json_type}"

    # Check for dict/Dict with type parameters BEFORE checking for union types
    # This is important because dict[str, A | B] contains " | "
    # but should be handled as a dict first
    # codespell:ignore
    if type_str.startswith(("dict[", "Dict[")):
        inner_type = _extract_generic_inner_type(type_str)
        if inner_type:
            parsed = _parse_dict_type_params(inner_type)
            if parsed:
                key_type_str, value_type_str = parsed
                key_type = _python_type_to_json_type(key_type_str, _depth + 1)
                value_type = _python_type_to_json_type(value_type_str, _depth + 1)
                # Use more descriptive format: "object with {key_type} keys and {value_type} values"
                return f"object with {key_type} keys and {value_type} values"

    # Handle Union types by splitting on | and recursively processing each part
    if " | " in type_str:
        # Use helper to split on | but respect brackets
        parts = _split_union_type(type_str)

        # Filter out None/null types (None, NoneType, null all mean JSON null)
        parts = [part for part in parts if part not in ("None", "NoneType", "null")]

        # If splitting didn't help (only one part or same as input), avoid infinite recursion
        if not parts or (len(parts) == 1 and parts[0] == type_str):
            # Can't split further, return as-is or "any"
            return type_str if parts else "any"

        if parts:
            converted_parts = [_python_type_to_json_type(part, _depth + 1) for part in parts]
            # Remove duplicates while preserving order
            seen = set()
            unique_parts = []
            for part in converted_parts:
                if part not in seen:
                    seen.add(part)
                    unique_parts.append(part)
            return " | ".join(unique_parts)
        return "any"

    # Check for Union/Optional types with brackets
    if "Union[" in type_str or "Optional[" in type_str:
        # Extract content from Union[...] or Optional[...]
        union_match = re.search(r"(?:Union|Optional)\[([^\]]+)\]", type_str)
        if union_match:
            inner = union_match.group(1)
            # Recursively process the union content
            return _python_type_to_json_type(inner, _depth + 1)

    # Direct mapping for basic types
    for py_type, json_type in type_mappings.items():
        if type_str == py_type:
            return json_type

    # Check if it's a complex type (starts with capital letter)
    complex_match = re.search(r"^([A-Z][a-zA-Z0-9_]*)$", type_str)
    if complex_match:
        return complex_match.group(1)

    # Default to the original string if no mapping found
    return type_str


def _make_type_links(type_str: str, server_url: str, as_list: bool = False) -> str:
    """Convert type string to HTML with links to schemas reference for complex types.

    Args:
        type_str: The type string to convert
        server_url: Base server URL for building links
        as_list: If True and type contains |, format as "Any of:" bullet list
    """

    # Find all complex types (capitalized words that aren't basic types)
    def replace_type(match: re.Match[str]) -> str:
        type_name = match.group(0)
        # Check if it's a complex type (starts with capital letter)
        # Exclude basic types and "Array" (which is used in "Array of Type")
        excluded = {"Union", "Optional", "List", "Dict", "Array", "None", "NoneType"}
        if type_name[0].isupper() and type_name not in excluded:
            # Create link to our schemas reference page
            schema_url = f"{server_url}/api-docs/schemas#schema-{type_name}"
            return f'<a href="{schema_url}" class="type-link">{type_name}</a>'
        return type_name

    # If it's a union type with multiple options and as_list is True, format as bullet list
    if as_list and " | " in type_str:
        # Use the bracket/parenthesis-aware splitter
        parts = _split_union_type(type_str)
        # Only use list format if there are 3+ options
        if len(parts) >= 3:
            html = '<div class="type-union"><span class="type-union-label">Any of:</span><ul>'
            for part in parts:
                linked_part = re.sub(r"\b[A-Z][a-zA-Z0-9_]*\b", replace_type, part)
                html += f"<li>{linked_part}</li>"
            html += "</ul></div>"
            return html

    # Replace complex type names with links
    result: str = re.sub(r"\b[A-Z][a-zA-Z0-9_]*\b", replace_type, type_str)
    return result


def generate_commands_json(command_handlers: dict[str, APICommandHandler]) -> list[dict[str, Any]]:
    """Generate JSON representation of all available API commands.

    This is used by client libraries to sync their methods with the server API.

    Returns a list of command objects with the following structure:
    {
        "command": str,  # Command name (e.g., "music/tracks/library_items")
        "category": str,  # Category (e.g., "Music")
        "summary": str,  # Short description
        "description": str,  # Full description
        "parameters": [  # List of parameters
            {
                "name": str,
                "type": str,  # JSON type (string, integer, boolean, etc.)
                "required": bool,
                "description": str
            }
        ],
        "return_type": str,  # Return type
        "authenticated": bool,  # Whether authentication is required
        "required_role": str | None,  # Required user role (if any)
    }
    """
    commands_data = []

    for command, handler in sorted(command_handlers.items()):
        # Skip aliases - they are for backward compatibility only
        if handler.alias:
            continue
        # Parse docstring
        summary, description, param_descriptions = _parse_docstring(handler.target)

        # Get return type
        return_type = handler.type_hints.get("return", Any)
        # If type is already a string (e.g., "ConfigValueType"), use it directly
        return_type_str = _python_type_to_json_type(
            return_type if isinstance(return_type, str) else str(return_type)
        )

        # Extract category from command name
        category = command.split("/")[0] if "/" in command else "general"
        category_display = category.replace("_", " ").title()

        # Build parameters list
        parameters = []
        for param_name, param in handler.signature.parameters.items():
            if param_name in ("self", "return_type"):
                continue

            is_required = param.default is inspect.Parameter.empty
            param_type = handler.type_hints.get(param_name, Any)
            # If type is already a string (e.g., "ConfigValueType"), use it directly
            type_str = param_type if isinstance(param_type, str) else str(param_type)
            json_type_str = _python_type_to_json_type(type_str)
            param_desc = param_descriptions.get(param_name, "")

            parameters.append(
                {
                    "name": param_name,
                    "type": json_type_str,
                    "required": is_required,
                    "description": param_desc,
                }
            )

        commands_data.append(
            {
                "command": command,
                "category": category_display,
                "summary": summary or "",
                "description": description or "",
                "parameters": parameters,
                "return_type": return_type_str,
                "authenticated": handler.authenticated,
                "required_role": handler.required_role,
            }
        )

    return commands_data


def generate_schemas_json(command_handlers: dict[str, APICommandHandler]) -> dict[str, Any]:
    """Generate JSON representation of all schemas/data models.

    Returns a dict mapping schema names to their OpenAPI schema definitions.
    """
    schemas: dict[str, Any] = {}

    for handler in command_handlers.values():
        # Skip aliases - they are for backward compatibility only
        if handler.alias:
            continue
        # Collect schemas from parameters
        for param_name in handler.signature.parameters:
            if param_name == "self":
                continue
            # Skip return_type parameter (used only for type hints)
            if param_name == "return_type":
                continue
            param_type = handler.type_hints.get(param_name, Any)
            if param_type is not Any and str(param_type) != "typing.Any":
                _get_type_schema(param_type, schemas)

        # Collect schemas from return type
        return_type = handler.type_hints.get("return", Any)
        if return_type is not Any and str(return_type) != "typing.Any":
            _get_type_schema(return_type, schemas)

    return schemas
