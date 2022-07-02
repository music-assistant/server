import argparse
import asyncio
from cgi import test
import logging
import os
from os.path import abspath, dirname
from sys import path

path.insert(1, dirname(dirname(abspath(__file__))))

from music_assistant.mass import MusicAssistant
from music_assistant.models.config import MassConfig, MusicProviderConfig
from music_assistant.models.enums import ProviderType
from music_assistant.models.player import Player, PlayerState
from music_assistant.models.player_queue import RepeatMode

# setup logger
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)-15s %(levelname)-5s %(name)s -- %(message)s",
)
# silence some loggers
logging.getLogger("aiorun").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.INFO)
logging.getLogger("aiosqlite").setLevel(logging.WARNING)
logging.getLogger("databases").setLevel(logging.WARNING)


# default database based on sqlite
data_dir = os.getenv("APPDATA") if os.name == "nt" else os.path.expanduser("~")
data_dir = os.path.join(data_dir, ".musicassistant")
if not os.path.isdir(data_dir):
    os.makedirs(data_dir)
db_file = os.path.join(data_dir, "music_assistant.db")


mass_conf = MassConfig(
    database_url=f"sqlite:///{db_file}",
)

mass_conf.providers.append(
    MusicProviderConfig(
        ProviderType.YTMUSIC
    )
)

class TestPlayer(Player):
    """Demonstatration player implementation."""

    def __init__(self, player_id: str):
        """Init."""
        self.player_id = player_id
        self._attr_name = player_id
        self._attr_powered = True
        self._attr_elapsed_time = 0
        self._attr_current_url = ""
        self._attr_state = PlayerState.IDLE
        self._attr_available = True
        self._attr_volume_level = 100

    async def play_url(self, url: str) -> None:
        """Play the specified url on the player."""
        print(f"stream url: {url}")
        self._attr_current_url = url
        self.update_state()

    async def stop(self) -> None:
        """Send STOP command to player."""
        print("stop called")
        self._attr_state = PlayerState.IDLE
        self._attr_current_url = None
        self._attr_elapsed_time = 0
        self.update_state()

    async def play(self) -> None:
        """Send PLAY/UNPAUSE command to player."""
        print("play called")
        self._attr_state = PlayerState.PLAYING
        self._attr_elapsed_time = 1
        self.update_state()

    async def pause(self) -> None:
        """Send PAUSE command to player."""
        print("pause called")
        self._attr_state = PlayerState.PAUSED
        self.update_state()

    async def power(self, powered: bool) -> None:
        """Send POWER command to player."""
        print(f"POWER CALLED - new power: {powered}")
        self._attr_powered = powered
        self._attr_current_url = None
        self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """Send volume level (0..100) command to player."""
        print(f"volume_set called - {volume_level}")
        self._attr_volume_level = volume_level
        self.update_state()

async def main():
    """Handle main execution."""

    asyncio.get_event_loop().set_debug(True)

    async with MusicAssistant(mass_conf) as mass:
        # get some data
        yt = mass.music.get_provider(ProviderType.YTMUSIC)
        track = await yt.get_track("pE3ju1qS848")      
        await yt.get_album("MPREb_AYetWMZunqA")
        await yt.get_artist("UCU2d6Vg6hp0vJb8K0krR5_g")
        
        #test_player1 = TestPlayer("test1")
        #await mass.players.register_player(test_player1)
        #await test_player1.active_queue.play_media(track)

        #await asyncio.sleep(3600)
        


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
