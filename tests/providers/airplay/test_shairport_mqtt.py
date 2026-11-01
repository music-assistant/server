"""Tests for the shairport-sync MQTT client of the AirPlay provider."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any, Self
from unittest.mock import MagicMock, patch

import aiomqtt
import pytest
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.helpers.json import json_dumps
from music_assistant.providers.airplay.shairport_mqtt import (
    MAX_MESSAGE_SIZE,
    ShairportMqttClient,
)

MODULE = "music_assistant.providers.airplay.shairport_mqtt"
PREFIX = "homeassistant"
DISCOVERY_FILTER = f"{PREFIX}/sensor/+/title/config"
LOGGER_NAME = "test.airplay.shairport_mqtt"
_real_sleep = asyncio.sleep

type Connect = Callable[[MagicMock], Awaitable[ShairportMqttClient]]


class FakeConnection:
    """One scripted broker connection that records what the client does with it."""

    def __init__(self, broker: FakeBroker) -> None:
        """Create a connection belonging to the given fake broker."""
        self.broker = broker
        self.subscriptions: set[str] = set()
        self.published: list[tuple[str, Any, int]] = []
        self._inbox: asyncio.Queue[aiomqtt.Message | aiomqtt.MqttError] = asyncio.Queue()

    async def __aenter__(self) -> Self:
        """Connect, or fail while the broker is set to refuse connections."""
        if self.broker.refuse > 0:
            self.broker.refuse -= 1
            raise aiomqtt.MqttError("Connection refused")
        self.broker.connections.append(self)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Disconnect."""

    async def subscribe(self, topic: list[tuple[str, int]]) -> None:
        """Record subscriptions."""
        self.subscriptions.update(name for name, _qos in topic)

    async def unsubscribe(self, topic: list[str]) -> None:
        """Record unsubscriptions."""
        self.subscriptions.difference_update(topic)

    async def publish(self, topic: str, payload: Any = None, qos: int = 0) -> None:
        """Record a publish, or fail when the broker is set to reject it."""
        if self.broker.publish_error is not None:
            raise self.broker.publish_error
        self.published.append((topic, payload, qos))

    @property
    def messages(self) -> AsyncIterator[aiomqtt.Message]:
        """Yield delivered messages until the connection drops."""
        return self._iterate()

    def deliver(self, topic: str, payload: bytes) -> None:
        """Queue an incoming message."""
        self._inbox.put_nowait(aiomqtt.Message(topic, payload, 0, False, 1, None))

    def drop(self) -> None:
        """Make the message iteration fail like a lost connection."""
        self._inbox.put_nowait(aiomqtt.MqttError("Disconnected during message iteration"))

    async def _iterate(self) -> AsyncIterator[aiomqtt.Message]:
        while True:
            item = await self._inbox.get()
            if isinstance(item, aiomqtt.MqttError):
                raise item
            yield item


class FakeBroker:
    """Stand-in for ``aiomqtt.Client`` that hands out scripted connections."""

    def __init__(self) -> None:
        """Create a broker that accepts every connection."""
        self.refuse = 0
        self.publish_error: aiomqtt.MqttError | None = None
        self.connections: list[FakeConnection] = []

    def __call__(self, **kwargs: Any) -> FakeConnection:
        """Create a connection like ``aiomqtt.Client(...)`` does."""
        return FakeConnection(self)

    @property
    def current(self) -> FakeConnection:
        """Return the most recent established connection."""
        return self.connections[-1]


async def _settle() -> None:
    """Let the client's background tasks run."""
    for _ in range(20):
        await _real_sleep(0)


def _player(player_id: str, name: str, topic: str = "") -> MagicMock:
    """Create a ShairportSyncPlayer stand-in."""
    player = MagicMock()
    player.player_id = player_id
    player.display_name = name
    player.mqtt_topic_setting = topic
    return player


def _provider(*players: MagicMock) -> MagicMock:
    """Create an AirPlay provider stand-in that owns the given players."""
    provider = MagicMock()
    provider.instance_id = "airplay"
    provider.logger = logging.getLogger(LOGGER_NAME)
    provider.mass.create_task.side_effect = lambda coro, **_kwargs: asyncio.ensure_future(coro)
    provider.players = list(players)
    provider.get_shairport_players.side_effect = lambda: list(provider.players)
    provider.get_shairport_player.side_effect = lambda player_id: next(
        (player for player in provider.players if player.player_id == player_id), None
    )
    return provider


