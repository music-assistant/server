"""MQTT client that feeds shairport-sync metadata to ShairportSyncPlayer instances."""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, Final

import aiomqtt
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.helpers.json import JSON_DECODE_EXCEPTIONS, json_loads
from music_assistant.helpers.shairport_metadata import ShairportMetadataDecoder

if TYPE_CHECKING:
    import logging

    from .provider import AirPlayProvider
    from .shairport_player import ShairportSyncPlayer

# A copl plist with artwork can be several MB, anything beyond this is not metadata.
MAX_MESSAGE_SIZE: Final[int] = 8 * 1024 * 1024
RECONNECT_DELAY_MIN: Final[int] = 1
RECONNECT_DELAY_MAX: Final[int] = 60
AUTODISCOVERY_OBJECT_PREFIX: Final[str] = "shairport-sync_"
AUTODISCOVERY_STATE_SUFFIX: Final[str] = "/title"
RAW_ITEM_TYPES: Final[frozenset[str]] = frozenset({"core", "ssnc"})
VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"(\d+)\.(\d+)")


@dataclass(frozen=True)
class _DiscoveredReceiver:
    """A shairport-sync receiver announced through Home Assistant MQTT autodiscovery."""

    base_topic: str
    name: str | None
    sw_version: str | None


class ShairportMqttClient:
    """Receive shairport-sync metadata over MQTT and send remote commands back."""

    def __init__(
        self,
        provider: AirPlayProvider,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        discovery_prefix: str,
    ) -> None:
        """
        Initialize the client.

        :param provider: The AirPlay provider that owns the shairport-sync players.
        :param host: Hostname or IP address of the MQTT broker.
        :param port: Port of the MQTT broker.
        :param username: Username for the broker, empty or None for an anonymous login.
        :param password: Password for the broker, empty or None when not needed.
        :param discovery_prefix: Home Assistant autodiscovery prefix shairport-sync publishes under.
        """
        self.provider = provider
        self.host = host
        self.port = port
        self.username = username or None
        self.password = password or None
        self._discovery_filter = f"{discovery_prefix}/sensor/+/title/config"
        self._task: asyncio.Task[None] | None = None
        self._client: aiomqtt.Client | None = None
        self._subscription_lock = asyncio.Lock()
        self._subscribed: set[str] = set()
        self._receivers: dict[str, _DiscoveredReceiver] = {}
        self._topics: dict[str, str] = {}
        self._player_by_topic: dict[str, str] = {}
        self._receiver_by_player: dict[str, _DiscoveredReceiver] = {}
        self._conflicts: dict[str, tuple[str, ...]] = {}
        self._decoders: dict[str, ShairportMetadataDecoder] = {}

    @property
    def logger(self) -> logging.Logger:
        """Return the provider's logger, which is renamed once the provider has set up."""
        return self.provider.logger

    def start(self) -> None:
        """Start connecting to the broker in the background."""
        self.refresh()
        self._task = self.provider.mass.create_task(
            self._run(), task_name=f"shairport_mqtt_{self.provider.instance_id}"
        )

    async def close(self) -> None:
        """Disconnect from the broker and forget all receiver state."""
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._client = None
        self._subscribed.clear()
        self._receivers.clear()
        self._topics.clear()
        self._player_by_topic.clear()
        self._receiver_by_player.clear()
        self._conflicts.clear()
        self._decoders.clear()

    def refresh(self) -> None:
        """Re-resolve the topic of every shairport-sync player and update the subscriptions."""
        old_topics = self._topics
        self._resolve_topics()
        for player_id in list(self._decoders):
            if self._topics.get(player_id) != old_topics.get(player_id):
                del self._decoders[player_id]
        if self._client is not None:
            self.provider.mass.create_task(self._refresh_subscriptions())

    def topic_for(self, player_id: str) -> str | None:
        """
        Return the MQTT base topic a player receives metadata on.

        :param player_id: The player id of the ShairportSyncPlayer.
        """
        return self._topics.get(player_id)

    def version_for(self, player_id: str) -> tuple[int, int] | None:
        """
        Return the (major, minor) shairport-sync version of the receiver behind a player.

        Only receivers that announce themselves through autodiscovery report a version.

        :param player_id: The player id of the ShairportSyncPlayer.
        """
        receiver = self._receiver_by_player.get(player_id)
        if receiver is None or receiver.sw_version is None:
            return None
        if not (match := VERSION_PATTERN.match(receiver.sw_version)):
            return None
        return int(match.group(1)), int(match.group(2))

    async def publish_remote(self, player_id: str, command: str) -> None:
        """
        Send a remote control command to the receiver behind a player.

        :param player_id: The player id of the ShairportSyncPlayer.
        :param command: A shairport-sync remote command such as ``pause`` or ``nextitem``.
        :raises PlayerCommandFailed: When the broker is unreachable or the player has no topic.
        """
        if (topic := self._topics.get(player_id)) is None:
            raise PlayerCommandFailed(f"No shairport-sync MQTT topic known for {player_id}")
        if (client := self._client) is None:
            raise PlayerCommandFailed("Not connected to the MQTT broker")
        try:
            await client.publish(f"{topic}/remote", command, qos=0)
        except aiomqtt.MqttError as err:
            raise PlayerCommandFailed(f"Failed to publish {command} to {topic}: {err}") from err

    async def _run(self) -> None:
        """Keep the broker connection alive and dispatch incoming messages until cancelled."""
        backoff = RECONNECT_DELAY_MIN
        outage = False
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self.host,
                    port=self.port,
                    username=self.username,
                    password=self.password,
                    # short enough for brokers that hold MQTT 3.1.1 to its 23 characters
                    identifier=f"mass-airplay-{self.provider.mass.server_id[:8]}",
                ) as client:
                    if outage:
                        self.logger.info(
                            "Connection to MQTT broker %s:%s restored", self.host, self.port
                        )
                    else:
                        self.logger.debug("Connected to MQTT broker %s:%s", self.host, self.port)
                    outage = False
                    backoff = RECONNECT_DELAY_MIN
                    async with self._subscription_lock:
                        self._client = client
                        self._subscribed.clear()
                    await self._sync_subscriptions()
                    async for message in client.messages:
                        try:
                            self._handle_message(message)
                        except Exception:
                            # one bad message must not take the link to every receiver down
                            self.logger.exception(
                                "Error handling MQTT message on %s", message.topic.value
                            )
            except aiomqtt.MqttError as err:
                # Only the first failure of an outage is worth a warning, retries are noise.
                log = self.logger.debug if outage else self.logger.warning
                log(
                    "MQTT broker %s:%s is unavailable, retrying in %ss: %s",
                    self.host,
                    self.port,
                    backoff,
                    err,
                )
                outage = True
            finally:
                self._client = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_DELAY_MAX)

    async def _refresh_subscriptions(self) -> None:
        """Sync the subscriptions outside the reader loop, leaving reconnects to that loop."""
        try:
            await self._sync_subscriptions()
        except aiomqtt.MqttError as err:
            self.logger.debug("Failed to update MQTT subscriptions: %s", err)

    async def _sync_subscriptions(self) -> None:
        """Subscribe to added player topics and unsubscribe from removed ones."""
        async with self._subscription_lock:
            if (client := self._client) is None:
                return
            wanted = {self._discovery_filter} | {f"{topic}/#" for topic in self._player_by_topic}
            if removed := self._subscribed - wanted:
                await client.unsubscribe(sorted(removed))
                if self._client is not client:
                    return
                self._subscribed -= removed
            if added := wanted - self._subscribed:
                await client.subscribe([(topic, 0) for topic in sorted(added)])
                if self._client is not client:
                    return
                self._subscribed |= added

    def _handle_message(self, message: aiomqtt.Message) -> None:
        """Route one incoming MQTT message to autodiscovery or a player's decoder."""
        topic = message.topic.value
        payload = message.payload
        if len(payload) > MAX_MESSAGE_SIZE:
            self.logger.debug("Ignoring %s byte MQTT message on %s", len(payload), topic)
            return
        if message.topic.matches(self._discovery_filter):
            self._handle_discovery(topic, payload)
            return
        parts = topic.rsplit("/", 2)
        if len(parts) != 3:
            return
        base, item_type, code = parts
        if item_type not in RAW_ITEM_TYPES or len(code) != 4:
            return
        if (player_id := self._player_by_topic.get(base)) is None:
            return
        if (decoder := self._decoders.get(player_id)) is None:
            decoder = ShairportMetadataDecoder(partial(self._deliver, player_id), self.logger)
            self._decoders[player_id] = decoder
        decoder.process_item(item_type, code, payload)

    def _deliver(self, player_id: str, update: dict[str, Any]) -> None:
        """Pass a decoded metadata update to the player, when it still exists."""
        if (player := self.provider.get_shairport_player(player_id)) is not None:
            player.on_shairport_metadata(update)

    def _handle_discovery(self, topic: str, payload: bytes) -> None:
        """Track a shairport-sync autodiscovery config message and refresh on changes."""
        object_id = topic.split("/")[-3]
        device_id = object_id.removeprefix(AUTODISCOVERY_OBJECT_PREFIX)
        if device_id == object_id or not device_id:
            return
        if not payload:
            changed = self._receivers.pop(device_id, None) is not None
        else:
            if (receiver := self._parse_discovery(topic, payload)) is None:
                return
            changed = self._receivers.get(device_id) != receiver
            self._receivers[device_id] = receiver
        if changed:
            self.refresh()

    def _parse_discovery(self, topic: str, payload: bytes) -> _DiscoveredReceiver | None:
        """Return the receiver an autodiscovery payload describes, None when malformed."""
        try:
            config = json_loads(payload)
        except JSON_DECODE_EXCEPTIONS as err:
            self.logger.debug("Ignoring malformed autodiscovery message on %s: %s", topic, err)
            return None
        state_topic = config.get("state_topic") if isinstance(config, dict) else None
        device = config.get("device") if isinstance(config, dict) else None
        if (
            not isinstance(state_topic, str)
            or not state_topic.endswith(AUTODISCOVERY_STATE_SUFFIX)
            or not isinstance(device, dict)
        ):
            self.logger.debug("Ignoring unexpected autodiscovery message on %s", topic)
            return None
        name = device.get("name")
        sw_version = device.get("sw_version")
        return _DiscoveredReceiver(
            base_topic=state_topic.removesuffix(AUTODISCOVERY_STATE_SUFFIX),
            name=name if isinstance(name, str) else None,
            sw_version=sw_version if isinstance(sw_version, str) else None,
        )

    def _resolve_topics(self) -> None:
        """Work out which topic and discovered receiver belongs to each player."""
        candidates: dict[str, str] = {}
        names: dict[str, str] = {}
        self._receiver_by_player = {}
        for player in self.provider.get_shairport_players():
            names[player.player_id] = player.display_name
            receiver: _DiscoveredReceiver | None
            if topic := player.mqtt_topic_setting.strip():
                receiver = next(
                    (r for r in self._receivers.values() if r.base_topic == topic), None
                )
            elif receiver := self._match_receiver(player):
                topic = receiver.base_topic
            if receiver is not None:
                self._receiver_by_player[player.player_id] = receiver
            # A topic with wildcards is not a valid subscription prefix and nothing publishes there.
            if topic and "+" not in topic and "#" not in topic:
                candidates[player.player_id] = topic

        players_on_topic: dict[str, list[str]] = defaultdict(list)
        for player_id, topic in candidates.items():
            players_on_topic[topic].append(player_id)
        conflicts = {
            topic: tuple(sorted(player_ids))
            for topic, player_ids in players_on_topic.items()
            if len(player_ids) > 1
        }
        for topic, player_ids in conflicts.items():
            if self._conflicts.get(topic) != player_ids:
                self.logger.warning(
                    "Players %s all use shairport-sync MQTT topic %s, "
                    "none of them gets metadata until each has its own topic",
                    ", ".join(names[player_id] for player_id in player_ids),
                    topic,
                )
        self._conflicts = conflicts
        self._topics = {
            player_id: topic for player_id, topic in candidates.items() if topic not in conflicts
        }
        self._player_by_topic = {topic: player_id for player_id, topic in self._topics.items()}

    def _match_receiver(self, player: ShairportSyncPlayer) -> _DiscoveredReceiver | None:
        """Return the discovered receiver matching a player by device id, else by name."""
        device_id = player.player_id[2:].replace(":", "").casefold()
        name = player.display_name.casefold()
        by_name: _DiscoveredReceiver | None = None
        for receiver_id, receiver in self._receivers.items():
            if receiver_id.replace(":", "").casefold() == device_id:
                return receiver
            if by_name is None and receiver.name is not None and receiver.name.casefold() == name:
                by_name = receiver
        return by_name
