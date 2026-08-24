"""
Tests for the Spotify setup flow's Soloist playback branch.

The soloist branch collects download consent, the user's personal API key and a
paired session, and must recover gracefully from refusals, unsupported
platforms, partial key pastes and pairing timeouts — every failure loops back
to a form instead of aborting the already-authorized flow. The pairing helper
itself is covered as well.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import LoginFailed

from music_assistant.models.setup_flow import StepExpiredError
from music_assistant.providers.spotify import helpers as spotify_helpers
from music_assistant.providers.spotify import setup_flow
from music_assistant.providers.spotify.constants import (
    BACKEND_LIBRESPOT,
    BACKEND_SOLOIST,
    CONF_LIBRESPOT_CREDENTIALS,
    CONF_PLAYBACK_BACKEND,
    CONF_SOLOIST_API_KEY,
    CONF_SOLOIST_CONSENT,
    CONF_SOLOIST_SESSION_DIR,
    SOLOIST_DATA_DIR_NAME,
)
from music_assistant.providers.spotify.helpers import (
    pair_soloist_session,
    soloist_session_account,
)
from music_assistant.providers.spotify_connect.soloist import UnsupportedPlatformError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable


async def test_soloist_branch_collects_consent_key_and_pairing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The soloist branch stores consent, API key and the flow-private session dir."""
    monkeypatch.setattr(setup_flow, "verify_platform_supported", MagicMock())
    paired_dirs = _record_pairing(monkeypatch)
    session = _make_session(
        tmp_path,
        [
            {CONF_PLAYBACK_BACKEND: BACKEND_SOLOIST},
            {CONF_SOLOIST_CONSENT: True},
            {CONF_SOLOIST_API_KEY: "k" * 20},
        ],
    )
    setup_data: dict[str, Any] = {}
    await setup_flow._setup_playback(session, setup_data, "spotify-user")
    assert setup_data[CONF_PLAYBACK_BACKEND] == BACKEND_SOLOIST
    assert setup_data[CONF_SOLOIST_CONSENT] is True
    assert setup_data[CONF_SOLOIST_API_KEY] == "k" * 20
    assert setup_data[CONF_SOLOIST_SESSION_DIR] == "spotify/pairing/flow1"
    # a leftover librespot credential is of no further use
    assert setup_data[CONF_LIBRESPOT_CREDENTIALS] is None
    assert paired_dirs == [tmp_path / "spotify" / "pairing" / "flow1"]
    assert _step_ids(session) == ["playback_backend", "soloist_terms", "soloist_api_key"]


async def test_consent_refusal_returns_to_the_backend_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refusing the soloist terms re-offers the backend choice with a clear error."""
    monkeypatch.setattr(setup_flow, "verify_platform_supported", MagicMock())
    monkeypatch.setattr(setup_flow, "_authorize_playback", AsyncMock(return_value="creds"))
    session = _make_session(
        tmp_path,
        [
            {CONF_PLAYBACK_BACKEND: BACKEND_SOLOIST},
            {CONF_SOLOIST_CONSENT: False},
            {CONF_PLAYBACK_BACKEND: BACKEND_LIBRESPOT},
        ],
    )
    setup_data: dict[str, Any] = {}
    await setup_flow._setup_playback(session, setup_data, "spotify-user")
    retry_call = session.form.await_args_list[2]
    assert retry_call.kwargs["step_id"] == "playback_backend"
    assert retry_call.kwargs["errors"] == {"base": "soloist_consent_required"}
    assert setup_data[CONF_PLAYBACK_BACKEND] == BACKEND_LIBRESPOT
    assert setup_data[CONF_LIBRESPOT_CREDENTIALS] == "creds"
    # switching away from soloist wipes its secrets
    assert setup_data[CONF_SOLOIST_API_KEY] is None
    assert setup_data[CONF_SOLOIST_CONSENT] is False
    assert setup_data[CONF_SOLOIST_SESSION_DIR] is None


async def test_unsupported_platform_falls_back_to_librespot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Picking soloist on an unsupported platform re-shows the choice with an error."""
    monkeypatch.setattr(
        setup_flow,
        "verify_platform_supported",
        MagicMock(side_effect=UnsupportedPlatformError("x")),
    )
    monkeypatch.setattr(setup_flow, "_authorize_playback", AsyncMock(return_value="creds"))
    session = _make_session(
        tmp_path,
        [
            {CONF_PLAYBACK_BACKEND: BACKEND_SOLOIST},
            {CONF_PLAYBACK_BACKEND: BACKEND_LIBRESPOT},
        ],
    )
    setup_data: dict[str, Any] = {}
    await setup_flow._setup_playback(session, setup_data, "spotify-user")
    retry_call = session.form.await_args_list[1]
    assert retry_call.kwargs["step_id"] == "playback_backend"
    assert retry_call.kwargs["errors"] == {"base": "soloist_unsupported_platform"}
    assert setup_data[CONF_PLAYBACK_BACKEND] == BACKEND_LIBRESPOT


