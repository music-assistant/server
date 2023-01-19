"""Generic models used for the (websockets) API communication."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Union

from mashumaro import DataClassDictMixin

from .enums import EventType


@dataclass
class CommandMessage(DataClassDictMixin):
    """Model for a Message holding a command from server to client or client to server."""

    message_id: str
    command: str
    args: Union[dict[str, Any], None] = None


@dataclass
class ResultMessageBase(DataClassDictMixin):
    """Base class for a result/response of a Command Message."""

    message_id: str


@dataclass
class SuccessResultMessage(ResultMessageBase):
    """Message sent when a Command has been successfully executed."""

    result: Any


@dataclass
class ErrorResultMessage(ResultMessageBase):
    """Message sent when a command did not execute successfully."""

    error_code: str
    details: Optional[str] = None


@dataclass
class EventMessage(DataClassDictMixin):
    """Message sent when server or client signals a (stateless) event."""

    event: EventType
    data: Any


@dataclass
class ServerInfoMessage(DataClassDictMixin):
    """Message sent by the server with it's info when a client connects."""

    server_version: str
    schema_version: int


MessageType = Union[
    CommandMessage,
    EventMessage,
    SuccessResultMessage,
    ErrorResultMessage,
    ServerInfoMessage,
]
