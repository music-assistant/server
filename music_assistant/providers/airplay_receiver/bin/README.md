# shairport-sync

The AirPlay Receiver runs a classic AirPlay build (not AirPlay 2) of
[shairport-sync](https://github.com/mikebrady/shairport-sync) 5.6.3 or newer, built with the
pipe backend and metadata support, and finds it on the system `PATH`.
`shairport-sync.conf` in this directory is the configuration template for each receiver.

## Docker

The base image (`Dockerfile.base`, `shairport-builder` stage) builds shairport-sync from
source against the image's own FFmpeg. The version and commit are pinned there.

## Local development

- **macOS:** `brew install shairport-sync`
- **Linux:** install your distribution's package if it is 5.6.3 or newer and not built for
  AirPlay 2. Otherwise build it from source with the `./configure` flags of the
  `shairport-builder` stage in `Dockerfile.base`, then `make install`.

`shairport-sync --version` must report 5.6.3 or newer, list `pipe` and `metadata`, and not
list `AirPlay2`.