def _discovery(
    device_id: str,
    base_topic: str,
    name: str,
    sw_version: str = "5.6.2-AirPlay2-OpenSSL-Avahi-ALSA-soxr-metadata-mqtt",
) -> tuple[str, bytes]:
    """Build the retained autodiscovery message shairport-sync publishes for its title sensor."""
    topic = f"{PREFIX}/sensor/shairport-sync_{device_id}/title/config"
    payload = {
        "name": "Title",
        "state_topic": f"{base_topic}/title",
        "unique_id": f"shairport-sync_{device_id}_title",
        "device": {
            "identifiers": [f"shairport-sync_{device_id}"],
            "name": name,
            "manufacturer": "Mike Brady",
            "model": "shairport-sync",
            "sw_version": sw_version,
        },
    }
    return topic, json_dumps(payload).encode()


@pytest.fixture
def broker() -> Iterator[FakeBroker]:
    """Replace the aiomqtt client with a scripted fake broker."""
    fake = FakeBroker()
    with patch(f"{MODULE}.aiomqtt.Client", fake):
        yield fake


@pytest.fixture
def sleeps() -> Iterator[list[float]]:
    """Make reconnect backoff instant and record the requested delays."""
    delays: list[float] = []

    async def fake_sleep(delay: float, result: Any = None) -> Any:
        delays.append(delay)
        await _real_sleep(0)
        return result

    with patch(f"{MODULE}.asyncio.sleep", fake_sleep):
        yield delays


@pytest.fixture
async def connect(
    broker: FakeBroker,  # noqa: ARG001
    sleeps: list[float],  # noqa: ARG001
) -> AsyncIterator[Connect]:
    """Start MQTT clients against the fake broker and close them after the test."""
    clients: list[ShairportMqttClient] = []

    async def _connect(provider: MagicMock) -> ShairportMqttClient:
        client = ShairportMqttClient(
            provider,
            host="broker.local",
            port=1883,
            username="",
            password="",
            discovery_prefix=PREFIX,
        )
        clients.append(client)
        client.start()
        await _settle()
        return client

    yield _connect
    for client in clients:
        await client.close()


async def test_raw_item_reaches_player_on_that_topic(connect: Connect, broker: FakeBroker) -> None:
    """A raw metadata item is decoded and delivered only to the player on that topic."""
    kitchen = _player("ap111111111111", "Kitchen", "shairport/kitchen")
    lounge = _player("ap222222222222", "Lounge", "shairport/lounge")
    await connect(_provider(kitchen, lounge))

    broker.current.deliver("shairport/kitchen/ssnc/paus", b"")
    await _settle()

    kitchen.on_shairport_metadata.assert_called_once_with({"paused": True})
    lounge.on_shairport_metadata.assert_not_called()


async def test_parsed_and_remote_topics_are_ignored(connect: Connect, broker: FakeBroker) -> None:
    """Parsed topics, the remote topic and malformed raw topics never reach the decoder."""
    kitchen = _player("ap111111111111", "Kitchen", "shairport/kitchen")
    await connect(_provider(kitchen))

    for topic, payload in (
        ("shairport/kitchen/title", b"Song"),
        ("shairport/kitchen/playing", b"1"),
        ("shairport/kitchen/remote", b"pause"),
        ("shairport/kitchen/ssnc/pausx", b""),
        ("shairport/kitchen/meta/paus", b""),
    ):
        broker.current.deliver(topic, payload)
    await _settle()

    kitchen.on_shairport_metadata.assert_not_called()


async def test_configured_topic_wins_over_autodiscovery(
    connect: Connect, broker: FakeBroker
) -> None:
    """A player's own topic setting is used even when autodiscovery announces another topic."""
    kitchen = _player("apaabbccddeeff", "Kitchen", " custom/kitchen ")
    client = await connect(_provider(kitchen))

    broker.current.deliver(*_discovery("AABBCCDDEEFF", "shairport/kitchen", "Kitchen"))
    await _settle()
    broker.current.deliver("shairport/kitchen/ssnc/paus", b"")
    await _settle()

    assert client.topic_for("apaabbccddeeff") == "custom/kitchen"
    assert broker.current.subscriptions == {DISCOVERY_FILTER, "custom/kitchen/#"}
    kitchen.on_shairport_metadata.assert_not_called()


async def test_autodiscovered_topic_matches_device_id(connect: Connect, broker: FakeBroker) -> None:
    """An AirPlay 2 receiver is matched to the player whose id holds its device id."""
    kitchen = _player("apaabbccddeeff", "Kitchen")
    client = await connect(_provider(kitchen))

    broker.current.deliver(*_discovery("AABBCCDDEEFF", "shairport/kitchen", "Some other name"))
    await _settle()
    broker.current.deliver("shairport/kitchen/ssnc/paus", b"")
    await _settle()

    assert client.topic_for("apaabbccddeeff") == "shairport/kitchen"
    assert broker.current.subscriptions == {DISCOVERY_FILTER, "shairport/kitchen/#"}
    kitchen.on_shairport_metadata.assert_called_once_with({"paused": True})


