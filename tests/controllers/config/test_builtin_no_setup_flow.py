"""
Invariant: no builtin or default-auto-created provider ships a ``setup_flow.py``.

Builtin providers and the providers in ``DEFAULT_PROVIDERS`` are created automatically at
startup with empty values (``create_builtin_provider_config`` / the default-providers pass)
and loaded without ever running an interactive setup flow. A ``setup_flow.py`` on such a
provider would therefore never run, so it must not exist. Enforced here so that adding a
provider to ``DEFAULT_PROVIDERS`` (or marking it ``builtin``) can't silently ship a setup
flow that the auto-create path bypasses.
"""

from __future__ import annotations

import json
from pathlib import Path

from music_assistant.constants import DEFAULT_PROVIDERS
from music_assistant.mass import PROVIDERS_PATH


def _auto_created_provider_domains() -> set[str]:
    """Return the domains of all providers that are auto-created without a setup flow."""
    domains = {domain for domain, _ in DEFAULT_PROVIDERS}
    for manifest_path in Path(PROVIDERS_PATH).glob("*/manifest.json"):
        data = json.loads(manifest_path.read_text())
        if data.get("builtin"):
            # the directory name is the provider domain
            domains.add(manifest_path.parent.name)
    return domains


def test_builtin_and_default_providers_have_no_setup_flow() -> None:
    """Builtin/default (auto-created) providers must not ship a setup_flow.py."""
    offenders = [
        domain
        for domain in sorted(_auto_created_provider_domains())
        if (Path(PROVIDERS_PATH) / domain / "setup_flow.py").exists()
    ]
    assert not offenders, (
        "builtin/default providers are auto-created without an interactive setup, so they "
        f"must not ship a setup_flow.py (it would never run): {offenders}"
    )
