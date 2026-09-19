"""Tests for provider/dialog_skill_meta.py — pure helper functions."""

from __future__ import annotations

import pytest

from music_assistant.providers.yandex_alice.dialog_skill_meta import (
    build_activation_phrases,
    build_backend_uri,
    build_skill_description,
    build_structured_examples,
)


class TestBuildBackendUri:
    """build_backend_uri composes the public webhook URL Yandex must call."""

    def test_happy_path(self) -> None:
        """Https + secret → standard /api/yandex_dialogs/webhook/<secret> URL."""
        url = build_backend_uri("https://ma.example.com", "abc123")
        assert url == "https://ma.example.com/api/yandex_dialogs/webhook/abc123"

    def test_strips_trailing_slash(self) -> None:
        """Trailing slash on base_url is removed before assembly."""
        url = build_backend_uri("https://ma.example.com/", "abc123")
        assert url == "https://ma.example.com/api/yandex_dialogs/webhook/abc123"

    def test_rejects_http(self) -> None:
        """Plain http:// rejected — Yandex requires HTTPS for skill webhooks."""
        with pytest.raises(ValueError, match="HTTPS"):
            build_backend_uri("http://ma.example.com", "secret")

    def test_rejects_empty_base_url(self) -> None:
        """Empty base URL → ValueError before any assembly."""
        with pytest.raises(ValueError, match="empty"):
            build_backend_uri("", "secret")

    def test_rejects_whitespace_base_url(self) -> None:
        """Whitespace-only base URL is treated as empty."""
        with pytest.raises(ValueError, match="empty"):
            build_backend_uri("   ", "secret")

    def test_rejects_empty_secret(self) -> None:
        """Empty webhook secret → ValueError; no half-baked URL."""
        with pytest.raises(ValueError, match="secret"):
            build_backend_uri("https://ma.example.com", "")


class TestBuildSkillDescription:
    """build_skill_description always returns a non-empty Russian string."""

    def test_returns_non_empty(self) -> None:
        """Default description is non-empty (Yandex rejects empty descriptions)."""
        desc = build_skill_description("Music Assistant")
        assert desc.strip()
        assert len(desc) > 30

    def test_embeds_skill_name(self) -> None:
        """Skill name appears in the description so catalog listing is self-contained."""
        desc = build_skill_description("Моя Колонка")
        assert "Моя Колонка" in desc

    def test_falls_back_on_empty_name(self) -> None:
        """Empty / whitespace skill name falls back to a generic default."""
        desc_empty = build_skill_description("")
        desc_ws = build_skill_description("   ")
        assert "Music Assistant" in desc_empty
        assert "Music Assistant" in desc_ws


class TestBuildActivationPhrases:
    """build_activation_phrases returns a single-element list with the skill name."""

    def test_returns_skill_name(self) -> None:
        """Default activation list = [skill_name]."""
        assert build_activation_phrases("My Skill") == ["My Skill"]

    def test_strips_whitespace(self) -> None:
        """Surrounding whitespace is stripped from the activation phrase."""
        assert build_activation_phrases("  Test  ") == ["Test"]

    def test_falls_back_on_empty(self) -> None:
        """Empty input falls back to default 'Music Assistant'."""
        assert build_activation_phrases("") == ["Music Assistant"]


class TestBuildStructuredExamples:
    """build_structured_examples returns the dict shape Yandex moderators expect."""

    def test_returns_three_examples(self) -> None:
        """Default set has playback / transport / multi-room intents."""
        examples = build_structured_examples("Music Assistant")
        assert len(examples) == 3

    def test_each_example_has_required_fields(self) -> None:
        """Each entry has marker / activationPhrase / request / is_valid (Yandex-required)."""
        examples = build_structured_examples("Music Assistant")
        for ex in examples:
            assert ex["marker"] == "попроси"
            assert ex["activationPhrase"] == "Music Assistant"
            assert isinstance(ex["request"], str)
            assert ex["request"]
            assert ex["is_valid"] is True

    def test_uses_provided_skill_name(self) -> None:
        """ActivationPhrase mirrors the supplied skill_name."""
        examples = build_structured_examples("Моя Колонка")
        for ex in examples:
            assert ex["activationPhrase"] == "Моя Колонка"
