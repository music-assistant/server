# cliairplay binaries

This directory intentionally contains no executables in source control or Python
packages. Official container builds download the pinned Linux release asset and
verify it against `SHA256SUMS`.

For local development, download the matching `cliairplay-<platform>-<architecture>`
asset from the private
[airplay-cli releases](https://github.com/music-assistant/airplay-cli/releases),
place it in this directory, and make it executable. Keep downloaded binaries
untracked.
