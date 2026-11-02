"""Test the stream URL solver of the native YouTube Music provider, without V8."""

import json
import logging
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from music_assistant.providers.ytmusic_native.cipher import CipherError, PlayerCipher

BASE_URL = "https://rr1.googlevideo.com/videoplayback?expire=1&itag=141&n=rawN"


class FakeIsolate:
    """Stands in for the V8 isolate: answers each portal function with canned JSON."""

    def __init__(self, answers: dict[str, dict[str, Any]]) -> None:
        """
        Initialize the fake.

        :param answers: The result of each portal function, by name.
        """
        self.answers = answers
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def call(self, func: str, *args: Any, timeout_sec: float | None = None) -> str:
        """Record the call and return the canned answer as JSON, like the portal does."""
        self.calls.append((func, args))
        return json.dumps(self.answers[func])

    def close(self) -> None:
        """Release nothing."""


def _cipher(answers: dict[str, dict[str, Any]], decorator: bool = True) -> PlayerCipher:
    """Return a solver loaded with a fake player build."""
    solver = PlayerCipher(logging.getLogger(__name__))
    solver._ctx = FakeIsolate(answers)
    solver._player_id = "build1"
    solver._has_decorator = decorator
    return solver


def _ciphered() -> dict[str, Any]:
    """Return a format whose URL needs both the signature and n solved."""
    return {"signatureCipher": urlencode({"s": "SCRAMBLED", "sp": "sig", "url": BASE_URL})}


def _params(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


async def test_a_pre_signed_url_without_n_is_used_as_is() -> None:
    """Nothing to solve, so the isolate is not asked."""
    solver = _cipher({})
    url = "https://rr1.googlevideo.com/videoplayback?itag=141&sig=done"

    assert await solver.resolve({"url": url}, AsyncMock()) == url
    assert solver._ctx.calls == []


async def test_a_pre_signed_url_gets_only_its_n_solved() -> None:
    """The decorator signs a dummy signature, which is ignored; only n is taken."""
    solver = _cipher({"__decorate": {"sig": "ignored", "n": "solvedN"}})

    url = await solver.resolve({"url": BASE_URL}, AsyncMock())

    assert _params(url)["n"] == "solvedN"
    func, (signature, n_in) = solver._ctx.calls[0]
    assert (func, n_in) == ("__decorate", "rawN")
    assert len(signature) == 100


async def test_a_pre_signed_url_fails_when_n_can_not_be_solved() -> None:
    """A URL with the raw n would be refused by googlevideo, so it is never returned."""
    solver = _cipher({"__decorate": {"error": "decorator could not be driven"}})

    with pytest.raises(CipherError):
        await solver.resolve({"url": BASE_URL}, AsyncMock())


async def test_the_decorator_url_is_probed_once_per_build() -> None:
    """The first decorator URL of a build is checked against googlevideo, later ones trusted."""
    solver = _cipher({"__decorate": {"sig": "SIGNED", "n": "solvedN"}})
    stream_ok = AsyncMock(return_value=True)

    first = await solver.resolve(_ciphered(), stream_ok)
    second = await solver.resolve(_ciphered(), stream_ok)

    assert first == second
    assert (_params(first)["sig"], _params(first)["n"]) == ("SIGNED", "solvedN")
    stream_ok.assert_awaited_once()


async def test_the_static_descramble_takes_over_when_the_decorator_url_fails() -> None:
    """A decorator URL googlevideo refuses is replaced by the static descramble's."""
    solver = _cipher(
        {
            "__decorate": {"sig": "SIGNED", "n": "solvedN"},
            "__descramble": {"sig": "STATIC", "n": "staticN"},
        }
    )

    url = await solver.resolve(_ciphered(), AsyncMock(return_value=False))

    assert (_params(url)["sig"], _params(url)["n"]) == ("STATIC", "staticN")
    assert not solver._decorator_verified


async def test_an_unverified_decorator_url_is_the_last_resort() -> None:
    """When the static descramble fails too, a failed probe (maybe a blip) does not fail playback."""
    solver = _cipher(
        {
            "__decorate": {"sig": "SIGNED", "n": "solvedN"},
            "__descramble": {"error": "n-challenge: no strategy matched"},
        }
    )

    url = await solver.resolve(_ciphered(), AsyncMock(return_value=False))

    assert _params(url)["sig"] == "SIGNED"


async def test_no_working_strategy_raises() -> None:
    """Without a decorator and with the static descramble failing, nothing is returned."""
    solver = _cipher({"__descramble": {"error": "signature: no strategy matched"}}, decorator=False)

    with pytest.raises(CipherError, match="no strategy matched"):
        await solver.resolve(_ciphered(), AsyncMock())


async def test_a_failed_build_is_skipped_until_another_build_or_the_cooldown() -> None:
    """The failed build raises right away; a new build or the passing cooldown loads again."""
    solver = PlayerCipher(logging.getLogger(__name__))

    with (
        patch.object(solver, "_seed", side_effect=CipherError("swap helper not found")),
        pytest.raises(CipherError),
    ):
        await solver.load("build2", "base.js source")

    with pytest.raises(CipherError, match="not retrying"):
        solver.check_player("build2")
    solver.check_player("build3")
    solver._failed_at -= 601
    solver.check_player("build2")
