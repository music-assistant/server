"""Helpers for dealing with API's to interact with Music Assistant."""

from __future__ import annotations

import importlib
import inspect
import logging
from collections.abc import AsyncGenerator, Callable, Coroutine, Iterable, Sequence
from dataclasses import MISSING, dataclass
from datetime import datetime
from enum import Enum
from types import NoneType, UnionType
from typing import Any, TypeVar, Union, get_args, get_origin, get_type_hints

from mashumaro.exceptions import MissingField
from music_assistant_models.media_items.media_item import MediaItem

from music_assistant.helpers.util import try_parse_bool

LOGGER = logging.getLogger(__name__)

_F = TypeVar("_F", bound=Callable[..., Any])

# Cache for resolved type alias strings to avoid repeated imports
_TYPE_ALIAS_CACHE: dict[str, Any] = {}


def _resolve_string_type(type_str: str) -> Any:
    """
    Resolve a string type reference back to the actual type.

    This is needed when type aliases like ConfigValueType are converted to strings
    during type hint resolution to avoid isinstance() errors with complex unions.

    Uses a module-level cache to avoid repeated imports.

    :param type_str: String name of the type (e.g., "ConfigValueType").
    :return: The actual type object, or the string if resolution fails.
    """
    # Check cache first
    if type_str in _TYPE_ALIAS_CACHE:
        return _TYPE_ALIAS_CACHE[type_str]

    type_alias_map = {
        "ConfigValueType": ("music_assistant_models.config_entries", "ConfigValueType"),
        "MediaItemType": ("music_assistant_models.media_items", "MediaItemType"),
    }

    if type_str not in type_alias_map:
        # Cache the string itself for unknown types
        _TYPE_ALIAS_CACHE[type_str] = type_str
        return type_str

    module_name, type_name = type_alias_map[type_str]
    try:
        module = importlib.import_module(module_name)
        resolved_type = getattr(module, type_name)
        # Cache the successfully resolved type
        _TYPE_ALIAS_CACHE[type_str] = resolved_type
        return resolved_type
    except (ImportError, AttributeError) as err:
        LOGGER.warning("Failed to resolve type alias %s: %s", type_str, err)
        # Cache the string to avoid repeated failed attempts
        _TYPE_ALIAS_CACHE[type_str] = type_str
        return type_str


def _resolve_generic_type_args(
    args: tuple[Any, ...],
    func: Callable[..., Coroutine[Any, Any, Any] | AsyncGenerator[Any, Any]],
    config_value_type: Any,
    media_item_type: Any,
) -> tuple[list[Any], bool]:
    """Resolve TypeVars and type aliases in generic type arguments.

    :param args: Type arguments from a generic type (e.g., from list[T] or dict[K, V])
    :param func: The function being analyzed
    :param config_value_type: The ConfigValueType type alias to compare against
    :param media_item_type: The MediaItemType type alias to compare against
    :return: Tuple of (resolved_args, changed) where changed is True if any args were modified
    """
    new_args: list[Any] = []
    changed = False

    for arg in args:
        # Check if arg matches ConfigValueType union (type alias that was expanded)
        if arg == config_value_type:
            # Replace with string reference to preserve type alias
            new_args.append("ConfigValueType")
            changed = True
        # Check if arg matches MediaItemType union (type alias that was expanded)
        elif arg == media_item_type:
            # Replace with string reference to preserve type alias
            new_args.append("MediaItemType")
            changed = True
        elif isinstance(arg, TypeVar):
            # For ItemCls, resolve to concrete type
            if arg.__name__ == "ItemCls" and hasattr(func, "__self__"):
                if hasattr(func.__self__, "item_cls"):
                    new_args.append(func.__self__.item_cls)
                    changed = True
                else:
                    new_args.append(arg)
            # For ConfigValue TypeVars, resolve to string name
            elif "ConfigValue" in arg.__name__:
                new_args.append("ConfigValueType")
                changed = True
            else:
                new_args.append(arg)
        # Check if arg is a Union containing a TypeVar
        elif get_origin(arg) in (Union, UnionType):
            union_args = get_args(arg)
            for union_arg in union_args:
                if isinstance(union_arg, TypeVar) and union_arg.__bound__ is not None:
                    # Resolve the TypeVar in the union
                    union_arg_index = union_args.index(union_arg)
                    resolved = _resolve_typevar_in_union(
                        union_arg, func, union_args, union_arg_index
                    )
                    new_args.append(resolved)
                    changed = True
                    break
            else:
                # No TypeVar found in union, keep as-is
                new_args.append(arg)
        else:
            new_args.append(arg)

    return new_args, changed


