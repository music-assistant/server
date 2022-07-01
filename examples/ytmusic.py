import argparse
import asyncio
import logging
import os
from os.path import abspath, dirname
from sys import path

path.insert(1, dirname(dirname(abspath(__file__))))

from music_assistant.mass import MusicAssistant
from music_assistant.models.config import MassConfig, MusicProviderConfig
from music_assistant.models.enums import ProviderType

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

async def main():
    """Handle main execution."""

    asyncio.get_event_loop().set_debug(True)

    async with MusicAssistant(mass_conf) as mass:
        # get some data
        yt = mass.music.get_provider(ProviderType.YTMUSIC)
        await yt.get_album("MPREb_9nqEki4ZDpp")
        #await yt.get_track("f3igK4EDUnk")
        await yt.get_track("pE3ju1qS848")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
