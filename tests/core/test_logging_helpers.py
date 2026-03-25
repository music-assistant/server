"""Tests for logging utility helpers."""

from __future__ import annotations

import logging
import logging.handlers
import queue
from functools import partial
from unittest.mock import MagicMock, patch

from music_assistant.helpers.logging import (
    LoggingQueueHandler,
    activate_log_queue_handler,
    catch_log_exception,
    log_exception,
)


def test_logging_queue_handler_prepare_strips_stack_info() -> None:
    """LoggingQueueHandler.prepare clears stack_info from the record."""
    simple_queue: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
    handler = LoggingQueueHandler(simple_queue)

    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname="test.py",
        lineno=1,
        msg="oops",
        args=(),
        exc_info=None,
    )
    record.stack_info = "some stack trace"

    prepared = handler.prepare(record)
    assert prepared.stack_info is None


def test_logging_queue_handler_handle_emits_when_filter_passes() -> None:
    """LoggingQueueHandler.handle emits the record when the filter allows it."""
    simple_queue: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
    handler = LoggingQueueHandler(simple_queue)

    record = logging.LogRecord(
        name="test",
        level=logging.WARNING,
        pathname="test.py",
        lineno=1,
        msg="warning",
        args=(),
        exc_info=None,
    )

    result = handler.handle(record)
    # Default filter always passes — queue should have one item
    assert result
    assert not simple_queue.empty()


def test_logging_queue_handler_close_stops_listener() -> None:
    """LoggingQueueHandler.close stops the attached QueueListener."""
    simple_queue: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
    handler = LoggingQueueHandler(simple_queue)

    mock_listener = MagicMock(spec=logging.handlers.QueueListener)
    handler.listener = mock_listener

    handler.close()

    mock_listener.stop.assert_called_once()
    assert handler.listener is None


def test_logging_queue_handler_close_no_listener() -> None:
    """LoggingQueueHandler.close without a listener does not raise."""
    simple_queue: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
    handler = LoggingQueueHandler(simple_queue)
    handler.listener = None
    # Should not raise
    handler.close()


def test_log_exception_logs_error() -> None:
    """log_exception formats the message and logs it at ERROR level."""
    with patch("logging.getLogger") as mock_get_logger:
        mock_logger = MagicMock()
        mock_get_logger.return_value = mock_logger
        try:
            raise ValueError("test error")
        except ValueError:
            log_exception(lambda: "friendly message")

        mock_logger.error.assert_called_once()


def test_catch_log_exception_sync_logs_on_error() -> None:
    """catch_log_exception wraps a sync function and logs exceptions."""

    def format_err(*_args: object) -> str:
        return "error in func"

    def bad_func(_x: int) -> None:
        raise RuntimeError("boom")

    wrapped = catch_log_exception(bad_func, format_err)

    with patch("logging.getLogger") as mock_get_logger:
        mock_logger = MagicMock()
        mock_get_logger.return_value = mock_logger
        # Should not raise — exception is swallowed and logged
        wrapped(42)
        mock_logger.error.assert_called_once()


def test_catch_log_exception_sync_no_error() -> None:
    """catch_log_exception passes through return for non-raising sync function."""

    def good_func(_x: int) -> None:
        pass

    wrapped = catch_log_exception(good_func, lambda: "err")
    # Should not raise
    wrapped(1)


async def test_catch_log_exception_async_logs_on_error() -> None:
    """catch_log_exception wraps an async function and logs exceptions."""

    def format_err(*_args: object) -> str:
        return "async error"

    async def bad_async(_x: int) -> None:
        raise RuntimeError("async boom")

    wrapped = catch_log_exception(bad_async, format_err)

    with patch("logging.getLogger") as mock_get_logger:
        mock_logger = MagicMock()
        mock_get_logger.return_value = mock_logger
        await wrapped(1)
        mock_logger.error.assert_called_once()


async def test_catch_log_exception_async_no_error() -> None:
    """catch_log_exception async wrapper passes through when no exception."""

    async def good_async(_x: int) -> None:
        pass

    wrapped = catch_log_exception(good_async, lambda: "err")
    await wrapped(1)


def test_activate_log_queue_handler_migrates_handlers() -> None:
    """activate_log_queue_handler moves root handlers into a queue listener."""
    stream_handler = logging.StreamHandler()
    original_handlers = logging.root.handlers[:]
    logging.root.addHandler(stream_handler)

    try:
        activate_log_queue_handler()
        # After activation the root logger should contain a LoggingQueueHandler
        queue_handlers = [h for h in logging.root.handlers if isinstance(h, LoggingQueueHandler)]
        assert len(queue_handlers) == 1
        qh = queue_handlers[0]
        # The listener should be running
        assert qh.listener is not None
    finally:
        # Clean up: stop listener and restore original handlers
        for h in logging.root.handlers[:]:
            if isinstance(h, LoggingQueueHandler) and h.listener:
                h.listener.stop()
            logging.root.removeHandler(h)
        for h in original_handlers:
            logging.root.addHandler(h)


def test_catch_log_exception_partial_sync() -> None:
    """catch_log_exception handles partial-wrapped sync callables."""

    def bad_func(_x: int, _y: int) -> None:
        raise RuntimeError("partial boom")

    partial_func = partial(bad_func, _y=2)
    wrapped = catch_log_exception(partial_func, lambda *_: "err")

    with patch("logging.getLogger") as mock_get_logger:
        mock_logger = MagicMock()
        mock_get_logger.return_value = mock_logger
        wrapped(1)
        mock_logger.error.assert_called_once()