async def test_short_api_key_is_rejected_and_reasked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial API key paste re-shows the key form until a plausible key is entered."""
    monkeypatch.setattr(setup_flow, "verify_platform_supported", MagicMock())
    _record_pairing(monkeypatch)
    session = _make_session(
        tmp_path,
        [
            {CONF_PLAYBACK_BACKEND: BACKEND_SOLOIST},
            {CONF_SOLOIST_CONSENT: True},
            {CONF_SOLOIST_API_KEY: "short"},
            {CONF_SOLOIST_API_KEY: "k" * 20},
        ],
    )
    setup_data: dict[str, Any] = {}
    await setup_flow._setup_playback(session, setup_data, "spotify-user")
    api_key_calls = [
        call for call in session.form.await_args_list if call.kwargs["step_id"] == "soloist_api_key"
    ]
    assert len(api_key_calls) == 2
    assert api_key_calls[0].kwargs["errors"] is None
    assert api_key_calls[1].kwargs["errors"] == {CONF_SOLOIST_API_KEY: "soloist_api_key_invalid"}
    assert setup_data[CONF_SOLOIST_API_KEY] == "k" * 20


async def test_pairing_timeout_reshows_the_key_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pairing step that expires re-shows the key form; a retry can then complete."""
    monkeypatch.setattr(setup_flow, "verify_platform_supported", MagicMock())
    paired_dirs = _record_pairing(monkeypatch)
    session = _make_session(
        tmp_path,
        [
            {CONF_PLAYBACK_BACKEND: BACKEND_SOLOIST},
            {CONF_SOLOIST_CONSENT: True},
            {CONF_SOLOIST_API_KEY: "k" * 20},
            # the stored key is kept on the retry by leaving the field empty
            {},
        ],
    )
    attempts = {"count": 0}

    async def _expire_first(awaitable: Any, **_kwargs: Any) -> Any:
        attempts["count"] += 1
        if attempts["count"] == 1:
            # never awaited: drop the pairing coroutine without a warning
            awaitable.close()
            raise StepExpiredError
        return await awaitable

    session.progress_until = AsyncMock(side_effect=_expire_first)
    setup_data: dict[str, Any] = {}
    await setup_flow._setup_playback(session, setup_data, "spotify-user")
    api_key_calls = [
        call for call in session.form.await_args_list if call.kwargs["step_id"] == "soloist_api_key"
    ]
    assert len(api_key_calls) == 2
    assert api_key_calls[1].kwargs["errors"] == {"base": "soloist_pairing_not_completed"}
    assert setup_data[CONF_SOLOIST_SESSION_DIR] == "spotify/pairing/flow1"
    # only the successful retry ran the pairing
    assert len(paired_dirs) == 1


