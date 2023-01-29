"""Generic models used for the (websockets) API communication."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mashumaro import DataClassDictMixin

from music_assistant.common.models.event import MassEvent


@dataclass
class CommandMessage(DataClassDictMixin):
    """Model for a Message holding a command from server to client or client to server."""

    message_id: str
    command: str
    args: dict[str, Any] | None = None


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
    details: str | None = None


EventMessage = MassEvent


@dataclass
class ServerInfoMessage(DataClassDictMixin):
    """Message sent by the server with it's info when a client connects."""

    server_version: str
    schema_version: int


MessageType = (
    CommandMessage
    | EventMessage
    | SuccessResultMessage
    | ErrorResultMessage
    | ServerInfoMessage
)
