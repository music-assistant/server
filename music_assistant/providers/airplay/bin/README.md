# cliairplay binaries

This directory intentionally contains no executables in source control or Python
packages. Official container builds download the pinned Linux release asset and
verify it against `SHA256SUMS`.

For local development, `scripts/setup.sh` downloads and verifies the same pinned
release asset when the platform binary is not already present. To install one
manually, download the matching `cliairplay-<platform>-<architecture>` asset from the
[airplay-cli releases](https://github.com/music-assistant/airplay-cli/releases),
place it in this directory, and make it executable. Keep downloaded binaries
untracked.
