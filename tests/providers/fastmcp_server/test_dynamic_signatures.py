"""Tests for dynamic Music Assistant handler signature compilation."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any, get_type_hints
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.server.auth import AccessToken
from music_assistant_models.media_items import Track  # noqa: TC002

from music_assistant.providers.fastmcp_server.dynamic_api import DynamicAPIAdapter
from music_assistant.providers.fastmcp_server.dynamic_signatures import (
    UnsupportedSignatureError,
    compile_signature,
)
from music_assistant.providers.fastmcp_server.policy import PolicyProfile, policy_snapshot


async def library_items(
    favorite: bool | None = None,
    limit: int = 500,
    **kwargs: Any,
) -> list[Track]:
    """Return library items for a representative MA controller."""
    del favorite, limit, kwargs
    return []


def _library_items_handler(calls: list[dict[str, Any]]) -> Any:
    """Build a library-items handler that records bound named arguments."""

    async def handler(
        favorite: bool | None = None,
        limit: int = 500,
        **kwargs: Any,
    ) -> list[Track]:
        calls.append({"favorite": favorite, "limit": limit, "kwargs": kwargs})
        return []

    return handler


def _compile(signature: inspect.Signature, type_hints: Mapping[str, Any]) -> Any:
    """Compile a handler signature through the public signature compiler."""
    return compile_signature(signature, type_hints)


def _handler(command: str, target: Any) -> Any:
    """Build the MA command-handler metadata relevant to compilation."""
    return SimpleNamespace(
        command=command,
        signature=inspect.signature(target),
        type_hints=get_type_hints(target),
        target=target,
        authenticated=True,
        required_scope="library.read",
        allow_impersonation=False,
        alias=False,
    )


def _adapter(handler: Any) -> DynamicAPIAdapter:
    """Build an authenticated adapter around one command handler."""
    mass = MagicMock()
    mass.command_handlers = {handler.command: handler}
    user = SimpleNamespace(user_id="u1", enabled=True, role="admin")
    mass.webserver.auth.get_user = AsyncMock(return_value=user)
    mass.webserver.auth.authenticate_with_token = AsyncMock(return_value=user)
    return DynamicAPIAdapter(
        mass,
        auth_required_provider=lambda: True,
        token_provider=lambda: AccessToken(token="secret", client_id="u1", scopes=[]),
        scope_checker=lambda _user, _scope: True,
        policy_provider=lambda _bearer: policy_snapshot(PolicyProfile.SAFE_QUERIES),
        default_policy_provider=lambda: policy_snapshot(PolicyProfile.SAFE_QUERIES),
    )


async def test_adapter_does_not_publish_kwargs_as_a_required_property() -> None:
    """The live adapter schema excludes MA's internal keyword catch-all."""
    entry = (
        await _adapter(_handler("music/tracks/library_items", library_items)).visible_entries()
    )[0]

    assert "kwargs" not in entry.input_schema["properties"]
    assert "kwargs" not in entry.input_schema.get("required", [])
    assert entry.input_schema["additionalProperties"] is False


@pytest.mark.parametrize(
    "command",
    [
        "music/albums/library_items",
        "music/artists/library_items",
        "music/audiobooks/library_items",
        "music/genres/library_items",
        "music/playlists/library_items",
        "music/podcasts/library_items",
        "music/tracks/library_items",
    ],
)
async def test_library_item_commands_bind_named_arguments_through_adapter(
    command: str,
) -> None:
    """Each library-item command exposes and invokes its named arguments."""
    calls: list[dict[str, Any]] = []
    handler = _library_items_handler(calls)
    adapter = _adapter(_handler(command, handler))
    entry = (await adapter.visible_entries())[0]

    assert entry.name == f"ma_api:{command}"
    assert "kwargs" not in entry.input_schema["properties"]
    assert "kwargs" not in entry.input_schema.get("required", [])
    assert entry.input_schema["additionalProperties"] is False
    result = await adapter.call(
        entry.name,
        {"favorite": True, "limit": 10},
        response_mode="compact",
        fields=None,
        max_items=None,
        ctx=MagicMock(),
    )
    assert result["data"] == []
    assert calls == [{"favorite": True, "limit": 10, "kwargs": {}}]


def test_var_positional_handler_is_incompatible() -> None:
    """Handlers with positional variadic values are rejected at discovery time."""

    def invalid(first: str, *values: str) -> None:
        pass

    with pytest.raises(UnsupportedSignatureError, match=r"\*values"):
        _compile(inspect.signature(invalid), get_type_hints(invalid))


def test_allow_extra_kwargs_retains_unknown_values_for_extension_handlers() -> None:
    """Profiles that opt in preserve extension keys without publishing ``kwargs``."""

    def extension(limit: int = 10, **kwargs: Any) -> None:
        del limit, kwargs

    compiled = compile_signature(
        inspect.signature(extension), get_type_hints(extension), allow_extra_kwargs=True
    )

    assert compiled.input_schema["additionalProperties"] is True
    assert "kwargs" not in compiled.input_schema["properties"]
    assert compiled.parse({"limit": 2, "upstream_extension": "enabled"}) == {
        "limit": 2,
        "upstream_extension": "enabled",
    }


def test_list_track_output_schema_is_not_a_string() -> None:
    """Track collection outputs never masquerade as scalar strings."""
    compiled = _compile(inspect.signature(library_items), get_type_hints(library_items))

    assert compiled.output_schema() != {"type": "string"}


def test_resolvable_output_type_uses_pydantic_json_schema() -> None:
    """Resolvable collection outputs retain Pydantic's array schema."""
    compiled = _compile(inspect.signature(lambda: None), {"return": list[str]})

    assert compiled.output_schema() == {"items": {"type": "string"}, "type": "array"}


def test_unresolved_output_type_has_unconstrained_python_type_metadata() -> None:
    """Unresolvable annotations do not masquerade as strings."""
    compiled = _compile(inspect.signature(lambda: None), {"return": "MissingModel"})

    assert compiled.output_schema() == {"x-python-type": "MissingModel"}