async def test_reconfigure_keeps_the_existing_paired_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Declining a re-pair keeps the existing session and skips the pairing step."""
    monkeypatch.setattr(setup_flow, "verify_platform_supported", MagicMock())
    paired_dirs = _record_pairing(monkeypatch)
    canonical = tmp_path / "spotify" / "spotify--test" / SOLOIST_DATA_DIR_NAME
    canonical.mkdir(parents=True)
    (canonical / "session.bin").write_bytes(b"session")
    session = _make_session(
        tmp_path,
        [
            {CONF_PLAYBACK_BACKEND: BACKEND_SOLOIST},
            {CONF_SOLOIST_CONSENT: True},
            {setup_flow.CONF_SOLOIST_REPAIR: False},
            # keep the stored API key
            {},
        ],
    )
    session.context.instance_id = "spotify--test"
    setup_data: dict[str, Any] = {
        CONF_PLAYBACK_BACKEND: BACKEND_SOLOIST,
        CONF_SOLOIST_CONSENT: True,
        CONF_SOLOIST_API_KEY: "k" * 20,
    }
    await setup_flow._setup_playback(session, setup_data, "spotify-user")
    assert _step_ids(session) == [
        "playback_backend",
        "soloist_terms",
        "soloist_repair",
        "soloist_api_key",
    ]
    assert paired_dirs == []
    # the kept session stays where it is; no freshly paired dir is left to adopt
    assert setup_data[CONF_SOLOIST_SESSION_DIR] is None
    assert setup_data[CONF_SOLOIST_API_KEY] == "k" * 20


def test_the_paired_account_is_read_from_the_session(tmp_path: Path) -> None:
    """The engine records the paired account as its per-user state directory."""
    data_dir = tmp_path / "soloist-data"
    assert soloist_session_account(data_dir) is None
    users = data_dir / "settings" / "Users"
    users.mkdir(parents=True)
    (users / "marcelveldt3-user").mkdir()
    assert soloist_session_account(data_dir) == "marcelveldt3"
    # state for more than one account cannot say which one paired
    (users / "someoneelse-user").mkdir()
    assert soloist_session_account(data_dir) is None


def test_unrelated_directories_are_not_read_as_an_account(tmp_path: Path) -> None:
    """Only the engine's per-user state dirs count."""
    users = tmp_path / "soloist-data" / "settings" / "Users"
    users.mkdir(parents=True)
    (users / "scratch").mkdir()
    assert soloist_session_account(tmp_path / "soloist-data") is None


async def test_pairing_from_another_account_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pairing from a Spotify app signed in as someone else is refused and re-asked."""
    monkeypatch.setattr(setup_flow, "verify_platform_supported", MagicMock())
    paired_dirs = _record_pairing(monkeypatch, account="someoneelse")
    session = _make_session(
        tmp_path,
        [
            {CONF_PLAYBACK_BACKEND: BACKEND_SOLOIST},
            {CONF_SOLOIST_CONSENT: True},
            {CONF_SOLOIST_API_KEY: "k" * 20},
            # the second attempt pairs with the right account
            {CONF_SOLOIST_API_KEY: "k" * 20},
        ],
    )
    setup_data: dict[str, Any] = {}

    async def _second_attempt_is_correct(_mass: Any, _key: str, data_dir: Path) -> None:
        account = "someoneelse" if len(paired_dirs) == 0 else "spotify-user"
        paired_dirs.append(data_dir)
        users = data_dir / "settings" / "Users" / f"{account}-user"
        users.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(setup_flow, "pair_soloist_session", _second_attempt_is_correct)
    await setup_flow._setup_playback(session, setup_data, "spotify-user")
    assert setup_data[CONF_PLAYBACK_BACKEND] == BACKEND_SOLOIST
    # the key step was shown again, with the mismatch reported
    assert _step_ids(session).count("soloist_api_key") == 2
    errors = session.form.await_args_list[-1].kwargs["errors"]
    assert errors == {"base": "soloist_account_mismatch"}


async def test_a_matching_pairing_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pairing from the same account completes on the first attempt."""
    monkeypatch.setattr(setup_flow, "verify_platform_supported", MagicMock())
    _record_pairing(monkeypatch, account="spotify-user")
    session = _make_session(
        tmp_path,
        [
            {CONF_PLAYBACK_BACKEND: BACKEND_SOLOIST},
            {CONF_SOLOIST_CONSENT: True},
            {CONF_SOLOIST_API_KEY: "k" * 20},
        ],
    )
    setup_data: dict[str, Any] = {}
    await setup_flow._setup_playback(session, setup_data, "spotify-user")
    assert _step_ids(session).count("soloist_api_key") == 1
    assert setup_data[CONF_SOLOIST_SESSION_DIR] == "spotify/pairing/flow1"


