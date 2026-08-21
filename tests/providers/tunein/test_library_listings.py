"""Tests for TuneIn library listing validation."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.tunein import TuneInProvider


@pytest.mark.parametrize("response", [None, {}, {"body": None}, {"body": {}}])
async def test_malformed_preset_list_raises(
    provider: TuneInProvider, response: dict[str, Any] | None
) -> None:
    """A malformed top-level preset response must abort the radio sync."""
    provider._TuneInProvider__get_data = AsyncMock(  # type: ignore[attr-defined]
        return_value=response
    )

    with pytest.raises(InvalidDataError):
        _ = [radio async for radio in provider.get_library_radios()]


async def test_empty_preset_list_is_valid(provider: TuneInProvider) -> None:
    """An explicit empty preset list remains a valid empty library."""
    provider._TuneInProvider__get_data = AsyncMock(  # type: ignore[attr-defined]
        return_value={"body": []}
    )

    assert [radio async for radio in provider.get_library_radios()] == []


async def test_malformed_preset_entry_is_reported(provider: TuneInProvider) -> None:
    """An unidentifiable malformed preset must hold back radio deletions."""
    provider._TuneInProvider__get_data = AsyncMock(  # type: ignore[attr-defined]
        return_value={"body": [None]}
    )
    provider.report_skipped_sync_item = Mock()  # type: ignore[method-assign]

    assert [radio async for radio in provider.get_library_radios()] == []

    provider.report_skipped_sync_item.assert_called_once()
    args = provider.report_skipped_sync_item.call_args.args
    assert args[:2] == (MediaType.RADIO, None)
    assert isinstance(args[2], InvalidDataError)
