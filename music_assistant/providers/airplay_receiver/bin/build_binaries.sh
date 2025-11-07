#!/usr/bin/env bash
set -e

# Build script for shairport-sync binaries across different platforms
# This script uses Docker to build binaries in isolated environments

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHAIRPORT_VERSION="${SHAIRPORT_VERSION:-4.3.7}"

echo "Building shairport-sync ${SHAIRPORT_VERSION} binaries..."

# Function to build Linux binaries using Docker
build_linux() {
    local arch="$1"
    local platform="$2"

    echo "Building for Linux ${arch}..."

    docker run --rm \
        --platform "${platform}" \
        -v "${SCRIPT_DIR}:/output" \
        debian:bookworm-slim \
        /bin/bash -c "
            set -e

            # Install build dependencies
            # NOTE: Do NOT install libavahi-client-dev - we want tinysvcmdns instead
            apt-get update && apt-get install -y --no-install-recommends \
                build-essential \
                git \
                autoconf \
                automake \
                libtool \
                pkg-config \
                libconfig-dev \
                libpopt-dev \
                libssl-dev \
                libdbus-1-dev \
                libglib2.0-dev \
                ca-certificates

            # Clone and checkout specific version
            cd /tmp
            git clone --depth 1 --branch ${SHAIRPORT_VERSION} https://github.com/mikebrady/shairport-sync.git
            cd shairport-sync

            # Configure and build
            # Build with tinysvcmdns (lightweight embedded mDNS, no external daemon needed)
            autoreconf -fi
            ./configure \
                --with-pipe \
                --with-metadata \
                --without-avahi \
                --without-dns-sd \
                --with-tinysvcmdns \
                --with-ssl=openssl \
                --with-stdout \
                --sysconfdir=/etc

            make -j\$(nproc)

            # Strip binary to reduce size
            strip shairport-sync

            # Copy to output
            cp shairport-sync /output/shairport-sync-linux-${arch}
            chmod +x /output/shairport-sync-linux-${arch}

            # Show size
            ls -lh /output/shairport-sync-linux-${arch}
        "

    echo "✓ Built shairport-sync-linux-${arch}"
}

# Function to build macOS binary
build_macos() {
    if [[ "$(uname)" != "Darwin" ]]; then
        echo "⚠ Skipping macOS build (must run on macOS)"
        return
    fi

    echo "Building for macOS arm64..."

    # Check if Homebrew is installed
    if ! command -v brew &> /dev/null; then
        echo "Error: Homebrew is required to build on macOS"
        exit 1
    fi

    # Install dependencies
    echo "Installing dependencies via Homebrew..."
    brew list autoconf &> /dev/null || brew install autoconf
    brew list automake &> /dev/null || brew install automake
    brew list libtool &> /dev/null || brew install libtool
    brew list pkg-config &> /dev/null || brew install pkg-config
    brew list openssl &> /dev/null || brew install openssl
    brew list popt &> /dev/null || brew install popt
    brew list libconfig &> /dev/null || brew install libconfig
    brew list libdaemon &> /dev/null || brew install libdaemon

    # Create temp directory
    TEMP_DIR=$(mktemp -d)
    cd "${TEMP_DIR}"

    # Clone and build
    git clone --depth 1 --branch "${SHAIRPORT_VERSION}" https://github.com/mikebrady/shairport-sync.git
    cd shairport-sync

    autoreconf -fi

    # On macOS, librt is not needed and doesn't exist - patch configure to skip the check
    sed -i.bak 's/as_fn_error $? "librt needed" "$LINENO" 5/echo "librt check skipped on macOS"/' configure

    # Build with tinysvcmdns (lightweight embedded mDNS) for macOS
    # Note: We still register via Music Assistant's Zeroconf, but shairport-sync
    # needs some mDNS backend present to function properly
    ./configure \
        --with-pipe \
        --with-metadata \
        --with-ssl=openssl \
        --with-stdout \
        --without-avahi \
        --without-dns-sd \
        --with-tinysvcmdns \
        --with-libdaemon \
        PKG_CONFIG_PATH="$(brew --prefix openssl)/lib/pkgconfig:$(brew --prefix libconfig)/lib/pkgconfig" \
        LDFLAGS="-L$(brew --prefix)/lib" \
        CFLAGS="-I$(brew --prefix)/include" \
        LIBS="-lm -lpthread -lssl -lcrypto -lconfig -lpopt"

    make -j$(sysctl -n hw.ncpu)

    # Strip binary
    strip shairport-sync

    # Copy to output
    cp shairport-sync "${SCRIPT_DIR}/shairport-sync-macos-$(uname -m)"
    chmod +x "${SCRIPT_DIR}/shairport-sync-macos-$(uname -m)"

    # Cleanup
    cd "${SCRIPT_DIR}"
    rm -rf "${TEMP_DIR}"

    ls -lh "${SCRIPT_DIR}/shairport-sync-macos-$(uname -m)"
    echo "✓ Built shairport-sync-macos-$(uname -m)"
}

# Main build process
case "${1:-all}" in
    linux-x86_64)
        build_linux "x86_64" "linux/amd64"
        ;;
    linux-aarch64)
        build_linux "aarch64" "linux/arm64"
        ;;
    macos)
        build_macos
        ;;
    all)
        build_linux "x86_64" "linux/amd64"
        build_linux "aarch64" "linux/arm64"
        build_macos
        ;;
    *)
        echo "Usage: $0 {linux-x86_64|linux-aarch64|macos|all}"
        echo
        echo "Environment variables:"
        echo "  SHAIRPORT_VERSION  - Version to build (default: 4.3.7)"
        exit 1
        ;;
esac

echo
echo "Build complete! Binaries are in:"
ls -lh "${SCRIPT_DIR}"/shairport-sync-* 2>/dev/null || echo "No binaries found"
echo
echo "Note: These binaries are dynamically linked. For Docker deployments,"
echo "it's recommended to install shairport-sync via apk/apt instead."
