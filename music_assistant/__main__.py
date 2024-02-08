"""Run the Music Assistant Server."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
import threading
from contextlib import suppress
from logging.handlers import RotatingFileHandler
from typing import Final

from aiorun import run
from colorlog import ColoredFormatter

from music_assistant.common.helpers.json import json_loads
from music_assistant.constants import ROOT_LOGGER_NAME
from music_assistant.server import MusicAssistant
from music_assistant.server.helpers.logging import activate_log_queue_handler

FORMAT_DATE: Final = "%Y-%m-%d"
FORMAT_TIME: Final = "%H:%M:%S"
FORMAT_DATETIME: Final = f"{FORMAT_DATE} {FORMAT_TIME}"
MAX_LOG_FILESIZE = 1000000 * 10  # 10 MB
ALPINE_RELEASE_FILE = "/etc/alpine-release"


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
    parser.add_argument("-u", "--enable-uvloop", action="store_true")
    return parser.parse_args()


def setup_logger(data_path: str, level: str = "DEBUG"):
    """Initialize logger."""
    # define log formatter
    log_fmt = "%(asctime)s.%(msecs)03d %(levelname)s (%(threadName)s) [%(name)s] %(message)s"

    # base logging config for the root logger
    logging.basicConfig(level=logging.INFO)

    colorfmt = f"%(log_color)s{log_fmt}%(reset)s"
    logging.getLogger().handlers[0].setFormatter(
        ColoredFormatter(
            colorfmt,
            datefmt=FORMAT_DATETIME,
            reset=True,
            log_colors={
                "DEBUG": "cyan",
                "INFO": "green",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "red",
            },
        )
    )

    # Capture warnings.warn(...) and friends messages in logs.
    # The standard destination for them is stderr, which may end up unnoticed.
    # This way they're where other messages are, and can be filtered as usual.
    logging.captureWarnings(True)

    # setup file handler
    log_filename = os.path.join(data_path, "musicassistant.log")
    file_handler = RotatingFileHandler(log_filename, maxBytes=MAX_LOG_FILESIZE, backupCount=1)
    # rotate log at each start
    with suppress(OSError):
        file_handler.doRollover()
    file_handler.setFormatter(logging.Formatter(log_fmt, datefmt=FORMAT_DATETIME))
    # file_handler.setLevel(logging.INFO)

    logger = logging.getLogger()
    logger.addHandler(file_handler)

    # apply the configured global log level to the (root) music assistant logger
    logging.getLogger(ROOT_LOGGER_NAME).setLevel(level)

    # silence some noisy loggers
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("databases").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("charset_normalizer").setLevel(logging.WARNING)

    sys.excepthook = lambda *args: logging.getLogger(None).exception(
        "Uncaught exception",
        exc_info=args,  # type: ignore[arg-type]
    )
    threading.excepthook = lambda args: logging.getLogger(None).exception(
        "Uncaught thread exception",
        exc_info=(  # type: ignore[arg-type]
            args.exc_type,
            args.exc_value,
            args.exc_traceback,
        ),
    )

    return logger


def _enable_posix_spawn() -> None:
    """Enable posix_spawn on Alpine Linux."""
    # pylint: disable=protected-access
    if subprocess._USE_POSIX_SPAWN:
        return

    # The subprocess module does not know about Alpine Linux/musl
    # and will use fork() instead of posix_spawn() which significantly
    # less efficient. This is a workaround to force posix_spawn()
    # on Alpine Linux which is supported by musl.
    subprocess._USE_POSIX_SPAWN = os.path.exists(ALPINE_RELEASE_FILE)


def main() -> None:
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
    enable_uvloop = bool(hass_options.get("enable_uvloop", args.enable_uvloop))
    dev_mode = os.environ.get("PYTHONDEVMODE", "0") == "1"

    # setup logger
    logger = setup_logger(data_dir, log_level)
    mass = MusicAssistant(data_dir)

    # enable alpine subprocess workaround
    _enable_posix_spawn()

    def on_shutdown(loop) -> None:
        logger.info("shutdown requested!")
        loop.run_until_complete(mass.stop())

    async def start_mass() -> None:
        loop = asyncio.get_running_loop()
        activate_log_queue_handler()
        if dev_mode or log_level == "DEBUG":
            loop.set_debug(True)
        await mass.start()

    run(
        start_mass(),
        use_uvloop=enable_uvloop,
        shutdown_callback=on_shutdown,
        executor_workers=64,
    )


if __name__ == "__main__":
    main()
