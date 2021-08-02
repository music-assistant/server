"""Initialize logger."""
import logging
import os
import random
from logging.handlers import TimedRotatingFileHandler

from .util import LimitedList


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

    html_handler = HistoryLogHandler()
    html_handler.setLevel(logging.DEBUG)
    html_handler.setFormatter(log_formatter)
    logger.addHandler(html_handler)

    # global level is debug
    logger.setLevel(logging.DEBUG)

    # silence some loggers
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("databases").setLevel(logging.WARNING)
    logging.getLogger("multipart.multipart").setLevel(logging.WARNING)
    logging.getLogger("passlib.handlers.bcrypt").setLevel(logging.WARNING)

    return logger


class HistoryLogHandler(logging.Handler):
    """A logging handler that keeps the last X records in memory."""

    def __init__(self, max_len: int = 200):
        """Initialize instance."""
        logging.Handler.__init__(self)
        # Our custom argument
        self._history = LimitedList(max_len=max_len)
        self._max_len = max_len

    @property
    def max_len(self) -> int:
        """Return the max size of the log list."""
        return self._max_len

    def emit(self, record):
        """Emit log record."""
        self._history.append(
            {
                "id": f"{record.asctime}.{random.randint(0, 9)}",
                "time": record.asctime,
                "name": record.name,
                "level": record.levelname,
                "message": record.message,
            }
        )

    def get_history(self):
        """Get all log lines in history."""
        return self._history
