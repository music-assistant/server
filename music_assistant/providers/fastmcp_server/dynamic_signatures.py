"""Schema compilation and strict binding for dynamic MA command handlers."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from types import UnionType
from typing import Any, Union, get_args, get_origin

from pydantic import TypeAdapter

from .dynamic_serialization import json_value


class UnsupportedSignatureError(ValueError):
    """Raised when an MA handler cannot be represented as JSON arguments."""


@dataclass(frozen=True, slots=True)
class CompiledSignature:
    """A handler signature compiled for MCP schema publication and invocation."""

    signature: inspect.Signature
    parse_signature: inspect.Signature
    type_hints: Mapping[str, Any]
    input_schema: dict[str, Any]
    allow_extra_kwargs: bool

    def output_schema(self) -> dict[str, Any] | None:
        """Return JSON schema metadata for the handler result."""
        annotation = self.type_hints.get("return", self.signature.return_annotation)
        if annotation in {inspect.Signature.empty, None, Any}:
            return None
        return _type_schema(annotation)

    def parse(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Strictly parse the JSON arguments accepted by this handler."""
        known = set(self.parse_signature.parameters)
        extras = {key: value for key, value in arguments.items() if key not in known}
        if extras and not self.allow_extra_kwargs:
            names = ", ".join(sorted(extras))
            raise ValueError(f"Unexpected argument(s): {names}")
        from music_assistant.helpers.api import parse_arguments  # noqa: PLC0415

        parsed = parse_arguments(
            self.parse_signature,
            dict(self.type_hints),
            {key: value for key, value in arguments.items() if key in known},
            strict=True,
        )
        parsed.update(extras)
        # The standalone provider environment treats MA's parser as untyped;
        # the transplanted MA environment verifies its concrete dict return.
        return parsed  # type: ignore[no-any-return, unused-ignore]


def compile_signature(
    signature: inspect.Signature,
    type_hints: Mapping[str, Any],
    *,
    allow_extra_kwargs: bool = False,
) -> CompiledSignature:
    """Compile an MA handler signature into a strict MCP-facing contract."""
    variadic = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL
    ]
    if variadic:
        raise UnsupportedSignatureError(f"Unsupported variadic parameter *{variadic[0].name}")
    named = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind is not inspect.Parameter.VAR_KEYWORD
    ]
    parse_signature = signature.replace(parameters=named)
    schema = _input_schema(parse_signature, type_hints)
    schema["additionalProperties"] = allow_extra_kwargs
    return CompiledSignature(signature, parse_signature, type_hints, schema, allow_extra_kwargs)


def _input_schema(signature: inspect.Signature, type_hints: Mapping[str, Any]) -> dict[str, Any]:
    """Build the JSON schema for supported named input parameters."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, parameter in signature.parameters.items():
        if name in {"self", "return_type"}:
            continue
        annotation = type_hints.get(name, Any)
        if _is_static_type_hint(annotation):
            continue
        properties[name] = _type_schema(annotation)
        if parameter.default is inspect.Parameter.empty:
            required.append(name)
        else:
            properties[name]["default"] = json_value(parameter.default)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _is_static_type_hint(annotation: Any) -> bool:
    """Return whether an annotation contains a non-input ``type[X]``."""
    origin = get_origin(annotation)
    if origin is type:
        return True
    return origin in {Union, UnionType} and any(
        get_origin(arg) is type for arg in get_args(annotation)
    )


def _type_schema(annotation: Any) -> dict[str, Any]:
    """Convert one annotation to JSON schema without mislabeling failures."""
    if annotation is Any:
        return {}
    try:
        return TypeAdapter(annotation).json_schema()
    except Exception:
        return {"x-python-type": str(annotation)}
