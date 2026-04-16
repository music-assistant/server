# AriaCast Server (Go Implementation)

This binary is a high-performance, lightweight receiver implementation for the AriaCast protocol, written in Go. It allows your Music Assistant instance to discover, connect to, and stream audio from AriaCast sources effortlessly.

[AriaCast Server Go](https://github.com/AirPlr/AriaCast-Server-GO)

## Features

-   **UDP Discovery**: Automatically announces itself to AriaCast clients on the network.
-   **Low Latency Streaming**: Receives audio via WebSocket and forwards it directly to a named pipe or local playback.
-   **Metadata Sync**: Real-time updates for Track Title, Artist, Album, and Artwork.
-   **Control API**: Supports playback controls (Play/Pause/Next/Previous) via HTTP/WebSocket.
-   **Web Dashboard**: Optional built-in web interface for testing and playback monitoring.
-   **Pipe Bridge**: Seamless integration with players (like `mpv` or `sox`) via named pipes.

## Endpoints

The server runs primarily on **port 12889**, alongside a UDP listener on **port 12888**.

-   **UDP 12888**: Service Discovery (Responds to `DISCOVER_AUDIOCAST`).
-   **WS `ws://IP:12889/audio`**: Binary audio stream receiver.
-   **WS `ws://IP:12889/metadata`**: Metadata updates.
-   **WS `ws://IP:12889/control`**: Remote control commands.
-   **POST `http://IP:12889/api/command`**: Send commands like `play`, `pause`, `next`.

## Audio Format

The server defaults to the following audio configuration:
-   **Sample Rate**: 48000 Hz
-   **Channels**: 2 (Stereo)
-   **Bit Depth**: 16-bit
