"""Simple example/script to run Music Assistant with Spotify provider."""
import argparse
import asyncio
import logging
import os


from music_assistant.mass import MusicAssistant
from music_assistant.providers.spotify import SpotifyProvider

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

mass = MusicAssistant(f"sqlite:///{db_file}")
spotify = SpotifyProvider(args.username, args.password)


async def main():
    """Handle main execution."""

    asyncio.get_event_loop().set_debug(args.debug)

    # without contextmanager we need to call the async setup
    await mass.setup()
    # register music provider(s)
    await mass.music.register_provider(spotify)
    # get some data
    await mass.music.artists.library()
    await mass.music.tracks.library()
    await mass.music.radio.library()

    # run for an hour until someone hits CTRL+C
    await asyncio.sleep(3600)

    # without contextmanager we need to call the stop
    await mass.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
