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
    frame_size: int = 3840  # 48000 * 2 channels * 2 bytes * 0.020s

    def __post_init__(self) -> None:
        """Provide uppercase aliases for backwards compatibility."""
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