async def test_an_unknown_account_never_blocks_pairing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sign-in whose account could not be read must not turn a good pairing away."""
    monkeypatch.setattr(setup_flow, "verify_platform_supported", MagicMock())
    _record_pairing(monkeypatch, account="someoneelse")
    session = _make_session(
        tmp_path,
        [
            {CONF_PLAYBACK_BACKEND: BACKEND_SOLOIST},
            {CONF_SOLOIST_CONSENT: True},
            {CONF_SOLOIST_API_KEY: "k" * 20},
        ],
    )
    await setup_flow._setup_playback(session, {}, None)
    assert _step_ids(session).count("soloist_api_key") == 1


async def test_a_kept_session_on_another_account_forces_a_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keeping a pairing that belongs to another account is not offered: it is redone."""
    monkeypatch.setattr(setup_flow, "verify_platform_supported", MagicMock())
    _record_pairing(monkeypatch, account="spotify-user")
    stored = tmp_path / "spotify" / "spotify--test" / SOLOIST_DATA_DIR_NAME / "settings" / "Users"
    (stored / "someoneelse-user").mkdir(parents=True)
    session = _make_session(
        tmp_path,
        [
            {CONF_PLAYBACK_BACKEND: BACKEND_SOLOIST},
            {CONF_SOLOIST_CONSENT: True},
            {setup_flow.CONF_SOLOIST_REPAIR: False},
            {CONF_SOLOIST_API_KEY: "k" * 20},
            {CONF_SOLOIST_API_KEY: "k" * 20},
        ],
    )
    session.context.instance_id = "spotify--test"
    setup_data: dict[str, Any] = {CONF_SOLOIST_API_KEY: "k" * 20}
    await setup_flow._setup_playback(session, setup_data, "spotify-user")
    # the kept pairing was refused, so a fresh one was made instead
    assert setup_data[CONF_SOLOIST_SESSION_DIR] == "spotify/pairing/flow1"


async def test_a_fresh_setup_preselects_librespot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh install lands on librespot: the short path, no consent or pairing."""
    monkeypatch.setattr(setup_flow, "verify_platform_supported", MagicMock())
    session = _make_session(tmp_path, [{CONF_PLAYBACK_BACKEND: BACKEND_LIBRESPOT}])
    monkeypatch.setattr(setup_flow, "_authorize_playback", AsyncMock(return_value="creds"))
    await setup_flow._setup_playback(session, {}, "spotify-user")
    entries = session.form.await_args_list[0].args[0]
    assert entries[0].value == BACKEND_LIBRESPOT


async def test_backend_choice_uses_expanded_options_for_card_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The backend choice shows options as expanded cards with descriptions.

    The single ConfigEntry uses expanded_options=True for visual grouping,
    preselects the stored choice or librespot by default.
    """
    monkeypatch.setattr(setup_flow, "verify_platform_supported", MagicMock())
    session = _make_session(tmp_path, [{CONF_PLAYBACK_BACKEND: BACKEND_LIBRESPOT}])
    monkeypatch.setattr(setup_flow, "_authorize_playback", AsyncMock(return_value="creds"))
    await setup_flow._setup_playback(session, {}, "spotify-user")
    entries = session.form.await_args_list[0].args[0]
    assert len(entries) == 1
    entry = entries[0]
    assert entry.key == CONF_PLAYBACK_BACKEND
    assert entry.expanded_options is True
    assert [opt.value for opt in entry.options] == [BACKEND_SOLOIST, BACKEND_LIBRESPOT]
    assert entry.default_value == BACKEND_LIBRESPOT


async def test_a_stored_choice_is_preselected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reconfigure preselects whatever this instance already uses."""
    monkeypatch.setattr(setup_flow, "verify_platform_supported", MagicMock())
    _record_pairing(monkeypatch)
    session = _make_session(
        tmp_path,
        [
            {CONF_PLAYBACK_BACKEND: BACKEND_SOLOIST},
            {CONF_SOLOIST_CONSENT: True},
            {CONF_SOLOIST_API_KEY: "k" * 20},
        ],
    )
    await setup_flow._setup_playback(
        session, {CONF_PLAYBACK_BACKEND: BACKEND_SOLOIST}, "spotify-user"
    )
    entries = session.form.await_args_list[0].args[0]
    assert entries[0].value == BACKEND_SOLOIST


async def test_pairing_captures_its_output_and_redacts_the_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The pairing daemon's output is captured, not inherited, and never carries the key."""
    api_key = "k" * 20
    spawns: list[tuple[list[str], dict[str, Any]]] = []
    _install_fake_soloist(
        monkeypatch,
        0,
        on_wait=lambda: (tmp_path / "data" / "session.bin").write_bytes(b"x"),
        spawns=spawns,
        output_lines=(f"starting with --api-key {api_key}",),
    )
    (tmp_path / "data").mkdir()
    with caplog.at_level(logging.DEBUG):
        await pair_soloist_session(MagicMock(), api_key, tmp_path / "data")
    _args, kwargs = spawns[0]
    # an unset stdout is inherited, which would put the argv on the server console
    assert kwargs["stdout"] is True
    assert kwargs["stderr"] == asyncio.subprocess.STDOUT
    assert api_key not in caplog.text
    assert "<redacted>" in caplog.text