def _resolve_typevar_in_union(
    arg: TypeVar,
    func: Callable[..., Coroutine[Any, Any, Any] | AsyncGenerator[Any, Any]],
    args: tuple[Any, ...],
    i: int,
) -> Any:
    """Resolve a TypeVar found in a Union to its concrete type.

    :param arg: The TypeVar to resolve.
    :param func: The function being analyzed.
    :param args: All args from the Union.
    :param i: Index of the TypeVar in the args.
    """
    bound_type = arg.__bound__
    if not bound_type or not hasattr(arg, "__name__"):
        return bound_type

    type_var_name = arg.__name__

    # Map TypeVar names to their type alias names
    if "ConfigValue" in type_var_name:
        return "ConfigValueType"

    if type_var_name == "ItemCls":
        # Resolve ItemCls to the actual media item class (e.g., Artist, Album, Track)
        if hasattr(func, "__self__") and hasattr(func.__self__, "item_cls"):
            resolved_type = func.__self__.item_cls
            # Preserve other types in the union (like None for Optional)
            other_args = [a for j, a in enumerate(args) if j != i]
            if other_args:
                # Reconstruct union with resolved type
                return Union[resolved_type, *other_args]
            return resolved_type
        # Fallback to bound if we can't get item_cls
        return bound_type

    # Check if the bound is MediaItemType by comparing the union
    from music_assistant_models.media_items import (  # noqa: PLC0415
        MediaItemType as media_item_type,  # noqa: N813
    )

    if bound_type == media_item_type:
        return "MediaItemType"

    # Fallback to the bound type
    return bound_type


@dataclass
class APICommandHandler:
    """Model for an API command handler."""

    command: str
    signature: inspect.Signature
    type_hints: dict[str, Any]
    target: Callable[..., Coroutine[Any, Any, Any] | AsyncGenerator[Any, Any]]
    authenticated: bool = True
    required_role: str | None = None  # "admin" or "user" or None
    alias: bool = False  # If True, this is an alias for backward compatibility

    @classmethod
    def parse(
        cls,
        command: str,
        func: Callable[..., Coroutine[Any, Any, Any] | AsyncGenerator[Any, Any]],
        authenticated: bool = True,
        required_role: str | None = None,
        alias: bool = False,
    ) -> APICommandHandler:
        """Parse APICommandHandler by providing a function.

        :param command: The command name/path.
        :param func: The function to handle the command.
        :param authenticated: Whether authentication is required (default: True).
        :param required_role: Required user role ("admin" or "user")
            None for any authenticated user.
        :param alias: Whether this is an alias for backward compatibility (default: False).
        """
        type_hints = get_type_hints(func)
        # workaround for generic typevar ItemCls that needs to be resolved
        # to the real media item type. TODO: find a better way to do this
        # without this hack
        # Import type aliases to compare against
        from music_assistant_models.config_entries import (  # noqa: PLC0415
            ConfigValueType as config_value_type,  # noqa: N813
        )
        from music_assistant_models.media_items import (  # noqa: PLC0415
            MediaItemType as media_item_type,  # noqa: N813
        )

        for key, value in type_hints.items():
            # Handle generic types (list, tuple, dict, etc.) that may contain TypeVars
            # For example: list[ItemCls] should become list[Artist]
            # For example: dict[str, ConfigValueType] should preserve ConfigValueType
            origin = get_origin(value)
            if origin in (list, tuple, set, frozenset, dict):
                args = get_args(value)
                if args:
                    new_args, changed = _resolve_generic_type_args(
                        args, func, config_value_type, media_item_type
                    )
                    if changed:
                        # Reconstruct the generic type with resolved TypeVars
                        type_hints[key] = origin[tuple(new_args)]
                continue

            # Handle Union types that may contain TypeVars
            # For example: _ConfigValueT | ConfigValueType should become just "ConfigValueType"
            # when _ConfigValueT is bound to ConfigValueType
            if origin is Union or origin is UnionType:
                args = get_args(value)
                # Check if union contains a TypeVar
                # If the TypeVar's bound is a union that was flattened into the current union,
                # we can just use the bound type for documentation purposes
                typevar_found = False
                for i, arg in enumerate(args):
                    if isinstance(arg, TypeVar) and arg.__bound__ is not None:
                        typevar_found = True
                        type_hints[key] = _resolve_typevar_in_union(arg, func, args, i)
                        break
                if typevar_found:
                    continue
            if not hasattr(value, "__name__"):
                continue
            if value.__name__ == "ItemCls":
                type_hints[key] = func.__self__.item_cls  # type: ignore[attr-defined]
            # Resolve TypeVars to their bound type for API documentation
            # This handles cases like _ConfigValueT which should show as ConfigValueType
            elif isinstance(value, TypeVar):
                if value.__bound__ is not None:
                    type_hints[key] = value.__bound__
        return APICommandHandler(
            command=command,
            signature=inspect.signature(func),
            type_hints=type_hints,
            target=func,
            authenticated=authenticated,
            required_role=required_role,
            alias=alias,
        )


def api_command(
    command: str, authenticated: bool = True, required_role: str | None = None
) -> Callable[[_F], _F]:
    """Decorate a function as API route/command.

    :param command: The command name/path.
    :param authenticated: Whether authentication is required (default: True).
    :param required_role: Required user role ("admin" or "user"), None means any authenticated user.
    """

    def decorate(func: _F) -> _F:
        func.api_cmd = command  # type: ignore[attr-defined]
        func.api_authenticated = authenticated  # type: ignore[attr-defined]
        func.api_required_role = required_role  # type: ignore[attr-defined]
        return func

    return decorate


