"""Start Music Assistant."""
import argparse
from logging.handlers import TimedRotatingFileHandler
import os

from aiorun import run
import logging
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


def setup_logger(data_path):
    """Initialize logger."""
    logs_dir = os.path.join(data_path, "logs")
    if not os.path.isdir(logs_dir):
        os.mkdir(logs_dir)
    logger = logging.getLogger()
    log_formatter = logging.Formatter(
        "%(asctime)-15s %(levelname)-5s %(name)s  -- %(message)s"
    )
    consolehandler = logging.StreamHandler()
    consolehandler.setFormatter(log_formatter)
    consolehandler.setLevel(logging.DEBUG)
    logger.addHandler(consolehandler)
    log_filename = os.path.join(logs_dir, "musicassistant.log")
    file_handler = TimedRotatingFileHandler(
        log_filename, when="midnight", interval=1, backupCount=10
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(log_formatter)
    logger.addHandler(file_handler)

    # global level is debug
    logger.setLevel(logging.DEBUG)

    # silence some loggers
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("databases").setLevel(logging.WARNING)
    logging.getLogger("multipart.multipart").setLevel(logging.WARNING)
    logging.getLogger("passlib.handlers.bcrypt").setLevel(logging.WARNING)

    return logger
