"""Extended example/script to run Music Assistant with all bells and whistles."""
import argparse
import asyncio
import logging
import os

from music_assistant.mass import MusicAssistant
from music_assistant.models.config import MassConfig
from music_assistant.models.player import Player, PlayerState
from music_assistant.models.player_queue import RepeatMode


parser = argparse.ArgumentParser(description="MusicAssistant")
parser.add_argument(
    "--spotify-username",
    required=False,
    help="Spotify username",
)
parser.add_argument(
    "--spotify-password",
    required=False,
    help="Spotify password.",
)
parser.add_argument(
    "--qobuz-username",
    required=False,
    help="Qobuz username",
)
parser.add_argument(
    "--qobuz-password",
    required=False,
    help="Qobuz password.",
)
parser.add_argument(
    "--tunein-username",
    required=False,
    help="Tunein username",
)
parser.add_argument(
    "--musicdir",
    required=False,
    help="Directory on disk for local music library",
)
parser.add_argument(
    "--playlistdir",
    required=False,
    help="Directory on disk for local (m3u) playlists",
)
parser.add_argument(
    "--debug",
    action="store_true",
    help="Enable verbose debug logging",
)
args = parser.parse_args()


# setup logger
logging.basicConfig(
    level=logging.DEBUG if args.debug else logging.INFO,
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
    spotify_enabled=args.spotify_username and args.spotify_password,
    spotify_username=args.spotify_username,
    spotify_password=args.spotify_password,
    qobuz_enabled=args.qobuz_username and args.qobuz_password,
    qobuz_username=args.qobuz_username,
    qobuz_password=args.qobuz_password,
    tunein_enabled=args.tunein_username is not None,
    tunein_username=args.tunein_username,
    filesystem_enabled=args.musicdir is not None,
    filesystem_music_dir=args.musicdir,
    filesystem_playlists_dir=args.playlistdir,
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
        print("play uri called: {url}")
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

    asyncio.get_event_loop().set_debug(args.debug)

    async with MusicAssistant(mass_conf) as mass:

        # start sync
        await mass.music.start_sync()

        # get some data
        artists = await mass.music.artists.count()
        print(f"Got {artists} artists in library")
        albums = await mass.music.albums.count()
        print(f"Got {albums} albums in library")
        tracks = await mass.music.tracks.count()
        print(f"Got {tracks} tracks in library")
        radios = await mass.music.radio.count()
        print(f"Got {radios} radio stations in library")
        playlists = await mass.music.playlists.library()
        print(f"Got {len(playlists)} playlists in library")
        # register a player
        test_player1 = TestPlayer("test1")
        test_player2 = TestPlayer("test2")
        await mass.players.register_player(test_player1)
        await mass.players.register_player(test_player2)
        # try to play some playlist
        test_player1.active_queue.settings.shuffle_enabled = True
        test_player1.active_queue.settings.repeat_mode = RepeatMode.ALL
        if len(playlists) > 0:
            await test_player1.active_queue.play_media(playlists[0].uri)

        await asyncio.sleep(3600)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
