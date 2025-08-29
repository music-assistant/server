
import pytest
import asyncio
from music_assistant.controllers.streams import StreamsController
from music_assistant.models.plugin import PluginSource, PluginProvider
from music_assistant.models.player import PlayerMedia
from music_assistant_models.enums import StreamType
from music_assistant_models.media_items.audio_format import AudioFormat

class DummyPluginProvider(PluginProvider):
    def __init__(self, player_id):
        self._source = PluginSource(
            id="dummy",
            name="Dummy",
            audio_format=AudioFormat(),
            stream_type=StreamType.CUSTOM,
            path=None,
            in_use_by=None,
        )
        self.player_id = player_id
    def get_source(self):
        return self._source
    async def get_audio_stream(self, player_id):
        yield b"audio-data"

class DummyPlayer:
    def __init__(self, player_id):
        self.player_id = player_id
        self.active_source = None

@pytest.mark.asyncio
async def test_plugin_source_in_use_by_reset_on_error():
    player_id = "test_player"
    plugin_provider = DummyPluginProvider(player_id)
    class DummyConfig:
        def get_raw_core_config_value(self, domain, key, fallback):
            return fallback
    dummy_mass = type("mass", (), {
        "get_provider": lambda self, pid: plugin_provider,
        "players": type("players", (), {"get": lambda self, pid: DummyPlayer(pid)})(),
        "config": DummyConfig()
    })()
    streams = StreamsController(dummy_mass)
    # Simulate plugin source already in use
    plugin_provider._source.in_use_by = player_id
    # Should raise RuntimeError and reset in_use_by
    with pytest.raises(RuntimeError):
        gen = streams.get_plugin_source_stream("dummy", AudioFormat(), player_id)
        await gen.__anext__()
    assert plugin_provider._source.in_use_by is None
