"""Run the Music Assistant Server."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import subprocess
import sys
import threading
import traceback
from contextlib import suppress
from logging.handlers import RotatingFileHandler
from typing import Any, Final

from aiorun import run
from colorlog import ColoredFormatter

from music_assistant.constants import MASS_LOGGER_NAME, VERBOSE_LOG_LEVEL
from music_assistant.helpers.json import json_loads
from music_assistant.helpers.logging import activate_log_queue_handler
from music_assistant.mass import MusicAssistant

FORMAT_DATE: Final = "%Y-%m-%d"
FORMAT_TIME: Final = "%H:%M:%S"
FORMAT_DATETIME: Final = f"{FORMAT_DATE} {FORMAT_TIME}"
MAX_LOG_FILESIZE = 1000000 * 10  # 10 MB
ALPINE_RELEASE_FILE = "/etc/alpine-release"

LOGGER = logging.getLogger(MASS_LOGGER_NAME)


def get_arguments() -> argparse.Namespace:
    """Arguments handling."""
    parser = argparse.ArgumentParser(description="MusicAssistant")

    # determine default data directory
    if os.path.isdir(old_data_dir := os.path.join(os.path.expanduser("~"), ".musicassistant")):
        # prefer (existing) legacy directory
        default_data_dir = old_data_dir
    else:
        default_data_dir = os.path.join(
            os.getenv("XDG_DATA_HOME", os.path.join(os.path.expanduser("~"), ".local", "share")),
            "music-assistant",
        )

    default_cache_dir = os.path.join(
        os.getenv("XDG_CACHE_HOME", os.path.join(os.path.expanduser("~"), ".cache")),
        "music-assistant",
    )

    parser.add_argument(
        "--data-dir",
        "-c",
        "--config",
        metavar="path_to_data_dir",
        default=default_data_dir,
        help="Directory that contains MusicAssistant persistent data",
    )
    parser.add_argument(
        "--cache-dir",
        metavar="path_to_cache_dir",
        default=default_cache_dir,
        help="Directory that contains MusicAssistant cache data",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default=os.environ.get("LOG_LEVEL", "info"),
        help="Provide logging level. Example --log-level debug, "
        "default=info, possible=(critical, error, warning, info, debug, verbose)",
    )
    parser.add_argument(
        "--safe-mode",
        action=argparse.BooleanOptionalAction,
        help="Start in safe mode (core controllers only, no providers)",
    )

    return parser.parse_args()


def setup_logger(data_path: str, level: str = "DEBUG") -> logging.Logger:
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
                "VERBOSE": "light_black",
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

    logger = logging.getLogger()
    logger.addHandler(file_handler)
    logging.addLevelName(VERBOSE_LOG_LEVEL, "VERBOSE")

    # apply the configured global log level to the (root) music assistant logger
    logging.getLogger(MASS_LOGGER_NAME).setLevel(level)

    # silence some noisy loggers
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("databases").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("charset_normalizer").setLevel(logging.WARNING)
    logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)

    sys.excepthook = lambda *args: logging.getLogger(None).exception(
        "Uncaught exception",
        exc_info=args,
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
    if subprocess._USE_POSIX_SPAWN:
        return

    # The subprocess module does not know about Alpine Linux/musl
    # and will use fork() instead of posix_spawn() which significantly
    # less efficient. This is a workaround to force posix_spawn()
    # on Alpine Linux which is supported by musl.
    subprocess._USE_POSIX_SPAWN = os.path.exists(ALPINE_RELEASE_FILE)  # type: ignore[misc]


def _global_loop_exception_handler(_: Any, context: dict[str, Any]) -> None:
    """Handle all exception inside the core loop."""
    kwargs = {}
    if exception := context.get("exception"):
        kwargs["exc_info"] = (type(exception), exception, exception.__traceback__)

    logger = logging.getLogger(__package__)
    if source_traceback := context.get("source_traceback"):
        stack_summary = "".join(traceback.format_list(source_traceback))
        logger.error(
            "Error doing job: %s: %s",
            context["message"],
            stack_summary,
            **kwargs,  # type: ignore[arg-type]
        )
        return

    logger.error(
        "Error doing task: %s",
        context["message"],
        **kwargs,  # type: ignore[arg-type]
    )


def main() -> None:
    """Start MusicAssistant."""
    # parse arguments
    args = get_arguments()

    data_dir = args.data_dir
    cache_dir = args.cache_dir

    # move legacy cache directory
    old_cache_dir = os.path.join(data_dir, ".cache")
    if os.path.isdir(old_cache_dir) and old_cache_dir != cache_dir:
        with suppress(OSError):
            shutil.move(old_cache_dir, cache_dir)

    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)

    # TEMP: override options though hass config file
    hass_options_file = os.path.join(data_dir, "options.json")
    if os.path.isfile(hass_options_file):
        with open(hass_options_file, "rb") as _file:
            hass_options = json_loads(_file.read())
    else:
        hass_options = {}

    # prefer value in hass_options
    log_level = hass_options.get("log_level", args.log_level).upper()
    dev_mode = os.environ.get("PYTHONDEVMODE", "0") == "1"
    safe_mode = bool(
        args.safe_mode or hass_options.get("safe_mode") or os.environ.get("MASS_SAFE_MODE")
    )

    # setup logger
    logger = setup_logger(data_dir, log_level)
    mass = MusicAssistant(data_dir, cache_dir, safe_mode)

    # enable alpine subprocess workaround
    _enable_posix_spawn()

    def on_shutdown(loop: asyncio.AbstractEventLoop) -> None:
        logger.info("shutdown requested!")
        loop.run_until_complete(mass.stop())

    async def start_mass() -> None:
        loop = asyncio.get_running_loop()
        activate_log_queue_handler()
        if dev_mode or log_level == "DEBUG":
            loop.set_debug(True)
        loop.set_exception_handler(_global_loop_exception_handler)
        try:
            await mass.start()
        except Exception:
            # exit immediately if startup fails
            loop.stop()
            raise

    run(
        start_mass(),
        shutdown_callback=on_shutdown,
        executor_workers=16,
    )


if __name__ == "__main__":
    main()
