"""Helpers for dealing with API's to interact with Music Assistant."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Coroutine
from dataclasses import MISSING, dataclass
from datetime import datetime
from enum import Enum
from types import NoneType, UnionType
from typing import Any, TypeVar, Union, get_args, get_origin, get_type_hints

LOGGER = logging.getLogger(__name__)

_F = TypeVar("_F", bound=Callable[..., Any])


@dataclass
class APICommandHandler:
    """Model for an API command handler."""

    command: str
    signature: inspect.Signature
    type_hints: dict[str, Any]
    target: Callable[..., Coroutine[Any, Any, Any]]

    @classmethod
    def parse(
        cls, command: str, func: Callable[..., Coroutine[Any, Any, Any]]
    ) -> APICommandHandler:
        """Parse APICommandHandler by providing a function."""
        return APICommandHandler(
            command=command,
            signature=inspect.signature(func),
            type_hints=get_type_hints(func),
            target=func,
        )


def api_command(command: str) -> Callable[[_F], _F]:
    """Decorate a function as API route/command."""

    def decorate(func: _F) -> _F:
        func.api_cmd = command  # type: ignore[attr-defined]
        return func

    return decorate


def parse_arguments(
    func_sig: inspect.Signature,
    func_types: dict[str, Any],
    args: dict | None,
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
        final_args[name] = parse_value(name, value, func_types[name], default)
    return final_args


def parse_utc_timestamp(datetime_string: str) -> datetime:
    """Parse datetime from string."""
    return datetime.fromisoformat(datetime_string.replace("Z", "+00:00"))


def parse_value(name: str, value: Any, value_type: Any, default: Any = MISSING) -> Any:
    """Try to parse a value from raw (json) data and type annotations."""
    if isinstance(value, dict) and hasattr(value_type, "from_dict"):
        if "media_type" in value and value["media_type"] != value_type.media_type:
            msg = "Invalid MediaType"
            raise ValueError(msg)
        return value_type.from_dict(value)

    if value is None and not isinstance(default, type(MISSING)):
        return default
    if value is None and value_type is NoneType:
        return None
    origin = get_origin(value_type)
    if origin in (tuple, list):
        return origin(
            parse_value(name, subvalue, get_args(value_type)[0])
            for subvalue in value
            if subvalue is not None
        )
    if origin is dict:
        subkey_type = get_args(value_type)[0]
        subvalue_type = get_args(value_type)[1]
        return {
            parse_value(subkey, subkey, subkey_type): parse_value(
                f"{subkey}.value", subvalue, subvalue_type
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
                return parse_value(name, value, sub_arg_type)
            except (KeyError, TypeError, ValueError):
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
        return eval(value)  # pylint: disable=eval-used
    if value_type is Any:
        return value
    if value is None and value_type is not NoneType:
        msg = f"`{name}` of type `{value_type}` is required."
        raise KeyError(msg)

    try:
        if issubclass(value_type, Enum):  # type: ignore[arg-type]
            return value_type(value)  # type: ignore[operator]
        if issubclass(value_type, datetime):  # type: ignore[arg-type]
            return parse_utc_timestamp(value)
    except TypeError:
        # happens if value_type is not a class
        pass

    if value_type is float and isinstance(value, int):
        return float(value)
    if value_type is int and isinstance(value, str) and value.isnumeric():
        return int(value)
    if not isinstance(value, value_type):  # type: ignore[arg-type]
        msg = (
            f"Value {value} of type {type(value)} is invalid for {name}, "
            f"expected value of type {value_type}"
        )
        raise TypeError(msg)
    return value
