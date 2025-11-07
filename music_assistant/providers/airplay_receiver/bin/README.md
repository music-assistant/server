# Shairport-Sync Binaries

This directory should contain the shairport-sync binaries for different platforms.

## Required Binaries

- `shairport-sync-macos-arm64` - macOS Apple Silicon
- `shairport-sync-linux-x86_64` - Linux x86_64
- `shairport-sync-linux-aarch64` - Linux ARM64 (Raspberry Pi, etc.)

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

For Docker deployments, it's recommended to add shairport-sync to the Music Assistant base Docker image (`Dockerfile.base`) instead of bundling binaries:

```dockerfile
# Add to Dockerfile.base runtime stage
RUN apk add --no-cache \
    shairport-sync
```

Alternatively, build from source in the Docker image for the latest version.

## Bundled Binaries

This directory contains pre-built shairport-sync binaries for **local development only**.

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

### Linux Binaries (Alpine/musl)
- **shairport-sync-linux-x86_64** (~225 KB)
- **shairport-sync-linux-aarch64** (~261 KB)

These binaries are built with Alpine Linux (musl libc). While musl binaries CAN technically run on glibc systems (Debian/Ubuntu), they require the musl interpreter and musl versions of their dependencies to be installed.

**Recommendation:** For simplest deployment, install shairport-sync via your system's package manager instead of using these binaries.

**If using bundled binaries on Debian/Ubuntu:**
The plugin's helper will use these binaries if found, but they may require additional packages. If you encounter issues, install shairport-sync via apt instead:
```bash
sudo apt-get install shairport-sync
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
  1. Check for bundled binaries in this directory first (macOS only)
  2. Fall back to system-installed shairport-sync in PATH
- For production deployments, always use the system package manager
- Static linking is not feasible due to shairport-sync's numerous dependencies (avahi, openssl, etc.)
