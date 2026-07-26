"""Tests for provider manifest loading, in particular the has_setup_flow flag."""

from __future__ import annotations

import json
import pathlib
from typing import TYPE_CHECKING

import pytest

from music_assistant import mass as mass_module

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


async def _load_manifests(mass_minimal: MusicAssistant) -> None:
    """Run the (name-mangled, private) manifest loader against PROVIDERS_PATH."""
    mass_minimal._provider_manifests.clear()
    await mass_minimal._MusicAssistant__load_provider_manifests()  # type: ignore[attr-defined]


def _write_provider(
    providers_dir: pathlib.Path, domain: str, *, setup_flow_source: str | None
) -> None:
    """Write a minimal provider package with an optional setup_flow.py module."""
    provider_dir = providers_dir / domain
    provider_dir.mkdir()
    (provider_dir / "__init__.py").write_text("")
    (provider_dir / "manifest.json").write_text(
        json.dumps(
            {
                "type": "music",
                "domain": domain,
                "name": domain,
                "description": "test provider",
                "codeowners": [],
            }
        )
    )
    if setup_flow_source is not None:
        (provider_dir / "setup_flow.py").write_text(setup_flow_source)


@pytest.fixture
def fake_providers_dir(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Redirect PROVIDERS_PATH to a throwaway directory for the duration of the test."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    monkeypatch.setattr(mass_module, "PROVIDERS_PATH", str(providers_dir))
    return providers_dir


async def test_has_setup_flow_true_when_module_present(
    mass_minimal: MusicAssistant, fake_providers_dir: pathlib.Path
) -> None:
    """A provider shipping a setup_flow.py module is flagged as having one."""
    _write_provider(fake_providers_dir, "with_flow", setup_flow_source="")
    await _load_manifests(mass_minimal)
    assert mass_minimal.get_provider_manifest("with_flow").has_setup_flow is True


async def test_has_setup_flow_false_when_module_absent(
    mass_minimal: MusicAssistant, fake_providers_dir: pathlib.Path
) -> None:
    """A provider without a setup_flow.py module is flagged as not having one."""
    _write_provider(fake_providers_dir, "without_flow", setup_flow_source=None)
    await _load_manifests(mass_minimal)
    assert mass_minimal.get_provider_manifest("without_flow").has_setup_flow is False


async def test_has_setup_flow_ignores_internal_import_errors(
    mass_minimal: MusicAssistant, fake_providers_dir: pathlib.Path
) -> None:
    """
    A setup_flow.py that itself fails to import still counts as present.

    The flag is derived from the file's mere existence, not from actually
    importing it, so a bug inside the module must not produce a false negative
    here; that failure only ever surfaces later, when the flow is started.
    """
    _write_provider(
        fake_providers_dir, "broken_flow", setup_flow_source="raise ImportError('boom')"
    )
    await _load_manifests(mass_minimal)
    assert mass_minimal.get_provider_manifest("broken_flow").has_setup_flow is True
