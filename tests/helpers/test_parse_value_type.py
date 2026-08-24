"""
Tests for type[X] parameter handling in API argument parsing.

The type[X] parameters (e.g. return_type: type[ConfigValueType]) exist only for
static type checking via @overload. They must be skipped by parse_arguments so
that arbitrary strings from API input are never resolved to types.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, get_type_hints

import pytest

import music_assistant.helpers.api as api_helpers
from music_assistant.helpers.api import APICommandHandler, parse_arguments, parse_value

if TYPE_CHECKING:
    from music_assistant_models.background_task import BackgroundTask


async def _api_method_returning_background_task() -> BackgroundTask:
    raise NotImplementedError  # pragma: no cover


def _example_with_type_param(
    instance_id: str,
    key: str,
    return_type: type[int] | None = None,
) -> None:
    """Simulate a config API method with a type[X] parameter."""


class TestParseArgumentsSkipsTypeParams:
    """Test that parse_arguments skips type[X] parameters."""

    def test_type_param_not_in_parsed_args(self) -> None:
        """type[X] parameters should not appear in parsed output."""
        sig = inspect.signature(_example_with_type_param)
        hints = get_type_hints(_example_with_type_param)
        result = parse_arguments(sig, hints, {"instance_id": "test", "key": "foo"})
        assert "return_type" not in result
        assert result["instance_id"] == "test"
        assert result["key"] == "foo"

    def test_type_param_ignored_even_if_provided(self) -> None:
        """Even if API input includes a type[X] value, it should be ignored."""
        sig = inspect.signature(_example_with_type_param)
        hints = get_type_hints(_example_with_type_param)
        result = parse_arguments(
            sig,
            hints,
            {"instance_id": "test", "key": "foo", "return_type": "str"},
        )
        assert "return_type" not in result


class TestParseValueTypeRejected:
    """Test that parse_value rejects type[X] as a safeguard."""

    @pytest.mark.parametrize("value", ["str", "int", "list[str]"])
    def test_type_resolution_rejected(self, value: str) -> None:
        """Direct calls to parse_value with type[X] should raise ValueError."""
        with pytest.raises(ValueError, match="Cannot resolve type from string"):
            parse_value("return_type", value, type[object])


def test_api_command_handler_resolves_type_checking_model_return_type() -> None:
    """API command parser should resolve TYPE_CHECKING-only model classes."""
    handler = APICommandHandler.parse("test/return_task", _api_method_returning_background_task)
    assert handler.type_hints["return"] is not None
    assert handler.type_hints["return"].__name__ == "BackgroundTask"


def test_get_type_hints_for_api_command_has_retry_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated resolution failures should stop after a bounded number of retries."""
    attempts = 0

    def fake_get_type_hints(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        raise NameError("name 'BackgroundTask' is not defined")

    monkeypatch.setattr(api_helpers, "get_type_hints", fake_get_type_hints)
    monkeypatch.setattr(api_helpers, "_resolve_model_type", lambda _name: object)

    with pytest.raises(RuntimeError, match="Exceeded type hint resolution attempts"):
        api_helpers._get_type_hints_for_api_command(_api_method_returning_background_task)

    assert attempts == api_helpers._MAX_TYPE_HINT_RESOLVE_ATTEMPTS