async def test_autodiscovered_topic_matches_device_name(
    connect: Connect, broker: FakeBroker
) -> None:
    """A Classic receiver is matched to the player with the same name, ignoring case."""
    lounge = _player("ap222222222222", "Living Room")
    client = await connect(_provider(lounge))

    broker.current.deliver(*_discovery("Living Room", "shairport/lounge", "living room"))
    await _settle()

    assert client.topic_for("ap222222222222") == "shairport/lounge"


async def test_removed_discovery_drops_the_topic(connect: Connect, broker: FakeBroker) -> None:
    """An empty autodiscovery payload takes the receiver's topic away from its player."""
    kitchen = _player("apaabbccddeeff", "Kitchen")
    client = await connect(_provider(kitchen))
    topic, payload = _discovery("AABBCCDDEEFF", "shairport/kitchen", "Kitchen")
    broker.current.deliver(topic, payload)
    await _settle()

    broker.current.deliver(topic, b"")
    await _settle()

    assert client.topic_for("apaabbccddeeff") is None
    assert broker.current.subscriptions == {DISCOVERY_FILTER}


async def test_malformed_discovery_keeps_known_receiver(
    connect: Connect, broker: FakeBroker
) -> None:
    """A malformed autodiscovery payload is ignored instead of replacing the known receiver."""
    kitchen = _player("apaabbccddeeff", "Kitchen")
    client = await connect(_provider(kitchen))
    topic, payload = _discovery("AABBCCDDEEFF", "shairport/kitchen", "Kitchen")
    broker.current.deliver(topic, payload)
    await _settle()

    broker.current.deliver(topic, b"{not json")
    broker.current.deliver(topic, json_dumps({"state_topic": "shairport/kitchen/a"}).encode())
    await _settle()

    assert client.topic_for("apaabbccddeeff") == "shairport/kitchen"


async def test_shared_topic_goes_to_neither_player(
    connect: Connect, broker: FakeBroker, caplog: pytest.LogCaptureFixture
) -> None:
    """Players that share a topic both get nothing, with one warning until the conflict ends."""
    kitchen = _player("ap111111111111", "Kitchen", "shairport/shared")
    lounge = _player("ap222222222222", "Lounge", "shairport/shared")
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        client = await connect(_provider(kitchen, lounge))
        client.refresh()
        await _settle()

    broker.current.deliver("shairport/shared/ssnc/paus", b"")
    await _settle()

    assert client.topic_for("ap111111111111") is None
    assert client.topic_for("ap222222222222") is None
    assert broker.current.subscriptions == {DISCOVERY_FILTER}
    kitchen.on_shairport_metadata.assert_not_called()
    lounge.on_shairport_metadata.assert_not_called()
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "Kitchen" in warnings[0].getMessage()
    assert "Lounge" in warnings[0].getMessage()

    lounge.mqtt_topic_setting = "shairport/lounge"
    client.refresh()
    await _settle()

    assert client.topic_for("ap111111111111") == "shairport/shared"
    assert client.topic_for("ap222222222222") == "shairport/lounge"


async def test_subscriptions_follow_topic_changes(connect: Connect, broker: FakeBroker) -> None:
    """After a refresh the client subscribes to the new topic and stops routing the old one."""
    kitchen = _player("ap111111111111", "Kitchen", "shairport/kitchen")
    client = await connect(_provider(kitchen))

    kitchen.mqtt_topic_setting = "shairport/den"
    client.refresh()
    await _settle()
    broker.current.deliver("shairport/kitchen/ssnc/paus", b"")
    await _settle()

    assert broker.current.subscriptions == {DISCOVERY_FILTER, "shairport/den/#"}
    kitchen.on_shairport_metadata.assert_not_called()


async def test_subscriptions_renewed_after_reconnect(
    connect: Connect, broker: FakeBroker, sleeps: list[float]
) -> None:
    """A lost connection is re-established and every subscription is made again."""
    kitchen = _player("ap111111111111", "Kitchen", "shairport/kitchen")
    await connect(_provider(kitchen))
    first = broker.current

    first.drop()
    await _settle()

    assert broker.current is not first
    assert broker.current.subscriptions == {DISCOVERY_FILTER, "shairport/kitchen/#"}
    assert sleeps == [1]


