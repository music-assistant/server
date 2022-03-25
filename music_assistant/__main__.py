"""Script to run a full Music Assistant server."""
import argparse
import asyncio
import logging
import os
from logging.handlers import TimedRotatingFileHandler

from aiorun import run

from music_assistant import MusicAssistant


def global_exception_handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """Global exception handler."""
    logging.getLogger().debug(
        "Caught exception: %s", context.get("exception", context["message"])
    )
    if "Broken pipe" in str(context.get("exception")):
        # fix for the spamming subprocess
        return
    loop.default_exception_handler(context)


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
        "--stream-port",
        metavar="stream_port",
        default=8095,
        help="TCP port on which the audio streaming should be run.",
    )
    parser.add_argument(
        "-p",
        "--web-port",
        metavar="api_port",
        default=8096,
        help="TCP port on which the (HTTP) webserver for the API will be run.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Start MusicAssistant with verbose debug logging",
    )
    arguments = parser.parse_args()
    return arguments


def main():
    """Start MusicAssistant stand-alone as script."""
    # parse arguments
    args = get_arguments()
    data_dir = args.config
    if not os.path.isdir(data_dir):
        os.makedirs(data_dir)
    # setup logger and debug settings
    debug = args.debug or bool(os.environ.get("DEBUG"))
    logger = setup_logger(data_dir, debug)
    db_file = os.path.join(data_dir, "music_assistant.db")
    mass = MusicAssistant(f"sqlite:///{db_file}", int(args.stream_port))

    async def async_main():
        """Async main routine."""
        loop = asyncio.get_event_loop()
        loop.set_exception_handler(global_exception_handler)
        loop.set_debug(debug)
        await mass.setup()

    def on_shutdown(loop):
        logger.info("shutdown requested!")
        loop.run_until_complete(mass.stop())

    run(
        async_main(),
        use_uvloop=True,
        shutdown_callback=on_shutdown,
        executor_workers=64,
    )


if __name__ == "__main__":
    main()


def setup_logger(data_path: str, debug: bool):
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

    if debug:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    # silence some loggers
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("databases").setLevel(logging.WARNING)
    logging.getLogger("multipart.multipart").setLevel(logging.WARNING)
    logging.getLogger("passlib.handlers.bcrypt").setLevel(logging.WARNING)

    return logger
