# ruff: noqa: D102, RUF001
"""
Tests for provider/dialogs_control.py — transfer parser + executor.

As of v1.4.0 the regex parser only handles ``transfer`` (target player
is a per-user dynamic enum that can't fit a static intent grammar);
every other control command is recognised by Yandex through declarative
custom intents (see ``tests/test_dialogs_grammar.py``). This file
covers what's left in this module: the transfer regex, the ``parse_control``
fallthrough behaviour, and the executor / confirmation / pluralisation
helpers used by the webhook handler regardless of which parser produced
the ``ParsedControl``.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import RepeatMode

from music_assistant.providers.yandex_alice.dialogs_control import (
    ParsedControl,
    _plural_ru,
    control_confirmation,
    execute_control,
    format_list_players,
    parse_control,
)


class TestParseControlTransfer:
    """``transfer`` is the only regex-handled action remaining in v1.4.0+."""

    @pytest.mark.parametrize(
        ("phrase", "expected_hint"),
        [
            ("переведи на спальню", "спальню"),
            ("перенеси на спальню", "спальню"),
            ("продолжи в спальне", "спальне"),
            ("переведи музыку на кухню", "кухню"),
            # The "Алиса," prefix is stripped before regex matching.
            ("Алиса, переведи на кухню", "кухню"),
        ],
    )
    def test_transfer_captures_target_into_player_hint(
        self,
        phrase: str,
        expected_hint: str,
    ) -> None:
        """Target is captured (lower-cased) into player_hint."""
        result = parse_control(phrase)
        assert result is not None
        assert result.action == "transfer"
        assert result.player_hint == expected_hint
        assert result.value is None


class TestParseControlFallthrough:
    """
    Non-transfer phrases return None.

    The platform-side intent parser (`provider.dialogs_grammar.parse_platform_intent`)
    handles them, and the play-command parser handles the rest.
    """

    @pytest.mark.parametrize(
        "phrase",
        [
            "",
            # Originally regex-handled, now platform-only.
            "пауза",
            "стоп",
            "следующая",
            "громче",
            "громкость 50",
            "прибавь на 20",
            "перемотай вперёд на 30 секунд",
            "повтори песню",
            "приглуши",
            "к началу",
            "что играет",
            "какие колонки",
            "забудь колонку",
            "перемешай",
            # Play domain — never handled here.
            "включи Metallica",
            "включи джаз на кухне",
            "включи мою волну",
            # Garbage.
            "что-то непонятное",
        ],
    )
    def test_returns_none(self, phrase: str) -> None:
        """Anything other than a transfer phrase falls through (None)."""
        assert parse_control(phrase) is None


class TestPluralRu:
    """Russian quantitative-form picker."""

    @pytest.mark.parametrize(
        ("n", "expected"),
        [
            (1, "колонку"),
            (2, "колонки"),
            (3, "колонки"),
            (4, "колонки"),
            (5, "колонок"),
            (10, "колонок"),
            (11, "колонок"),  # 11 is exception — uses 5+ form
            (12, "колонок"),
            (14, "колонок"),
            (21, "колонку"),  # 21 → 1-form
            (22, "колонки"),
            (25, "колонок"),
            (101, "колонку"),
            (111, "колонок"),
            (0, "колонок"),
        ],
    )
    def test_plural(self, n: int, expected: str) -> None:
        """Russian quantitative agreement matches expected form for `n`."""
        assert _plural_ru(n, ("колонку", "колонки", "колонок")) == expected


class TestFormatListPlayers:
    """`list_players` confirmation builder."""

    def test_zero_players(self) -> None:
        assert format_list_players([]) == "Не вижу ни одной колонки."

    def test_one_player(self) -> None:
        p = MagicMock()
        p.name = "Кухня"
        p.player_id = "p1"
        assert format_list_players([p]) == "Вижу одну колонку: Кухня."

    def test_three_players(self) -> None:
        ps = []
        for name, pid in [("Кухня", "p1"), ("Спальня", "p2"), ("Гостиная", "p3")]:
            p = MagicMock()
            p.name = name
            p.player_id = pid
            ps.append(p)
        assert format_list_players(ps) == "Вижу 3 колонки: Кухня, Спальня, Гостиная."

    def test_five_players(self) -> None:
        ps = []
        for i in range(5):
            p = MagicMock()
            p.name = f"Player{i}"
            p.player_id = f"p{i}"
            ps.append(p)
        text = format_list_players(ps)
        assert text.startswith("Вижу 5 колонок:")


class TestControlConfirmation:
    """User-facing confirmation strings."""

    @pytest.mark.parametrize(
        ("action", "value", "expected"),
        [
            ("pause", None, "Пауза."),
            ("resume", None, "Продолжаю."),
            ("stop", None, "Остановил."),
            ("next", None, "Следующая."),
            ("previous", None, "Предыдущая."),
            ("volume_up", None, "Громче."),
            ("volume_down", None, "Тише."),
            ("volume_set", 50, "Громкость 50."),
            ("volume_relative", 20, "Громче на 20."),
            ("volume_relative", -15, "Тише на 15."),
            ("mute", None, "Звук выключен."),
            ("unmute", None, "Звук включен."),
            ("shuffle_on", None, "Включил перемешивание."),
            ("shuffle_off", None, "Выключил перемешивание."),
            ("repeat_off", None, "Выключил повтор."),
            ("repeat_one", None, "Повтор песни."),
            ("repeat_all", None, "Повтор очереди."),
            ("seek_forward", 60, "Перемотал на 60 секунд вперёд."),
            ("seek_back", 30, "Перемотал на 30 секунд назад."),
            ("seek_start", None, "Перемотал к началу."),
        ],
    )
    def test_confirmation(self, action: str, value: int | None, expected: str) -> None:
        ctrl = ParsedControl(action=action, value=value)  # type: ignore[arg-type]
        assert control_confirmation(ctrl) == expected


@pytest.mark.asyncio
class TestExecuteControl:
    """execute_control dispatches each ParsedControl to the correct MA call."""

    def _make_mass(self) -> MagicMock:
        mass = MagicMock()
        mass.player_queues = MagicMock()
        mass.player_queues.pause = AsyncMock()
        mass.player_queues.resume = AsyncMock()
        mass.player_queues.stop = AsyncMock()
        mass.player_queues.next = AsyncMock()
        mass.player_queues.previous = AsyncMock()
        mass.player_queues.set_shuffle = AsyncMock()
        mass.player_queues.set_repeat = MagicMock()  # NB: sync, not async
        mass.player_queues.skip = AsyncMock()
        mass.player_queues.seek = AsyncMock()
        mass.players = MagicMock()
        mass.players.cmd_volume_up = AsyncMock()
        mass.players.cmd_volume_down = AsyncMock()
        mass.players.cmd_volume_set = AsyncMock()
        mass.players.cmd_volume_mute = AsyncMock()
        return mass

    def _player(self) -> MagicMock:
        player = MagicMock()
        player.player_id = "p1"
        return player

    async def test_pause_calls_pause(self) -> None:
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="pause"), self._player())
        mass.player_queues.pause.assert_awaited_once_with("p1")

    async def test_resume_calls_resume(self) -> None:
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="resume"), self._player())
        mass.player_queues.resume.assert_awaited_once_with("p1")

    async def test_stop_calls_stop(self) -> None:
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="stop"), self._player())
        mass.player_queues.stop.assert_awaited_once_with("p1")

    async def test_next_calls_next(self) -> None:
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="next"), self._player())
        mass.player_queues.next.assert_awaited_once_with("p1")

    async def test_previous_calls_previous(self) -> None:
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="previous"), self._player())
        mass.player_queues.previous.assert_awaited_once_with("p1")

    async def test_volume_up(self) -> None:
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="volume_up"), self._player())
        mass.players.cmd_volume_up.assert_awaited_once_with("p1")

    async def test_volume_down(self) -> None:
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="volume_down"), self._player())
        mass.players.cmd_volume_down.assert_awaited_once_with("p1")

    async def test_volume_set(self) -> None:
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="volume_set", value=42), self._player())
        mass.players.cmd_volume_set.assert_awaited_once_with("p1", 42)

    async def test_volume_set_none_falls_back_to_zero(self) -> None:
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="volume_set", value=None), self._player())
        mass.players.cmd_volume_set.assert_awaited_once_with("p1", 0)

    async def test_volume_relative_increase(self) -> None:
        mass = self._make_mass()
        player = self._player()
        player.volume_level = 40
        await execute_control(mass, ParsedControl(action="volume_relative", value=20), player)
        mass.players.cmd_volume_set.assert_awaited_once_with("p1", 60)

    async def test_volume_relative_decrease(self) -> None:
        mass = self._make_mass()
        player = self._player()
        player.volume_level = 70
        await execute_control(mass, ParsedControl(action="volume_relative", value=-15), player)
        mass.players.cmd_volume_set.assert_awaited_once_with("p1", 55)

    async def test_volume_relative_clamps_high(self) -> None:
        mass = self._make_mass()
        player = self._player()
        player.volume_level = 90
        await execute_control(mass, ParsedControl(action="volume_relative", value=50), player)
        mass.players.cmd_volume_set.assert_awaited_once_with("p1", 100)

    async def test_volume_relative_clamps_low(self) -> None:
        mass = self._make_mass()
        player = self._player()
        player.volume_level = 5
        await execute_control(mass, ParsedControl(action="volume_relative", value=-30), player)
        mass.players.cmd_volume_set.assert_awaited_once_with("p1", 0)

    async def test_volume_relative_missing_volume_level_uses_default(self) -> None:
        mass = self._make_mass()
        player = self._player()
        del player.volume_level
        await execute_control(mass, ParsedControl(action="volume_relative", value=10), player)
        mass.players.cmd_volume_set.assert_awaited_once_with("p1", 60)

    async def test_mute(self) -> None:
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="mute"), self._player())
        mass.players.cmd_volume_mute.assert_awaited_once_with("p1", True)

    async def test_unmute(self) -> None:
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="unmute"), self._player())
        mass.players.cmd_volume_mute.assert_awaited_once_with("p1", False)

    async def test_list_players_is_a_safe_noop_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        mass = self._make_mass()
        with caplog.at_level(
            logging.WARNING, logger="music_assistant.providers.yandex_alice.dialogs_control"
        ):
            await execute_control(mass, ParsedControl(action="list_players"), self._player())
        mass.player_queues.pause.assert_not_awaited()
        mass.player_queues.resume.assert_not_awaited()
        mass.players.cmd_volume_set.assert_not_awaited()
        assert any("list_players" in r.getMessage() for r in caplog.records)

    async def test_shuffle_on(self) -> None:
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="shuffle_on"), self._player())
        mass.player_queues.set_shuffle.assert_awaited_once_with("p1", shuffle_enabled=True)

    async def test_shuffle_off(self) -> None:
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="shuffle_off"), self._player())
        mass.player_queues.set_shuffle.assert_awaited_once_with("p1", shuffle_enabled=False)

    async def test_repeat_off(self) -> None:
        """`set_repeat` is sync, not async — verified via `assert_called_once`."""
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="repeat_off"), self._player())
        mass.player_queues.set_repeat.assert_called_once_with("p1", RepeatMode.OFF)

    async def test_repeat_one(self) -> None:
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="repeat_one"), self._player())
        mass.player_queues.set_repeat.assert_called_once_with("p1", RepeatMode.ONE)

    async def test_repeat_all(self) -> None:
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="repeat_all"), self._player())
        mass.player_queues.set_repeat.assert_called_once_with("p1", RepeatMode.ALL)

    async def test_seek_forward(self) -> None:
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="seek_forward", value=60), self._player())
        mass.player_queues.skip.assert_awaited_once_with("p1", seconds=60)

    async def test_seek_back_negates_value(self) -> None:
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="seek_back", value=30), self._player())
        mass.player_queues.skip.assert_awaited_once_with("p1", seconds=-30)

    async def test_seek_start(self) -> None:
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="seek_start"), self._player())
        mass.player_queues.seek.assert_awaited_once_with("p1", position=0)

    async def test_now_playing_is_safe_noop(self, caplog: pytest.LogCaptureFixture) -> None:
        mass = self._make_mass()
        with caplog.at_level(
            logging.WARNING, logger="music_assistant.providers.yandex_alice.dialogs_control"
        ):
            await execute_control(mass, ParsedControl(action="now_playing"), self._player())
        mass.player_queues.skip.assert_not_awaited()
        assert any("now_playing" in r.getMessage() for r in caplog.records)

    async def test_transfer_is_safe_noop(self, caplog: pytest.LogCaptureFixture) -> None:
        mass = self._make_mass()
        with caplog.at_level(
            logging.WARNING, logger="music_assistant.providers.yandex_alice.dialogs_control"
        ):
            await execute_control(
                mass, ParsedControl(action="transfer", player_hint="спальню"), self._player()
            )
        mass.player_queues.skip.assert_not_awaited()
        assert any("transfer" in r.getMessage() for r in caplog.records)

    async def test_underlying_failure_is_swallowed(self) -> None:
        """An exception from the MA call is logged + swallowed (no re-raise)."""
        mass = self._make_mass()
        mass.player_queues.pause = AsyncMock(side_effect=RuntimeError("boom"))
        # Must not raise.
        await execute_control(mass, ParsedControl(action="pause"), self._player())
