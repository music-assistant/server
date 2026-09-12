"""v1.2.0 #9 — validate_skill_name: pre-flight Yandex skill name rules."""

from __future__ import annotations

import pytest

from music_assistant.providers.yandex_alice.dialog_skill_meta import validate_skill_name


class TestValidateSkillName:
    """Yandex enforces ≥ 2 words + 2-64 chars; we check both up front."""

    @pytest.mark.parametrize(
        "value",
        [
            "Music Assistant",
            "Музыкальный Ассистент",
            "Домашняя Музыка",
            "My Cool Skill",
            "a b",  # 3 chars, exactly 2 words — boundary OK
            "X" * 30 + " " + "Y" * 30,  # 61 chars total, 2 words
        ],
    )
    def test_valid_names_accepted(self, value: str) -> None:
        """Two-or-more words within 2..64 chars → pass."""
        assert validate_skill_name(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "   ",
            "Single",
            "Singleword",
            "OneWordOnlyButLong",
        ],
    )
    def test_single_word_rejected(self, value: str) -> None:
        """0 or 1 word → fail (Yandex requires ≥ 2)."""
        assert validate_skill_name(value) is False

    def test_too_short(self) -> None:
        """1 char even with two 'words' → fail (under DIALOG_NAME_MIN_LEN)."""
        assert validate_skill_name("a") is False

    def test_too_long(self) -> None:
        """> 64 chars → fail."""
        assert validate_skill_name("X" * 33 + " " + "Y" * 33) is False  # 67

    def test_non_string_rejected(self) -> None:
        """None / int / list → fail (frontend may hand us anything)."""
        assert validate_skill_name(None) is False
        assert validate_skill_name(42) is False
        assert validate_skill_name(["Music", "Assistant"]) is False
