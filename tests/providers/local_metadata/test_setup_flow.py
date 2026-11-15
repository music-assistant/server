"""Tests for the local metadata setup flow."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.constants import CONF_PATH
from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.local_metadata.setup_flow import run_setup


class _StopFlowError(Exception):
    """Ends a flow that would otherwise keep asking."""


def _session(submissions: list[str], allowed: bool = True) -> MagicMock:
    """
    Return a setup session the given folders are submitted to, one per form.

    :param submissions: The folders submitted, in order.
    :param allowed: Whether the folders lie in a storage location the caller may use.
    """
    session = MagicMock()
    session.context.setup_data = {}
    session.context.manages_all_sources = True
    session.form = AsyncMock(side_effect=[{CONF_PATH: path} for path in submissions])
    session.finish = AsyncMock()
    session.mass.storage.can_hold_music_source = MagicMock(return_value=allowed)
    session.mass.storage.is_available = AsyncMock(side_effect=lambda path: Path(path).is_dir())
    session.mass.storage.get_location_for_path = MagicMock(return_value=None)
    return session


def _form_errors(session: MagicMock) -> list[dict[str, str | SetupFlowError] | None]:
    """Return the errors each shown form carried."""
    return [call.kwargs["errors"] for call in session.form.await_args_list]


async def test_an_existing_folder_creates_the_provider(tmp_path: Path) -> None:
    """An existing folder in a storage location is accepted."""
    session = _session([str(tmp_path)])

    await run_setup(session)

    session.finish.assert_awaited_once_with({CONF_PATH: str(tmp_path)})


async def test_the_folder_is_stored_without_surrounding_spaces(tmp_path: Path) -> None:
    """Spaces around the submitted folder are not stored."""
    session = _session([f"  {tmp_path} "])

    await run_setup(session)

    session.finish.assert_awaited_once_with({CONF_PATH: str(tmp_path)})


async def test_an_empty_or_missing_folder_is_asked_again(tmp_path: Path) -> None:
    """An empty value and a folder that does not exist are refused."""
    session = _session(["", str(tmp_path / "missing"), str(tmp_path)])

    await run_setup(session)

    errors = _form_errors(session)
    assert errors[0] is None
    assert errors[1] == {CONF_PATH: "required"}
    error = errors[2][CONF_PATH] if errors[2] else None
    assert isinstance(error, SetupFlowError)
    assert error.translation_key == "folder_not_found"
    session.finish.assert_awaited_once()


async def test_a_folder_outside_the_storage_locations_is_refused(tmp_path: Path) -> None:
    """A folder the caller may not use is refused."""
    session = _session([str(tmp_path)], allowed=False)
    session.form.side_effect = [{CONF_PATH: str(tmp_path)}, _StopFlowError()]

    with pytest.raises(_StopFlowError):
        await run_setup(session)

    errors = _form_errors(session)
    error = errors[1][CONF_PATH] if errors[1] else None
    assert isinstance(error, SetupFlowError)
    assert error.translation_key == "folder_not_allowed"
    session.finish.assert_not_awaited()


async def test_a_folder_on_unavailable_storage_is_refused(tmp_path: Path) -> None:
    """A folder on a storage location that is not there, such as an unmounted share, is refused."""
    session = _session([str(tmp_path)])
    session.form.side_effect = [{CONF_PATH: str(tmp_path)}, _StopFlowError()]
    session.mass.storage.is_available = AsyncMock(return_value=False)
    session.mass.storage.get_location_for_path = MagicMock(
        return_value=MagicMock(path=str(tmp_path), available=False)
    )

    with pytest.raises(_StopFlowError):
        await run_setup(session)

    errors = _form_errors(session)
    error = errors[1][CONF_PATH] if errors[1] else None
    assert isinstance(error, SetupFlowError)
    assert error.translation_key == "storage_location_unavailable"
    session.finish.assert_not_awaited()
