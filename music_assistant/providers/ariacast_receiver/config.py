"""Configuration classes for AriaCast Server."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AudioConfig:
    """Audio stream configuration parameters."""

    sample_rate: int = 48000
    channels: int = 2
    sample_width: int = 2  # 16-bit = 2 bytes
    frame_duration_ms: int = 20
    frame_size: int = 3840  # Default value, re-calculated in __post_init__

    def __post_init__(self) -> None:
        """Compute derived values and provide uppercase aliases for backwards compatibility."""
        # Dynamically calculate frame_size based on the current audio parameters to
        # avoid relying on a hardcoded value that assumes specific defaults.
        self.frame_size = int(
            self.sample_rate * self.channels * self.sample_width * self.frame_duration_ms / 1000
        )

        # Provide uppercase aliases for backwards compatibility with previous internal naming.
        self.SAMPLE_RATE = self.sample_rate
        self.CHANNELS = self.channels
        self.SAMPLE_WIDTH = self.sample_width
        self.FRAME_DURATION_MS = self.frame_duration_ms
        self.FRAME_SIZE = self.frame_size


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
        """Initialize default values after instantiation and set aliases."""
        if self.codecs is None:
            self.codecs = ["PCM"]
        if self.audio is None:
            self.audio = AudioConfig()

        # Provide uppercase aliases for backwards compatibility.
        self.SERVER_NAME = self.server_name
        self.VERSION = self.version
        self.PLATFORM = self.platform
        self.CODECS = self.codecs
        self.DISCOVERY_PORT = self.discovery_port
        self.STREAMING_PORT = self.streaming_port
        self.HOST = self.host
        self.AUDIO = self.audio
