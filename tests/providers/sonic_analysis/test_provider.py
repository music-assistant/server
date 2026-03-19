"""Tests for the sonic_analysis plugin provider."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.constants import DB_TABLE_SONIC_SIGNATURES
from music_assistant.helpers.sonic_analysis import SIGNATURE_VERSION, SonicSignature
from music_assistant.providers.sonic_analysis import SonicAnalysisProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_mass() -> MagicMock:
    """Return a MagicMock that looks enough like MusicAssistant for provider tests."""
    mass = MagicMock()
    mass.subscribe = MagicMock(return_value=lambda: None)
    mass.storage_path = "test_sonic_analysis"

    # music controller with an async database
    db = AsyncMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.get_row = AsyncMock(return_value=None)
    db.insert_or_replace = AsyncMock(return_value=1)
    mass.music = MagicMock()
    mass.music.database = db

    return mass


def _make_provider(mass: MagicMock) -> SonicAnalysisProvider:
    """Instantiate SonicAnalysisProvider with minimal mocked dependencies."""
    manifest = MagicMock()
    manifest.name = "Sonic Analysis"
    manifest.domain = "sonic_analysis"
    config = MagicMock()
    # get_value must return a plain string so the logger level branch doesn't crash
    config.get_value = MagicMock(return_value="GLOBAL")

    return SonicAnalysisProvider(mass, manifest, config, set())


# ---------------------------------------------------------------------------
# Tests: handle_async_init
# ---------------------------------------------------------------------------


class TestHandleAsyncInit:
    """Tests for SonicAnalysisProvider.handle_async_init."""

    @pytest.mark.asyncio
    async def test_creates_sonic_signatures_table(self) -> None:
        """handle_async_init must issue a CREATE TABLE for sonic_signatures."""
        mass = _make_mock_mass()
        provider = _make_provider(mass)

        with patch("music_assistant.providers.sonic_analysis.voyager", create=True):
            await provider.handle_async_init()

        db = mass.music.database
        assert db.execute.called, "database.execute was never called"

        # Collect all SQL strings passed to execute()
        all_sql: list[str] = []
        for call in db.execute.call_args_list:
            args, _ = call
            if args:
                all_sql.append(str(args[0]))

        table_name = DB_TABLE_SONIC_SIGNATURES
        create_calls = [s for s in all_sql if "CREATE TABLE" in s and table_name in s]
        assert create_calls, (
            f"No CREATE TABLE statement found for '{table_name}'. SQL calls seen: {all_sql}"
        )

    @pytest.mark.asyncio
    async def test_initialises_corpus_stats_to_none(self) -> None:
        """After handle_async_init with no DB row, corpus stats must be None."""
        mass = _make_mock_mass()
        mass.music.database.get_row = AsyncMock(return_value=None)
        provider = _make_provider(mass)

        with patch("music_assistant.providers.sonic_analysis.voyager", create=True):
            await provider.handle_async_init()

        assert provider.corpus_means is None
        assert provider.corpus_stds is None


# ---------------------------------------------------------------------------
# Tests: get_sonic_signature / set_sonic_signature
# ---------------------------------------------------------------------------


class TestSignatureRoundTrip:
    """Tests for get/set sonic signature round-trip."""

    @pytest.mark.asyncio
    async def test_get_returns_none_when_no_row(self) -> None:
        """get_sonic_signature must return None when the DB has no matching row."""
        mass = _make_mock_mass()
        mass.music.database.get_row = AsyncMock(return_value=None)
        provider = _make_provider(mass)

        with patch("music_assistant.providers.sonic_analysis.voyager", create=True):
            await provider.handle_async_init()

        result = await provider.get_sonic_signature("track_1", "local")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_returns_signature_from_db(self) -> None:
        """get_sonic_signature must deserialise a DB row into a SonicSignature."""
        mass = _make_mock_mass()
        features = [float(i) for i in range(38)]
        db_row: Mapping[str, Any] = {
            "item_id": "track_1",
            "provider": "local",
            "features": json.dumps(features),
            "version": SIGNATURE_VERSION,
        }
        mass.music.database.get_row = AsyncMock(return_value=db_row)
        provider = _make_provider(mass)

        with patch("music_assistant.providers.sonic_analysis.voyager", create=True):
            await provider.handle_async_init()

        result = await provider.get_sonic_signature("track_1", "local")
        assert isinstance(result, SonicSignature)
        assert result.features == features
        assert result.version == SIGNATURE_VERSION

    @pytest.mark.asyncio
    async def test_set_calls_insert_or_replace(self) -> None:
        """set_sonic_signature must persist the signature to the DB."""
        mass = _make_mock_mass()
        provider = _make_provider(mass)

        with patch("music_assistant.providers.sonic_analysis.voyager", create=True):
            await provider.handle_async_init()

        sig = SonicSignature(features=[1.0] * 38, version=SIGNATURE_VERSION)
        await provider.set_sonic_signature("track_1", "local", sig)

        db = mass.music.database
        assert db.insert_or_replace.called, "insert_or_replace was never called"
        call_args = db.insert_or_replace.call_args
        assert call_args is not None

        # First positional arg is the table name
        args, kwargs = call_args
        table = args[0] if args else kwargs.get("table")
        assert table == DB_TABLE_SONIC_SIGNATURES

    @pytest.mark.asyncio
    async def test_set_then_get_round_trips(self) -> None:
        """set_sonic_signature followed by get_sonic_signature must return equivalent data."""
        mass = _make_mock_mass()
        features = [float(i) * 0.1 for i in range(38)]
        sig = SonicSignature(features=features, version=SIGNATURE_VERSION)

        # Capture what was stored so get can return it
        stored: dict[str, Any] = {}

        async def fake_insert_or_replace(_table: str, values: dict[str, Any]) -> int:
            stored.update(values)
            return 1

        async def fake_get_row(_table: str, match: dict[str, Any]) -> Mapping[str, Any] | None:
            if stored and stored.get("item_id") == match.get("item_id"):
                return stored
            return None

        mass.music.database.insert_or_replace = AsyncMock(side_effect=fake_insert_or_replace)
        mass.music.database.get_row = AsyncMock(side_effect=fake_get_row)

        provider = _make_provider(mass)
        with patch("music_assistant.providers.sonic_analysis.voyager", create=True):
            await provider.handle_async_init()

        await provider.set_sonic_signature("track_1", "local", sig)
        result = await provider.get_sonic_signature("track_1", "local")

        assert result is not None
        assert result.features == features
        assert result.version == SIGNATURE_VERSION


# ---------------------------------------------------------------------------
# Tests: unload
# ---------------------------------------------------------------------------


class TestUnload:
    """Tests for SonicAnalysisProvider.unload."""

    @pytest.mark.asyncio
    async def test_unload_calls_unsubscribes(self) -> None:
        """Unload must call every registered unsubscribe callback."""
        mass = _make_mock_mass()
        provider = _make_provider(mass)

        with patch("music_assistant.providers.sonic_analysis.voyager", create=True):
            await provider.handle_async_init()

        # Manually register a mock unsubscribe callback
        unsubscribe_mock = MagicMock()
        provider._on_unload.append(unsubscribe_mock)

        await provider.unload()

        unsubscribe_mock.assert_called_once()
