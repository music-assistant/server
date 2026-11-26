"""Tests for the startup of the server (``python -m music_assistant``)."""

from __future__ import annotations

import asyncio
import logging
import resource
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant import __main__ as main_module
from music_assistant.constants import MASS_LOGGER_NAME


def _arguments(monkeypatch: pytest.MonkeyPatch, *argv: str) -> Any:
    """Return the parsed arguments for the given command line."""
    monkeypatch.setattr(sys, "argv", ["mass", *argv])
    return main_module.get_arguments()


def test_default_directories_follow_the_home_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without options the data lives in the home directory, with the cache inside it."""
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.setenv("HOME", "/home/jane")

    args = _arguments(monkeypatch)

    assert args.data_dir == "/home/jane/.musicassistant"
    assert args.cache_dir == "/home/jane/.musicassistant/.cache"
    assert args.log_level == "info"
    assert args.safe_mode is None
    assert args.dev_providers_dir is None


def test_default_directories_follow_the_xdg_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """The XDG directories and the LOG_LEVEL variable set the defaults."""
    monkeypatch.setenv("XDG_DATA_HOME", "/xdg/data")
    monkeypatch.setenv("XDG_CACHE_HOME", "/xdg/cache")
    monkeypatch.setenv("LOG_LEVEL", "debug")

    args = _arguments(monkeypatch)

    assert args.data_dir == "/xdg/data/music-assistant"
    assert args.cache_dir == "/xdg/cache/music-assistant"
    assert args.log_level == "debug"


@pytest.mark.parametrize("flag", ["--data-dir", "-c", "--config"])
def test_data_dir_option(monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
    """Each spelling of the data directory option sets the data directory."""
    assert _arguments(monkeypatch, flag, "/data").data_dir == "/data"


def test_other_options(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cache directory, log level and dev providers directory are taken as given."""
    args = _arguments(
        monkeypatch,
        "--cache-dir",
        "/cache",
        "--log-level",
        "verbose",
        "--dev-providers-dir",
        "providers",
    )

    assert args.cache_dir == "/cache"
    assert args.log_level == "verbose"
    assert args.dev_providers_dir == ["providers"]


@pytest.mark.parametrize(("flag", "expected"), [("--safe-mode", True), ("--no-safe-mode", False)])
def test_safe_mode_option(monkeypatch: pytest.MonkeyPatch, flag: str, expected: bool) -> None:
    """Safe mode can be switched on and off on the command line."""
    assert _arguments(monkeypatch, flag).safe_mode is expected


@pytest.fixture
def restored_logging() -> Iterator[None]:
    """Put the logging setup of the test run back after a test changed it."""
    root = logging.getLogger()
    handlers = list(root.handlers)
    formatters = [handler.formatter for handler in handlers]
    loggers = [root, *(logging.getLogger(name) for name in list(root.manager.loggerDict))]
    levels = [logger.level for logger in loggers]
    asyncio_filters = list(logging.getLogger("asyncio").filters)
    hooks = (sys.excepthook, threading.excepthook)
    yield
    for handler in root.handlers[:]:
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    for handler, formatter in zip(handlers, formatters, strict=True):
        handler.setFormatter(formatter)
    for name in list(root.manager.loggerDict):
        logging.getLogger(name).setLevel(logging.NOTSET)
    for logger, level in zip(loggers, levels, strict=True):
        logger.setLevel(level)
    logging.getLogger("asyncio").filters[:] = asyncio_filters
    logging.captureWarnings(False)
    sys.excepthook, threading.excepthook = hooks


@pytest.mark.usefixtures("restored_logging")
def test_setup_logger_writes_to_a_log_file_in_the_data_directory(tmp_path: Path) -> None:
    """The log file sits in the data directory and Music Assistant logs at the chosen level."""
    with patch.object(main_module, "install_diagnostics_log_handler") as install_diagnostics:
        logger = main_module.setup_logger(str(tmp_path), "DEBUG")

    logging.getLogger(MASS_LOGGER_NAME).debug("hello from the test")
    for handler in logger.handlers:
        handler.flush()

    install_diagnostics.assert_called_once_with()
    assert "hello from the test" in (tmp_path / "musicassistant.log").read_text()
    assert logging.getLogger(MASS_LOGGER_NAME).level == logging.DEBUG
    assert logging.getLogger("aiosqlite").level == logging.WARNING


