"""Start Music Assistant."""
import argparse
import logging
import os

from aiorun import run
from music_assistant.mass import MusicAssistant


def get_arguments():
    """Arguments handling."""
    parser = argparse.ArgumentParser(description="MusicAssistant")

    default_data_dir = (
        os.getenv("APPDATA") if os.name == "nt" else os.path.expanduser("~")
    )
    default_data_dir = os.path.join(default_data_dir, ".musicassistant")

    parser.add_argument(
        "-c",
        "--config",
        metavar="path_to_config_dir",
        default=default_data_dir,
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
        "%(asctime)-15s %(levelname)-5s %(name)s -- %(message)s"
    )
    consolehandler = logging.StreamHandler()
    consolehandler.setFormatter(logformat)
    logger.addHandler(consolehandler)

    # parse arguments
    args = get_arguments()
    data_dir = args.config
    if not os.path.isdir(data_dir):
        os.makedirs(data_dir)
    # config debug settings if needed
    if args.debug:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)
    # cool down logging for asyncio and aiosqlite
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.INFO)

    mass = MusicAssistant(data_dir, args.debug)

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