async def test_pairing_failure_exit_code_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nonzero pairing exit code surfaces as a login failure."""
    _install_fake_soloist(monkeypatch, returncode=1)
    with pytest.raises(LoginFailed, match="exit code 1"):
        await pair_soloist_session(MagicMock(), "k" * 20, tmp_path / "pairdir")


async def test_pairing_without_a_stored_session_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit code 0 without a stored session still fails: pairing never completed."""
    _install_fake_soloist(monkeypatch, returncode=0)
    with pytest.raises(LoginFailed, match="did not store"):
        await pair_soloist_session(MagicMock(), "k" * 20, tmp_path / "pairdir")


async def test_pairing_success_stores_a_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pairing run that stores a session completes without error."""
    data_dir = tmp_path / "pairdir"
    _install_fake_soloist(
        monkeypatch, returncode=0, on_wait=lambda: (data_dir / "session.bin").write_bytes(b"x")
    )
    await pair_soloist_session(MagicMock(), "k" * 20, data_dir)
    assert (data_dir / "session.bin").is_file()


async def _run_awaitable(awaitable: Any, **_kwargs: Any) -> Any:
    """Stand in for session.progress_until, which awaits the work it displays progress for."""
    return await awaitable


def _make_session(tmp_path: Path, form_answers: list[dict[str, Any]]) -> MagicMock:
    """Return a setup-session mock answering its forms from the given sequence."""
    session = MagicMock()
    session.form = AsyncMock(side_effect=form_answers)
    session.progress_until = AsyncMock(side_effect=_run_awaitable)
    session.mass.storage_path = str(tmp_path)
    session.context.instance_id = None
    session.flow_id = "flow1"
    return session


def _step_ids(session: MagicMock) -> list[str]:
    """Return the step ids of every form shown, in order."""
    return [call.kwargs["step_id"] for call in session.form.await_args_list]


def _record_pairing(monkeypatch: pytest.MonkeyPatch, account: str | None = None) -> list[Path]:
    """
    Replace pair_soloist_session with a recording no-op, returning the recorded dirs.

    :param account: The Spotify username the fake pairing lands on, written the
        way the engine records it. None leaves the account unreadable.
    """
    paired_dirs: list[Path] = []

    async def _fake_pair(_mass: Any, _api_key: str, data_dir: Path) -> None:
        paired_dirs.append(data_dir)
        if account is not None:
            (data_dir / "settings" / "Users" / f"{account}-user").mkdir(parents=True)

    monkeypatch.setattr(setup_flow, "pair_soloist_session", _fake_pair)
    return paired_dirs


def _install_fake_soloist(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    on_wait: Callable[[], object] | None = None,
    spawns: list[tuple[list[str], dict[str, Any]]] | None = None,
    output_lines: tuple[str, ...] = (),
) -> None:
    """
    Replace the binary manager and pairing process in helpers with canned fakes.

    :param spawns: Collects the (argv, kwargs) of every spawn, for assertions.
    :param output_lines: Lines the fake daemon writes to its stdout.
    """
    recorded = spawns if spawns is not None else []
    manager = MagicMock()
    manager.ensure_fresh = AsyncMock(return_value=Path("/fake/soloist"))
    monkeypatch.setattr(spotify_helpers, "SoloistBinaryManager", MagicMock(return_value=manager))

    class _FakePairProcess:
        """AsyncProcess stand-in for the soloist --pair run."""

        def __init__(self, args: list[str], **kwargs: Any) -> None:
            recorded.append((args, kwargs))

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_exc_info: object) -> None:
            return None

        async def iter_stdout(self) -> AsyncGenerator[str]:
            # the daemon echoes its own argv, api key included
            for line in output_lines:
                yield line

        async def wait(self) -> int:
            if on_wait is not None:
                on_wait()
            return returncode

    monkeypatch.setattr(spotify_helpers, "AsyncProcess", _FakePairProcess)
