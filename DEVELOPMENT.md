Developer docs
==================================

## 📝 Prerequisites
* ffmpeg (minimum version 8.0), must be available in the path so install at OS level
* Python 3.14 is minimal required (the exact pinned runtime lives in `.python-version` at the repo root — that file is the single source of truth for all tools)
* [Python venv](https://docs.python.org/3/library/venv.html)
* libchromaprint, also at OS level. Install it if you want to run the `acoustid_lookup` provider: without libchromaprint AcoustID refuses to load. The test suite does not need it: the AcoustID tests drive a fake fingerprinter.

      # macOS
      brew install chromaprint

      # Debian/Ubuntu
      sudo apt-get install libchromaprint1

    On Apple Silicon, Homebrew installs into `/opt/homebrew`, which is not on the dynamic loader's default search path. `pyacoustid` opens libchromaprint by bare filename, so installing the package is not enough — add this to your shell profile as well:

      export DYLD_FALLBACK_LIBRARY_PATH="$(brew --prefix)/lib:/usr/local/lib:/usr/lib"

* shairport-sync 5.6.3 or newer (a classic AirPlay build), also at OS level. Install it if you want to run the `airplay_receiver` plugin. On macOS, run `brew install shairport-sync`. Distribution packages are often older or built for AirPlay 2; [the plugin's README](music_assistant/providers/airplay_receiver/bin/README.md) explains how to build it.

We recommend developing on a (recent) macOS or Linux machine.
It is recommended to use Visual Studio Code as your IDE, since launch files to start Music Assistant are provided as part of the repository. Furthermore, the current code base is not verified to work on a native Windows machine. If you would like to develop on a Windows machine, install [WSL2](https://code.visualstudio.com/blogs/2019/09/03/wsl2) to increase your swag-level 🤘.

## 🚀 Setting up your development environment

### Python venv (recommended)
With this repository cloned locally, execute the following commands in a terminal from the root of your repository:
* Run our development setup script to setup the development environment:

      scripts/setup.sh

    (creates a new separate virtual environment to nicely separate the project dependencies)

* The setup script will create a separate virtual environment (if needed), install all the project/test dependencies and configure pre-commit for linting and testing.
* Make sure, that the python interpreter in VS Code is set to the newly generated venv.
* Debug: Hit (Fn +) F5 to start Music Assistant locally (VS Code, using the launch configuration in `.vscode/launch.json`), or run `python -m music_assistant --log-level debug` from the command line
  * asyncio debug mode, with its slow-callback warnings and the scheduling stack in unhandled-error logs, comes from Python's development mode: run `python -X dev -m music_assistant --log-level debug` or set `PYTHONDEVMODE=1`. The VS Code launch configuration already enables it. The environment variable also turns on the slow-query warnings of the database helper. Dev mode records a stack trace for every scheduled callback and future, so expect a slower server while it is on.
  * Core maintainers: the launch configuration reads the bundled provider credentials (Spotify, Qobuz, ...) from a checkout of the private `appvars` repository next to this one (`../appvars/app_vars.json`). From the command line, point `MASS_APP_VARS_FILE` at that file. Without it the bundled credentials stay empty, see `music_assistant/helpers/app_vars.py`.
* The pre-compiled UI of Music Assistant will be available at `localhost:8095` 🎉

NOTE: Always re-run the setup script after you fetch the latest code because requirements could have changed.

### Using Devcontainer/Codespace
We removed support for devcontainers because we do not have anyone willing to maintain it.
It also is not very convenient due to all the port requirements, binaries etc.
If somebody is willing to create and maintain a devcontainer with host networking and based on our base alpine image, we will add the support back. Until then: Develop with Python venv on a Linux or macOS machine (see above).

### Using devenv (Nix-based)
If you prefer a reproducible, Nix-based development environment, this repository includes support for devenv 2 (latest). Devenv builds a per-project development environment (using Nix) that provides the exact tools and packages you need without changing your global system.

- What it is: devenv uses Nix to create isolated, reproducible dev shells that contain system packages (ffmpeg, git, build tools) and language runtimes (Python, Node, etc.). It helps avoid "it works on my machine" problems.
- Install: follow the official guide for installing Nix and devenv for your OS: https://devenv.sh/getting-started/
- Quick start (from the repository root):

  ```sh
  # build the environment and enter an interactive shell
  devenv up

  # alternatively: enter an environment shell if already built
  devenv shell

  # run a single command inside the environment without opening a shell
  devenv run -- scripts/setup.sh
  ```

- Inside the devenv shell you can run the usual project commands (for example `scripts/setup.sh`, `python -m music_assistant --log-level debug` or the test runner). When finished, exit the shell with `exit`.
- Files: This repo contains `devenv.nix` and `devenv.yaml` at the repository root which declare the packages and dev actions used by the environment — inspect them if you need to add or change tools.

Notes:
- If you are new to Nix, the getting-started link above includes details about single-user vs multi-user installs and common troubleshooting steps.
- Using devenv does not prevent using the provided `scripts/setup.sh` (the script creates a Python venv inside the environment if you prefer that workflow).

### Developing on the Music Assistant Server Models

If you're working on core Music Assistant features, you may need to modify the shared data models. The **Python models which are shared between client and server** are located in the [`music-assistant/models`](https://github.com/music-assistant/models) repository, while the corresponding **client-side TypeScript interfaces** are in [`interfaces.ts`](https://github.com/music-assistant/frontend/blob/main/src/plugins/api/interfaces.ts) in the [Frontend repository](https://github.com/music-assistant/frontend).

In most cases, you won't need to modify the models. However, if you do need to make changes, here's how to set up your development environment to use a local models repository instead of the one installed via pip:

  * First, clone the [`models` repository](https://github.com/music-assistant/models) to your local machine.

  * Then, install your local `models` clone in "**editable**" mode. This allows your changes to be reflected immediately without a reinstall. Run the following command from the server repository's root:

    ```bash
    uv pip install -e /path/to/your/cloned/models/repo --config-settings editable_mode=strict
    ```

**Note:** You must rerun this command whenever you add or remove files from the `models` repository to ensure the changes are picked up.

### Running a provider from a folder outside the server

A provider that lives outside the server repository (for example in a repository of its own) can be run from where it is:
* Put the provider in a folder named after its `domain`, in a folder named `provider`, or in `providers/<domain>` in a repository with several providers, as the community catalog accepts. It loads under its domain either way. A domain the server already has is refused.
* Start the server in dev mode (`PYTHONDEVMODE=1`, or from a checkout with a `.venv` in the repository root) and pass the folder that holds them, such as the root of your repository:

      PYTHONDEVMODE=1 python -m music_assistant --dev-providers-dir /path/to/my_providers

  Pass `--dev-providers-dir` more than once to load providers from several folders at once, which is useful when you work on providers across repositories:

      PYTHONDEVMODE=1 python -m music_assistant \
          --dev-providers-dir /path/to/repo_a \
          --dev-providers-dir /path/to/repo_b

  If two folders carry a provider with the same `domain`, the first-passed folder wins and the other is skipped with a warning naming both paths.
* The `requirements` in `manifest.json` are plain package names with versions, such as `aiohttp-retry==2.9.1`. They are installed next to the server's own packages and never replace one of them: a provider whose requirements clash with the server is not loaded.
* A change to the code takes effect after a restart of the server.
* To test the provider against a real server, add `pytest_plugins = ["music_assistant.testing"]` to `tests/conftest.py` (with pytest-asyncio's `asyncio_mode = "auto"`). The `mass` fixture then gives a started server in dev mode that loads the providers in the root folder of the test run. It announces nothing on the network and listens on the loopback address only; the requests a provider makes itself still go out. Next to the server, install the requirements of its built-in Sendspin provider (`aiosendspin[server]` and `av`, at the versions in `music_assistant/providers/sendspin/manifest.json`): the server starts that provider at every boot.

### Trying a branch or your own provider in Docker

The development image `ghcr.io/music-assistant/server:dev` runs a server without a development setup. It is the nightly server image plus the tools to install the server from a branch, a pull request or a fork, to build the frontend from a branch, and to load providers from their own repository. It runs the code of the repositories you name, so use it to test, not to run your daily server.

Three environment variables say what to run; an empty or unset one is not used.
* `SERVER_REPO`: the server to install instead of the nightly. A branch (`dev`, `feature/new-player`), a pull request (`pr-123`), a full commit hash, or a fork as `owner/repo@ref` (`owner/repo` uses the fork's default branch).
* `FRONTEND_REPO`: the frontend to build instead of the one that comes with the server, in the same forms.
* `PROVIDER_REPO`: a repository with one or more provider folders, such as one made from the [community provider template](https://github.com/music-assistant/community-provider-template). It is `owner/repo` (its default branch), `owner/repo@ref` (a branch, a tag or a commit) or `owner/repo@pr-12` (pull request 12 of that repository). The repository is fetched again at every start and the server runs in dev mode with it as its provider folder, see [Running a provider from a folder outside the server](#running-a-provider-from-a-folder-outside-the-server).

The server keeps its data in `/data`, so give that a volume:

```sh
docker run --rm --network host -v ma-dev-data:/data \
  -e PROVIDER_REPO=your-github-handle/your-repository \
  ghcr.io/music-assistant/server:dev
```

With Docker Compose:

```yaml
services:
  music-assistant-dev:
    image: ghcr.io/music-assistant/server:dev
    network_mode: host
    volumes:
      - ma-dev-data:/data
    environment:
      SERVER_REPO: pr-123
      PROVIDER_REPO: your-github-handle/your-repository@my-branch

volumes:
  ma-dev-data:
```

Further arguments go to the server, as with the server image: `docker run … ghcr.io/music-assistant/server:dev --log-level debug`. A restart fetches and installs the named references again, so it picks up new commits. Pull the image again for a newer nightly. The Music Assistant DEV SERVER app for Home Assistant runs this image, and its options `server_repo`, `frontend_repo` and `provider_repo` are the three variables.

## Listing random order

Server listing commands accept an optional integer `random_seed`. With `sort_field=random`,
reuse the same seed for every page of one listing load; with unchanged items and filters,
this avoids repeated or skipped items across pages. SQL listings also accept the seed for
`random_play_count`, which keeps play count as the primary order and randomizes ties.
Start a new seed when requesting a new random order. Seed `0` is valid. Without a seed,
random sorts continue to draw a new order for each request.

The seed is a request parameter, not stored state or a frozen snapshot: library edits,
provider changes and updated play counts can change the result. It has no effect on other
sort fields. `music/sort_options` remains the authority for the sorts each listing supports;
accepting a seed does not add Random to a listing's offered options. Provider methods are
unchanged; the server applies search, sort and paging to their listings.

For the shared API parameters and client migration notes, see the [listing contract](https://developers.music-assistant.io/listing-contract/).

## Note on async Python
The Music Assistant server is fully built in Python. The Python language has no real supported for multi-threading. This is why Music Assistant heavily relies on asyncio to handle blocking IO. It is important to get a good understanding of asynchronous programming before building your first provider. [This](https://www.youtube.com/watch?v=M-UcUs7IMIM) video is an excellent first step in the world of asyncio.





## Building a new Music Provider
A Music Provider is the provider type that adds support for a 'source of music' to Music Assistant. Spotify and Youtube Music are examples of a Music Provider, but also Local files can be put in the Music Provider category. All Providers (of all types) can be found in the `music_assistant/providers` folder.

TIP: We have created a template/stub provider in `music_assistant/providers/_demo_music_provider` to get you started fast!

Before you start, read [what a provider in the server asks of you](#-contributing-a-provider).


**Adding the necessary files for a new Music Provider**

Add a new folder to the `providers` folder with the name of provider. Add two files inside:
1. `__init__.py`. This file contains the Python code of your provider.
2. `manifest.json`. This file contains metadata and configuration for your provider.

**Configuring the manifest.json file**

The easiest way to get start is to copy the contents of the manifest of an existing Music Provider, e.g. Spotify or Youtube Music. See [the manifest section](#%EF%B8%8F-manifest-file) for all available properties.

**Creating the provider**

Create a file called `__init__.py` inside the folder of your provider. This file will contain the logic for the provider. All Music Providers must inherit from the [`MusicProvider`](./music_assistant/models/music_provider.py) base class and override the necessary functions where applicable. A few things to note:
* The `setup()` function is called by Music Assistant upon initialization of the provider. It gives you the opportunity the prepare the provider for usage. For example, logging in a user or obtaining a token can be done in this function.
* A provider should let Music Assistant know which [`ProviderFeature`](https://github.com/music-assistant/models/blob/main/music_assistant_models/enums.py) it supports by implementing the property `supported_features`, which returns a list of `ProviderFeature`.
* List the media types a user looks for the provider under, whatever its config, in the optional `provides` of its manifest, such as `audiobook` and `podcast` for an audiobook server or `radio` for a radio directory. It is metadata for discovery only: what the provider does at runtime always comes from its code.
* A podcast provider must set each episode's `position` so that the oldest episode has the lowest number and the newest the highest. The episode listing is sorted on it and "play from here" queues the selected episode plus everything newer, so a provider that numbers episodes by the order its API happens to return them will show them backwards. Rank on the publication date using `rank_episodes_by_date()` in [`podcast_parsers`](./music_assistant/helpers/podcast_parsers.py), and leave the position at 0 where it is unknown.
* Image paths that are not URLs (for example a cover id that `resolve_image()` fetches from the service) are dropped when they are read back from a playlist file, because a stored path could point at a local file. If your `resolve_image()` only ever looks such a path up in your own service, override `accepts_stored_image_path()` to accept the paths your service uses. See the [Plex](./music_assistant/providers/plex/__init__.py) provider as an example. [OpenSubsonic](./music_assistant/providers/opensubsonic/sonic_provider.py) accepts any id, which is only safe because the id is sent to its server and never used for anything else.
* The actual playback of audio in Music Assistant happens in two phases:
    1. `get_stream_details()` is called to obtain information about the audio, like the quality, format, # of channels etc.
    2. `get_audio_stream()` is called to stream raw bytes of audio to the player. There are a few [helpers](./music_assistant/helpers/audio.py) to help you with this. Note that this function is not applicable to direct url streams.
* Examples:
    1. Streaming raw bytes using an external executable (librespot) to get audio, see the [Spotify](./music_assistant/providers/spotify/__init__.py) provider as an example
    2. Streaming a direct URL, see the [Youtube Music](./music_assistant/providers/ytmusic/__init__.py) provider as an example
    3. Streaming an https stream that uses an expiring URL, see the [Qobuz](./music_assistant/providers/qobuz/__init__.py) provider as an example


**Rules for providers that stream from a music service**

Music Assistant plays music to your own speakers; it is not a way to download or keep
copies of it. See the [usage policy](https://github.com/music-assistant/.github/blob/main/USAGE_POLICY.md).
A provider that talks to a music service must therefore:

* **Authenticate as the user.** Fetch audio the way an ordinary client would, using the
  account the user configured. Do not bypass a subscription tier or regional availability.
* **Decode only what the user is entitled to.** Where a service protects its audio, obtain the
  key or licence the way its own client does, for the account the user configured. Decode in
  order to play, never to produce a file. Where there is no licence path for the account, skip
  the item rather than reaching for one.
* **Be a well-behaved client of the API.** Put a
  [`Throttler` or `ThrottlerManager`](./music_assistant/helpers/throttle_retry.py) in front of a
  service's API and set it to what that service actually tolerates, and put
  [`@use_cache`](./music_assistant/controllers/cache/helpers.py) on the lookups that repeat
  instead of asking again. Back off on a 429 rather than retrying into it.
  One user action or background job never walks the service's catalogue: no requests per album,
  track or page of an artist's discography or a service listing, and never build what the
  service has no endpoint for that way. Paging through the user's own library, through the
  throttler, is fine. A provider without an artist tracks endpoint does not offer
  `ProviderFeature.ARTIST_TRACKS`; artist top tracks (`ARTIST_TOPTRACKS`) is a feature of its own.
  A listing of an artist's albums stops at `ARTIST_ALBUMS_LIMIT` releases (100, in
  [`music_provider.py`](./music_assistant/models/music_provider.py)).
  The throttler serves playback first, user actions second and background work last. Background
  work may use only half of the rate limit and is spread evenly over time, which keeps room free
  for the user. Music Assistant sets the priority where a request starts, so a provider normally
  does not have to.
  A provider that hammers a service puts every Music Assistant user's account at risk, not just
  the developer's.
* **Keep the provider's audio address inside the server.** Return it in `StreamDetails`, which
  does not serialize it. Never place it on a media item, an API result, or a route that hands
  it to a caller.
* **Do not persist decoded audio.** Streaming providers must return a stream, not bytes cached
  to disk.
* **Honour the service's own limits.** Where a service states how many streams one account may
  run at once, override `max_concurrent_streams` with that number — see
  [`MusicProvider`](./music_assistant/models/music_provider.py).

Changes that weaken any of these will not be accepted, however useful they are otherwise.

## ▶️ Building your own Player Provider
A Player Provider is the provider type that adds support for a 'target of playback' to Music Assistant. Sonos, Chromecast and AirPlay are examples of a Player Provider.
All Providers (of all types) can be found in the `music_assistant/providers` folder.

TIP: We have created a template/stub provider in `music_assistant/providers/_demo_player_provider` to get you started fast!

Before you start, read [what a provider in the server asks of you](#-contributing-a-provider).

A player provider implements one `PlayerProtocol` (`music_assistant/models/player_protocol.py`) per device and registers it with `mass.players.register()`. The server builds the `Player` that users see from the protocols of a device, and attaches the protocol to it: the protocol finds it as `self.player`. The contract between the two:

* A protocol reports its own values (`_attr_*` or its properties) and reads them back from itself, never from a published state.
* It reads the final values of its player (`self.player.<field>`: what the player published last, the same values as in `self.player.state`) only for what the device has to display or follow: the current media and its position.
* The grouping as users see it comes from the player too (`self.player.group_members`, `self.player.synced_to`, `self.player.active_group`); what a protocol reports about grouping names the protocol ids of its own provider.
* A property of a protocol is read while the server derives the state of its player, when `self.player.<field>` is still the previous value. Such a property never reads a final value of its own player: it asks what is true right now (`self.player.attached_protocols`, `self.player.resolve_output_protocol()`).
* Whether another player takes part in what a protocol does (a member of its group, the target of a handover) is the `available` that player published, as for a client. A player that is off or starting up at an external power control that cuts its power is available too, while its device can not play (`offline_by_power_control`). A protocol never switches such a player on itself: the server powers it on, and waits for its start-up time, before a command starts the group or adds the player. A protocol that restarts playback by itself, which is no command of a user, does that inside `with mass.players.without_member_power_on():`, so the restart switches no member of a group on and waits for none. The members that play along right now come from `mass.players.iter_group_members()`.
* A user can say that the external power control of a player (a smart plug) cuts the power of the player itself. The server then tells every attached protocol of that player what happens to its device: `on_power_cut()` once the control has stayed off for a while or was switched off through the server, and `on_power_restored()` when the start-up time of the player has passed since the control came on. Every `on_power_cut()` is followed by an `on_power_restored()`: also when the server stops following the power of the player (its setting or its power control changed, or the control went away), while the device may still be off. Override them when your provider keeps a connection to the device: it would otherwise keep a dead connection, read available for long after the power is gone, or only reach the device again on a reconnect of its own that can be minutes away. In `on_power_cut()` close the connection, call off a reconnect that waits or runs, read unavailable and publish that. In `on_power_restored()` start from that same state, as it is also called when `on_power_cut()` was not (a play within the first seconds of a cut) and can be called twice: close what is left of an earlier connection, connect anew and return once the device is reached or can not be. The server waits at most 10 seconds for it and then ends the call, before the playback that waited for the start-up goes to the protocols that read available, so leave a retry of your own behind for a device that is not up yet. Either call can overlap a detach: after an `await`, check that the protocol is still attached before you go on. The Sonos and Chromecast players do this. A protocol without such a connection, one that connects for every stream, needs neither.
* Data that only lives while the server runs (`extra_data`, the stop and external source markers) stays on `self.player`.
* `self.player` is already `None` when `on_detach()` runs: a protocol releases what it holds itself.
* The same object can be attached again after a detach, when the server moves it to another player or changes its role: whatever `on_detach()` ends, `on_attach()` starts again.
* `on_remove()` also reaches a protocol that is not attached, as long as its provider holds the object: the server keeps those only weakly, so a provider with a configured device list keeps the object of every device on it.
* The config of a protocol has no name: the name the user gave the device is `self.player.config.name`.
* Software players that share a host report an id of their own instance as `UUID` or `SERIAL_NUMBER` identifier.
* A provider whose native protocol stands for devices of other makes (a Home Assistant media player) is ranked below the vendor's own protocol in `NATIVE_PRIORITY`: on a device that has both, it attaches as a control protocol of the vendor's player.
* Every zone of a multi-zone chassis reports its zone as `ZONE` next to the identifiers of the chassis: `MAIN_ZONE` for the zone where the protocols of the chassis that report no zone (AirPlay, DLNA, ...) play, its own id for every other zone.
* A protocol that delivers a set of speakers as one player (a stereo pair, a home theater set) reports `PlayerType.MULTI_CHANNEL` as its type (`_attr_type`) and the layout of its speakers (`_attr_channel_layout`). It reports the other speakers of the set with `mass.players.remove_absorbed_players(protocol_id, protocol_ids)`, which removes their players for good. So it names a speaker only when the set itself names it as one of its speakers at that moment, never because a speaker is silent, after a timeout or from an incomplete list, and only by a protocol id of its own provider. It registers no protocol for a speaker while the set names it. The Sonos and AirPlay players do this.

The server asks a provider to look for its devices again when one may be found again: after a player is enabled, after a player that is not a group is removed, and when a protocol that is not attached moves to another player (in a merge, or when it is taken off its player) or is switched on again as an output. It then replays the cached mDNS records of the provider's `mdns_discovery` types and reruns the SSDP search of its `upnp_discovery` targets. So `discover_players()` only has to offer the known devices that this cannot reach.

An audio protocol that matches no player and has no globally administered MAC address (none at all, or only a locally administered one) waits up to 15 seconds before it gets a player of its own, as the other protocols of its device may still identify it. A protocol that only exists while connected does not wait; a protocol that rides on another one waits for that one instead.

## 💽 Building your own Metadata Provider
Will follow soon™

## 🔌 Building your own Plugin Provider
Will follow soon™

A provider of any type that stores the id of a player (in its config or setup data) can find it turned into a legacy id: the user merged that player into another one, which answers to the stored id from then on. Resolve a stored id with `mass.players.resolve_player_id()` where it is read, and compare the ids of events and live players with the resolved id. A browser or app player only stays while its client is away once something about it is stored; a plugin that stores something the user set up for such a player (a DJ on its queue, a choice in its settings) calls `mass.players.keep_player()` for it, so the player and what was set up are there when the client comes back. Drop what you store about a player on `PLAYER_REMOVED`: it is only sent for a player that is gone for good, removed by the user, merged into another one, or a browser or app player whose client disconnected while nothing was stored of it and its queue was empty. A disabled player is no removal: it gets a last `PLAYER_UPDATED`, disabled and unavailable, and `PLAYER_ADDED` when it is enabled again. A browser or app player that was removed and connects again is a new player under the same id, announced with `PLAYER_ADDED`. A player stays while its device is away, so neither `PLAYER_ADDED` nor `get_player()` tells whether the device is there: a device that goes away or comes back is a change of `available` in `PLAYER_UPDATED`.

## ⚙️ Manifest file
The manifest file contains metadata and configuration about a provider. The supported properties are:
| Name  | Description  | Type  |
|---|---|---|
| type  | `music`, `player`, `metadata`, `plugin` or `audio_analysis`  | string  |
| domain  | The internal unique id of the provider, e.g. `spotify` or `ytmusic`  | string  |
| name  | The full name of the provider, e.g. `Spotify` or `Youtube Music`  | string  |
| description  | The full description of the provider  | string  |
| codeowners  | List of Github names of the codeowners of the provider  | array[string]  |
| support_url | Optional https URL where the maintainer takes bug reports and questions, when not the project's support repository. | string |
| stage | The development/stability stage: `alpha`, `beta`, `stable`, `experimental`, `unmaintained` or `deprecated`. Defaults to `stable`. | string |
| requirements | Python packages the provider needs, in standard pip / [PEP 508](https://peps.python.org/pep-0508/) requirement syntax, e.g. `package==version`, a version range, an extras marker, or a `git+https://…` direct reference. A provider in the community catalog may only use package names with optional extras and version specifiers (no URLs, environment markers or options), available as wheels for Linux amd64 and arm64. | array[string] |
| documentation | URL to the provider's documentation page. | string |
| multi_instance | Whether multiple instances of the provider are supported, e.g. multiple user accounts for Spotify | boolean |
| builtin | Whether this is a system/builtin provider that is loaded by default | boolean |
| allow_disable | Whether a builtin provider may be disabled. Defaults to `true`. | boolean |
| depends_on | Domain of another provider this provider needs to function | string |
| icon | Name of the [Material Design Icon](https://pictogrammers.com/library/mdi) to use for the provider | string |
| mdns_discovery | List of Zeroconf service types the provider wants to subscribe to. | array[string] |
| upnp_discovery | List of SSDP search targets the provider wants to subscribe to. | array[string] |
| self_service | Whether members may set up and reconfigure an instance of the provider as a music source of their own. Defaults to `true`. A provider whose setup reaches into the server itself, like a folder on its disk, may only keep it `true` when its setup flow checks what a member picks against what that member may use (the Local files flow checks every folder against `mass.storage.can_hold_music_source`); otherwise set it to `false` | boolean |
| credits | List of credits/attributions, e.g. for libraries or icons used. Accepts markdown formatting. | array[string] |
| menu_actions | Entries the provider adds to the menu of a media item, see [Menu actions](#-menu-actions). | array[object] |
| mcp_tools | Tools the provider offers to AI assistants, see [Tools for AI assistants (MCP)](#-tools-for-ai-assistants-mcp). | array[object] |
| page | A page the provider serves, offered in the main menu of the app, see [Provider pages](#-provider-pages). | object |
| provides | Optional metadata for discovery only, such as the Marketplace filters: what the provider offers whatever its config, known before it is loaded. A music provider lists the media types a user looks for it under: `artist`, `album`, `track`, `playlist`, `radio`, `podcast`, `audiobook` or `sound_effect`. A plugin lists its main features: `audio_source` (plays audio into Music Assistant as a live source), `ai` (provides AI engines), `tts` (provides speech engines), `scrobble` (records what is played to an outside listening history), `assistant` (lets an AI or voice assistant find music and control players), `visualizer` (turns music into light effects or visuals), `recommendations` (builds playlists, mixes or recommendations from your music) or `fun` (party modes and games for guests). A player provider can list `audio_source`. A provider with the `AUDIO_SOURCE`, `AI_QUERY`, `TTS` or `SCROBBLE` feature in every config lists the matching value. What a provider does at runtime always comes from its code. | array[string] |

A provider's config entries are not declared in the manifest. They are built in code by overriding `get_config_entries` on the provider, and their values are read via `self.config.get_value(key)`. One-time setup input is collected by the provider's setup flow (`setup_flow.py`).

Three more `ProviderManifest` fields are filled in automatically at load time and must not be set in the manifest: `icon_images` (the icon variants found in the provider folder — `icon.svg`/`icon.png`, `icon_dark.svg`/`icon_dark.png` and `icon_monochrome.svg`/`icon_monochrome.png`, with SVG preferred over PNG), `has_setup_flow` (set when the provider folder contains a `setup_flow.py`) and `tier` (`core`, built-in, for every provider that ships with the server).

## 📋 Menu actions
A provider can add its own entries to the menu of a track, album, artist, playlist, radio station, podcast, podcast episode or audiobook. It declares them in `menu_actions` in its manifest and implements what they do in its code; the Music Assistant app draws the entries, in a submenu under the name of the provider instance, so the provider ships no code for the app. The demo plugin (`_demo_plugin_provider`) has a complete example.

```json
"menu_actions": [
  {
    "id": "send_to_frame",
    "label": "Show on the picture frame",
    "media_types": ["album", "playlist"],
    "scope": "library.read",
    "icon": "mdi-image-frame",
    "requires_player": false
  }
]
```

* `id`: names the action within the provider, in lower case letters, digits and underscores (at most 32).
* `label`: the English text of the menu entry, one readable line of at most 40 characters. Put the same text in the provider's `strings.json` under `menu_actions.<id>.label` to have it translated, as with the other texts of the provider. A text from `strings.json` or `translations/` may have up to 80 characters, as translations grow; one that is not a single readable line of at most 80 is not used, and the label from the manifest is shown instead.
* `media_types`: the kinds of media item the entry is offered for: `track`, `album`, `artist`, `playlist`, `radio`, `podcast`, `podcast_episode` and `audiobook`.
* `scope`: what a user needs to use the action, such as `library.read`, `library.write` or `queues.control` (the values of `Scope` in the models). The app does not show the entry to a user without it, and the server refuses it.
* `icon`: optional, the name of a [Material Design Icon](https://pictogrammers.com/library/mdi), such as `mdi-image-frame`, of at most 64 characters.
* `requires_player`: optional, `true` when the action acts on a player. The app then also offers it in the menu of the play button, and passes the chosen player.

When a user chooses an entry, the app calls the API command `providers/invoke_menu_action` with the instance of the provider, the id of the action, the `uri` of the item and, for an action that requires a player, the `player_id`. The server checks that the user may use that instance and holds the scope of the action, that the `uri` is of one of its media types and that the user may use the player, and then calls `handle_menu_action(action_id, uri, player_id)` on that instance. The server only checks the kind of item in the `uri`, not that the item exists or that it belongs to your provider, so `handle_menu_action` looks the item up itself. Implement it in your provider: it answers nothing, or a `ConfigActionResult` with a message the app shows and/or a url it opens once (only an `https` url for a provider from the community catalog). To say the action failed, raise a `MusicAssistantError`. The provider registers no command. A provider that allows several instances needs nothing extra: the entries are offered for each instance, and each instance handles the actions chosen for it.

The server checks the declared actions when it reads the manifest. An action that breaks one of the rules above, has the same `id` as an earlier one, or comes after the first ten valid ones is left out with a warning in the log, and the provider loads without it. For a provider in this repository the same rules fail the `check_manifests` check. A provider in its own repository declares menu actions in the same way, with its texts in the `strings.json` and `translations/` of its own folder.

## 🤖 Tools for AI assistants (MCP)
A provider can offer its own tools to AI assistants, such as Home Assistant Assist, that use the MCP server of Music Assistant. It declares them in `mcp_tools` in its manifest and implements them in `handle_mcp_tool`; the MCP server offers them next to its own tools. The demo plugin (`_demo_plugin_provider`) has a complete example.

```json
"mcp_tools": [
  {
    "id": "play_station",
    "description": "Play one of the user's saved stations of this service on a player.",
    "scope": "queues.control",
    "requires_player": true,
    "input_schema": {
      "type": "object",
      "properties": {
        "station": {"type": "string", "description": "The name of the station."},
        "shuffle": {"type": "boolean", "description": "Shuffle the station.", "default": false}
      },
      "required": ["station"]
    }
  }
]
```

* `id`: names the tool within the provider, in groups of lower case letters and digits joined by single underscores (at most 32). The assistant sees the tool as `<domain>__<id>`, of at most 64 characters.
* `description`: what the tool does, one readable line of at most 300 characters, in English and not translated, as a language model reads it. Like the results, it only describes the tool and its outcome, and never instructs the assistant about other tools, the user or the conversation.
* `scope`: what a user needs to use the tool, such as `library.read` or `queues.control` (the values of `Scope` in the models). A user without it does not get the tool. A tool with a scope that ends in `.read` is announced as read-only; for any other tool a client may ask the user to confirm.
* `requires_player`: optional, `true` when the tool acts on a player.
* `input_schema`: optional, the arguments: `type` `object`, at most 10 `properties` and the `required` ones. A property name is lower case letters, digits and underscores, starts with a letter (at most 32) and is not `player` or `instance`. A property has a `type` (`string`, `integer`, `number`, `boolean` or `array`, whose `items` have one of the other types), a `description` (one readable line of at most 200 characters), and optionally a `default` of its type and, for strings, an `enum` of at most 50 values of at most 64 characters. Nothing else is accepted. A tool without `input_schema` takes no arguments.

The server adds a required `player` argument (a player name or id) to a tool that requires a player, a required `instance` argument when the user may use several instances of the provider, and ` (from the <provider name> provider)` to the description. It checks that the user may use the instance and holds the scope, that the arguments match the schema and that the user may use the player, and then calls `handle_mcp_tool(tool_id, arguments, player_id)` on that instance. `arguments` holds every declared property: the value passed, else its default, else `None`; `player_id` is the checked player, or `None` for a tool without one. What the tool calls on `mass` runs as the user, so it sees only the players and music sources the user may use; a tool that acts on a player other than `player_id` calls `mass.players.check_player_access` first. Return a `dict` of JSON values, short (at most 50 rows) and with media items by their `uri`. To say the tool failed, raise a `MusicAssistantError`: the assistant reads its message. Any other exception, or a result that is no `dict` of JSON values, is logged and the assistant reads "Internal error". The provider registers nothing: its tools come and go with it.

The server checks the declared tools when it reads the manifest, as it does menu actions: a tool that breaks a rule above, has the same `id` as an earlier one, or comes after the first ten valid ones is left out with a warning in the log, and for a provider in this repository it fails the `check_manifests` check.

## 🌐 Provider pages
A provider can serve a page of its own, such as a small web interface. It declares the page in `page` in its manifest, and the Music Assistant app offers it as an entry in its main menu, under a heading of its own, for each instance the user may use when the user holds the scope of the page.

```json
"page": {
  "label": "Picture frame",
  "scope": "library.read",
  "icon": "mdi-image-frame"
}
```

* `label`, `scope` and `icon` follow the rules of a [menu action](#-menu-actions). Put the label in the provider's `strings.json` under `page.label` to have it translated. A page that breaks a rule is left out with a warning in the log, and the provider loads without it; for a provider in this repository it fails the `check_manifests` check.

When a user chooses the entry, the app calls the API command `providers/open_page` with the instance. The server checks that the user may use that instance and holds the scope of the page, and answers a fresh, short-lived address of the page's root, which the app opens in a new tab. The provider registers no route: it implements `handle_page_request(request, path)` and returns any aiohttp response, a streamed or WebSocket one included, where `path` is the part of the address after the page's root.

* The server calls it only while that user may still use the instance and holds the scope of the page, and runs it as that user (`get_current_user()`). An address lapses after 15 minutes without a request and after 12 hours in any case.
* The page runs sandboxed in an origin of its own. It gets no token or cookie and can not reach the app, so it talks to the provider through relative urls to its own sub-paths (a `fetch` with JSON included). The server sets the security headers on every response and removes any cookie.
* A page may not offer downloads of provider audio (see the [Usage Policy](https://github.com/music-assistant/.github/blob/main/USAGE_POLICY.md)); the sandbox blocks downloads.

The demo plugin (`_demo_plugin_provider`) declares a one-line page.

## 🤝 Contributing a provider
**Consider the community catalog first.** Instead of adding a provider to the server itself, which comes with the expectations below and a longer wait for reviews, you can publish it in the [community catalog](https://github.com/music-assistant/.github/blob/main/COMMUNITY_CATALOG_RULES.md), especially when it covers a limited use case or a small group of users. There you keep it in your own repository and release whenever you want, and an administrator installs it from the Marketplace in Music Assistant. The [provider template](https://github.com/music-assistant/community-provider-template) gets you started.

A provider that ships with Music Assistant is installed for every user and supported by the project. A new one therefore asks for more than working code:

* **People ask for it.** Link the feature request or discussion that shows the demand.
* **It is documented.** A pull request on the [documentation](https://github.com/music-assistant/music-assistant.io/blob/main/CONTRIBUTING.md) comes with it.
* **It meets the standards.** It follows this guide and the [usage policy](https://github.com/music-assistant/.github/blob/main/USAGE_POLICY.md), passes `lint` and `test`, and has tests of its own.
* **You maintain it.** You are listed in `codeowners` and keep following up on bug reports and on changes in the service, the device or the server.

A provider whose maintainer no longer follows up may be moved out of the server to the [community catalog](https://github.com/music-assistant/.github/blob/main/COMMUNITY_CATALOG_RULES.md). That is announced first, so that someone can take it over. A maintainer can also move their own provider there, to release it from their own repository. Users keep it either way: a server that has it set up installs it from the community catalog after the upgrade, with its settings. How a move goes, and the check that guards it, is in the [community catalog README](music_assistant/controllers/community_catalog/README.md#moved-providers).

Every provider in this repository is reviewed against the same usage policy, whoever maintains it. The people listed in `codeowners` maintain the provider. Set `support_url` if you want bug reports and questions somewhere other than the support repository.

**Getting your changes merged**

Bot-MArley, the maintainers' review bot, reviews every PR that is ready for review within about an hour of its last push, and approves it when the review is clean and the change stays inside one provider. An approval does not merge the PR. As a codeowner of that provider, comment `@Bot-MArley merge` on a line of its own, and the bot adds the PR to the merge queue once its current commit is approved and `lint` and `test` are green. This works on PRs into `dev` that only change your provider's folders (`music_assistant/providers/<domain>/` and `tests/providers/<domain>/`), whoever opened them. After a new push, ask again.

A maintainer still approves or merges:
* a new provider, after checking it against the [usage policy](https://github.com/music-assistant/.github/blob/main/USAGE_POLICY.md);
* a change to your provider's `codeowners` or `support_url`;
* a change to `requirements`, or to anything outside your provider's folders;
* a PR of 1,000 changed lines or more;
* a change to a player provider, or other code that talks to a device, until you say in the PR which real device you tested it on.

Comment `@Bot-MArley help` on a PR for all of the bot's commands.

**Keeping your provider working**

Changes to the provider base classes land several times a month, and the core team updates every provider in the PR that makes them. When a change in Music Assistant still breaks your provider, you are expected to fix it.

**Backports**

A bugfix that also affects the current stable release can be backported. Ask for it in your PR: a maintainer adds the `backport-to-stable` label, and the fix is cherry-picked onto the stable branch once it merges into `dev`.