def parse_arguments(
    func_sig: inspect.Signature,
    func_types: dict[str, Any],
    args: dict[str, Any] | None,
    strict: bool = False,
) -> dict[str, Any]:
    """Parse (and convert) incoming arguments to correct types."""
    if args is None:
        args = {}
    final_args = {}
    # ignore extra args if not strict
    if strict:
        for key, value in args.items():
            if key not in func_sig.parameters:
                raise KeyError(f"Invalid parameter: '{key}'")
    # parse arguments to correct type
    for name, param in func_sig.parameters.items():
        value = args.get(name)
        default = MISSING if param.default is inspect.Parameter.empty else param.default
        try:
            final_args[name] = parse_value(name, value, func_types[name], default)
        except TypeError:
            # retry one more time with allow_value_convert=True
            final_args[name] = parse_value(
                name, value, func_types[name], default, allow_value_convert=True
            )
    return final_args


def parse_utc_timestamp(datetime_string: str) -> datetime:
    """Parse datetime from string."""
    return datetime.fromisoformat(datetime_string)


def parse_value(  # noqa: PLR0911
    name: str,
    value: Any,
    value_type: Any,
    default: Any = MISSING,
    allow_value_convert: bool = False,
) -> Any:
    """Try to parse a value from raw (json) data and type annotations."""
    # Resolve string type hints early for proper handling
    if isinstance(value_type, str):
        value_type = _resolve_string_type(value_type)
        # If still a string after resolution, return value as-is
        if isinstance(value_type, str):
            LOGGER.debug("Unknown string type hint: %s, returning value as-is", value_type)
            return value

    if isinstance(value, dict) and hasattr(value_type, "from_dict"):
        # Only validate media_type for actual MediaItem subclasses, not for other classes
        # like StreamDetails that have a media_type field for a different purpose
        if (
            "media_type" in value
            and value_type.__name__ != "ItemMapping"
            and issubclass(value_type, MediaItem)
            and value["media_type"] != value_type.media_type
        ):
            msg = "Invalid MediaType"
            raise ValueError(msg)
        return value_type.from_dict(value)

    if value is None and not isinstance(default, type(MISSING)):
        return default
    if value is None and value_type is NoneType:
        return None
    origin = get_origin(value_type)
    if origin in (tuple, list, Sequence, Iterable):
        # For abstract types like Sequence and Iterable, use list as the concrete type
        concrete_type = list if origin in (Sequence, Iterable) else origin
        return concrete_type(
            parse_value(
                name, subvalue, get_args(value_type)[0], allow_value_convert=allow_value_convert
            )
            for subvalue in value
            if subvalue is not None
        )
    if origin is dict:
        subkey_type = get_args(value_type)[0]
        subvalue_type = get_args(value_type)[1]
        return {
            parse_value(subkey, subkey, subkey_type): parse_value(
                f"{subkey}.value", subvalue, subvalue_type, allow_value_convert=allow_value_convert
            )
            for subkey, subvalue in value.items()
        }
    if origin is Union or origin is UnionType:
        # try all possible types
        sub_value_types = get_args(value_type)
        for sub_arg_type in sub_value_types:
            if value is NoneType and sub_arg_type is NoneType:
                return value
            # try them all until one succeeds
            try:
                return parse_value(
                    name, value, sub_arg_type, allow_value_convert=allow_value_convert
                )
            except (KeyError, TypeError, ValueError, MissingField):
                pass
        # if we get to this point, all possibilities failed
        # find out if we should raise or log this
        err = (
            f"Value {value} of type {type(value)} is invalid for {name}, "
            f"expected value of type {value_type}"
        )
        if NoneType not in sub_value_types:
            # raise exception, we have no idea how to handle this value
            raise TypeError(err)
        # failed to parse the (sub) value but None allowed, log only
        logging.getLogger(__name__).warning(err)
        return None
    if origin is type:
        assert isinstance(value, str)  # for type checking
        return eval(value)
    if value_type is Any:
        return value
    if value is None and value_type is not NoneType:
        msg = f"`{name}` of type `{value_type}` is required."
        raise KeyError(msg)

    try:
        if issubclass(value_type, Enum):
            return value_type(value)
        if issubclass(value_type, datetime):
            assert isinstance(value, str)  # for type checking
            return parse_utc_timestamp(value)
    except TypeError:
        # happens if value_type is not a class
        pass

    if allow_value_convert:
        # allow conversion of common types/mistakes
        if value_type is float and isinstance(value, int):
            return float(value)
        if value_type is int and isinstance(value, float):
            return int(value)
        if value_type is int and isinstance(value, str) and value.isnumeric():
            return int(value)
        if value_type is float and isinstance(value, str) and value.isnumeric():
            return float(value)
        if value_type is bool and isinstance(value, str | int):
            return try_parse_bool(value)

    if not isinstance(value, value_type):
        # all options failed, raise exception
        msg = (
            f"Value {value} of type {type(value)} is invalid for {name}, "
            f"expected value of type {value_type}"
        )
        raise TypeError(msg)
    return value