@pytest.mark.usefixtures("restored_logging")
def test_setup_logger_hides_the_buffered_stream_warning(tmp_path: Path) -> None:
    """The slow callback warning of a waiting audio buffer is dropped, other warnings stay."""
    with patch.object(main_module, "install_diagnostics_log_handler"):
        main_module.setup_logger(str(tmp_path), "INFO")
    asyncio_logger = logging.getLogger("asyncio")

    def passes(level: int, message: str) -> bool:
        return (
            asyncio_logger.filter(
                asyncio_logger.makeRecord("asyncio", level, "", 0, message, (), None)
            )
            is not False
        )

    assert not passes(logging.WARNING, "Executing buffered.<locals>.producer() took 0.2 seconds")
    assert passes(logging.WARNING, "Executing something_else() took 0.2 seconds")
    assert passes(logging.ERROR, "buffered.<locals>.producer() failed")


@pytest.mark.parametrize(("is_alpine", "expected"), [(True, True), (False, False)])
def test_posix_spawn_is_enabled_on_alpine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, is_alpine: bool, expected: bool
) -> None:
    """Subprocesses are spawned the cheap way on Alpine Linux."""
    release_file = tmp_path / "alpine-release"
    if is_alpine:
        release_file.touch()
    monkeypatch.setattr(main_module, "ALPINE_RELEASE_FILE", str(release_file))
    monkeypatch.setattr(subprocess, "_USE_POSIX_SPAWN", False)

    main_module._enable_posix_spawn()

    assert subprocess._USE_POSIX_SPAWN is expected


