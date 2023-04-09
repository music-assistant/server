"""Run the Music Assistant Server."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from logging.handlers import TimedRotatingFileHandler

import coloredlogs
from aiorun import run

from music_assistant.common.helpers.json import json_loads
from music_assistant.server import MusicAssistant


def get_arguments():
    """Arguments handling."""
    parser = argparse.ArgumentParser(description="MusicAssistant")

    default_data_dir = os.getenv("APPDATA") if os.name == "nt" else os.path.expanduser("~")
    default_data_dir = os.path.join(default_data_dir, ".musicassistant")

    parser.add_argument(
        "-c",
        "--config",
        metavar="path_to_config_dir",
        default=default_data_dir,
        help="Directory that contains the MusicAssistant configuration",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="info",
        help="Provide logging level. Example --log-level debug, "
        "default=info, possible=(critical, error, warning, info, debug)",
    )
    arguments = parser.parse_args()
    return arguments


def setup_logger(data_path: str, level: str = "DEBUG"):
    """Initialize logger."""
    logs_dir = os.path.join(data_path, "logs")
    if not os.path.isdir(logs_dir):
        os.mkdir(logs_dir)
    logger = logging.getLogger()
    log_fmt = "%(asctime)-15s %(levelname)-5s %(name)s  -- %(message)s"
    log_formatter = logging.Formatter(log_fmt)
    consolehandler = logging.StreamHandler()
    consolehandler.setFormatter(log_formatter)
    consolehandler.setLevel(logging.DEBUG)
    logger.addHandler(consolehandler)
    log_filename = os.path.join(logs_dir, "musicassistant.log")
    file_handler = TimedRotatingFileHandler(
        log_filename, when="midnight", interval=1, backupCount=10
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(log_formatter)
    logger.addHandler(file_handler)

    # global level is debug by default unless overridden
    logger.setLevel(level)

    # silence some loggers
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("databases").setLevel(logging.WARNING)

    # enable coloredlogs
    coloredlogs.install(level=level, fmt=log_fmt)
    return logger


def main():
    """Start MusicAssistant."""
    # parse arguments
    args = get_arguments()
    data_dir = args.config
    if not os.path.isdir(data_dir):
        os.makedirs(data_dir)

    # TEMP: override options though hass config file
    hass_options_file = os.path.join(data_dir, "options.json")
    if os.path.isfile(hass_options_file):
        with open(hass_options_file, "rb") as _file:
            hass_options = json_loads(_file.read())
    else:
        hass_options = {}

    log_level = hass_options.get("log_level", args.log_level).upper()
    dev_mode = bool(os.environ.get("PYTHONDEVMODE", "0"))

    # setup logger
    logger = setup_logger(data_dir, log_level)
    mass = MusicAssistant(data_dir)

    def on_shutdown(loop):
        logger.info("shutdown requested!")
        loop.run_until_complete(mass.stop())

    async def start_mass():
        loop = asyncio.get_running_loop()
        if dev_mode:
            loop.set_debug(True)
        await mass.start()

    run(
        start_mass(),
        use_uvloop=False,
        shutdown_callback=on_shutdown,
        executor_workers=32,
    )


if __name__ == "__main__":
    main()
