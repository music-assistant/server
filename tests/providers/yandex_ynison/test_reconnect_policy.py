"""Tests for the Ynison reconnect policy."""

from __future__ import annotations

import pytest

from music_assistant.providers.yandex_ynison.reconnect_policy import ReconnectPolicy

DEFAULT = (1.0, 5.0, 30.0)
IDLE = 300.0


def _delays(policy: ReconnectPolicy, count: int) -> list[float]:
    """Return the next ``count`` delays of a policy."""
    return [policy.next_delay() for _ in range(count)]


@pytest.fixture
def policy() -> ReconnectPolicy:
    """Create a policy with the official default ladder."""
    return ReconnectPolicy(default_ladder=DEFAULT, idle_delay=IDLE)


def test_transport_failures_walk_ladder_then_idle(policy: ReconnectPolicy) -> None:
    """Transport failures use the default ladder without jitter, then the idle delay."""
    policy.on_transport_failure()

    assert _delays(policy, 5) == [1.0, 5.0, 30.0, IDLE, IDLE]


def test_go_away_is_exact_and_restarts_ladder(policy: ReconnectPolicy) -> None:
    """A go-away waits exactly the server window and then restarts the ladder."""
    _delays(policy, 3)
    policy.on_server_error("500140000", grpc_code=14, go_away=3600.0, ladder="")

    assert _delays(policy, 3) == [3600.0, 1.0, 5.0]


def test_repeated_go_away_has_no_escalation(policy: ReconnectPolicy) -> None:
    """Each go-away waits only the server window."""
    for _ in range(3):
        policy.on_server_error("500140000", grpc_code=14, go_away=3600.0, ladder="")
        assert policy.next_delay() == 3600.0


def test_server_ladder_is_sorted_and_deduplicated(policy: ReconnectPolicy) -> None:
    """``ynison-backoff-millis`` replaces the default ladder."""
    policy.on_server_error(
        "300100001", grpc_code=10, go_away=0.0, ladder="0:100:500:1000:1000:5000"
    )

    assert _delays(policy, 6) == [0.0, 0.1, 0.5, 1.0, 5.0, IDLE]


def test_same_error_key_keeps_iteration(policy: ReconnectPolicy) -> None:
    """Repeated errors with the same code continue the current ladder."""
    policy.on_server_error("300100001", grpc_code=10, go_away=0.0, ladder="")
    _delays(policy, 2)
    policy.on_server_error("300100001", grpc_code=10, go_away=0.0, ladder="")

    assert policy.next_delay() == 30.0


def test_error_key_change_restarts_ladder(policy: ReconnectPolicy) -> None:
    """A different error code resets the iteration."""
    policy.on_server_error("300100001", grpc_code=10, go_away=0.0, ladder="")
    _delays(policy, 2)
    policy.on_server_error("300100002", grpc_code=10, go_away=0.0, ladder="")

    assert policy.next_delay() == 1.0


def test_transport_failure_after_server_error_restarts_ladder(policy: ReconnectPolicy) -> None:
    """A transport failure is a distinct error key and restores the default ladder."""
    policy.on_server_error("300100001", grpc_code=10, go_away=0.0, ladder="0:100")
    _delays(policy, 2)
    policy.on_transport_failure()

    assert _delays(policy, 2) == [1.0, 5.0]


def test_healthy_session_restarts_ladder(policy: ReconnectPolicy) -> None:
    """A healthy session resets the iteration."""
    policy.on_transport_failure()
    _delays(policy, 3)
    policy.on_healthy()

    assert policy.next_delay() == 1.0


@pytest.mark.parametrize("grpc_code", [3, 5, 7, 13])
def test_non_retryable_status_waits_idle(policy: ReconnectPolicy, grpc_code: int) -> None:
    """Statuses the official client does not retry wait the idle delay."""
    policy.on_server_error("400030001", grpc_code=grpc_code, go_away=0.0, ladder="")

    assert _delays(policy, 2) == [IDLE, IDLE]


@pytest.mark.parametrize("grpc_code", [8, 10, 14])
def test_retryable_status_uses_ladder(policy: ReconnectPolicy, grpc_code: int) -> None:
    """RESOURCE_EXHAUSTED, ABORTED and UNAVAILABLE follow the ladder."""
    policy.on_server_error("500000000", grpc_code=grpc_code, go_away=0.0, ladder="")

    assert policy.next_delay() == 1.0


def test_missing_grpc_code_is_retryable(policy: ReconnectPolicy) -> None:
    """An error without a gRPC status keeps retrying on the ladder."""
    policy.on_server_error("", grpc_code=None, go_away=0.0, ladder="")

    assert policy.next_delay() == 1.0


def test_go_away_beats_non_retryable_status(policy: ReconnectPolicy) -> None:
    """A go-away directive wins over the status classification."""
    policy.on_server_error("400090001", grpc_code=7, go_away=60.0, ladder="")

    assert _delays(policy, 2) == [60.0, 1.0]


def test_healthy_clears_server_ladder(policy: ReconnectPolicy) -> None:
    """After a healthy session the next transport failure uses the default ladder."""
    policy.on_server_error("300100001", grpc_code=10, go_away=0.0, ladder="0:100")
    policy.on_healthy()
    policy.on_transport_failure()

    assert policy.next_delay() == 1.0
