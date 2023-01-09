"""Simple example/script to run Music Assistant with Spotify provider."""
import argparse
import asyncio
import logging
import os
from os.path import abspath, dirname
from sys import path

path.insert(1, dirname(dirname(abspath(__file__))))

# pylint: disable=wrong-import-position
from music_assistant.server import MusicAssistant
from music_assistant.common.models.config import MassConfig, MusicProviderConfig
from music_assistant.common.models.enums import ProviderType

parser = argparse.ArgumentParser(description="MusicAssistant")
parser.add_argument(
    "--username",
    required=True,
    help="Spotify username",
)
parser.add_argument(
    "--password",
    required=True,
    help="Spotify password.",
)
parser.add_argument(
    "--debug",
    action="store_true",
    help="Enable verbose debug logging",
)
args = parser.parse_args()


# setup logger
if args.debug:
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

mass = MusicAssistant(
    MassConfig(
        database_url=MassConfig,
        providers=[
            MusicProviderConfig(
                ProviderType.SPOTIFY,
                username=args.spotify_username,
                password=args.spotify_password,
            )
        ],
    )
)


async def main():
    """Handle main execution."""

    asyncio.get_event_loop().set_debug(args.debug)

    # without contextmanager we need to call the async setup
    await mass.setup()

    # start sync
    await mass.music.start_sync(schedule=3)

    # get some data
    await mass.music.artists.db_items()
    await mass.music.tracks.db_items()
    await mass.music.radio.db_items()

    # run for an hour until someone hits CTRL+C
    await asyncio.sleep(3600)

    # without contextmanager we need to call the stop
    await mass.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
