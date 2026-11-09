# Player lifecycle scenarios

Acceptance scenarios for attaching and detaching the protocols of a device: what a client sees
when a provider finds, loses or re-identifies a device, after a restart, or when the user removes
something. They drive a real server only through what providers and clients use (register and
unregister, reported state, `players/all`, events, queues, the stored config), so they stay valid
while the players controller is rebuilt for the player split
(music-assistant/backlog `docs/plans/player-split.md`, "Scenario suite").

## The harness

`harness.py` holds the scenario DSL, `conftest.py` the `scenario` fixture.

- **Server**: a real `MusicAssistant` per scenario on a temporary folder (`full_mass_context`).
  `restart()` boots a new one on the same `settings.json` and cache, `seed_config()` does the
  same with values merged into the stored config.
- **Providers**: every player provider domain except `universal_player` and `sync_group` loads as
  a `FakePlayerProvider`. It keeps its real domain, as protocol linking keys on it (priority, one
  protocol per domain), and a domain can have several instances.
- **Protocols**: `provider.protocol(...)` creates a `FakeProtocol` for one device. The scenario
  sets what it reports; its commands are recorded in `protocol.commands` and applied to its state.
  `role="native"` or `role="audio"` stands for the role of the protocol on its device.
- **Time**: the event loop clock only moves through `advance(seconds)`, which runs every timer
  that falls due on the way (the 15 s and 45 s protocol evaluation delays, debounces, polls). Every
  action waits until the server finished the work it started, so no scenario sleeps.
- **ARP**: `scenario.arp[ip] = mac` is the MAC address an IP address resolves to.

| Action | Does |
|---|---|
| `add_provider(domain)`, `remove_provider(provider)` | load a new fake provider instance, remove one as a user does |
| `attach(protocol)`, `detach(protocol)` | the provider found the device (again), or lost it |
| `report(protocol, **state)` | the device reports new state (`available`, `powered`, `identifiers`, ...) |
| `remove(player_id)` | the user removes a player |
| `advance(seconds)`, `settle()` | let time pass, let due work finish |
| `restart()`, `seed_config(values)` | boot again on the stored config, optionally changed first |

| Observation | Returns |
|---|---|
| `visible_players()` | the `players/all` listing as `VisiblePlayer` records |
| `assert_single_player(*protocols)` | the one visible player carrying these protocols |
| `player(id)`, `queue(id)`, `stored(key)` | a player state, a queue, a stored config value |
| `events(*types)`, `clear_events()` | the emitted events in order, consecutive repeats collapsed |
| `provider(domain_or_id)` | a loaded fake provider, a new instance after a restart |

For a client command the DSL does not wrap, call the server's own API on `scenario.mass` (e.g.
`await scenario.mass.players.cmd_power(player_id, True)`) and `await scenario.settle()` after it.

The harness touches two server internals, both in `harness.py`: the manifest loading of
`MusicAssistant` (a fake domain needs a manifest without requirements, dependencies or network
discovery) and `config._create_provider_instance` (the end of the add-provider setup flow, which
itself would import the real provider's setup module). Scenarios use none.

## Example

```python
async def test_native_device_attaches_as_available_player(scenario: Scenario) -> None:
    """A native protocol that attaches is listed as an available player with a queue."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_1", "Kitchen", identifiers={IdentifierType.MAC_ADDRESS: MAC})

    await scenario.attach(speaker)

    player = scenario.assert_single_player(speaker)
    assert player == VisiblePlayer(
        player_id="RINCON_1",
        name="Kitchen",
        available=True,
        powered=None,
        provider="sonos",
        outputs={"sonos": True},
    )
    assert scenario.queue("RINCON_1") is not None
```

## One xfail marker per phase

A scenario states one behaviour. When it asserts today's behaviour that the target changes, its
docstring says so. When it describes the target and fails today, it carries the phase that makes it
pass (issue numbers refer to music-assistant/backlog):

```python
@pytest.mark.xfail(strict=True, reason="phase 1 (#415): a player outlives its detached protocol")
```

The marker is strict, so a scenario that starts to pass fails the run until its marker is removed.
A phase is done when all of its markers are gone; the scenarios of today's behaviour it changes are
rewritten in the same PR.
