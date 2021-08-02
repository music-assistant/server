"""Start Music Assistant."""
import argparse
import os

from aiorun import run
from music_assistant.helpers.logger import setup_logger
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
        "-p",
        "--port",
        metavar="port",
        default=8095,
        help="TCP port on which the server should be run.",
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
    # parse arguments
    args = get_arguments()
    data_dir = args.config
    if not os.path.isdir(data_dir):
        os.makedirs(data_dir)
    # setup logger
    logger = setup_logger(data_dir)
    # config debug settings if needed
    if args.debug or bool(os.environ.get("DEBUG")):
        debug = True
    else:
        debug = False
    mass = MusicAssistant(data_dir, debug, int(args.port))

    def on_shutdown(loop):
        logger.info("shutdown requested!")
        loop.run_until_complete(mass.stop())

    run(
        mass.start(),
        use_uvloop=True,
        shutdown_callback=on_shutdown,
        executor_workers=64,
    )


if __name__ == "__main__":
    main()
