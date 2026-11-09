"""Fixtures for the player lifecycle scenarios."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from .harness import Scenario

if TYPE_CHECKING:
    import pathlib
    from collections.abc import AsyncGenerator


@pytest.fixture
async def scenario(tmp_path: pathlib.Path) -> AsyncGenerator[Scenario]:
    """
    Boot a real server with fake player providers, ready for a scenario.

    :param tmp_path: Temporary directory for the server's storage.
    """
    scenario = Scenario(tmp_path)
    try:
        await scenario.start()
        yield scenario
    finally:
        await scenario.close()
