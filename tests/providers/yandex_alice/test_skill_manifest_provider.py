# ruff: noqa: D101, D102, D103, PT018, PLC0415
# mypy: disable-error-code="union-attr"
"""
Unit tests for ``provider.skill_manifest_provider``.

Covers:

* parse_intent — runtime dispatch via the manifest's ``[intents.runtime]``
  blocks, replicating the v1.5.0 behaviour 1:1.
* status / manifest / grammar / entities — bundled-default vs
  override-valid vs override-invalid file-state matrix.
* export_to_override / import_from_paste / reset_override /
  validate_override_message — UI action helpers, including base64
  paste fallback.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from music_assistant.providers.yandex_alice.dialogs_control import ParsedControl
from music_assistant.providers.yandex_alice.dialogs_nlu import ParsedCommand
from music_assistant.providers.yandex_alice.skill_manifest_provider import (
    SkillManifestProvider,
)


@pytest.fixture
def fake_mass(tmp_path: Path) -> MagicMock:
    """MA mock with ``storage_path`` pointing to a tmpdir."""
    mass = MagicMock()
    mass.storage_path = str(tmp_path)
    return mass


@pytest.fixture
def provider(fake_mass: MagicMock) -> SkillManifestProvider:
    return SkillManifestProvider(fake_mass)


def _intent(slots: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"slots": slots} if slots is not None else {}


# ---------------------------------------------------------------------------
# parse_intent — runtime dispatch (parity with v1.5.0 behaviour)
# ---------------------------------------------------------------------------


class TestParseIntentNoSlot:
    """No-slot control intents map directly to ParsedControl(action)."""

    @pytest.mark.parametrize(
        ("form_name", "expected_action"),
        [
            ("control.pause", "pause"),
            ("control.resume", "resume"),
            ("control.next", "next"),
            ("control.previous", "previous"),
            ("control.stop", "stop"),
            ("control.volume_up", "volume_up"),
            ("control.volume_down", "volume_down"),
            ("control.shuffle_on", "shuffle_on"),
            ("control.shuffle_off", "shuffle_off"),
            ("control.now_playing", "now_playing"),
            ("control.mute", "mute"),
            ("control.unmute", "unmute"),
            ("control.seek_start", "seek_start"),
            ("control.repeat_one", "repeat_one"),
            ("control.repeat_all", "repeat_all"),
            ("control.repeat_off", "repeat_off"),
            ("control.list_players", "list_players"),
            ("control.forget_player", "forget_player"),
        ],
    )
    def test_no_slot_intent(
        self,
        provider: SkillManifestProvider,
        form_name: str,
        expected_action: str,
    ) -> None:
        result = provider.parse_intent({form_name: {}})
        assert isinstance(result, ParsedControl)
        assert result.action == expected_action


class TestParseIntentVolumeSet:
    def test_int_value_passes_through(self, provider: SkillManifestProvider) -> None:
        result = provider.parse_intent({"control.volume_set": _intent({"level": {"value": 50}})})
        assert isinstance(result, ParsedControl)
        assert result.action == "volume_set" and result.value == 50

    def test_float_value_coerced_to_int(self, provider: SkillManifestProvider) -> None:
        result = provider.parse_intent({"control.volume_set": _intent({"level": {"value": 50.7}})})
        assert result is not None and result.value == 50

    def test_above_max_clamped(self, provider: SkillManifestProvider) -> None:
        result = provider.parse_intent({"control.volume_set": _intent({"level": {"value": 150}})})
        assert result is not None and result.value == 100

    def test_below_min_clamped(self, provider: SkillManifestProvider) -> None:
        result = provider.parse_intent({"control.volume_set": _intent({"level": {"value": -5}})})
        assert result is not None and result.value == 0

    def test_missing_slot_skips(self, provider: SkillManifestProvider) -> None:
        assert provider.parse_intent({"control.volume_set": _intent({})}) is None


class TestParseIntentVolumeRelative:
    def test_increase_with_delta(self, provider: SkillManifestProvider) -> None:
        result = provider.parse_intent(
            {"control.volume_increase": _intent({"delta": {"value": 20}})}
        )
        assert isinstance(result, ParsedControl)
        assert result.action == "volume_relative" and result.value == 20

    def test_decrease_negates(self, provider: SkillManifestProvider) -> None:
        result = provider.parse_intent(
            {"control.volume_decrease": _intent({"delta": {"value": 15}})}
        )
        assert result is not None and result.value == -15

    def test_increase_default_when_missing(self, provider: SkillManifestProvider) -> None:
        # default=10 for missing slot.
        result = provider.parse_intent({"control.volume_increase": _intent({})})
        assert result is not None and result.value == 10

    def test_decrease_default_when_missing(self, provider: SkillManifestProvider) -> None:
        result = provider.parse_intent({"control.volume_decrease": _intent({})})
        assert result is not None and result.value == -10

    def test_negative_delta_normalised(self, provider: SkillManifestProvider) -> None:
        # abs_clamp normalises sign.
        result = provider.parse_intent(
            {"control.volume_increase": _intent({"delta": {"value": -5}})}
        )
        assert result is not None and result.value == 5

    def test_huge_delta_clamped(self, provider: SkillManifestProvider) -> None:
        result = provider.parse_intent(
            {"control.volume_increase": _intent({"delta": {"value": 999}})}
        )
        assert result is not None and result.value == 100


class TestParseIntentSeek:
    def test_forward_seconds(self, provider: SkillManifestProvider) -> None:
        result = provider.parse_intent(
            {
                "control.seek_forward": _intent(
                    {"amount": {"value": 30}, "unit": {"value": "seconds"}}
                )
            }
        )
        assert isinstance(result, ParsedControl)
        assert result.action == "seek_forward" and result.value == 30

    def test_forward_minutes_converts(self, provider: SkillManifestProvider) -> None:
        result = provider.parse_intent(
            {
                "control.seek_forward": _intent(
                    {"amount": {"value": 2}, "unit": {"value": "minutes"}}
                )
            }
        )
        assert result is not None and result.value == 120

    def test_back_minutes_converts(self, provider: SkillManifestProvider) -> None:
        result = provider.parse_intent(
            {"control.seek_back": _intent({"amount": {"value": 1}, "unit": {"value": "minutes"}})}
        )
        assert result is not None and result.action == "seek_back" and result.value == 60

    def test_unit_missing_defaults_seconds(self, provider: SkillManifestProvider) -> None:
        result = provider.parse_intent({"control.seek_forward": _intent({"amount": {"value": 45}})})
        assert result is not None and result.value == 45

    def test_zero_amount_skipped(self, provider: SkillManifestProvider) -> None:
        # reject_if_below=1 → 0 is skipped.
        assert (
            provider.parse_intent({"control.seek_forward": _intent({"amount": {"value": 0}})})
            is None
        )

    def test_huge_amount_capped(self, provider: SkillManifestProvider) -> None:
        # cap=86400 → 100000 sec rejected, falls through.
        assert (
            provider.parse_intent({"control.seek_forward": _intent({"amount": {"value": 100_000}})})
            is None
        )

    def test_minutes_above_cap_rejected(self, provider: SkillManifestProvider) -> None:
        # 2000 minutes * 60 = 120000 sec > cap.
        assert (
            provider.parse_intent(
                {
                    "control.seek_forward": _intent(
                        {"amount": {"value": 2000}, "unit": {"value": "minutes"}}
                    )
                }
            )
            is None
        )


class TestParseIntentPlay:
    def test_my_wave(self, provider: SkillManifestProvider) -> None:
        result = provider.parse_intent({"play.my_wave": {}})
        assert isinstance(result, ParsedCommand)
        assert result.kind == "my_wave"
        assert result.radio_mode is True
        assert result.query == ""


class TestParseIntentEdgeCases:
    def test_none_returns_none(self, provider: SkillManifestProvider) -> None:
        assert provider.parse_intent(None) is None

    def test_empty_dict_returns_none(self, provider: SkillManifestProvider) -> None:
        assert provider.parse_intent({}) is None

    def test_unknown_intent_returns_none(self, provider: SkillManifestProvider) -> None:
        assert provider.parse_intent({"unknown.intent": {}}) is None

    def test_first_recognised_wins(self, provider: SkillManifestProvider) -> None:
        result = provider.parse_intent(
            {"unknown.intent": {}, "control.pause": {}, "control.next": {}}
        )
        assert result is not None and result.action in ("pause", "next")


# ---------------------------------------------------------------------------
# status / manifest / grammar / entities — file-state matrix
# ---------------------------------------------------------------------------


class TestManifestStatus:
    """``status()`` reports bundled / override_valid / override_invalid."""

    def test_no_override_file(self, provider: SkillManifestProvider) -> None:
        s = provider.status()
        assert s.source == "bundled"
        assert s.error is None
        assert s.intent_count > 0

    def test_override_valid(self, provider: SkillManifestProvider) -> None:
        provider.export_to_override()
        s = provider.status()
        assert s.source == "override_valid"
        assert s.override_path.exists()
        assert s.error is None

    def test_override_invalid(self, provider: SkillManifestProvider) -> None:
        path = provider.override_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not = valid = toml = ===", encoding="utf-8")
        s = provider.status()
        assert s.source == "override_invalid"
        assert s.error is not None
        # Bundled defaults still load as fallback.
        assert s.intent_count > 0


class TestEffectiveManifest:
    """``manifest()`` / ``grammar()`` / ``entities()`` use override when valid."""

    def test_bundled_when_no_override(self, provider: SkillManifestProvider) -> None:
        bundled = provider.manifest()
        assert len(bundled.intents) > 0
        # All slot-bearing intents have runtime mapping in the bundled default.
        slot_bearing = [i for i in bundled.intents if i.runtime and i.runtime.mapping]
        assert len(slot_bearing) >= 5  # volume_set + volume_inc/dec + seek_fwd/back

    def test_override_replaces_bundled(self, provider: SkillManifestProvider) -> None:
        path = provider.override_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            'schema_version = 1\n[entities]\ntext = ""\n'
            '[[intents]]\nform_name = "control.test"\ngrammar = "root: x"\n'
            '[intents.runtime]\nkind = "control"\naction = "pause"\n',
            encoding="utf-8",
        )
        m = provider.manifest()
        assert len(m.intents) == 1
        assert m.intents[0].form_name == "control.test"

    def test_invalid_override_falls_back(self, provider: SkillManifestProvider) -> None:
        provider.override_path.parent.mkdir(parents=True, exist_ok=True)
        provider.override_path.write_text("garbage = = =", encoding="utf-8")
        bundled = provider.manifest()
        assert len(bundled.intents) > 0
        # Sanity — bundled has the original intents.
        assert any(i.form_name == "control.pause" for i in bundled.intents)


# ---------------------------------------------------------------------------
# UI actions — export / import / reset / validate
# ---------------------------------------------------------------------------


class TestExportToOverride:
    def test_first_export_creates_file(self, provider: SkillManifestProvider) -> None:
        msg = provider.export_to_override()
        assert "Манифест экспортирован" in msg
        assert provider.override_path.exists()
        text = provider.override_path.read_text(encoding="utf-8")
        assert "schema_version" in text
        assert "control.pause" in text

    def test_second_export_does_not_overwrite(self, provider: SkillManifestProvider) -> None:
        provider.export_to_override()
        # Tamper with the override.
        provider.override_path.write_text("schema_version = 1\n", encoding="utf-8")
        msg = provider.export_to_override()
        assert "уже существует" in msg
        # File still has the user's edit.
        assert provider.override_path.read_text(encoding="utf-8") == "schema_version = 1\n"


class TestImportFromPaste:
    def _valid_toml(self) -> str:
        return (
            'schema_version = 1\n[entities]\ntext = ""\n'
            '[[intents]]\nform_name = "control.x"\ngrammar = "root: x"\n'
            '[intents.runtime]\nkind = "control"\naction = "pause"\n'
        )

    def test_empty_paste_no_op(self, provider: SkillManifestProvider) -> None:
        msg = provider.import_from_paste("")
        assert "пусто" in msg
        assert not provider.last_import_success
        assert not provider.override_path.exists()

    def test_whitespace_paste_no_op(self, provider: SkillManifestProvider) -> None:
        msg = provider.import_from_paste("   \n\n  ")
        assert "пусто" in msg
        assert not provider.last_import_success

    def test_valid_toml_writes_override(self, provider: SkillManifestProvider) -> None:
        text = self._valid_toml()
        msg = provider.import_from_paste(text)
        assert "импортирован" in msg.lower()
        assert provider.last_import_success
        assert provider.override_path.read_text(encoding="utf-8") == text

    def test_invalid_toml_does_not_write(self, provider: SkillManifestProvider) -> None:
        msg = provider.import_from_paste("not = = valid")
        assert "невалиден" in msg.lower() or "не удался" in msg
        assert not provider.last_import_success
        assert not provider.override_path.exists()

    def test_valid_toml_invalid_schema(self, provider: SkillManifestProvider) -> None:
        # TOML parses but schema_version missing.
        msg = provider.import_from_paste('[entities]\ntext = ""\n')
        assert not provider.last_import_success
        assert not provider.override_path.exists()
        assert "schema_version" in msg or "невалиден" in msg.lower()

    def test_base64_paste_decoded(self, provider: SkillManifestProvider) -> None:
        text = self._valid_toml()
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        msg = provider.import_from_paste(f"data:base64,{encoded}")
        assert provider.last_import_success
        assert "импортирован" in msg.lower()
        assert provider.override_path.read_text(encoding="utf-8") == text

    def test_base64_invalid_paste_rejected(self, provider: SkillManifestProvider) -> None:
        msg = provider.import_from_paste("data:base64,!!!!not-base64!!!!")
        assert not provider.last_import_success
        assert "base64" in msg.lower() or "не удался" in msg

    def test_base64_paste_with_wrapped_lines(self, provider: SkillManifestProvider) -> None:
        # `base64 -i skill.toml` wraps at 76 cols by default — the decoder
        # must tolerate the embedded newlines & arbitrary whitespace.
        text = self._valid_toml()
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        wrapped = "\n".join(encoded[i : i + 60] for i in range(0, len(encoded), 60))
        wrapped_with_spaces = f"  {wrapped}\n  "
        msg = provider.import_from_paste(f"data:base64,{wrapped_with_spaces}")
        assert provider.last_import_success, msg
        assert provider.override_path.read_text(encoding="utf-8") == text


class TestResetOverride:
    def test_idempotent_when_absent(self, provider: SkillManifestProvider) -> None:
        msg = provider.reset_override()
        assert "отсутствует" in msg
        assert not provider.override_path.exists()

    def test_removes_override_file(self, provider: SkillManifestProvider) -> None:
        provider.export_to_override()
        assert provider.override_path.exists()
        msg = provider.reset_override()
        assert "удалён" in msg
        assert not provider.override_path.exists()


class TestValidateOverrideMessage:
    def test_no_override(self, provider: SkillManifestProvider) -> None:
        msg = provider.validate_override_message()
        assert "отсутствует" in msg

    def test_valid_override(self, provider: SkillManifestProvider) -> None:
        provider.export_to_override()
        msg = provider.validate_override_message()
        assert "✓" in msg
        assert "валиден" in msg

    def test_invalid_override(self, provider: SkillManifestProvider) -> None:
        path = provider.override_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('schema_version = 999\n[entities]\ntext = ""\n', encoding="utf-8")
        msg = provider.validate_override_message()
        assert "✗" in msg
        assert "невалиден" in msg.lower()


# ---------------------------------------------------------------------------
# Storage path resolution
# ---------------------------------------------------------------------------


class TestStorageRoot:
    def test_uses_mass_storage_path_when_set(self, tmp_path: Path) -> None:
        mass = MagicMock()
        mass.storage_path = str(tmp_path)
        provider = SkillManifestProvider(mass)
        assert provider.override_path == tmp_path / "yandex_alice" / "skill.toml"

    def test_falls_back_to_home_dir_when_missing(self) -> None:
        mass = MagicMock(spec=[])  # no storage_path attribute
        provider = SkillManifestProvider(mass)
        # Just check shape — we don't actually want to write into ~/.musicassistant.
        assert provider.override_path.parts[-2:] == ("yandex_alice", "skill.toml")


# ---------------------------------------------------------------------------
# Effective-manifest cache
# ---------------------------------------------------------------------------


class TestResolvedCache:
    """
    ``manifest()`` / ``status()`` reuse the parsed manifest until stat changes.

    The cache is keyed by ``(exists, mtime_ns)``: identical stat → same
    object; mtime change → re-parse; mutating actions invalidate even
    when mtime stays put (some filesystems coalesce sub-ms writes).
    """

    def test_bundled_path_returns_same_object(self, provider: SkillManifestProvider) -> None:
        first = provider.manifest()
        second = provider.manifest()
        assert first is second

    def test_override_path_returns_same_object_until_mtime_changes(
        self, provider: SkillManifestProvider
    ) -> None:
        provider.export_to_override()
        first = provider.manifest()
        second = provider.manifest()
        assert first is second
        # Touch the file with a fresh mtime — cache must invalidate.
        import os as _os

        st = provider.override_path.stat()
        _os.utime(provider.override_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        third = provider.manifest()
        assert third is not first

    def test_export_invalidates_cache(self, provider: SkillManifestProvider) -> None:
        # Prime cache on bundled.
        before = provider.manifest()
        assert provider.status().source == "bundled"
        provider.export_to_override()
        # Now status must observe the freshly-written override.
        assert provider.status().source == "override_valid"
        # Effective manifest still parses to a SkillManifest equivalent
        # to the bundled one (Export copies bundled bytes verbatim) but
        # is a fresh object — i.e. cache was actually invalidated.
        after = provider.manifest()
        assert after is not before

    def test_reset_invalidates_cache(self, provider: SkillManifestProvider) -> None:
        provider.export_to_override()
        assert provider.status().source == "override_valid"
        provider.reset_override()
        assert provider.status().source == "bundled"

    def test_import_invalidates_cache(self, provider: SkillManifestProvider) -> None:
        toml = (
            'schema_version = 1\n[entities]\ntext = ""\n'
            '[[intents]]\nform_name = "control.test"\ngrammar = "root: x"\n'
            '[intents.runtime]\nkind = "control"\naction = "pause"\n'
        )
        # Prime cache on bundled.
        provider.manifest()
        provider.import_from_paste(toml)
        m = provider.manifest()
        assert len(m.intents) == 1
        assert m.intents[0].form_name == "control.test"


class TestBundledResourceLookup:
    """
    Bundled manifest resolution must work after the upstream package rename.

    Locally this provider is ``provider``; upstream-synced into
    ``music-assistant/server`` it lives under
    ``music_assistant.providers.yandex_alice``. The bundled-resource
    lookup must not hardcode a package name — regressing this breaks
    MA startup as soon as the wheel ships, which is much harder to
    catch than a unit-test failure.
    """

    def test_bundled_lookup_uses_dunder_package(self, provider: SkillManifestProvider) -> None:
        # Smoke: the lookup resolves at all.
        text = provider._bundled_manifest_text()
        assert "schema_version" in text

    def test_no_hardcoded_package_string_literal(self) -> None:
        # Static guard: source must not contain the legacy literal.
        from music_assistant.providers.yandex_alice import skill_manifest_provider as smp

        source = Path(smp.__file__).read_text(encoding="utf-8")
        assert '"music_assistant.providers.yandex_alice.data"' not in source
        assert "'music_assistant.providers.yandex_alice.data'" not in source


class TestAtomicWrite:
    """Export / Import use tmp+rename so readers never see a half-written file."""

    def test_export_does_not_leave_tmp_file(self, provider: SkillManifestProvider) -> None:
        provider.export_to_override()
        siblings = list(provider.override_path.parent.iterdir())
        assert len(siblings) == 1
        assert siblings[0] == provider.override_path

    def test_import_does_not_leave_tmp_file(self, provider: SkillManifestProvider) -> None:
        toml = (
            'schema_version = 1\n[entities]\ntext = ""\n'
            '[[intents]]\nform_name = "control.test"\ngrammar = "root: x"\n'
            '[intents.runtime]\nkind = "control"\naction = "pause"\n'
        )
        provider.import_from_paste(toml)
        siblings = list(provider.override_path.parent.iterdir())
        assert len(siblings) == 1
        assert siblings[0] == provider.override_path
