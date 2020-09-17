"""Start Music Assistant."""
import argparse
import logging
import os

from aiorun import run
from music_assistant.mass import MusicAssistant


def get_arguments():
    """Arguments handling."""
    parser = argparse.ArgumentParser(description="MusicAssistant")

    data_dir = os.getenv("APPDATA") if os.name == "nt" else os.path.expanduser("~")
    data_dir = os.path.join(data_dir, ".musicassistant")
    if not os.path.isdir(data_dir):
        os.makedirs(data_dir)

    parser.add_argument(
        "-c",
        "--config",
        metavar="path_to_config_dir",
        default=data_dir,
        help="Directory that contains the MusicAssistant configuration",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Start MusicAssistant with verbose debug logging",
    )
    arguments = parser.parse_args()
    return arguments


def main():
    """Start MusicAssistant."""
    # setup logger
    logger = logging.getLogger()
    logformat = logging.Formatter(
        "%(asctime)-15s %(levelname)-5s %(name)s.%(module)s -- %(message)s"
    )
    consolehandler = logging.StreamHandler()
    consolehandler.setFormatter(logformat)
    logger.addHandler(consolehandler)

    # parse arguments
    args = get_arguments()
    data_dir = args.config
    # config debug settings if needed
    if args.debug:
        logger.setLevel(logging.DEBUG)
        logging.getLogger("aiosqlite").setLevel(logging.INFO)
        logging.getLogger("asyncio").setLevel(logging.WARNING)
    else:
        logger.setLevel(logging.INFO)

    mass = MusicAssistant(data_dir)

    def on_shutdown(loop):
        logger.info("shutdown requested!")
        loop.run_until_complete(mass.async_stop())

    run(
        mass.async_start(),
        use_uvloop=True,
        shutdown_callback=on_shutdown,
        executor_workers=64,
    )


if __name__ == "__main__":
    main()