def test_posix_spawn_is_left_alone_when_already_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A platform that spawns already is not looked at again."""
    monkeypatch.setattr(main_module, "ALPINE_RELEASE_FILE", "/does/not/exist")
    monkeypatch.setattr(subprocess, "_USE_POSIX_SPAWN", True)

    main_module._enable_posix_spawn()

    assert subprocess._USE_POSIX_SPAWN is True


def test_loop_errors_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    """An error the event loop reports ends up in the log, with where it came from if known."""
    error = ValueError("boom")

    with caplog.at_level(logging.ERROR, logger="music_assistant"):
        main_module._global_loop_exception_handler(None, {"message": "a task", "exception": error})
        main_module._global_loop_exception_handler(
            None, {"message": "a job", "source_traceback": [("file.py", 12, "run", "x = 1")]}
        )

    assert "Error doing task: a task" in caplog.text
    assert "ValueError: boom" in caplog.text
    assert "Error doing job: a job" in caplog.text
    assert 'File "file.py", line 12, in run' in caplog.text


@dataclass
class Startup:
    """The stand-ins a run of ``main`` works with."""

    mass: MagicMock
    mass_class: MagicMock
    setup_logger: MagicMock


@pytest.fixture
def startup(monkeypatch: pytest.MonkeyPatch) -> Iterator[Startup]:
    """Let ``main`` run against a stand-in server, without touching logging or process limits."""
    mass = MagicMock(start=AsyncMock(side_effect=lambda: signal.raise_signal(signal.SIGTERM)))
    mass.stop = AsyncMock()
    setup_logger = MagicMock()
    monkeypatch.delenv("MASS_SAFE_MODE", raising=False)
    monkeypatch.setattr(main_module, "setup_logger", setup_logger)
    monkeypatch.setattr(main_module, "cap_native_thread_pools", MagicMock(return_value=2))
    monkeypatch.setattr(main_module, "set_model_cache_dirs", MagicMock())
    monkeypatch.setattr(main_module, "activate_log_queue_handler", MagicMock())
    monkeypatch.setattr(main_module, "_enable_posix_spawn", MagicMock())
    monkeypatch.setattr(resource, "getrlimit", MagicMock(return_value=(64, 64)))
    monkeypatch.setattr(resource, "setrlimit", MagicMock())
    with patch.object(main_module, "MusicAssistant", return_value=mass) as mass_class:
        signal_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            yield Startup(mass, mass_class, setup_logger)
        finally:
            for sig, handler in signal_handlers.items():
                signal.signal(sig, handler)


def _run_main(monkeypatch: pytest.MonkeyPatch, data_dir: Path, *argv: str) -> None:
    """Run ``main`` with the given data directory and further command line options."""
    cache_dir = data_dir / "cache"
    monkeypatch.setattr(
        sys, "argv", ["mass", "--data-dir", str(data_dir), "--cache-dir", str(cache_dir), *argv]
    )
    main_module.main()


def test_main_starts_the_server_and_stops_it_on_a_signal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, startup: Startup
) -> None:
    """The directories are created, the server runs until a signal and is then stopped."""
    data_dir = tmp_path / "data"

    _run_main(monkeypatch, data_dir, "--log-level", "debug")

    assert data_dir.is_dir()
    assert (data_dir / "cache").is_dir()
    startup.mass_class.assert_called_once_with(str(data_dir), str(data_dir / "cache"), False, None)
    startup.setup_logger.assert_called_once_with(str(data_dir), "DEBUG")
    startup.mass.start.assert_awaited_once_with()
    startup.mass.stop.assert_awaited_once_with()


@pytest.mark.parametrize("source", ["flag", "environment", "add-on options"])
def test_main_starts_in_safe_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, startup: Startup, source: str
) -> None:
    """Safe mode comes from the command line, the environment or the add-on options."""
    argv = ["--safe-mode"] if source == "flag" else []
    if source == "environment":
        monkeypatch.setenv("MASS_SAFE_MODE", "1")
    if source == "add-on options":
        (tmp_path / "options.json").write_text('{"safe_mode": true}')

    _run_main(monkeypatch, tmp_path, *argv)

    assert startup.mass_class.call_args.args[2] is True


def test_main_prefers_the_add_on_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, startup: Startup
) -> None:
    """The log level of the Home Assistant add-on options wins over the command line."""
    (tmp_path / "options.json").write_text('{"log_level": "verbose"}')
    providers_dir = tmp_path / "providers"

    _run_main(
        monkeypatch, tmp_path, "--log-level", "info", "--dev-providers-dir", str(providers_dir)
    )

    startup.setup_logger.assert_called_once_with(str(tmp_path), "VERBOSE")
    assert startup.mass_class.call_args.args[3] == (str(providers_dir.resolve()),)


@pytest.mark.parametrize("error", [None, OSError("not permitted")])
def test_main_raises_the_open_file_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    startup: Startup,
    caplog: pytest.LogCaptureFixture,
    error: OSError | None,
) -> None:
    """The open-file limit is raised to its maximum, and a refusal only gives a warning."""
    monkeypatch.setattr(resource, "getrlimit", MagicMock(return_value=(1024, 4096)))
    setrlimit = MagicMock(side_effect=error)
    monkeypatch.setattr(resource, "setrlimit", setrlimit)

    with caplog.at_level(logging.WARNING, logger=MASS_LOGGER_NAME):
        _run_main(monkeypatch, tmp_path)

    setrlimit.assert_called_once_with(resource.RLIMIT_NOFILE, (4096, 4096))
    assert ("Could not raise open-file limit" in caplog.text) is (error is not None)
    startup.mass.stop.assert_awaited_once_with()


def test_main_stops_the_server_when_its_startup_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, startup: Startup
) -> None:
    """A startup that fails part-way is still cleaned up."""
    startup.mass.start.side_effect = RuntimeError("no database")

    with pytest.raises(RuntimeError, match="no database"):
        _run_main(monkeypatch, tmp_path)

    startup.mass.stop.assert_awaited_once_with()


def test_main_ends_quietly_on_a_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, startup: Startup
) -> None:
    """Ctrl+C during startup stops the server without an error."""
    startup.mass.start.side_effect = KeyboardInterrupt

    _run_main(monkeypatch, tmp_path)

    startup.mass.stop.assert_awaited_once_with()


def test_the_event_loop_reports_errors_through_the_handler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, startup: Startup
) -> None:
    """The running event loop hands its errors to the handler of the server."""
    handlers: list[Any] = []

    async def start() -> None:
        handlers.append(asyncio.get_running_loop().get_exception_handler())
        signal.raise_signal(signal.SIGTERM)

    startup.mass.start.side_effect = start

    _run_main(monkeypatch, tmp_path)

    assert handlers == [main_module._global_loop_exception_handler]
