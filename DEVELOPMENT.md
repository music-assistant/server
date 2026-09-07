Developer docs
==================================

## 📝 Prerequisites
* ffmpeg (minimum version 6.1, version 7 recommended), must be available in the path so install at OS level
* Python 3.14 is minimal required (the exact pinned runtime lives in `.python-version` at the repo root — that file is the single source of truth for all tools)
* [Python venv](https://docs.python.org/3/library/venv.html)
* libchromaprint, also at OS level. Install it if you want to run the `acoustid_lookup` provider: without libchromaprint AcoustID refuses to load. The test suite does not need it: the AcoustID tests drive a fake fingerprinter.

      # macOS
      brew install chromaprint

      # Debian/Ubuntu
      sudo apt-get install libchromaprint1

    On Apple Silicon, Homebrew installs into `/opt/homebrew`, which is not on the dynamic loader's default search path. `pyacoustid` opens libchromaprint by bare filename, so installing the package is not enough — add this to your shell profile as well:

      export DYLD_FALLBACK_LIBRARY_PATH="$(brew --prefix)/lib:/usr/local/lib:/usr/lib"

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
* Debug: Hit (Fn +) F5 to start Music Assistant locally (VS Code), or run `python -m music_assistant --log-level debug` from the command line
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

## Note on async Python
The Music Assistant server is fully built in Python. The Python language has no real supported for multi-threading. This is why Music Assistant heavily relies on asyncio to handle blocking IO. It is important to get a good understanding of asynchronous programming before building your first provider. [This](https://www.youtube.com/watch?v=M-UcUs7IMIM) video is an excellent first step in the world of asyncio.





## Building a new Music Provider
A Music Provider is the provider type that adds support for a 'source of music' to Music Assistant. Spotify and Youtube Music are examples of a Music Provider, but also Filesystem and SMB can be put in the Music Provider category. All Providers (of all types) can be found in the `music_assistant/providers` folder.

TIP: We have created a template/stub provider in `music_assistant/providers/_demo_music_provider` to get you started fast!


**Adding the necessary files for a new Music Provider**

Add a new folder to the `providers` folder with the name of provider. Add two files inside:
1. `__init__.py`. This file contains the Python code of your provider.
2. `manifest.json`. This file contains metadata and configuration for your provider.

**Configuring the manifest.json file**

The easiest way to get start is to copy the contents of the manifest of an existing Music Provider, e.g. Spotify or Youtube Music. See [the manifest section](#⚙️-manifest-file) for all available properties.

**Creating the provider**

Create a file called `__init__.py` inside the folder of your provider. This file will contain the logic for the provider. All Music Providers must inherit from the [`MusicProvider`](./music_assistant/models/music_provider.py) base class and override the necessary functions where applicable. A few things to note:
* The `setup()` function is called by Music Assistant upon initialization of the provider. It gives you the opportunity the prepare the provider for usage. For example, logging in a user or obtaining a token can be done in this function.
* A provider should let Music Assistant know which [`ProviderFeature`](https://github.com/music-assistant/models/blob/main/music_assistant_models/enums.py) it supports by implementing the property `supported_features`, which returns a list of `ProviderFeature`.
* A podcast provider must set each episode's `position` so that the oldest episode has the lowest number and the newest the highest. The episode listing is sorted on it and "play from here" queues the selected episode plus everything newer, so a provider that numbers episodes by the order its API happens to return them will show them backwards. Rank on the publication date using `rank_episodes_by_date()` in [`podcast_parsers`](./music_assistant/helpers/podcast_parsers.py), and leave the position at 0 where it is unknown.
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

## 💽 Building your own Metadata Provider
Will follow soon™

## 🔌 Building your own Plugin Provider
Will follow soon™

## ⚙️ Manifest file
The manifest file contains metadata and configuration about a provider. The supported properties are:
| Name  | Description  | Type  |
|---|---|---|
| type  | `music`, `player`, `metadata` or `plugin`  | string  |
| domain  | The internal unique id of the provider, e.g. `spotify` or `ytmusic`  | string  |
| name  | The full name of the provider, e.g. `Spotify` or `Youtube Music`  | string  |
| description  | The full description of the provider  | string  |
| codeowners  | List of Github names of the codeowners of the provider  | array[string]  |
| config_entries  | List of configurable properties for the provider, e.g. `username` or `password`*. | array[object]  |
| config_entries.key  | The unique key of the config entry, used to obtain the value in the provider code  | string  |
| config_entries.type  | The type of the config entry. Possible values: `string`, `secure_string` (for passwords), `boolean`, `float`, `integer`, `label` (for a single line of text in the settings page)  | string  |
| config_entries.label | The label of the config entry. Used in the settings page | string |
| requirements | List of requirements for the provider in pip string format. Supported values are `package==version` and `git+https://gitrepoforpackage` | array[string]
| documentation | URL to the Github discussion containing the documentation for the provider. | string |
| multi_instances | Whether multiple instances of the configuration are supported, e.g. multiple user accounts for Spotify | boolean |
| mdns_discovery | List of Zeroconf service types the provider wants to subscribe to. | array[string] |
| upnp_discovery | List of SSDP search targets the provider wants to subscribe to. | array[string] |

\* These `config_entries` are used to automatically generate the settings page for the provider in the front-end. The values can be obtained via `self.config.get_value(key)`.
