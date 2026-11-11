"""Tests for the PKCE helpers."""

from __future__ import annotations

import re

from music_assistant.helpers.pkce import generate_pkce_pair, pkce_challenge

# the code verifier and challenge of RFC 7636, appendix B
RFC_7636_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
RFC_7636_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_pkce_challenge_matches_the_rfc_example() -> None:
    """The S256 challenge of the RFC 7636 example verifier is the one the RFC lists."""
    assert pkce_challenge(RFC_7636_VERIFIER) == RFC_7636_CHALLENGE


def test_generate_pkce_pair_returns_a_matching_rfc_7636_pair() -> None:
    """A generated verifier is 128 unreserved characters and the challenge is its S256 hash."""
    code_verifier, code_challenge = generate_pkce_pair()

    assert re.fullmatch(r"[A-Za-z0-9_-]{128}", code_verifier)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", code_challenge)
    assert code_challenge == pkce_challenge(code_verifier)


def test_generate_pkce_pair_is_random() -> None:
    """Every call returns a new verifier."""
    assert generate_pkce_pair()[0] != generate_pkce_pair()[0]
