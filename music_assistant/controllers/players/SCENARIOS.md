# Player Controller Scenarios

What a user, a provider and a client can rely on from the player controller, written out as
scenarios. Each scenario is proven by the tests it names: they run a real server with stand-in
providers and devices, and check what a client sees (the player list, a player's fields, the
announcements of a player added or removed, an error).

## Reading an entry

Each entry has an id, the rule in one line, the journey in a few sentences, and two lines:

- **End-to-end** says whether the scenario suits a run against real provider code and virtual or
  real devices:
  - **yes**: as it stands;
  - **possible with**: once the run has what the line names;
  - **no**: with the reason.
- **Proven by** names the tests, as `file::test` within
  [tests/scenarios](../../../tests/scenarios), or with their full path when they live elsewhere.

"The lab" in the end-to-end lines is the end-to-end test lab, music-assistant/end-to-end-test-lab.

The devices and rooms in the examples are made up: Kitchen, Office, Den, Living, Patio, and so on.

## Adding a scenario

A new test in `tests/scenarios` goes into exactly one entry: into the entry whose rule it proves,
or into a new entry with the next free number of its chapter. An id is never renumbered or
reused, so others can refer to it. A test checks that this document names every scenario test
once and that every test it names exists.

## Chapters

- [A device appears](#a-device-appears) (APPEAR)
- [A device goes away and comes back](#a-device-goes-away-and-comes-back) (AWAY)
- [Several devices on one host](#several-devices-on-one-host) (HOST)
- [Zones of one chassis](#zones-of-one-chassis) (ZONE)
- [A speaker and its Home Assistant media player](#a-speaker-and-its-home-assistant-media-player) (TWIN)
- [A device that changes how it is identified](#a-device-that-changes-how-it-is-identified) (IDENT)
- [Duplicates, merging and taking a protocol off](#duplicates-merging-and-taking-a-protocol-off) (MERGE)
- [Restart and upgrade](#restart-and-upgrade) (RESTART)
- [Removing and disabling](#removing-and-disabling) (REMOVE)
- [Browser and app players](#browser-and-app-players) (WEB)
- [Commands to a busy player](#commands-to-a-busy-player) (BUSY)
- [What a stopped player keeps for a moment](#what-a-stopped-player-keeps-for-a-moment) (KEEP)
- [Grouping](#grouping) (GROUP)
- [Where playback goes](#where-playback-goes) (OUTPUT)
- [Volume](#volume) (VOLUME)
- [A pause, a stop and the end of a queue](#a-pause-a-stop-and-the-end-of-a-queue) (PAUSE)

## A device appears

A provider finds a device it has not seen before. A device is one player, whichever of its
protocols are found first: the protocols of one device are tied together by what they report
about it, such as its MAC address.

### APPEAR-1 · A device found by its own provider is an available player with a queue

The Sonos provider finds the Kitchen speaker. Clients see one new player, Kitchen: available,
its power state unknown, with Sonos named as its provider and as its only output. Clients are
told about the player and then about its queue.

- End-to-end: **yes**. A virtual Squeezelite player starts.
- Proven by: `test_attach_detach.py::test_native_device_attaches_as_available_player`

### APPEAR-2 · The AirPlay and Cast receivers of one device are one player

The AirPlay provider finds the receiver of the Den device, then the Chromecast provider finds its
Cast receiver. Both report the MAC address of the device, so the Cast receiver joins the player
the AirPlay receiver got: one player, Den, with AirPlay and Chromecast as outputs and AirPlay
named as its provider. Clients are told about one new player only.

- End-to-end: **possible with** one host that runs two receivers of different kinds, such as
  AirPlay and DLNA.
- Proven by: `test_attach_detach.py::test_airplay_and_cast_of_one_device_become_one_player`

### APPEAR-3 · A player of an AirPlay receiver alone names the AirPlay provider

The AirPlay provider finds a receiver that has no other protocol. Its player names AirPlay as its
provider.

- End-to-end: **yes**. A shairport-sync receiver starts.
- Proven by: `test_identity.py::test_protocol_only_player_names_the_provider_of_its_protocol`

### APPEAR-4 · A device that is disabled by default is no player until the user enables it

A provider finds a device that it keeps disabled by default (tested for an AirPlay receiver and
for a Cast device). No player appears and clients are told about none; the settings list it,
disabled. Once the user enables it, it is an available player with a queue, clients are told
about it, and it plays.

- End-to-end: **no**: there is no virtual device that its provider disables by default.
- Proven by: `test_lifecycle.py::test_disabled_by_default_device_is_no_player_until_enabled`

### APPEAR-5 · A device whose first setup fails or is called off is no player

A provider finds the Kitchen speaker, but its setup fails, or is called off halfway, as a new
discovery round does. No player appears, clients are told about none, and nothing of it is
stored. After a failed setup there is still no player after a restart. A setup that was called
off leaves the device free: once its provider finds it again, it is the player Kitchen.

- End-to-end: **no**: it needs a device whose setup fails or stalls on purpose.
- Proven by:
  - `test_attach_detach.py::test_a_new_device_whose_setup_is_called_off_leaves_nothing_behind`
  - `tests/controllers/players/test_attach_detach.py::test_failed_attach_of_a_new_device_leaves_nothing_behind`

### APPEAR-6 · A protocol that rides on another belongs to that protocol's player

A Sendspin bridge rides on the AirPlay receiver of the Kitchen speaker and reports nothing else
about the device. It joins the player of that receiver, however late the receiver is found: while
there is none, it has no player. On the player, the bridge's output names the receiver as the
output it rides on. While the player has a Sendspin output already, a second bridge waits, and
it joins once the provider forgets the first. When the provider forgets the receiver, the bridge
goes with it, and the player goes with its last protocol.

- End-to-end: **possible with** a Sendspin bridge on a Cast or AirPlay receiver of the lab.
- Proven by:
  - `tests/controllers/players/test_identity.py::test_protocol_that_rides_on_another_waits_for_it`
  - `tests/controllers/players/test_protocol_linking.py::test_derived_protocol_attaches_to_the_player_of_the_protocol_it_rides_on`
  - `tests/controllers/players/test_protocol_linking.py::test_derived_protocol_waits_while_its_player_has_a_protocol_of_its_domain`
  - `tests/controllers/players/test_protocol_linking.py::test_waiting_derived_protocol_attaches_once_its_domain_is_free`
  - `tests/controllers/players/test_attach_detach.py::test_removed_protocol_takes_the_protocols_that_ride_on_it_along`

### APPEAR-7 · An AirPlay receiver without a hardware address waits up to 15 s for its device

The AirPlay provider finds the Kitchen receiver, which reports only the address of its host.
For 14 s it is no player and nothing is stored for it; at 15 s it gets a player of its own, and
clients are told about that one player. When the receiver reports the hardware address of its
device during the wait, it gets its player before the 15 s are up. When the Sonos provider finds
the device's own protocol during the wait, the receiver joins the player that protocol gets. The
own protocol of a device never waits.

- End-to-end: **possible with** an AirPlay receiver whose host has no hardware MAC address the
  server can resolve.
- Proven by:
  - `tests/controllers/players/test_identity.py::test_audio_protocol_without_a_hardware_address_waits_for_its_device`
  - `tests/controllers/players/test_identity.py::test_waiting_protocol_gets_its_player_once_its_hardware_address_is_known`
  - `tests/controllers/players/test_identity.py::test_waiting_protocol_joins_the_player_its_native_protocol_creates`
  - `tests/controllers/players/test_identity.py::test_native_protocol_never_waits`

### APPEAR-8 · A player of audio receivers alone has the neutral speaker icon

The Den device has an AirPlay and a Cast receiver and no protocol of its own, the Patio device an
AirPlay receiver alone: both players show the speaker icon. The Office device, an Apple speaker
with AirPlay as its own protocol, a Cast receiver and a Home Assistant media player as a control,
shows the AirPlay icon, also while its AirPlay protocol is away. An icon the user picks replaces
the default.

- End-to-end: **yes**. Two receivers of different kinds on one host, and one receiver alone.
- Proven by: `test_identity.py::test_a_player_without_a_protocol_of_its_own_has_the_neutral_icon`

### APPEAR-9 · A player of an audio receiver alone takes the name the receiver reports

The Den device is an AirPlay receiver with no protocol of its own. The user renames it in its own
app: the player and its queue take the new name, and it is stored as the name of the player.

- End-to-end: **yes**. Rename a shairport-sync receiver and restart it.
- Proven by: `test_attach_detach.py::test_a_player_of_an_audio_receiver_alone_follows_its_name`

## A device goes away and comes back

A device can go away: switched off, off the network, or its provider reloads. It can also stay
known but not be reachable. Its player stays all the same, with its id, its settings and its
queue. These scenarios say what a client sees meanwhile and which commands the player takes.

### AWAY-1 · A player whose device goes away stays listed

The Sonos provider loses the Kitchen speaker. The player stays in the list, unavailable, with its
queue. Clients get an update of the player, never a removal. After a restart clients are told
about the player before any provider has loaded, and once its provider has loaded the player is
in the list, still unavailable and with its queue.
When the provider finds the speaker again, the player is available again under the same id.

- End-to-end: **yes**. Stop the container of a virtual player, check the player list, restart
  the server, start the container again.
- Proven by:
  - `test_attach_detach.py::test_detached_native_player_stays_listed_unavailable`
  - `test_attach_detach.py::test_detached_native_player_is_listed_also_after_restart`

### AWAY-2 · A player stays available while another of its protocols can play

The Den device is one player with a Snapcast client of its own, an AirPlay receiver and a Cast
receiver. When the Snapcast client can no longer be reached, the player stays available. Its
Snapcast output shows as unavailable, and play goes to the AirPlay or the Cast receiver. The
Snapcast client gets no command.

- End-to-end: **possible with** a virtual player that runs a Snapcast client and an AirPlay
  receiver on one host. Stop only the Snapcast client, then play.
- Proven by: `test_availability.py::test_player_stays_available_on_its_audio_protocols`

### AWAY-3 · A command to a player whose device is gone fails, except power on

The provider lost the Den speaker, which has no power control. Play, pause, stop, set volume,
mute and play media each fail as unavailable instead of silently doing nothing. Power on is
accepted without an error.

- End-to-end: **yes**. Stop the container of a virtual player, send each command through the API.
- Proven by:
  - `test_availability.py::test_command_to_an_unavailable_player_raises`
  - `test_availability.py::test_power_on_of_an_unavailable_player_is_accepted`

### AWAY-4 · A player on a power control is switched on through it

The user has the Bathroom player on a smart plug and switches the plug off, after which the
device drops off the network. The player shows as available and powered off. Power on switches
the plug on and returns once the device is back; the player is then powered on and plays. Play
media on the switched-off player does the same: it switches the plug on, waits for the device
and plays, instead of failing as unavailable.

- End-to-end: **possible with** a power control the lab can switch, which stops and starts the
  container of a virtual player.
- Proven by:
  - `test_availability.py::test_offline_device_is_powered_on_through_its_power_control`
  - `test_availability.py::test_play_on_a_switched_off_player_powers_it_on`

### AWAY-5 · A stop calls off a power on that waits for the device

As in AWAY-4, the user powers on the Bathroom player. While the power on waits for the device,
the player shows as available and powered on. The user stops the player: the waiting power on
fails as unavailable, and since the device never came, the player shows as unavailable.

- End-to-end: **possible with** a power control the lab can switch and a container that stays
  stopped.
- Proven by: `test_player_memory.py::test_a_stop_calls_off_a_power_on_that_waits_for_the_device`

### AWAY-6 · A device that comes back finds its queue where it was

The Kitchen speaker drops off the network while it plays. Its queue stops and keeps its item and
the position it got to. The provider finds the speaker again, idle: nothing plays by itself, and
the queue still has that item at that position. A resume plays the item from there.

- End-to-end: **yes**. Stop the container of a playing virtual player, start it again, resume.
- Proven by: `test_attach_detach.py::test_a_returning_device_resumes_where_its_queue_was`

### AWAY-7 · A bridge alone does not make a player available

The Den device is an AirPlay receiver with a Sendspin bridge that rides on it. While the receiver
can not be reached, or still needs pairing, the player is unavailable, however available the
bridge reports itself: its bridge output shows as unavailable and play fails as unavailable. Once
the receiver can play, the player is available and plays through the bridge when the user prefers
it.

- End-to-end: **possible with** an AirPlay receiver of the lab that needs pairing, with its
  Sendspin bridge.
- Proven by: `test_availability.py::test_bridge_alone_does_not_make_its_player_available`

### AWAY-8 · A player keeps its stored name while only another protocol of it is there

The Kitchen speaker has its Sonos protocol and a DLNA renderer that reports a long technical name.
While the Sonos protocol is away, also after a restart, the player and its queue keep the name
Kitchen. The speaker is renamed to Study in its own app: once its Sonos protocol is back, the player
is Study. A name the user gives the player stays, whatever its protocols report.

- End-to-end: **possible with** a Sonos speaker of the lab whose Sonos connection the lab can cut
  while its DLNA renderer stays up.
- Proven by: `test_attach_detach.py::test_a_player_keeps_its_stored_name_while_only_another_protocol_is_there`

### AWAY-9 · A player keeps the defaults of its device while the device is away

The Living speaker's protocol hides it in the UI, keeps it from Home Assistant and keeps it
private by default. The speaker goes away: the player is still hidden, not exposed and private,
with the same device info and icon, also after a restart. When the speaker comes back with other
defaults, the player has those from then on, also while it is away again.

- End-to-end: **no**: no player provider of the lab gives a device these defaults.
- Proven by: `test_lifecycle.py::test_a_player_keeps_the_defaults_of_its_device_while_the_device_is_away`

### AWAY-10 · Only the player's own protocol gives it those defaults

The Den player has only an AirPlay receiver and a Sendspin bridge on it, and the Living player has
a WiiM protocol of its own and an AirPlay receiver. Their AirPlay receivers and the bridge would
hide the player, keep it from Home Assistant and keep it private. Neither player is: both are
shown, exposed and not private, while attached, while away and after a restart.

- End-to-end: **no**: no audio protocol of the lab gives a device these defaults.
- Proven by: `test_lifecycle.py::test_only_the_own_protocol_of_a_player_gives_it_its_defaults`

## Several devices on one host

One host, such as a Raspberry Pi, can run several players behind one MAC and one IP address.
They stay separate players: an identifier of their own tells them apart, or each of them stands
for a device of its own.

### HOST-1 · Two receivers of one kind on one host are two players

A Raspberry Pi runs two AirPlay receivers, Kitchen and Den, that share its MAC and IP address but
each have an AirPlay id of their own. Kitchen is found and gets a player, then goes away. While it
is away, Den is found: it gets a player of its own instead of Kitchen's. When Kitchen comes back
it returns to its own player. There are two players, each available with its one output. The
same holds for two DLNA renderers that each have a UUID of their own.

- End-to-end: **possible with** two shairport-sync receivers in one container.
- Proven by: `test_identity.py::test_receivers_on_one_host_stay_separate_players`

### HOST-2 · A Squeezelite and a Snapcast client on one host are two players

A Raspberry Pi runs a Squeezelite player, Kitchen, and a Snapcast client, Den. Each stands for a
device of its own and neither ranks above the other, so they are two players. The user can not
merge them either: the merge is refused.

- End-to-end: **possible with** one container that runs Squeezelite and a Snapcast client.
- Proven by:
  - `test_identity.py::test_two_native_protocols_of_one_rank_on_one_host_stay_two_players`
  - `test_identity.py::test_two_players_of_one_rank_on_one_host_can_not_be_merged`

### HOST-3 · A Raumfeld room is the player of its own renderer only

A Raumfeld room, Living, is controlled through its host and reports the DLNA renderer that runs
on that host. Two more speakers of the room, One M Living and One S Living, each have a DLNA
renderer of their own. The room and the host's renderer are one player, Living, named by Raumfeld,
with Raumfeld and DLNA as outputs. Each other renderer is a DLNA player of its own. That holds in
every discovery order, and after a restart with everything found in the reverse order.

- End-to-end: **no**: there is no virtual Raumfeld system.
- Proven by: `test_identity.py::test_a_room_is_the_player_of_its_own_renderer_only`

## Zones of one chassis

A multi-zone amplifier or AV receiver has one network interface: all its zones share one MAC
address and one IP address. Each zone is a player of its own. The receivers of the chassis
(AirPlay, DLNA) either report the zone they play on or report no zone at all. Most examples use a
receiver with a main zone, Living, and a second zone, Patio, plus one AirPlay receiver that knows
nothing of zones.

### ZONE-1 · Each zone of a chassis is a player of its own

The provider of a two-zone amplifier finds Zone 1 and Zone 2, and the AirPlay provider finds a
receiver for each zone that reports its zone. There are two players, each available with its own
zone and the AirPlay receiver of that zone as its outputs. That holds also when the receiver of
Zone 2 is found before the one of Zone 1.

- End-to-end: **no**: there is no virtual multi-zone device.
- Proven by: `test_identity.py::test_zones_of_one_chassis_are_one_player_each`

### ZONE-2 · A receiver that reports no zone plays on the main zone

The AirPlay receiver of the chassis reports no zone. It becomes an output of the main zone's
player, never of the second zone's, whichever of the three is found first. After a restart, with
everything found in the reverse order, each protocol is on the same player again and there are
still exactly two players.

- End-to-end: **no**: there is no virtual multi-zone device.
- Proven by: `test_identity.py::test_a_receiver_without_a_zone_lands_on_the_main_zone`

### ZONE-3 · A receiver found while the main zone is away still belongs to it

The second zone and the main zone are found, then the main zone goes away, or the user disables
its player. Now the AirPlay receiver is found. It does not land on the second zone, which is
there: while the main zone is away it becomes an output of the main zone's player, and while that
player is disabled it waits. Once the main zone is back, or enabled, the receiver is an output of
the main zone.

- End-to-end: **no**: there is no virtual multi-zone device.
- Proven by: `test_identity.py::test_a_receiver_first_seen_without_its_main_zone_still_lands_on_it`

### ZONE-4 · A receiver found again after a removal lands on the main zone again

The AirPlay provider forgets the receiver and finds it again: it is an output of the main zone
again. The user removes the main zone's player: the main zone comes back as a new player with the
receiver as its output, and the second zone keeps its player.

- End-to-end: **no**: there is no virtual multi-zone device.
- Proven by: `test_identity.py::test_a_receiver_lands_on_the_main_zone_again_after_a_removal`

### ZONE-5 · An upgraded install keeps a receiver where it had it

An older version, which did not tell zones apart, stored the AirPlay receiver on the second zone.
After the upgrade it stays there: whether that is the right zone is not known, so nothing moves
it by itself. The main zone and the second zone keep their ids.

- End-to-end: **no**: the lab has no settings of an older version.
- Proven by: `test_identity.py::test_a_receiver_stored_on_another_zone_stays_there`

### ZONE-6 · Zone players are never merged by themselves

Zone players share the MAC and IP address of their chassis, yet none is ever merged into
another. A zone joins the player of the chassis's AirPlay receiver only when it is the main zone,
also when the receiver is found first. That holds in every discovery order, and no player is ever
announced as removed.

- End-to-end: **no**: there is no virtual multi-zone device.
- Proven by: `test_identity.py::test_unused_zone_players_are_never_merged_by_themselves`

### ZONE-7 · The user puts a receiver that joined the wrong zone right

A chassis where every zone has an AirPlay receiver of its own, and the receivers report no zone:
the first receiver found joins the main zone, the other one becomes a player of its own. Here the
Patio receiver is found before the Living receiver, in every order with the zones, and lands on
Living. The user takes it
off Living and merges its new player into Patio, then merges the Living receiver's player into
Living. After a restart, in either order, every protocol is where the user put it, there are two
players, and no automatic merge undoes it.

- End-to-end: **no**: there is no virtual multi-zone device.
- Proven by: `test_identity.py::test_a_receiver_on_the_wrong_zone_is_put_right_by_hand`

## A speaker and its Home Assistant media player

Home Assistant has a media player for many speakers that also have a provider of their vendor,
here Sonos. That media player, the twin, is a second protocol of the same device. While the
vendor's protocol is switched on, it stands for the speaker and the twin is a control: it is no
output, takes no command and does not make the player available. Most examples use the Kitchen
speaker with its vendor protocol, its twin and its AirPlay receiver.

### TWIN-1 · The twin joins the speaker's player as a control

The vendor protocol, the twin and the AirPlay receiver are found, in any order. They are one
player, named by Sonos, with Sonos and AirPlay as outputs. The vendor protocol is its own
protocol, the twin a control, AirPlay an audio protocol. After a restart, with everything found
in the reverse order, they are the same one player with the same roles.

- End-to-end: **no**: the lab has no Home Assistant.
- Proven by: `test_identity.py::test_a_home_assistant_twin_is_a_control_of_the_vendor_player`

### TWIN-2 · A speaker that was a player through Home Assistant keeps it when its vendor arrives

Only the twin is found, so it is a player, and the user renames it Kitchen Speaker. Then the
vendor protocol is found. It joins that player: same id and name, now named by Sonos with Sonos as
its output, and the twin becomes a control.

- End-to-end: **no**: the lab has no Home Assistant.
- Proven by: `test_identity.py::test_a_vendor_protocol_joins_the_player_of_its_home_assistant_twin`

### TWIN-3 · The twin stays a control while the vendor protocol is away

The vendor protocol goes away. The player stays, unavailable, since only the twin reaches the
speaker, and the roles stay as they were. Once the vendor protocol is back it is the same player
with the same roles. The same player and roles are there after the vendor's provider reloads,
and after the user disables the player and enables it again.

- End-to-end: **no**: the lab has no Home Assistant.
- Proven by: `test_identity.py::test_the_twin_stays_a_control_while_the_vendor_protocol_is_gone`

### TWIN-4 · The twin stands for the speaker once the vendor protocol is gone for good

The vendor's provider forgets the speaker, or the user removes that provider. The twin becomes
the own protocol of the same player: available, under the same id, now with the twin's name,
named by Home Assistant and with the twin as its output.

- End-to-end: **no**: the lab has no Home Assistant.
- Proven by: `test_identity.py::test_the_twin_is_the_native_protocol_once_the_vendor_protocol_is_removed`

### TWIN-5 · The twin stands for the speaker while the user has the vendor protocol switched off

The vendor protocol is away, and the user disables the vendor's provider or switches the vendor
protocol off as an output. The twin stands for the speaker again, and the player is available
through it, also after a restart, and plays through it. A provider that is only not loaded, as
after a failed load, leaves the twin a control until the user disables it. Once the user switches
the vendor protocol on again, it ranks above the twin at once: the twin is a control again, and
the player is unavailable while the vendor protocol is away.

- End-to-end: **no**: the lab has no Home Assistant.
- Proven by: `test_identity.py::test_the_twin_is_native_while_the_user_has_the_vendor_protocol_switched_off`

### TWIN-6 · A disabled player has the settings of the protocol that ranks highest

The user disables the player. A setting of the device then reads as the vendor protocol stores
it, not as the twin does, also when the vendor protocol was first found while the player was
already disabled: it does not attach then, and the twin stays stored as the own protocol. While
the user has the vendor's provider disabled, the setting reads as the twin stores it.

- End-to-end: **no**: the lab has no Home Assistant.
- Proven by:
  - `test_identity.py::test_the_settings_of_a_disabled_player_are_those_of_its_vendor_protocol`
  - `test_identity.py::test_the_settings_of_a_disabled_player_follow_the_protocol_switched_on`

### TWIN-7 · A vendor protocol that fails its first setup leaves the twin as it was

Only the twin is found, so it is the player. Then the vendor protocol is found, but its setup
fails, or is called off halfway. The player stays as it was: available, under the same id and
name, with the twin as its own protocol and only output, and it plays through the twin.

- End-to-end: **no**: the lab has no Home Assistant.
- Proven by: `test_identity.py::test_a_vendor_protocol_whose_first_attach_fails_leaves_the_twin_native`

### TWIN-8 · A twin object its provider announces again is not set up again

The Home Assistant provider announces the same twin object again while it is a control. The twin
is not set up a second time, and the roles stay as they were.

- End-to-end: **no**: the lab has no Home Assistant.
- Proven by: `test_identity.py::test_the_twin_object_attached_again_keeps_its_role_and_its_hooks_quiet`

### TWIN-9 · A twin that returns under a new entity id goes back to its own player

An older version stored the speaker and its twin as two players, the twin's named Kitchen (HA).
While the server is down, the user renames the Home Assistant entity. After the start the twin
comes back under the new entity id. It goes to the twin's own player, and the speaker's player
keeps only its Sonos output. The new entity takes over the record of the old one: nothing is
stored for the old entity any more, the new one names the twin's player, and that player keeps
its name. That holds whichever of the two players is listed first.

- End-to-end: **no**: the lab has no Home Assistant.
- Proven by: `test_identity.py::test_a_twin_under_a_new_entity_id_returns_to_its_own_player`

### TWIN-10 · A removed speaker player comes back as one player with the same roles

The user removes the speaker's player. It comes back as one player under a new id, with the
vendor protocol as its own protocol and the twin as a control.

- End-to-end: **no**: the lab has no Home Assistant.
- Proven by: `test_identity.py::test_a_removed_player_of_a_speaker_comes_back_with_the_same_roles`

### TWIN-11 · A speaker and a named twin stored as two players stay two

An older version stored the speaker and its twin as two players, and the user named the twin's
player Kitchen (HA), and each player has a setting and a saved queue of its own. After the upgrade
both are there, under their ids and names, each with its own setting and saved queue, and each
with its protocol as its own protocol. An unused twin player is
merged instead (MERGE-5).

- End-to-end: **no**: the lab has no Home Assistant.
- Proven by: `test_identity.py::test_a_speaker_and_its_twin_stored_as_two_players_stay_two_players`

### TWIN-12 · A control or output the user picked for the twin is passed over once it is a control

While the twin stood for the speaker, the user picked it as the volume and mute control, or as
the preferred output. Then the vendor protocol arrives and the twin becomes a control. Volume and
mute go where they go when nothing is picked: to the vendor protocol, which is also the volume
control of its own output, or to the AirPlay receiver when the vendor protocol has no volume of
its own. The player plays through the vendor protocol, and the twin gets no command.

- End-to-end: **no**: the lab has no Home Assistant.
- Proven by: `test_identity.py::test_a_choice_that_names_the_demoted_twin_sends_it_nothing`

### TWIN-13 · Commands to the player never reach the twin

A user who may only use this player sets its volume and powers it on, and a user who may only use
another player is refused. The player plays. Then the vendor protocol goes away, and play fails
as unavailable. The vendor protocol got the volume and the play; the twin got no command at all.

- End-to-end: **no**: the lab has no Home Assistant.
- Proven by: `test_identity.py::test_a_command_to_the_player_never_reaches_the_twin`

## A device that changes how it is identified

A device can show up under another protocol, or its provider can change how it identifies it.
The device keeps its player.

### IDENT-1 · A device re-flashed to another firmware keeps its player

An ESP32 speaker runs Squeezelite, and the user names its player Kitchen Speaker. It is re-flashed
to Sendspin: the Squeezelite player goes away and a Sendspin client with the same MAC address
appears. The client joins the same player, which keeps its id and name, is named by Sendspin, and
has Sendspin as an available output.

- End-to-end: **possible with** a virtual player whose container switches from Squeezelite to a
  Sendspin client on one MAC address.
- Proven by: `test_identity.py::test_reflashed_device_keeps_its_player`

### IDENT-2 · A device its provider identifies anew keeps its player and settings

The Sendspin provider knew the ESP32 speaker by its MAC address and now knows it by a key, so a
protocol with a new id appears with the same MAC address. It joins the player, which keeps its id
and the name Kitchen Speaker. The new protocol has the settings of the old one, and nothing of the
old protocol is stored any more.

- End-to-end: **no**: it needs a provider that changes how it identifies a device.
- Proven by: `test_identity.py::test_device_with_a_new_protocol_id_keeps_its_player_and_settings`

### IDENT-3 · A protocol that changes its role stays on its player

A Cast TV is found as an audio receiver and later as the device's own protocol, or the other way
round. It stays on its player under the same id, and no player is added or removed.

- End-to-end: **no**: the role comes from how the provider detects the device.
- Proven by: `test_identity.py::test_role_flip_of_a_protocol_keeps_its_player`

## Duplicates, merging and taking a protocol off

A device can end up as two players, when its second protocol is found before what ties it to the
first is known. Two players of one device merge by themselves only while the younger one is
unused; otherwise the user merges them. The player that goes hands its protocols to the one that
stays, which answers to the id of the player that went. A protocol on the wrong player can be
taken off it.

### MERGE-1 · Two players of one device merge into the older one while the younger is unused

A TV's AirPlay and Cast receivers report nothing in common at first, so they are two players, the
AirPlay one the older. Later the AirPlay receiver is found again and now reports the TV's IP
address, which ties it to the Cast receiver. The younger player is unused, so it is merged into
the older: one player under the older id, with AirPlay and Chromecast as outputs. The younger id
keeps answering for it.

- End-to-end: **no**: it needs identifiers that arrive late.
- Proven by: `test_identity.py::test_unused_duplicate_is_merged_into_the_older_player`

### MERGE-2 · A younger duplicate that has been used stays a player of its own

Two TVs, Living TV and Bedroom TV, are two players each. The younger player of Bedroom TV has
played, or the user renamed it. Once the receivers of each TV are tied together, the two players
of Living TV merge, and the younger one is announced as removed. The two players of Bedroom TV
stay apart.

- End-to-end: **no**: it needs identifiers that arrive late.
- Proven by: `test_identity.py::test_used_duplicate_is_not_merged`

### MERGE-3 · The player that goes in a merge is announced as removed

The user merges the player of a TV's Cast receiver into the player of its AirPlay receiver.
Clients are told the Cast receiver's player is removed.

- End-to-end: **possible with** two players that may be one, such as an AirPlay and a DLNA
  receiver.
- Proven by: `test_lifecycle.py::test_the_player_that_goes_in_a_merge_is_announced_as_removed`

### MERGE-4 · The user merges a speaker and its twin

An older version stored the speaker and its twin as two players. The user merges them, keeping
either one, also while the vendor protocol is away. One player is left: it carries the vendor
protocol as its own protocol and the twin as a control, and the id of the other player answers
for it.

- End-to-end: **no**: the lab has no Home Assistant.
- Proven by: `test_identity.py::test_a_speaker_and_its_twin_stored_apart_can_be_merged_by_hand`

### MERGE-5 · An unused twin player goes into the speaker's player by itself

An older version stored the speaker and its twin as two players. When the user never named the
twin's player, it goes into the speaker's player by itself, as a control. When the user named it,
both players stay.

- End-to-end: **no**: the lab has no Home Assistant.
- Proven by: `test_identity.py::test_a_twin_stored_apart_is_merged_by_itself_only_while_unused`

### MERGE-6 · A protocol taken off the speaker's player gets a player of its own

The user takes the vendor protocol, or the twin, off the speaker's player. Each is then the own
protocol of a player of its own, and both players are available. Nothing merges them back by
itself, also not after a restart.

- End-to-end: **no**: the lab has no Home Assistant.
- Proven by: `test_identity.py::test_a_protocol_unlinked_from_a_speaker_stands_on_its_own`

### MERGE-7 · Disabling and enabling the vendor's provider leaves the players as they are

The user merged the speaker and its twin, and took the AirPlay receiver off to a player of its
own. Then the user disables the vendor's provider and enables it again. Both times there are
exactly these two players, the twin stays on the speaker's player, and no player is added or
removed. While the provider is disabled the twin stands for the speaker; once it is enabled the
twin is a control again.

- End-to-end: **no**: the lab has no Home Assistant.
- Proven by: `test_identity.py::test_a_role_change_neither_merges_nor_splits_players`

### MERGE-8 · The user merges two players of one device

A TV's AirPlay and Cast receivers ended up as two players, and the user merges the Cast one into
the AirPlay one. The kept player carries both receivers, also after a restart. Where both
players have a name or a setting, the kept player's stays; a name, a setting, DSP and queue
settings only the other one had are carried over, and nothing of it is stored under its own id
any more. A group that listed it, and a user limited to it, name the kept player from then on.
When it played, its device is stopped and its saved queue goes.

- End-to-end: **possible with** two players that may be one, such as an AirPlay and a DLNA
  receiver.
- Proven by:
  - `tests/controllers/players/test_merge.py::test_merged_player_stays_one_player_after_a_restart`
  - `tests/controllers/players/test_merge.py::test_merge_keeps_the_name_and_settings_of_the_kept_player`
  - `tests/controllers/players/test_merge.py::test_merge_carries_over_what_only_the_absorbed_player_has`
  - `tests/controllers/players/test_merge.py::test_merge_moves_group_memberships_and_user_filters`
  - `tests/controllers/players/test_merge.py::test_merge_of_a_player_that_plays_stops_its_queue`

### MERGE-9 · A merged player's old id keeps working for clients

After the merge of MERGE-8 the kept player lists the id of the player that went as a legacy id.
Reading the player or its queue by that id gives the kept player, also after a further merge
into a third player, and a later merge can name either player, the one that goes or the one that
stays, by such an id, also through the API command. Player and queue commands by the old id
reach the kept player. Through the websocket and the HTTP API, a user limited to a player reads
it by its legacy id and moves its queue by it; a user without access to it is refused by that id
too.

- End-to-end: **possible with** two players that may be one, such as an AirPlay and a DLNA
  receiver.
- Proven by:
  - `tests/controllers/players/test_merge.py::test_merged_player_carries_both_protocols_and_answers_to_the_absorbed_id`
  - `tests/controllers/players/test_merge.py::test_chain_of_merges_keeps_every_absorbed_id_resolving`
  - `tests/controllers/players/test_merge.py::test_merge_takes_legacy_ids`
  - `tests/controllers/players/test_merge.py::test_merge_command_takes_legacy_ids`
  - `tests/controllers/players/test_merge.py::test_queue_commands_take_the_id_of_a_merged_player`
  - `tests/controllers/players/test_merge.py::test_player_commands_take_the_id_of_a_merged_player`
  - `tests/controllers/webserver/test_legacy_player_ids.py::test_player_is_read_by_its_legacy_id`
  - `tests/controllers/webserver/test_legacy_player_ids.py::test_queue_is_transferred_from_a_legacy_id`
  - `tests/controllers/webserver/test_legacy_player_ids.py::test_transfer_from_a_legacy_id_needs_access_to_its_player`

### MERGE-10 · A receiver taken off a player gets one of its own, and can be merged back

An AirPlay receiver that reports only the address of its host joined the Kitchen speaker's
player. The user takes it off: it gets a new player, Studio, available with its AirPlay output,
and clients are told about it and its queue. Kitchen keeps its id, name and own output, and an
output choice that named the receiver falls back to its default. Found again, also after a
restart, the receiver goes to its new player. When the user merges the two players, the receiver
is back on Kitchen.

- End-to-end: **possible with** an AirPlay receiver on the host of a native player, such as
  shairport-sync next to Squeezelite.
- Proven by:
  - `tests/controllers/players/test_unlink.py::test_unlinked_protocol_gets_a_player_of_its_own`
  - `tests/controllers/players/test_unlink.py::test_unlinked_protocol_stays_on_its_player`
  - `tests/controllers/players/test_unlink.py::test_unlink_then_merge_back_puts_the_protocol_back`

### MERGE-11 · A disabled player that goes in a merge is announced as removed

The user disables the player of a TV's Cast receiver, which played, and merges it into the
player of its AirPlay receiver. Clients are told once that the Cast receiver's player is removed,
its saved queue is gone, and so is the DJ of the AI radio on its queue.

- End-to-end: **possible with** two players that may be one, such as an AirPlay and a DLNA
  receiver, one of them disabled.
- Proven by:
  - `test_lifecycle.py::test_a_disabled_player_that_goes_in_a_merge_is_announced_as_removed`
  - `tests/providers/ai_radio/test_queue_dj_player_lifecycle.py::test_a_dj_goes_with_a_disabled_player_that_goes_in_a_merge`

## Restart and upgrade

Players are stored. After a restart a player is back with its id, name and settings, unavailable
until its devices are found again (AWAY-1). An older version stored a player and its own protocol
as one record, and kept universal players over the receivers of a device; its settings are
converted at the first start.

### RESTART-1 · A player of audio receivers only comes back after a restart

The Den TV is one player of its AirPlay and Cast receivers, and the user named it Den TV. After a
restart it is back with its id, its name and both outputs, available, and it plays.

- End-to-end: **yes**. Rename a shairport-sync receiver, restart the server.
- Proven by: `test_lifecycle.py::test_protocol_only_player_is_restored_with_its_id_after_a_restart`

### RESTART-2 · A universal player of an older version comes back as the same player

An older version stored the Den TV as a universal player over its AirPlay and Cast receivers.
After the upgrade it is the same player: its id, the name Den TV, both receivers as outputs, and
available. That holds also when the stored record lacks its list of receivers.

- End-to-end: **no**: the lab has no settings of an older version.
- Proven by: `test_lifecycle.py::test_pre_split_universal_player_config_comes_back_as_the_same_player`

### RESTART-3 · A player whose own protocol was taken off starts as it was

The Living player keeps its id from an older version. Its WiiM protocol was taken off to a player
of its own, and its AirPlay receiver stayed. At the next start it is there with its id, the name
Living Room, its queue settings and its AirPlay receiver, and the WiiM protocol is on its own
player.

- End-to-end: **no**: the lab has no settings of an older version.
- Proven by: `test_lifecycle.py::test_a_player_whose_own_protocol_was_unlinked_stays_at_the_next_start`

### RESTART-4 · What an older version stored in between goes at the next start

The user ran an older version on these settings in between, which stored its own records of the
players. At the next start exactly this version's players are left. Living and Den keep their
ids and protocols, a TV that only the older version saw is found again as a player, and a browser
that connects afterwards is a player under its client id.

- End-to-end: **no**: the lab has no settings of an older version.
- Proven by: `test_lifecycle.py::test_what_an_older_version_stored_goes_at_the_next_start`

### RESTART-5 · The cleanup at start keeps every player of this version

The start removes records that an older version stored, never one of this version. The players of
a twin that is a control (found after its speaker, or kept by a merge), of the zones of a chassis,
and of a receiver the user moved to another zone keep their records at the start, and after a
restart.

- End-to-end: **no**: it is about records an older version stores.
- Proven by: `test_identity.py::test_the_cleanup_at_start_keeps_every_player_of_this_version`

### RESTART-6 · A stored player of a provider that is not installed says so

The settings hold the Patio player of a provider this install does not have, and the Kitchen
player of the Sonos provider, which is installed but not set up. After the start both are
listed, unavailable. Patio gives as its setup reason that its provider is not installed; Kitchen
gives no setup reason.

- End-to-end: **no**: the lab boots fresh, without stored players.
- Proven by: `test_lifecycle.py::test_a_stored_player_whose_provider_is_not_installed_says_so`

### RESTART-7 · The settings of a player whose provider is not installed can be opened

The settings hold the Patio player with two protocols, of two providers this install does not
have. The settings of Patio open, and it is in the list of player settings. Its outputs are
named after the domains of those providers, for lack of a name.

- End-to-end: **no**: the lab boots fresh, without stored players.
- Proven by: `test_lifecycle.py::test_the_settings_of_a_player_whose_provider_is_not_installed_can_be_read`

### RESTART-8 · A provider installed while the server runs counts as installed

The settings hold the Patio player of a provider this install does not have: Patio gives as its
setup reason that its provider is not installed. The provider is installed from a second
providers folder while the server runs. Without a restart Patio gives no setup reason any more,
as the player of a shipped provider that is not set up, and its settings name the provider. Once
the provider is removed again, Patio says again that its provider is not installed.

- End-to-end: **no**: nothing installs a provider into a running server yet.
- Proven by: `test_lifecycle.py::test_a_stored_player_follows_a_provider_installed_or_removed_while_the_server_runs`

## Removing and disabling

What goes when the user removes a provider, removes a player or disables one. Clients are told
a player is removed, with no data besides its id, only when it is gone for good: removed or merged
into another player (MERGE-3), or a browser player that was only seen (WEB-4); a device that goes
away is no removal (AWAY-1), and neither is disabling a player (REMOVE-4).

### REMOVE-1 · A player outlives the removal of its vendor's provider on its other protocols

The Living player has a WiiM protocol of its own, an AirPlay receiver and a Sendspin client. The
user removes the WiiM provider. The player stays: same id and name, available, now named by
AirPlay, with AirPlay and Sendspin as outputs. No removal is announced.

- End-to-end: **possible with** a host that runs a native player and an AirPlay receiver, such as
  Squeezelite and shairport-sync.
- Proven by: `test_lifecycle.py::test_player_outlives_its_removed_provider_on_its_other_protocols`

### REMOVE-2 · A player with nothing else left goes with its provider

The Living player has only its WiiM protocol. When the user removes the WiiM provider, the player,
its queue and its settings go, and clients are told it is removed.

- End-to-end: **yes**. Remove the provider of a virtual player that has only that protocol.
- Proven by: `test_lifecycle.py::test_player_without_other_protocols_goes_with_its_removed_provider`

### REMOVE-3 · A removed player whose device is still there comes back with its defaults

The user named the Living player Kitchen, then removes it. Clients are told it is removed. Its
provider finds the device again, and clients are told about a player added under a new
id, available, with the default name Living.

- End-to-end: **yes**. Remove a virtual player whose container keeps running.
- Proven by:
  - `test_lifecycle.py::test_removed_player_of_a_present_device_comes_back_as_new`
  - `test_lifecycle.py::test_a_player_the_user_removes_is_announced_as_removed`

### REMOVE-4 · A disabled player leaves the players and comes back with its id when enabled

The Living player played a track. The user disables it: clients get a last update that it is
disabled and unavailable, also when its power control just switched it off, not that it is
removed; it is no player anymore, and the settings still list it, disabled. The user enables it again: clients are told it is added under the same id, available,
with its queue. A DJ of the AI radio on its queue is still there.

- End-to-end: **yes**. Disable a virtual player and enable it again.
- Proven by:
  - `test_lifecycle.py::test_disabling_a_player_takes_it_out_and_enabling_brings_it_back`
  - `test_lifecycle.py::test_the_last_update_of_a_disabled_player_says_it_is_disabled_and_unavailable`
  - `tests/providers/ai_radio/test_queue_dj_player_lifecycle.py::test_a_dj_stays_on_the_queue_of_a_player_that_is_disabled_and_enabled`

### REMOVE-5 · "Remove all unavailable players" removes only players nothing finds

The user asks the settings to remove all unavailable players. Removed, and named in the answer,
are the players no running provider finds a device of, and a stored player without protocols.
Kept are a player whose device is there but awaits its setup, and a player of a provider that is
not loaded. Right after a start, or after a provider loaded, the request is refused while the
providers still find their devices. A provider that loaded a moment ago holds back its own
players and the stored players without protocols, which any provider may find again, until its
grace ends; the players of the other providers can be removed meanwhile. A dry run names what
would go and changes nothing.

- End-to-end: **yes**. Stop the container of a virtual player, wait past the start, and remove
  all unavailable players.
- Proven by:
  - `tests/controllers/players/test_attach_detach.py::test_remove_unavailable_removes_the_players_whose_devices_are_gone`
  - `tests/controllers/players/test_attach_detach.py::test_remove_unavailable_keeps_a_player_whose_device_needs_setup`
  - `tests/controllers/players/test_attach_detach.py::test_remove_unavailable_is_refused_while_the_providers_find_their_devices`
  - `tests/controllers/players/test_attach_detach.py::test_remove_unavailable_waits_only_for_the_players_of_a_provider_that_just_loaded`
  - `tests/controllers/players/test_attach_detach.py::test_dry_run_of_remove_unavailable_names_what_the_command_removes`

### REMOVE-6 · A removed group is gone for good

The user removes the sync group or the universal group Group Z. Its player, its stored records
and, for a universal group, its stream routes go, and it does not come back when its provider
looks for groups again.

- End-to-end: **yes**. Remove a sync group of two Squeezelite players.
- Proven by:
  - `tests/providers/sync_group/test_lifecycle.py::test_removing_a_group_forgets_it`
  - `tests/providers/universal_group/test_lifecycle.py::test_removing_a_group_forgets_it`

### REMOVE-7 · A player that is not there is announced once when it is removed

The Living player played and is then disabled, or its provider is unloaded, or its speaker is
away and the user removes all unavailable players; or a browser player that played is away. The
user removes it: clients are told once that it is removed, its record and its saved queue are
gone, and so is the DJ of the AI radio on the queue of the disabled one.

- End-to-end: **yes**. Disable a virtual player that played, then remove it.
- Proven by:
  - `test_lifecycle.py::test_a_player_that_is_not_there_is_announced_once_when_it_is_removed`
  - `tests/providers/ai_radio/test_queue_dj_player_lifecycle.py::test_a_dj_goes_with_a_disabled_player_that_is_removed`

## Browser and app players

A browser or app player has the id of its client and is the same player in every session. Until
it is used, nothing of it is stored and it exists only while it is connected. Once it is used (the
user customised it, it played, or a plugin keeps it for something the user set up), it is stored
and stays like the player of a device: unavailable while its client is away. The examples use a
browser connected through Sendspin.

### WEB-1 · A browser player exists while it is connected and leaves nothing stored

A browser connects: a player appears, and nothing of it is stored. The page reloads: the new
session takes over the same player, with the volume the new session reports, and plays; the old
session gets no command. The browser disconnects: the player is gone, and still nothing is
stored. After a restart there is no player and no stored player.

- End-to-end: **possible with** a Sendspin client that connects as a browser player.
- Proven by: `test_lifecycle.py::test_session_only_web_player_exists_while_attached`

### WEB-2 · A customised browser player stays listed, unavailable, while its browser is away

The user renames a browser player and plays on it. The browser disconnects: the player stays,
unavailable, with its name, and clients are told it is updated, not removed. After a restart it is
still listed, unavailable, in the players and in the settings. When the browser connects again,
the player is available under the same id, with its name and its queue.

- End-to-end: **possible with** a Sendspin client that connects as a browser player.
- Proven by: `test_lifecycle.py::test_a_customised_browser_player_stays_listed_while_its_browser_is_away`

### WEB-3 · A browser player has the same id in every session

A browser connects, disconnects and connects again, and after a restart connects once more. Each
time its player has the id of its client, and clients are told about it as added under that id.

- End-to-end: **possible with** a Sendspin client that connects as a browser player.
- Proven by: `test_lifecycle.py::test_session_only_player_has_the_same_id_in_every_session`

### WEB-4 · A browser player that was only seen is removed when its browser disconnects

A browser connects and disconnects without playing; nobody changed its player. Clients are told
the player is removed, and the settings do not list it. The browser connects again: clients are
told the player is added under the same id.

- End-to-end: **possible with** a Sendspin client that connects as a browser player.
- Proven by: `test_lifecycle.py::test_a_browser_player_that_was_only_seen_is_removed_when_its_client_disconnects`

### WEB-5 · A browser player stored by an older version keeps its id

An older version stored a browser player. When the user had named it, it is a player after the
upgrade, unavailable, and the settings list it; otherwise it is no player and nothing is listed
until it connects. When it connects it is an available player under its client id, with the
stored name, or else its default name.

- End-to-end: **no**: the lab has no settings of an older version.
- Proven by: `test_lifecycle.py::test_pre_split_session_only_player_comes_back_with_its_id`

### WEB-6 · A browser player the user disabled stays disabled after an upgrade

An older version stored a browser player that the user disabled. After the upgrade, when it
connects, no player appears and no player is announced as added; the settings list it, disabled.

- End-to-end: **no**: the lab has no settings of an older version.
- Proven by: `test_lifecycle.py::test_pre_split_session_only_player_the_user_disabled_stays_disabled`

### WEB-7 · A browser player is offered to a plugin only while it is the plugin's choice

A plugin builds its settings with the server's player pickers: one player, or the players it
connects to. With the Living speaker and a browser connected, the pickers offer Living only. A
browser player that is already the plugin's choice stays offered next to it, so that saving the
form keeps it.

- End-to-end: **possible with** a Sendspin client that connects as a browser player, and the
  settings form of a plugin that uses these pickers.
- Proven by: `test_lifecycle.py::test_a_browser_player_is_offered_to_a_plugin_only_while_it_is_its_choice`

### WEB-8 · The settings list every stored player and every connected player without a record

The settings list the Living speaker, a browser player the user named whose browser is away, and
an app player that is connected and was never changed.

- End-to-end: **possible with** a Sendspin client that connects as a browser player.
- Proven by: `test_lifecycle.py::test_the_settings_list_every_stored_player_and_every_connected_one`

### WEB-9 · A kept browser player keeps the defaults of its browser while it is away

A browser player is hidden in the UI, not exposed to Home Assistant and private. The user renames
it, and the browser disconnects: the player is still hidden, not exposed and private, with the
same device info and icon, also after a restart and when the browser connects again.

- End-to-end: **possible with** a Sendspin client that connects as a browser player.
- Proven by: `test_lifecycle.py::test_a_kept_browser_player_keeps_the_defaults_of_its_client_while_away`

### WEB-10 · What the user set on a browser player wins over what its browser gives it

The user shows a browser player in the UI and exposes it to Home Assistant. The browser
disconnects: the player stays shown and exposed, also after a restart, and is still private.

- End-to-end: **possible with** a Sendspin client that connects as a browser player.
- Proven by: `test_lifecycle.py::test_a_value_the_user_set_on_a_kept_browser_player_wins_over_its_client`

### WEB-11 · A browser player with a DJ stays while its browser is away

The user switches on an AI radio DJ for the queue of a browser player that has not played yet.
The browser disconnects: the player stays, unavailable, and is not removed. When the browser
connects again the queue still has its DJ. Switching the DJ off again does not undo that.

- End-to-end: **possible with** a Sendspin client that connects as a browser player and the AI
  radio plugin.
- Proven by: `tests/providers/ai_radio/test_queue_dj_player_lifecycle.py::test_a_dj_keeps_a_browser_player_whose_client_disconnects`

### WEB-12 · A browser player that played stays listed, unavailable, while its browser is away

A browser plays and disconnects right after, before its queue was saved. Clients are told its
player is updated, unavailable, not removed, and the settings list it, also after a restart. When
the browser connects again, the player is available under the same id, with its queue.

- End-to-end: **possible with** a Sendspin client that connects as a browser player.
- Proven by: `test_lifecycle.py::test_a_browser_player_that_played_stays_listed_while_its_browser_is_away`

### WEB-13 · A browser player whose queue was cleared is removed when its browser disconnects

A browser plays, and its queue is cleared. When the browser disconnects, there is nothing to keep:
clients are told its player is removed, the settings do not list it, and nothing of its queue
stays saved.

- End-to-end: **possible with** a Sendspin client that connects as a browser player.
- Proven by: `test_lifecycle.py::test_a_browser_player_whose_queue_was_cleared_is_removed_when_its_client_disconnects`

### WEB-14 · A browser player an older version stored without changes keeps its saved queue

An older version stored a browser player the user never changed, and the queue it played. After
the upgrade it is no player and the settings do not list it, but its queue is still saved. When
the browser connects, its player has that queue under the same id, and when the browser
disconnects again, the player stays, unavailable.

- End-to-end: **no**: the lab has no settings of an older version.
- Proven by: `test_lifecycle.py::test_a_pre_split_browser_player_with_a_saved_queue_is_kept_once_it_connects`

### WEB-15 · A browser player an older version stored is hidden, not exposed and private from the start

An older version stored three browser players: one the user renamed, one the user never touched,
and one the user exposed to Home Assistant. After the upgrade, before any browser connected, the
untouched one is gone, the renamed one is listed, unavailable, hidden, not exposed and private,
and the exposed one is listed the same but exposed. When their browsers connect, nothing of that
changes.

- End-to-end: **no**: the lab has no settings of an older version.
- Proven by: `test_lifecycle.py::test_pre_split_browser_players_keep_the_defaults_of_their_client_from_the_start`

### WEB-16 · A stored browser player with part of its defaults has the generic rest

The record of a browser player holds only that it is hidden, of the defaults its browser gives it.
While the browser is away, the player is hidden, exposed to Home Assistant and not private.

- End-to-end: **no**: no version stores part of these defaults.
- Proven by: `test_lifecycle.py::test_a_stored_player_with_part_of_its_defaults_has_the_generic_rest`

## Commands to a busy player

Commands to one player run one at a time: a command waits for the one before it. Moving a queue
to another player holds both players until the other one plays: a play, stop, resume, next,
previous, seek or play of either queue waits for the move. A pause and a clear of a queue take no
lock and do not wait.

### BUSY-1 · A command waits for the command that holds the player

A play takes 40 s at the Den speaker. A switch to its HDMI input sent right behind it does not
reach the speaker while the play is under way. It does once the play is done, after the play.

- End-to-end: **no**: it needs a device that takes that long over a command.
- Proven by: `test_commands.py::test_a_command_waits_for_the_command_that_holds_the_player`

### BUSY-2 · A command behind one that never returns fails as busy after 120 s

A play on the Den speaker never returns. An input switch sent behind it waits 120 s, then fails
with a busy error that names Den, and the input switch does not reach the speaker. The warning
that is logged names what the play waits for. Once the play returns, the player takes commands
again: the input switch reaches the speaker.

- End-to-end: **no**: it needs a device that never answers a command.
- Proven by: `test_commands.py::test_a_command_behind_a_command_that_never_returns_fails_as_busy`

### BUSY-3 · A stop sent to the player a queue moves to stops what the move started

The queue of Den moves to Kitchen, and Den takes 40 s to stop. A stop sent to Kitchen meanwhile
waits for the move. Once Kitchen plays the queue, the stop reaches it, and Kitchen is stopped.

- End-to-end: **no**: it needs a device that takes that long over a command.
- Proven by: `test_commands.py::test_a_stop_sent_to_the_target_of_a_transfer_stops_what_the_transfer_started`

### BUSY-4 · A queue that is still starting moves once it plays

A resume of Den takes 40 s at the speaker. The queue of Den, moved to Kitchen meanwhile, moves
once Den plays: Den stops, Kitchen plays the queue, and Den's queue is empty. Den does not go on
playing an empty queue next to Kitchen.

- End-to-end: **no**: it needs a device that takes that long over a command.
- Proven by: `test_commands.py::test_a_resume_that_is_still_starting_is_done_before_a_transfer_moves_its_queue`

### BUSY-5 · A queue moved to a player that is still starting a play replaces it after the play

A play takes 40 s at Kitchen. The queue of Den, moved to Kitchen meanwhile, waits for the play:
until it is done Kitchen's queue is the one the play started and Den plays on. Then Kitchen plays
Den's queue, and Den is stopped.

- End-to-end: **no**: it needs a device that takes that long over a command.
- Proven by: `test_commands.py::test_a_play_on_the_target_that_is_still_starting_is_done_before_a_transfer_replaces_it`

### BUSY-6 · A queue moved to a player that stays busy stays where it was

A play on Kitchen never returns. The queue of Den, moved to Kitchen, fails with a busy error that
names Kitchen after 120 s. Nothing has moved: Den plays its queue on, and Kitchen keeps the queue
of its play.

- End-to-end: **no**: it needs a device that never answers a command.
- Proven by: `test_commands.py::test_a_transfer_to_a_player_that_stays_busy_fails_and_leaves_both_as_they_were`

### BUSY-7 · A queue moved to a switched-off player holds neither player while it comes on

The Bathroom player is on a smart plug that is switched off. The queue of Den, moved to Bathroom
while Den plays, switches the plug on and waits for the device. A seek on Den meanwhile runs at
once. Once the device is back, Bathroom plays Den's queue, and Den is stopped.

- End-to-end: **possible with** a power control the lab can switch, which stops and starts the
  container of a virtual player.
- Proven by: `test_commands.py::test_a_transfer_to_a_switched_off_player_holds_neither_player_while_it_powers_on`

### BUSY-8 · A queue moved to a player that is grouped elsewhere meanwhile still plays there

The queue of Den moves to Kitchen, by a transfer or because Den leaves the group it leads with
Kitchen, and Den takes 40 s to stop. Meanwhile Kitchen joins Office's group, or Downstairs, a
group of Office and Kitchen, takes it: a sync group that starts to play another track, or a
universal group that powers on. The move still completes: Kitchen plays the moved queue and Den
is idle with an empty queue. Kitchen leaves Office's group again; the sync group plays its track
on Office; the universal group is idle. When the group takes Kitchen first and the queue moves
while Office takes 40 s to do its part, Kitchen plays the moved queue as well, and a sync group
still lists it, at the other track. That is a limit, not the intended end: Kitchen plays its own
queue while the sync group still lists it as a member.

- End-to-end: **no**: it needs a device that takes that long over a command.
- Proven by:
  - `test_commands.py::test_a_transfer_whose_target_joins_another_group_meanwhile_still_completes`
  - `test_commands.py::test_a_transfer_whose_target_a_group_takes_meanwhile_still_completes`

### BUSY-9 · A play on the player a queue moved away from waits until the other one plays

Kitchen takes 40 s to start the queue moved there from Den. Den has stopped and its queue is empty
by then, but a play on Den sent meanwhile waits until Kitchen plays, and then plays on Den.

- End-to-end: **no**: it needs a device that takes that long over a command.
- Proven by: `test_commands.py::test_a_play_on_the_source_waits_until_the_target_of_a_transfer_plays`

### BUSY-10 · A hand-over whose new leader joins another group meanwhile still completes

Den leads Kitchen and Office, plays its own queue and leaves its group: its queue moves to
Kitchen, the new leader, which takes 40 s to regroup Office, or to start the queue. Meanwhile
Kitchen joins Patio's group. The hand-over still completes, and Den is idle with an empty queue.
When Kitchen joined during the regroup, it leaves Patio's group again, leads Office and plays the
moved queue. When it joined during the start, Kitchen's speaker plays the moved queue while
Kitchen is listed in Patio's group. That is a limit, not the intended end: Kitchen plays its own
queue while it is listed as a member of Patio's group.

- End-to-end: **no**: it needs a device that takes that long over a command.
- Proven by: `test_commands.py::test_a_hand_over_whose_new_leader_joins_another_group_meanwhile_completes`

### BUSY-11 · A play request that takes longer than a minute to look up is given up and changes nothing

Kitchen is grouped to Den, which plays. A play on Kitchen never finds out what to play. It holds
Kitchen and Den meanwhile, so a stop of Den sent behind it waits. After 60 s the play fails with
an error that says the lookup took too long. Kitchen is still in the group of Den and its queue
is still empty, neither queue shows an action in progress, and the stop reaches Den at once.

- End-to-end: **no**: it needs a music provider that never answers a lookup.
- Proven by: `test_commands.py::test_a_play_request_that_is_given_up_frees_the_player_and_its_leader`

### BUSY-12 · The minute of a play request does not run while it waits for its turn

A play takes 90 s at the Den speaker. A play of another track sent right behind it waits for it
all that time, longer than the minute it has to look up what to play. It then looks its track up
and plays it.

- End-to-end: **no**: it needs a device that takes that long over a command.
- Proven by: `test_commands.py::test_a_play_request_that_waits_longer_than_a_minute_for_its_turn_still_plays`

### BUSY-13 · A play that its music service refuses fails at once and holds no player

Kitchen is grouped to Den, which plays. A play of two tracks on Kitchen starts the first one,
but its music service asked to wait an hour and refuses. The play fails with that refusal at
once. The second track is not tried and both tracks stay playable in the queue of Kitchen.
Commands sent right behind the play wait for neither player: a second start of the first track
is refused the same way, and a stop reaches Den. Kitchen is still in the group of Den, which
played on until the stop, and neither queue shows an action in progress.

- End-to-end: **no**: it needs a music service that refuses requests for an hour.
- Proven by: `test_commands.py::test_a_play_that_its_music_service_refuses_frees_the_player_and_its_leader`

## What a stopped player keeps for a moment

A player that stops still names what it played for a moment, so a playback that is replaced, and
stops for a moment on the way, does not lose its source. In the examples the Den speaker plays a
group's queue.

### KEEP-1 · A stopped player names the source it played for 5 s

The Den speaker stops. For 5 s it still names the group's queue as its source, then its own
queue. The 5 s count from the last stop: a speaker that plays again in between and stops again
gets the full 5 s again.

- End-to-end: **possible with** a source read at margins well inside the 5 s.
- Proven by:
  - `test_player_memory.py::test_a_stopped_player_keeps_its_source_for_5_seconds`
  - `test_player_memory.py::test_stopping_again_starts_the_5_seconds_over`

### KEEP-2 · Playing again within 5 s keeps the source

The Den speaker stops and plays again 3 s later, or is paused 3 s later. It keeps naming the
group's queue as its source, also long after the 5 s.

- End-to-end: **possible with** a source read at margins well inside the 5 s.
- Proven by: `test_player_memory.py::test_playing_again_within_5_seconds_keeps_the_source`

### KEEP-3 · A source chosen after the stop is kept

A second after the Den speaker stops, the user selects the queue of the Kitchen player as its
source. It is still the source after the 5 s have passed.

- End-to-end: **possible with** a source read at margins well inside the 5 s.
- Proven by: `test_player_memory.py::test_a_source_selected_while_stopped_is_kept`

## Grouping

Players are grouped through the protocol that can carry the group: their own protocol when they
are of one make, else an audio protocol they share, such as AirPlay. A sync group is a player of
its own whose members are grouped on one of them, its leader, while it plays. Most examples use
speakers of one make that can all group with each other (Kitchen, Office, Den), and speakers of
two makes that share an AirPlay receiver (Lounge, Study).

### GROUP-1 · Speakers of one make group through their own protocol

The user adds Office to Kitchen. Kitchen's own protocol is asked to add Office, by the id its
provider knows Office by. Office shows as synced to Kitchen, and Kitchen lists both as members.

- End-to-end: **yes**. Two Squeezelite players.
- Proven by: `test_grouping.py::test_speakers_of_one_make_group_on_their_own_protocol`

### GROUP-2 · A speaker that can not join is left out, and the rest join

The user adds Garden and Office to Kitchen, but Kitchen can not group with Garden: Office joins,
Garden stays out, and that is logged. A speaker that can not be reached right now is left out the
same way.

- End-to-end: **yes**. Add a stopped player, or a player of another kind, next to a Squeezelite
  player.
- Proven by:
  - `test_grouping.py::test_a_speaker_that_can_not_group_with_the_leader_is_left_out`
  - `test_grouping.py::test_an_unavailable_speaker_is_left_out`

### GROUP-3 · A speaker that can not group can not lead

A group change on a speaker without grouping is refused before anything happens: nothing is sent
to it, and it stays idle.

- End-to-end: **yes**. A group change on an MPD player, which offers no grouping.
- Proven by: `test_grouping.py::test_a_speaker_without_grouping_can_not_lead`

### GROUP-4 · A speaker in another group moves over

Office is a member of Den. The user adds Office to Kitchen: Den lets Office go first, then
Kitchen takes it. Office shows as synced to Kitchen.

- End-to-end: **possible with** a third player of one kind.
- Proven by: `test_grouping.py::test_a_speaker_moves_over_from_the_group_it_is_in`

### GROUP-5 · An ungrouped member leaves its group

Kitchen leads Office and Den. The user ungroups Office: Kitchen is asked to let Office go, Office
is synced to nothing, and Kitchen keeps Den.

- End-to-end: **yes**. Ungroup one member of a group of three Squeezelite players.
- Proven by: `test_grouping.py::test_a_member_leaves_its_group_when_it_is_ungrouped`

### GROUP-6 · A member that is powered off or drops off leaves its group

Kitchen leads Office and Den. The user powers Office off: it leaves the group, and Kitchen keeps
Den. A member that can no longer be reached is let go by its leader the same way.

- End-to-end: **yes**. Power off a member, or restart its container.
- Proven by:
  - `test_grouping.py::test_powering_off_a_member_takes_it_out_of_its_group`
  - `test_grouping.py::test_a_member_that_goes_offline_is_let_go_by_its_leader`

### GROUP-7 · Ungrouping an idle leader dissolves its group

Kitchen leads Office and plays nothing. The user ungroups Kitchen: all its members are let go,
and Kitchen lists no members.

- End-to-end: **yes**. Ungroup the leader of an idle two-player group.
- Proven by: `test_grouping.py::test_ungrouping_an_idle_leader_dissolves_its_group`

### GROUP-8 · A playing leader that leaves hands its group to a member

Kitchen leads Office and Den and plays its queue. The user ungroups Kitchen: Kitchen's queue
moves to Office, which plays it as its own queue and leads Den. Kitchen's queue is empty, and
Kitchen is no longer part of the group.

- End-to-end: **possible with** a third player of one kind.
- Proven by: `test_grouping.py::test_a_leader_that_leaves_its_playing_group_hands_it_to_a_member`

### GROUP-9 · Speakers of two makes group over an audio protocol they share

Lounge and Study can not group with each other on their own protocols, but each has an AirPlay
receiver. The user adds Study to Lounge: Lounge's AirPlay receiver takes Study's AirPlay
receiver, Study shows as synced to Lounge, and both have their AirPlay receiver as their output.

- End-to-end: **possible with** two virtual players of different kinds that each also run an
  AirPlay receiver on their host.
- Proven by: `test_grouping.py::test_speakers_of_different_makes_group_over_a_shared_audio_protocol`

### GROUP-10 · A playing leader moves to the protocol a new member needs

Lounge plays through its own protocol. The user adds Study, which can only follow over AirPlay:
Lounge's playback moves to its AirPlay receiver and goes on playing there, with Study.

- End-to-end: **possible with** the setup of GROUP-9.
- Proven by: `test_grouping.py::test_a_native_session_moves_to_the_protocol_a_new_member_needs`

### GROUP-11 · A member that would silence another member is refused

Lounge plays over AirPlay with Study, which only has AirPlay. Garage can only follow over
Sendspin, which Lounge also has; moving the group to Sendspin would leave Study silent. Adding
Garage is refused with an error: Lounge's Sendspin receiver gets no grouping command, Study stays
synced to Lounge and Garage to nothing, and Lounge plays on through its AirPlay receiver.

- End-to-end: **possible with** the setup of GROUP-9 with a Sendspin receiver on Lounge's host,
  plus a player that only has Sendspin.
- Proven by: `test_grouping.py::test_a_protocol_switch_that_would_silence_a_member_is_refused`

### GROUP-12 · An AirPlay speaker lets its own members go before it joins another

Attic leads Porch over AirPlay, where a member plays from its leader's own stream. The user adds
Attic to Hall: Attic lets Porch go first, because Porch would be silent once Attic follows Hall.
Then Attic joins Hall.

- End-to-end: **possible with** three AirPlay receivers.
- Proven by: `test_grouping.py::test_an_airplay_speaker_gives_up_its_own_group_before_it_joins_another`

### GROUP-13 · A sync group that plays groups its members on a leader

The user creates the sync group Downstairs of Kitchen and Office and plays it. Kitchen leads,
Office follows Kitchen, Kitchen shows Downstairs as the group it plays for, and Downstairs plays.

- End-to-end: **yes**. A sync group of two Squeezelite players.
- Proven by: `test_grouping.py::test_a_sync_group_groups_its_members_on_a_leader_when_it_plays`

### GROUP-14 · A player added to a playing sync group joins its leader

Downstairs plays with Kitchen leading. The user adds Den to Downstairs: Den joins Kitchen at once
and is a member of Downstairs.

- End-to-end: **possible with** a third player of one kind.
- Proven by: `test_grouping.py::test_a_member_added_to_a_playing_sync_group_joins_its_leader`

### GROUP-15 · A player added to a sync group's leader joins the group

Downstairs plays with Kitchen leading. The user adds Den to Kitchen itself. The group decides who
joins, so Den becomes a member of Downstairs, which joins it to Kitchen.

- End-to-end: **possible with** a third player of one kind.
- Proven by: `test_grouping.py::test_a_member_change_on_a_sync_groups_leader_goes_to_the_group`

### GROUP-16 · An ungrouped member leaves a playing sync group, which plays on

Downstairs plays with Kitchen, Office and Den. The user ungroups Den: Kitchen lets Den go, Den is
no member of Downstairs any more, and Downstairs keeps playing.

- End-to-end: **possible with** a third player of one kind.
- Proven by: `test_grouping.py::test_ungrouping_a_member_of_a_playing_sync_group_takes_only_that_member_out`

### GROUP-17 · A member of a fixed group can not leave it alone

Downstairs is a sync group with a fixed member list, playing on Kitchen and Office. The user
ungroups Office: that ends the session of the whole group. Downstairs is idle, Office is synced
to nothing, and neither Kitchen nor Office plays for Downstairs any more.

- End-to-end: **yes**. A static sync group of two Squeezelite players.
- Proven by: `test_grouping.py::test_ungrouping_a_member_of_a_fixed_group_ends_the_groups_session`

### GROUP-18 · A returning member of a fixed group rejoins its leader

Downstairs is a sync group with a fixed member list, Kitchen, Office and Den, and plays with
Kitchen leading. Office drops off the network, and Kitchen's device lets it go. When the provider
finds Office again, Kitchen takes it back: Office shows as synced to Kitchen, and Downstairs plays
on.

- End-to-end: **yes**. A static sync group of three Squeezelite players; restart the container
  of a member.
- Proven by: `test_grouping.py::test_a_returning_member_of_a_fixed_group_rejoins_its_leader`

### GROUP-19 · A leader the device pauses during a group change moves without resuming

Lounge plays through its own protocol. The user adds Study, which can only follow over AirPlay.
While Lounge's AirPlay receiver takes Study, Lounge is paused at the device. Study shows as synced
to Lounge and Lounge's output moves to its AirPlay receiver, as in GROUP-10, but nothing resumes
the playback there: the receiver gets no play, and Lounge does not play.

- End-to-end: **no**: the pause has to fall within the group change.
- Proven by: `test_grouping.py::test_a_leader_paused_during_the_join_moves_its_output_without_resuming`

### GROUP-20 · A member is controlled through its leader, and leaves on its own access

Kitchen leads Office and plays. A user who may only use Office pauses Office, resumes it and
stops it: each is refused, as Kitchen owns the playback, and nothing reaches Kitchen. A user who
may use both does the same: the pause, the resume and the stop each reach Kitchen, not Office,
and Kitchen is paused, plays again and stops, with Office still synced to it. The user who may
only use Office ungroups it: Kitchen lets Office go, and Office is synced to nothing.

- End-to-end: **possible with** two Squeezelite players and a user limited to one of them.
- Proven by: `test_grouping.py::test_a_member_is_controlled_through_its_leader_and_leaves_on_its_own_access`

### GROUP-21 · A group comes back after a restart with its members

The user created the sync group or the universal group Group Z of Player A and Player B, with a
power control. After a restart it is the same player again: available, named Group Z, with its
members and its power.

- End-to-end: **yes**. A sync group of two Squeezelite players; restart the server.
- Proven by:
  - `tests/providers/sync_group/test_lifecycle.py::test_a_new_group_comes_back_with_its_members_after_a_restart`
  - `tests/providers/universal_group/test_lifecycle.py::test_a_new_group_comes_back_with_its_members_after_a_restart`

## Where playback goes

A player plays through one of its outputs, and what the device plays by itself is steered
through the protocol that plays it. These scenarios show which output plays and where a
transport command goes.

### OUTPUT-1 · A player plays through the output the user prefers, else through its own

The Kitchen speaker has its own protocol and an AirPlay receiver: it plays through its own
protocol. Once the user prefers the AirPlay receiver as its output, it plays through that, and
names it as its active output. A LinkPlay device, which is controlled without an audio path of
its own, plays through its DLNA renderer, the output LinkPlay prefers, and not through its
AirPlay receiver, which otherwise ranks higher.

- End-to-end: **possible with** a host that runs a native player and an AirPlay receiver, such as
  Squeezelite and shairport-sync; for the LinkPlay part, a LinkPlay device that is no WiiM, with
  its DLNA renderer and an AirPlay receiver.
- Proven by:
  - `test_outputs.py::test_playback_goes_to_the_output_the_user_prefers`
  - `tests/providers/wiim/test_linkplay_control.py::test_playback_goes_to_dlna_before_a_higher_priority_output`

### OUTPUT-2 · Transport commands follow the app that plays on the device

A Spotify Connect session plays on the DLNA renderer of a LinkPlay device. The player names
Spotify as its source, shows the renderer as its active protocol and offers next and previous.
Pause, play and next reach the renderer, in that order, and the player shows paused after the
pause.

- End-to-end: **possible with** a LinkPlay device that is no WiiM (the server controls a WiiM
  through its own protocol), with its DLNA renderer and a Spotify Connect session started on it.
- Proven by: `tests/providers/wiim/test_linkplay_control.py::test_transport_commands_reach_an_app_playing_on_the_renderer`

## Volume

The level and mute of a player are its own. A volume or mute command goes to the output that
renders, else to the own protocol, and an output that takes over gets the player's level first.
The examples use the Kitchen speaker with its own protocol at level 30 and its AirPlay receiver at
level 80.

### VOLUME-1 · An output that takes over gets the player's level before it plays

The user prefers the AirPlay receiver as Kitchen's output and plays. The receiver gets level 30
once, before the media, and the player never shows 80. When Kitchen and Hall group over AirPlay,
each receiver gets the level and mute of its own player.

- End-to-end: **possible with** a host that runs a native player and an AirPlay receiver at
  different levels, such as Squeezelite and shairport-sync.
- Proven by:
  - `tests/controllers/players/test_routing.py::test_audio_protocol_that_takes_over_gets_the_level_of_the_player_first`
  - `tests/controllers/players/test_routing.py::test_players_grouped_through_an_audio_protocol_hand_it_their_level_and_mute`

### VOLUME-2 · A volume command goes to the output that renders, and only it reports back

While Kitchen plays through its AirPlay receiver, a volume command goes to the receiver, not to
the own protocol. A level the idle own protocol reports does not move the player's level; one the
receiver reports does. While nothing else renders, a volume command goes to the own protocol.

- End-to-end: **possible with** the setup of VOLUME-1.
- Proven by:
  - `tests/controllers/players/test_routing.py::test_volume_goes_to_the_output_that_renders_and_only_it_reports_back`
  - `tests/controllers/players/test_routing.py::test_native_protocol_owns_the_volume_while_nothing_else_renders`

### VOLUME-3 · A player whose own protocol can not be reached keeps its level

Kitchen's own protocol can no longer be reached. The player stays available, controlled through
its AirPlay receiver, and keeps level 30 and its mute, whatever takes the control meanwhile.
Nothing is sent to the receiver until it plays: then it gets the player's level first, and never
a level above what the device was left at. Volume and mute commands go to the receiver. Once the
own protocol can be reached again, it has the controls back at its own level, and gets no
command.

- End-to-end: **possible with** a native player whose control connection the lab can cut while
  its AirPlay receiver stays up.
- Proven by:
  - `tests/controllers/players/test_routing.py::test_native_protocol_that_can_not_be_reached_leaves_the_controls_to_the_others`
  - `tests/controllers/players/test_routing.py::test_audio_protocol_that_takes_over_from_an_unreachable_native_gets_the_level_to_play`
  - `tests/controllers/players/test_routing.py::test_level_the_player_had_is_kept_while_its_native_can_not_be_reached`
  - `tests/controllers/players/test_routing.py::test_output_that_takes_over_is_never_made_louder_than_the_device_was_left`
  - `tests/controllers/players/test_routing.py::test_native_that_can_be_reached_again_takes_the_controls_back_at_its_own_level`

### VOLUME-4 · A device shows the level it reported before its protocol was attached

A Sendspin voice device connects after a restart and reports level 28, unmuted, while its provider
still sets it up. From the moment its player has it, the player shows level 28 and unmuted, and a
volume command reaches the device; the level it reports back shows. A level it reports while its
protocol waits for its player (for its identity, or for the protocol a bridge rides on), or while
its protocol is detached to be attached again, shows once the protocol is attached.

- End-to-end: **possible with** a Sendspin client of the lab that reports its volume right after
  its hello, checked after a restart of the server.
- Proven by:
  - `tests/providers/sendspin/test_reported_volume.py::test_a_device_shows_the_volume_it_reported_before_its_protocol_was_attached`
  - `tests/providers/sendspin/test_reported_volume.py::test_a_volume_report_while_the_protocol_is_held_shows_once_it_is_bound`
  - `tests/providers/sendspin/test_reported_volume.py::test_a_protocol_attached_again_shows_the_level_reported_while_it_was_detached`

### VOLUME-5 · A device keeps the level it reported when a second connection of it takes over

A Sendspin voice device reports level 28, unmuted, on its first connection, and a second
connection of it replaces the first before its player has it, without reporting its state again.
The player shows level 28 and unmuted. Once the device reports its state on the new connection,
a volume command reaches it.

- End-to-end: **possible with** a Sendspin client of the lab that opens a second connection right
  after its first one, checked after a restart of the server.
- Proven by: `tests/providers/sendspin/test_reported_volume.py::test_a_device_keeps_its_reported_volume_when_a_second_connection_replaces_the_first`

## A pause, a stop and the end of a queue

A queue whose player plays its last item out is taken for ended 5 s later: it is parked on its
last item and a play starts it over. A pause or a stop is not that end: the queue keeps its
place, also when the pause stops the player. In the examples the Kitchen speaker is a Sendspin
speaker without a pause of its own, so a pause stops it, and it has the whole last track of its
queue in its buffer.

### PAUSE-1 · A pause on a player that can not pause is not the end of its queue

The Kitchen speaker is paused 5 s into the last track of its queue. The speaker stops and the
queue is idle on that track at 5 s. It still is 5 s later, and a minute later.

- End-to-end: **yes**. The `elapsed_tracks_pause` check of the lab on its Sendspin player.
- Proven by: `test_paused_queue.py::test_a_pause_on_a_player_that_can_not_pause_is_not_the_end_of_its_queue`

### PAUSE-2 · A queue paused on a player that can not pause plays on from there

The Kitchen speaker is paused 5 s into the last track of its queue and resumed 4.5 s, 5 s, 5.5 s
or 40 s later. The track plays on from 5 s. Once it has played to its end, the queue is taken
for ended after 5 s as any other.

- End-to-end: **yes**. The `elapsed_tracks_pause` check of the lab on its Sendspin player, which
  resumes 5 s after the pause.
- Proven by: `test_paused_queue.py::test_a_queue_paused_on_a_player_that_can_not_pause_plays_on_from_there`

### PAUSE-3 · A paused queue that is stopped at the pause timeout is not ended

A speaker with a pause of its own is paused 5 s into the last track of its queue and stays
paused. After the pause timeout of 30 s the server stops it. The queue is idle on that track at
5 s, and a resume plays the track on from there.

- End-to-end: **possible with** a player of the lab that has a pause of its own and plays in flow
  mode, paused for longer than the pause timeout.
- Proven by: `test_paused_queue.py::test_a_paused_queue_that_is_stopped_at_the_pause_timeout_is_not_ended`

### PAUSE-4 · A play that starts as an ended queue is settled plays

The Kitchen speaker has played its queue to the end. Half a second before the queue is taken for
ended, a resume, a play of another track or a play of the first item starts, and its music
source takes a second to deliver audio. The play starts the speaker once, as soon as the audio
is there, and the queue is not taken for ended. A play that still looks up its music at that
moment plays as well, and no client sees the queue as ended in between.

- End-to-end: **no**: it needs a play that starts within the few milliseconds in which the
  server takes the queue for ended.
- Proven by:
  - `test_paused_queue.py::test_a_play_that_starts_as_an_ended_queue_is_settled_plays`
  - `test_stopped_queue.py::test_a_play_that_looks_up_its_music_as_the_queue_would_end_plays`

### PAUSE-5 · A queue whose pause failed still ends when it played out

A speaker refuses a pause 5 s into the last track of its queue, so the pause fails and the queue
plays on. Once the track has played to its end, the queue is taken for ended after 5 s. The
same goes for a stop that the speaker refuses. A paused queue whose stop or play is refused
stays paused: it is not taken for ended when its speaker stops later.

- End-to-end: **no**: it needs a device that refuses a pause, a stop or a play.
- Proven by:
  - `test_paused_queue.py::test_a_queue_whose_pause_failed_still_ends_when_it_played_out`
  - `test_stopped_queue.py::test_a_queue_whose_stop_failed_still_ends_when_it_played_out`
  - `test_stopped_queue.py::test_a_stop_that_fails_leaves_a_paused_queue_paused`
  - `test_stopped_queue.py::test_a_play_that_fails_leaves_a_paused_queue_paused`

### PAUSE-6 · A stop is not the end of a queue

The Kitchen speaker is stopped 5 s into the last track of its queue, or paused there and stopped
a second later. The queue is idle on that track at 5 s. It still is 5 s later and a minute
later, and a resume plays the track on from 5 s. The same goes for a speaker with a pause of its
own.

- End-to-end: **possible with** a check of the lab that stops a player in flow mode on the last
  track of its queue and reads the queue 6 s later.
- Proven by: `test_stopped_queue.py::test_a_stop_is_not_the_end_of_its_queue`

### PAUSE-7 · A stop starts no new music from a dynamic source

The queue of the Kitchen speaker was started from a dynamic playlist and plays its last track.
It is stopped 5 s in. The playlist is not asked for more tracks, and the speaker stays stopped.

- End-to-end: **possible with** a dynamic playlist in the lab, on a player in flow mode that is
  stopped on the last track of its queue.
- Proven by: `test_stopped_queue.py::test_a_stop_does_not_refill_a_queue_from_its_dynamic_source`

### PAUSE-8 · A queue halted in a new playback is not ended for the one before

The Kitchen speaker has played its queue to the end. Before the queue is taken for ended, its
first track is played again from 5 s and paused or stopped at once. The queue stays idle on that
track at 5 s. The same goes for a queue whose dynamic playlist is being asked for more tracks at
that moment: whatever the playlist hands back, nothing is added to the queue and nothing starts.
And for a play that is stopped before the speaker reports that it plays: the queue is not taken
for ended, and a dynamic playlist is not asked for more tracks.

- End-to-end: **no**: it needs a play and a pause within the 5 s in which the server waits to
  take the queue for ended.
- Proven by:
  - `test_stopped_queue.py::test_a_queue_halted_in_a_new_playback_is_not_ended_for_the_one_before`
  - `test_stopped_queue.py::test_a_queue_paused_while_its_dynamic_source_is_fetched_stays_paused`
  - `test_stopped_queue.py::test_a_play_stopped_before_its_speaker_plays_is_not_ended_for_the_playback_before`

### PAUSE-9 · A play on a speaker that is slow to start is not ended for the playback before

The Kitchen speaker has played its queue to the end. Half a second before the queue is taken
for ended, a resume, a play of another track or a play of the first item starts. The speaker
takes the stream and reports that it plays only 11 s later, or never. The play command is done
after the 5 s it waits for the speaker, as before. The queue is not taken for ended and keeps
the audio it prepared, and it plays once the speaker reports that it does. The same goes for a
play of another track 2 s before the last track of the queue ends, on a speaker that drops what
it played and is as slow to start.

- End-to-end: **no**: it needs a device that takes more than 5 s to start a stream, in the
  second in which the server takes its queue for ended.
- Proven by:
  - `test_stopped_queue.py::test_a_play_on_a_slow_speaker_keeps_its_audio_as_the_queue_would_end`
  - `test_stopped_queue.py::test_a_play_in_the_last_seconds_of_a_queue_keeps_its_audio_on_a_slow_speaker`

### PAUSE-10 · A queue that played out is taken for ended

The Kitchen speaker plays the last track of its queue out, as one flow stream or track by
track. The queue is taken for ended 5 s later, also when a stop arrives in those 5 s, or a pause
or a stop in the moment the speaker went idle and its queue still reads as playing. A queue
that was started from a dynamic playlist plays the track that playlist hands back instead. A
sleep timer that stops the speaker at the end of the last track ends the queue at once.

- End-to-end: **possible with** a check of the lab that lets a queue play out and reads it 6 s
  later.
- Proven by:
  - `test_stopped_queue.py::test_a_queue_that_played_out_is_taken_for_ended`
  - `test_stopped_queue.py::test_a_halt_as_the_speaker_just_played_out_still_lets_its_queue_end`
  - `test_stopped_queue.py::test_a_queue_that_played_out_is_refilled_from_its_dynamic_source`
  - `test_stopped_queue.py::test_a_queue_stopped_by_its_sleep_timer_at_the_end_is_taken_for_ended`

### PAUSE-11 · A queue that plays again after a pause or a stop still ends

The Kitchen speaker takes a stop 5 s into the last track of its queue, and has not reported that
it stopped when a play of that track follows. The track plays. Once it has played to its end,
the queue is taken for ended after 5 s. So is the queue of a speaker with a pause of its own
that is paused and plays on a second later, or at once: before its queue, which follows the
speaker with a short delay, read paused.

- End-to-end: **no**: it needs a play within the moment in which a device has not reported its
  stop yet.
- Proven by:
  - `test_stopped_queue.py::test_a_queue_stopped_and_started_at_once_still_ends_when_it_played_out`
  - `test_stopped_queue.py::test_a_queue_that_plays_on_after_a_pause_still_ends_when_it_played_out`

### PAUSE-12 · A queue whose content went is not one that was paused

The Kitchen speaker is paused 5 s into the last track of its queue. The queue is cleared, or
moved to the Hall speaker without playing it there. The queue of the Kitchen speaker is empty,
and the server no longer holds it for a queue that a command halted.

- End-to-end: **no**: a client sees no difference; the test reads the server's own record of the
  queue.
- Proven by: `test_stopped_queue.py::test_a_queue_whose_content_went_is_not_one_a_command_halted`

### PAUSE-13 · A queue that played out on a speaker that stays busy keeps its place

The Kitchen speaker plays the last track of its queue out while a command holds the speaker,
and that command is still not done two minutes later. The server gives up waiting for it and
leaves the queue as it is: on its last track, not ended.

- End-to-end: **no**: it needs a command that hangs for two minutes.
- Proven by:
  `test_stopped_queue.py::test_a_queue_that_played_out_on_a_player_that_stays_busy_keeps_its_place`
