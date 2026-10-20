# Shairport-Sync Binaries

This directory contains the bundled shairport-sync binary for macOS development.

Linux deployments do not use bundled binaries: the docker image builds shairport-sync
from source in `Dockerfile.base` (see the `shairport-builder` stage), and the provider
finds it via its system PATH lookup. Other Linux installs should install shairport-sync
via their system package manager.

## Bundled Binaries

- `shairport-sync-macos-arm64` - macOS Apple Silicon (local development)

## Installation Options

### Option 1: System Package Manager (Recommended)

The easiest way to use this plugin is to install shairport-sync via your system's package manager:

**Debian/Ubuntu:**
```bash
apt-get update
apt-get install -y shairport-sync
```

**macOS (Homebrew):**
```bash
brew install shairport-sync
```

**Arch Linux:**
```bash
pacman -S shairport-sync
```

### Option 2: Build Static Binaries

If you want to include pre-built binaries with Music Assistant, you'll need to build them yourself. See `build_binaries.sh` for a script that helps with this process.

## Building Shairport-Sync

### Prerequisites

Shairport-sync requires several dependencies:
- OpenSSL
- Avahi (for mDNS/Bonjour)
- ALSA (Linux only)
- libpopt
- libconfig
- libsndfile
- libsoxr (optional, for resampling)

### Build Instructions

#### Linux (Static Build with musl)

```bash
# Install dependencies
apk add --no-cache \
    build-base \
    git \
    autoconf \
    automake \
    libtool \
    alsa-lib-dev \
    libconfig-dev \
    popt-dev \
    openssl-dev \
    avahi-dev \
    libsndfile-dev \
    libsoxr-dev

# Clone and build
git clone https://github.com/mikebrady/shairport-sync.git
cd shairport-sync
git checkout tags/4.3.7  # Use latest stable version
autoreconf -fi
./configure \
    --with-pipe \
    --with-metadata \
    --with-avahi \
    --with-ssl=openssl \
    --with-stdout \
    --with-soxr \
    LDFLAGS="-static"
make
strip shairport-sync

# Copy to provider bin directory
cp shairport-sync ../music_assistant/providers/airplay_receiver/bin/shairport-sync-linux-$(uname -m)
```

#### macOS

```bash
# Install dependencies
brew install autoconf automake libtool pkg-config openssl libsodium libsoxr popt libconfig

# Clone and build
git clone https://github.com/mikebrady/shairport-sync.git
cd shairport-sync
git checkout tags/4.3.7
autoreconf -fi
./configure \
    --with-pipe \
    --with-metadata \
    --with-ssl=openssl \
    --with-stdout \
    --with-soxr \
    PKG_CONFIG_PATH="/opt/homebrew/opt/openssl/lib/pkgconfig"
make
strip shairport-sync

# Copy to provider bin directory
cp shairport-sync ../music_assistant/providers/airplay_receiver/bin/shairport-sync-macos-$(uname -m)
```

## Docker Integration

The Music Assistant base image (`Dockerfile.base`, `shairport-builder` stage) builds
shairport-sync from source with the same configuration as `build_binaries.sh`, so the
binary is always linked against the image's own libraries.

### macOS Binary
- **shairport-sync-macos-arm64** (~262 KB)

⚠️ **Important**: The macOS binary requires Homebrew libraries to be installed:
```bash
brew install openssl libdaemon libconfig popt libao pulseaudio libsoxr
```

For macOS development, it's easier to just install shairport-sync via Homebrew:
```bash
brew install shairport-sync
```

**For local Linux development:**
```bash
# Debian/Ubuntu (recommended)
sudo apt-get install shairport-sync

# Arch Linux
sudo pacman -S shairport-sync

# Fedora
sudo dnf install shairport-sync
```

## Notes

- The helper code in `helpers.py` will automatically:
  1. Check for a bundled binary in this directory first (macOS only)
  2. Fall back to system-installed shairport-sync in PATH (docker image, package managers)
- Static linking is not feasible due to shairport-sync's numerous dependencies (glib, openssl, etc.)
