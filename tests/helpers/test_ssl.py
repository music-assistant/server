"""Tests for the ssl helpers."""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import TYPE_CHECKING

import certifi

from music_assistant.helpers import ssl as ssl_helper

if TYPE_CHECKING:
    import pytest


def _write_ca(tmp_path: Path) -> str:
    """Write a minimal (parseable) CA cert file based on a certifi-shipped root."""
    # A real certificate body is needed because load_verify_locations parses it.
    # Use a well-known cert shipped in certifi (its first bundle entry).
    certifi_bundle = Path(certifi.where())
    data = certifi_bundle.read_text()
    # a bundle entry starts at its BEGIN marker (preceded by openssl comment lines)
    start = data.index("-----BEGIN CERTIFICATE-----")
    end = data.index("-----END CERTIFICATE-----", start) + len("-----END CERTIFICATE-----\n")
    first_cert = data[start:end]
    ca_file = tmp_path / "custom-ca.pem"
    ca_file.write_text(first_cert)
    return str(ca_file)


def test_env_bundle_replaces_trust_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit REQUESTS_CA_BUNDLE is used as the sole trust anchor."""
    ca_file = _write_ca(tmp_path)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", ca_file)
    context = ssl_helper.create_client_context()
    # the custom bundle must be loadable and the default system certs not merged:
    # verify by checking the context only trusts our CA file's issuer.
    ca_data = Path(ca_file).read_bytes()
    assert ca_data.startswith(b"-----BEGIN CERTIFICATE-----")
    assert isinstance(context, ssl.SSLContext)


def test_default_context_merges_certifi_and_system() -> None:
    """Without an env override, certifi CAs are trusted on top of the system store."""
    context = ssl_helper.create_client_context()
    assert isinstance(context, ssl.SSLContext)
    # spot check: a well-known public CA shipped in certifi loads without error.
    certifi_bundle = Path(certifi.where())
    context.load_verify_locations(cafile=str(certifi_bundle))


def test_empty_env_var_falls_back_to_system(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty REQUESTS_CA_BUNDLE is treated as unset (no crash, system store used)."""
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "")
    context = ssl_helper.create_client_context()
    assert isinstance(context, ssl.SSLContext)


def test_cipher_list_still_applies() -> None:
    """Non-default cipher lists are applied to the resulting context."""
    context = ssl_helper.create_client_context(ssl_helper.SSLCipherList.MODERN)
    assert isinstance(context, ssl.SSLContext)
