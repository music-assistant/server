# ruff: noqa: D102, RUF001
"""
Unit tests for ``provider.dialogs_grammar`` — trailing player-hint parser.

After the v1.6.0 manifest refactor this module shrinks to a single
helper: ``extract_trailing_player_hint``. ParsedControl/ParsedCommand
construction and runtime mapping moved to
``provider.skill_manifest_provider`` and are tested in
``tests/test_skill_manifest_provider.py``.
"""

from __future__ import annotations

import pytest

from music_assistant.providers.yandex_alice.dialogs_grammar import extract_trailing_player_hint


class TestExtractTrailingPlayerHint:
    """
    Recover trailing "на <player>" suffix attached to intent payloads.

    Yandex's static intents can't enumerate the per-user player list,
    so the hint comes from raw command text. The function rejects
    "на" tokens that introduce a numeric slot value or an action-content
    word like "паузу".
    """

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # Positive — trailing player hint.
            ("пауза на кухне", "кухне"),
            ("следующая на спальне", "спальне"),
            ("приглуши на гостиной", "гостиной"),
            # Multiple "на" — only the rightmost suffix is taken.
            ("поставь на паузу на кухне", "кухне"),
            ("громкость на 50 на кухне", "кухне"),
            ("перемотай вперёд на 30 секунд на кухне", "кухне"),
            # Multi-word hint stays intact.
            ("пауза на колонке у окна", "колонке у окна"),
            # Capitalisation is normalised.
            ("Пауза На Кухне", "кухне"),
        ],
    )
    def test_returns_hint_when_trailing_suffix_is_a_player(
        self,
        text: str,
        expected: str,
    ) -> None:
        assert extract_trailing_player_hint(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            # No "на" boundary at all.
            "",
            "пауза",
            "следующий трек",
            # Trailing "на" leads into a numeric slot, not a player name.
            "громкость на 50",
            "прибавь на 20",
            "убавь на 25 процентов",
            "перемотай вперёд на 30",
            "перемотай вперёд на 30 секунд",
            "перемотай назад на 1 минуту",
            # Trailing "на" leads into a unit word with no number — still
            # not a player name.
            "поставь на паузу",
        ],
    )
    def test_returns_none_when_suffix_is_a_slot_value(self, text: str) -> None:
        assert extract_trailing_player_hint(text) is None
