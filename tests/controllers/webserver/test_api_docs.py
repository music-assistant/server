"""Tests for the generated API documentation."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

import pytest
from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import (
    ConfigValueType,  # noqa: TC002 - the command parser resolves it at run time
)
from syrupy.extensions.json import JSONSnapshotExtension

from music_assistant.controllers.webserver.api_docs import (
    generate_commands_json,
    generate_openapi_spec,
    generate_schemas_json,
)
from music_assistant.helpers.api import APICommandHandler

if TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion


@pytest.fixture
def snapshot(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    """Store the snapshots as JSON, which keeps blank lines in texts free of trailing spaces."""
    return snapshot.use_extension(JSONSnapshotExtension)


class Colour(StrEnum):
    """Colour of a widget."""

    RED = "red"
    BLUE = "blue"


@dataclass
class Part:
    """One part of a widget."""

    name: str
    weight: float | None = None


@dataclass
class Widget:
    """A widget, the model the sample commands hand around."""

    widget_id: str
    colour: Colour
    parts: list[Part] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    size: Literal["small", "large"] = "small"
    owner: Widget | None = None
    count: int = 0
    enabled: bool = True


class SampleController:
    """A representative set of API commands, covering each kind of signature."""

    async def get_widget(self, widget_id: str, lazy: bool = True) -> Widget:
        """
        Return a widget.

        The widget is looked up in the library first.

        :param widget_id: The id of the widget.
        :param lazy: Whether stale details are acceptable.
        """
        raise NotImplementedError

    async def list_widgets(
        self,
        colour: Colour | None = None,
        limit: int = 25,
        ids: list[str] | None = None,
        size: Literal["small", "large"] = "small",
    ) -> list[Widget]:
        """Return the widgets of a colour."""
        raise NotImplementedError

    async def iter_parts(self, widget: Widget) -> AsyncGenerator[Part]:
        """Yield the parts of a widget."""
        yield Part(name=widget.widget_id)

    async def set_option(
        self, key: str, value: ConfigValueType, options: dict[str, Any] | None = None
    ) -> None:
        # the docs generator also reads this docstring style, so one command keeps it
        """
        Store an option.

        Args:
            key: The key of the option.
            value: The value to store.
            options: Extra options.

        Returns:
            Nothing.
        """
        raise NotImplementedError

    async def find_item(self, uri: str) -> Widget | Part | None:
        """Return the widget or part behind a uri."""
        raise NotImplementedError

    async def stats(self) -> dict[str, int | float]:
        """Return counters by name."""
        raise NotImplementedError

    async def ping(self) -> tuple[str, int]:  # noqa: D102 - a command without a description
        raise NotImplementedError


def _handlers() -> dict[str, APICommandHandler]:
    """Return the handlers of the sample commands, as the server registers them."""
    controller = SampleController()
    handlers = [
        APICommandHandler.parse("widgets/get", controller.get_widget),
        APICommandHandler.parse(
            "widgets/list", controller.list_widgets, required_scope=Scope.LIBRARY_READ
        ),
        APICommandHandler.parse("widgets/parts", controller.iter_parts, allow_impersonation=True),
        APICommandHandler.parse(
            "config/set_option", controller.set_option, required_scope=Scope.ALL
        ),
        APICommandHandler.parse("widgets/find", controller.find_item),
        APICommandHandler.parse("stats", controller.stats, authenticated=False),
        APICommandHandler.parse("ping", controller.ping, authenticated=False),
        # hidden from the documentation
        APICommandHandler.parse("widgets/get_legacy", controller.get_widget, alias=True),
    ]
    return {handler.command: handler for handler in handlers}


def test_commands_json(snapshot: SnapshotAssertion) -> None:
    """The commands reference lists each command with its parameters and return type."""
    assert generate_commands_json(_handlers()) == snapshot


def test_schemas_json(snapshot: SnapshotAssertion) -> None:
    """The schemas reference holds the models the commands use."""
    assert generate_schemas_json(_handlers()) == snapshot


def test_openapi_spec(snapshot: SnapshotAssertion) -> None:
    """The OpenAPI document describes the server, the single endpoint and the models."""
    spec = generate_openapi_spec(_handlers(), server_url="http://192.0.2.10:8095", version="2.99.0")

    assert spec == snapshot


@pytest.mark.parametrize("generate", [generate_commands_json, generate_schemas_json])
def test_alias_commands_are_left_out(generate: Any) -> None:
    """A command kept for backward compatibility does not show in the documentation."""
    handlers = _handlers()
    assert "widgets/get_legacy" not in str(generate(handlers))
    alias = handlers["widgets/get_legacy"]
    assert not generate({alias.command: alias})