async def test_outage_backs_off_and_warns_once(
    connect: Connect,
    broker: FakeBroker,
    sleeps: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unreachable broker is retried with doubling delays and reported once, then restored."""
    broker.refuse = 3
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        await connect(_provider())

    assert sleeps == [1, 2, 4]
    assert len(broker.connections) == 1
    levels = [
        record.levelno
        for record in caplog.records
        if "unavailable" in record.getMessage() or "restored" in record.getMessage()
    ]
    assert levels == [logging.WARNING, logging.DEBUG, logging.DEBUG, logging.INFO]


async def test_oversized_message_is_dropped(connect: Connect, broker: FakeBroker) -> None:
    """Messages above the size cap are dropped, messages at the cap are still decoded."""
    kitchen = _player("ap111111111111", "Kitchen", "shairport/kitchen")
    await connect(_provider(kitchen))

    broker.current.deliver("shairport/kitchen/ssnc/PICT", b"\xff" * (MAX_MESSAGE_SIZE + 1))
    await _settle()
    kitchen.on_shairport_metadata.assert_not_called()

    broker.current.deliver("shairport/kitchen/ssnc/PICT", b"\xff" * MAX_MESSAGE_SIZE)
    await _settle()
    kitchen.on_shairport_metadata.assert_called()


@pytest.mark.parametrize(
    ("sw_version", "expected"),
    [
        ("5.6.2-AirPlay2-OpenSSL-Avahi-ALSA", (5, 6)),
        ("4.3.7-OpenSSL-Avahi-ALSA-metadata-mqtt", (4, 3)),
        ("development", None),
    ],
)
async def test_version_for_parses_sw_version(
    connect: Connect,
    broker: FakeBroker,
    sw_version: str,
    expected: tuple[int, int] | None,
) -> None:
    """The receiver's announced version is reduced to (major, minor), None when unparsable."""
    kitchen = _player("apaabbccddeeff", "Kitchen")
    client = await connect(_provider(kitchen))

    broker.current.deliver(
        *_discovery("AABBCCDDEEFF", "shairport/kitchen", "Kitchen", sw_version=sw_version)
    )
    await _settle()

    assert client.version_for("apaabbccddeeff") == expected


async def test_version_for_configured_topic_uses_receiver_on_that_topic(
    connect: Connect, broker: FakeBroker
) -> None:
    """A configured topic takes its version from the discovered receiver publishing there."""
    kitchen = _player("ap111111111111", "Kitchen", "shairport/kitchen")
    lounge = _player("ap222222222222", "Lounge", "shairport/lounge")
    client = await connect(_provider(kitchen, lounge))

    broker.current.deliver(*_discovery("unrelated", "shairport/kitchen", "Elsewhere"))
    await _settle()

    assert client.version_for("ap111111111111") == (5, 6)
    assert client.version_for("ap222222222222") is None


async def test_publish_remote_sends_command_to_remote_topic(
    connect: Connect, broker: FakeBroker
) -> None:
    """A remote command is published to the player's remote topic at QoS 0."""
    kitchen = _player("ap111111111111", "Kitchen", "shairport/kitchen")
    client = await connect(_provider(kitchen))

    await client.publish_remote("ap111111111111", "pause")

    assert broker.current.published == [("shairport/kitchen/remote", "pause", 0)]


async def test_publish_remote_fails_without_connection() -> None:
    """A remote command fails while there is no broker connection."""
    kitchen = _player("ap111111111111", "Kitchen", "shairport/kitchen")
    client = ShairportMqttClient(
        _provider(kitchen),
        host="broker.local",
        port=1883,
        username=None,
        password=None,
        discovery_prefix=PREFIX,
    )
    client.refresh()

    with pytest.raises(PlayerCommandFailed):
        await client.publish_remote("ap111111111111", "pause")


async def test_publish_remote_fails_without_topic(connect: Connect) -> None:
    """A remote command fails for a player without a topic."""
    client = await connect(_provider(_player("ap111111111111", "Kitchen")))

    with pytest.raises(PlayerCommandFailed):
        await client.publish_remote("ap111111111111", "pause")


async def test_publish_remote_wraps_broker_errors(connect: Connect, broker: FakeBroker) -> None:
    """A publish the broker connection rejects surfaces as a failed player command."""
    client = await connect(_provider(_player("ap111111111111", "Kitchen", "shairport/kitchen")))
    broker.publish_error = aiomqtt.MqttError("Could not publish message")

    with pytest.raises(PlayerCommandFailed):
        await client.publish_remote("ap111111111111", "pause")
