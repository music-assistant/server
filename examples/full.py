"""Extended example/script to run Music Assistant with all bells and whistles."""
import argparse
import asyncio
import logging
import os
import webbrowser

from os.path import abspath, dirname
from sys import path

path.insert(1, dirname(dirname(abspath(__file__))))

# pylint: disable=wrong-import-position
from music_assistant.mass import MusicAssistant
from music_assistant.models.config import MassConfig, MusicProviderConfig
from music_assistant.models.enums import (
    CrossFadeMode,
    ProviderType,
    RepeatMode,
    PlayerState,
)
from music_assistant.models.player import Player


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
    "--ytmusic-username",
    required=False,
    help="YoutubeMusic username",
)
parser.add_argument(
    "--ytmusic-cookie",
    required=False,
    help="YoutubeMusic cookie",
)
parser.add_argument(
    "--smb-username",
    required=False,
    help="SMB username",
)
parser.add_argument(
    "--smb-password",
    required=False,
    help="SMB password",
)
parser.add_argument(
    "--smb-target-name",
    required=False,
    help="The NetBIOS machine name of the remote server.",
)
parser.add_argument(
    "--smb-ip",
    required=False,
    help="The IP of the remote server",
)
parser.add_argument(
    "--smb-share",
    required=False,
    help="The share on the remote server to connect to.",
)
parser.add_argument(
    "--path",
    required=False,
    help="The path on the share to connect to.",
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
logging.getLogger("databases").setLevel(logging.INFO)


# default database based on sqlite
data_dir = os.getenv("APPDATA") if os.name == "nt" else os.path.expanduser("~")
data_dir = os.path.join(data_dir, ".musicassistant")
if not os.path.isdir(data_dir):
    os.makedirs(data_dir)
db_file = os.path.join(data_dir, "music_assistant.db")


mass_conf = MassConfig(
    database_url=f"sqlite:///{db_file}",
)
if args.spotify_username and args.spotify_password:
    mass_conf.providers.append(
        MusicProviderConfig(
            ProviderType.SPOTIFY,
            username=args.spotify_username,
            password=args.spotify_password,
        )
    )
if args.qobuz_username and args.qobuz_password:
    mass_conf.providers.append(
        MusicProviderConfig(
            type=ProviderType.QOBUZ,
            username=args.qobuz_username,
            password=args.qobuz_password,
        )
    )
if args.tunein_username:
    mass_conf.providers.append(
        MusicProviderConfig(
            type=ProviderType.TUNEIN,
            username=args.tunein_username,
        )
    )

if args.ytmusic_username and args.ytmusic_cookie:
    mass_conf.providers.append(
        MusicProviderConfig(
            ProviderType.YTMUSIC,
            username=args.ytmusic_username,
            password=args.ytmusic_cookie,
        )
    )

if args.musicdir:
    mass_conf.providers.append(
        MusicProviderConfig(type=ProviderType.FILESYSTEM_LOCAL, path=args.musicdir)
    )

if args.smb_share and args.smb_target_name:
    mass_conf.providers.append(
        MusicProviderConfig(
            ProviderType.FILESYSTEM_SMB,
            username=args.smb_username,
            password=args.smb_password,
            target_name=args.smb_target_name,
            target_ip=args.smb_ip,
            share_name=args.smb_share,
            path=args.path,
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
        # launch stream url in browser so we can hear it playing ;-)
        # normally this url is sent to the actual player implementation
        webbrowser.open(url)

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

        # run sync
        await mass.music.start_sync()

        # # get some data
        artists = await mass.music.artists.db_items()
        artists_lib = await mass.music.artists.db_items(True)
        print(
            f"Got {artists_lib.total} artists  in library (of {artists.total} total in db)"
        )

        albums = await mass.music.albums.db_items()
        albums_lib = await mass.music.albums.db_items(True)
        print(
            f"Got {albums_lib.total} albums  in library (of {albums.total} total in db)"
        )

        tracks = await mass.music.tracks.db_items()
        tracks_lib = await mass.music.tracks.db_items(True)
        print(
            f"Got {tracks_lib.total} tracks  in library (of {tracks.total} total in db)"
        )

        playlists = await mass.music.playlists.db_items()
        playlists_lib = await mass.music.playlists.db_items(True)
        print(
            f"Got {playlists_lib.total} tracks  in library (of {playlists.total} total in db)"
        )

        # register a player
        test_player1 = TestPlayer("test1")
        test_player2 = TestPlayer("test2")
        await mass.players.register_player(test_player1)
        await mass.players.register_player(test_player2)

        # try to play some music
        test_player1.active_queue.settings.shuffle_enabled = True
        test_player1.active_queue.settings.repeat_mode = RepeatMode.ALL
        test_player1.active_queue.settings.crossfade_duration = 10
        test_player1.active_queue.settings.crossfade_mode = CrossFadeMode.SMART

        # we can send a MediaItem object (such as Artist, Album, Track, Playlist)
        # we can also send an uri, such as spotify://track/abcdfefgh
        # or database://playlist/1
        # or a list of items
        if playlists.count > 0:
            await test_player1.active_queue.play_media(playlists.items[0])

        await asyncio.sleep(3600)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
