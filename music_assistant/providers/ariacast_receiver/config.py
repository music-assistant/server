"""Configuration classes for AriaCast Server."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AudioConfig:
    """Audio stream configuration parameters.

    Note: Instances of this class should be treated as immutable after creation.
    Modifying audio parameters at runtime will not trigger recalculation of
    derived values like frame_size.
    """

    sample_rate: int = 48000
    channels: int = 2
    sample_width: int = 2  # 16-bit = 2 bytes
    frame_duration_ms: int = 20

    @property
    def frame_size(self) -> int:
        """Return the frame size in bytes derived from the current audio parameters."""
        return int(
            self.sample_rate * self.channels * self.sample_width * self.frame_duration_ms / 1000
        )


@dataclass
class ServerConfig:
    """Server configuration parameters."""

    server_name: str = "AriaCast Speaker"
    version: str = "1.0"
    platform: str = "MusicAssistant"
    codecs: list[str] | None = None
    discovery_port: int = 12888
    streaming_port: int = 12889
    host: str = "0.0.0.0"
    audio: AudioConfig | None = None

    def __post_init__(self) -> None:
        """Initialize default values after instantiation."""
        if self.codecs is None:
            self.codecs = ["PCM"]
        if self.audio is None:
            self.audio = AudioConfig()
