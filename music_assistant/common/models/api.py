"""Generic models used for the (websockets) API communication."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mashumaro.mixins.orjson import DataClassORJSONMixin

from music_assistant.common.helpers.json import get_serializable_value
from music_assistant.common.models.event import MassEvent

# pylint: disable=unnecessary-lambda


@dataclass
class CommandMessage(DataClassORJSONMixin):
    """Model for a Message holding a command from server to client or client to server."""

    message_id: str | int
    command: str
    args: dict[str, Any] | None = None


@dataclass
class ResultMessageBase(DataClassORJSONMixin):
    """Base class for a result/response of a Command Message."""

    message_id: str


@dataclass
class SuccessResultMessage(ResultMessageBase):
    """Message sent when a Command has been successfully executed."""

    result: Any = field(default=None, metadata={"serialize": lambda v: get_serializable_value(v)})


@dataclass
class ErrorResultMessage(ResultMessageBase):
    """Message sent when a command did not execute successfully."""

    error_code: str
    details: str | None = None


# EventMessage is the same as MassEvent, this is just a alias.
EventMessage = MassEvent


@dataclass
class ServerInfoMessage(DataClassORJSONMixin):
    """Message sent by the server with it's info when a client connects."""

    server_id: str
    server_version: str
    schema_version: int
    min_supported_schema_version: int
    base_url: str
    homeassistant_addon: bool = False


MessageType = (
    CommandMessage | EventMessage | SuccessResultMessage | ErrorResultMessage | ServerInfoMessage
)


def parse_message(raw: dict) -> MessageType:
    """Parse Message from raw dict object."""
    if "event" in raw:
        return EventMessage.from_dict(raw)
    if "error_code" in raw:
        return ErrorResultMessage.from_dict(raw)
    if "result" in raw:
        return SuccessResultMessage.from_dict(raw)
    if "sdk_version" in raw:
        return ServerInfoMessage.from_dict(raw)
    return CommandMessage.from_dict(raw)
