"""Models for the resonate audio protocol."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from mashumaro.mixins.orjson import DataClassORJSONMixin


class MediaCommand(Enum):
    """Enum for Media Commands."""

    PLAY = "play"
    PAUSE = "pause"
    STOP = "stop"
    SEEK = "seek"
    VOLUME = "volume"


@dataclass
class Message(DataClassORJSONMixin):
    """Message type used by resonate."""

    type: str
    payload: dict[str, Any] | Any


class RepeatMode(Enum):
    """Enum for Repeat Modes."""

    OFF = "off"
    ONE = "one"
    ALL = "all"


class PlayerStateType(Enum):
    """Enum for Player States."""

    PLAYING = "playing"
    PAUSED = "paused"
    IDLE = "idle"


class BinaryMessageType(Enum):
    """Enum for Binary Message Types."""

    PlayAudioChunk = 1


@dataclass
class SessionInfo(DataClassORJSONMixin):
    """Information about an active streaming session."""

    session_id: str
    codec: str
    sample_rate: int
    channels: int
    bit_depth: int
    now: int
    codec_header: str | None = None


@dataclass
class PlayerInfo(DataClassORJSONMixin):
    """Information about a connected player."""

    player_id: str
    name: str
    role: str
    buffer_capacity: int
    support_codecs: list[str]
    support_channels: list[int]
    support_sample_rates: list[int]
    support_bit_depth: list[int]
    support_streams: list[str]
    support_picture_formats: list[str]
    media_display_size: str | None = None


@dataclass
class PlayerTimeInfo(DataClassORJSONMixin):
    """Timing information from the player."""

    player_transmitted: int


@dataclass
class SourceTimeInfo(DataClassORJSONMixin):
    """Timing information from the source."""

    player_transmitted: int
    source_received: int
    source_transmitted: int


@dataclass
class SourceInfo(DataClassORJSONMixin):
    """Information about the source (e.g., Music Assistant)."""

    source_id: str
    name: str


@dataclass
class SessionEndPayload(DataClassORJSONMixin):
    """Payload for the session/end message."""

    sessionId: str  # noqa: N815


@dataclass
class Metadata(DataClassORJSONMixin):
    """Metadata about the currently playing media."""

    title: str | None = None
    artist: str | None = None
    album: str | None = None
    year: int | None = None
    track: int | None = None
    group_members: list[str] = field(default_factory=list)
    support_commands: list[MediaCommand] = field(default_factory=list)
    repeat: RepeatMode = RepeatMode.OFF
    shuffle: bool = False


@dataclass
class PartialMetadata(DataClassORJSONMixin):
    """Represents a partial update to Metadata."""

    title: str | None = None
    artist: str | None = None
    album: str | None = None
    year: int | None = None
    track: int | None = None
    group_members: list[str] | None = None
    support_commands: list[MediaCommand] | None = None
    repeat: RepeatMode | None = None
    shuffle: bool | None = None


@dataclass
class StreamCommandPayload(DataClassORJSONMixin):
    """Payload for stream commands."""

    command: MediaCommand


@dataclass
class PlayerState(DataClassORJSONMixin):
    """State information of the player."""

    state: PlayerStateType
    volume: int
    muted: bool


# Server -> Client Messages
@dataclass
class SessionStartMessage(DataClassORJSONMixin):
    """Message sent by the server to start a session."""

    payload: SessionInfo
    type: Literal["session/start"] = "session/start"


@dataclass
class SessionEndMessage(DataClassORJSONMixin):
    """Message sent by the server to end a session."""

    payload: SessionEndPayload
    type: Literal["session/end"] = "session/end"


@dataclass
class SourceHelloMessage(DataClassORJSONMixin):
    """Message sent by the server to identify itself."""

    payload: SourceInfo
    type: Literal["source/hello"] = "source/hello"


@dataclass
class MetadataUpdateMessage(DataClassORJSONMixin):
    """Message sent by the server to update metadata."""

    payload: PartialMetadata
    type: Literal["metadata/update"] = "metadata/update"


@dataclass
class SourceTimeMessage(DataClassORJSONMixin):
    """Message sent by the server for time synchronization."""

    payload: SourceTimeInfo
    type: Literal["source/time"] = "source/time"


# Client -> Server Messages
@dataclass
class PlayerHelloMessage(DataClassORJSONMixin):
    """Message sent by the player to identify itself."""

    payload: PlayerInfo
    type: Literal["player/hello"] = "player/hello"


@dataclass
class StreamCommandMessage(DataClassORJSONMixin):
    """Message sent by the client to issue a stream command (e.g., play/pause)."""

    payload: StreamCommandPayload
    type: Literal["stream/command"] = "stream/command"


@dataclass
class PlayerStateMessage(DataClassORJSONMixin):
    """Message sent by the player to report its state."""

    payload: PlayerState
    type: Literal["player/state"] = "player/state"


@dataclass
class PlayerTimeMessage(DataClassORJSONMixin):
    """Message sent by the player for time synchronization."""

    payload: PlayerTimeInfo
    type: Literal["player/time"] = "player/time"


ClientMessages = PlayerHelloMessage | StreamCommandMessage | PlayerStateMessage | PlayerTimeMessage

ServerMessages = (
    SessionStartMessage
    | SessionEndMessage
    | SourceHelloMessage
    | MetadataUpdateMessage
    | SourceTimeMessage
)

# TODO: check this
BINARY_HEADER_FORMAT = ">BQI"
BINARY_HEADER_SIZE = struct.calcsize(BINARY_HEADER_FORMAT)
