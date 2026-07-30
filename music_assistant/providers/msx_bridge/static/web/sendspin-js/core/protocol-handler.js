import { TimeSyncManager } from "./time-sync-manager.js";
import { getSupportedFormats } from "./codec-support.js";
import { clampSyncDelayMs } from "../sync-delay.js";
// Constants
const STATE_UPDATE_INTERVAL = 5000; // 5 seconds
const DEFAULT_REQUIRED_LEAD_TIME_MS = 250;
const DEFAULT_MIN_BUFFER_MS = 250;
function assertBufferMs(value, name) {
    if (!isFinite(value) || value < 0) {
        throw new RangeError(`${name} must be a non-negative finite number`);
    }
}
export class ProtocolHandler {
    constructor(playerId, wsManager, streamHandler, stateManager, timeFilter, config = {}) {
        this.playerId = playerId;
        this.wsManager = wsManager;
        this.streamHandler = streamHandler;
        this.stateManager = stateManager;
        this.timeFilter = timeFilter;
        this.clientName = config.clientName ?? "Sendspin Player";
        this.codecs = config.codecs ?? ["opus", "flac", "pcm"];
        this.bufferCapacity = config.bufferCapacity ?? 1024 * 1024 * 5; // 5MB default
        this.requiredLeadTimeMs =
            config.requiredLeadTimeMs ?? DEFAULT_REQUIRED_LEAD_TIME_MS;
        assertBufferMs(this.requiredLeadTimeMs, "requiredLeadTimeMs");
        this.minBufferMs = config.minBufferMs ?? DEFAULT_MIN_BUFFER_MS;
        assertBufferMs(this.minBufferMs, "minBufferMs");
        this.useHardwareVolume = config.useHardwareVolume ?? false;
        this.onVolumeCommand = config.onVolumeCommand;
        this.onDelayCommand = config.onDelayCommand;
        this.getExternalVolume = config.getExternalVolume;
        this.timeSyncManager = new TimeSyncManager(wsManager, stateManager, timeFilter);
    }
    // Handle WebSocket messages
    handleMessage(event) {
        if (typeof event.data === "string") {
            // JSON message
            const message = JSON.parse(event.data);
            this.handleServerMessage(message);
        }
        else if (event.data instanceof ArrayBuffer) {
            // Binary message (audio chunk)
            this.streamHandler.handleBinaryMessage(event.data);
        }
        else if (event.data instanceof Blob) {
            // Convert Blob to ArrayBuffer
            event.data.arrayBuffer().then((buffer) => {
                this.streamHandler.handleBinaryMessage(buffer);
            });
        }
    }
    // Handle server messages
    handleServerMessage(message) {
        switch (message.type) {
            case "server/hello":
                this.handleServerHello();
                break;
            case "server/time":
                this.timeSyncManager.handleServerTime(message);
                break;
            case "stream/start":
                this.handleStreamStart(message);
                break;
            case "stream/clear":
                this.handleStreamClear(message);
                break;
            case "stream/end":
                this.handleStreamEnd(message);
                break;
            case "server/command":
                this.handleServerCommand(message);
                break;
            case "server/state":
                this.stateManager.updateServerState(message.payload);
                break;
            case "group/update":
                this.stateManager.updateGroupState(message.payload);
                break;
        }
    }
    // Handle server hello
    handleServerHello() {
        console.log("Sendspin: Connected to server");
        // Per spec: Send initial client/state immediately after server/hello
        this.sendStateUpdate();
        // Start time synchronization with fixed bursts.
        this.timeSyncManager.startAndSchedule();
        // Start periodic state updates
        const stateInterval = globalThis.setInterval(() => this.sendStateUpdate(), STATE_UPDATE_INTERVAL);
        this.stateManager.setStateUpdateInterval(stateInterval);
    }
    // Restart the periodic state update interval.
    // Called after volume commands to prevent a pending periodic update
    // from sending stale hardware volume shortly after the command response.
    restartStateUpdateInterval() {
        const newInterval = globalThis.setInterval(() => this.sendStateUpdate(), STATE_UPDATE_INTERVAL);
        this.stateManager.setStateUpdateInterval(newInterval);
    }
    stopTimeSync() {
        this.timeSyncManager.stop();
    }
    handleStreamStart(message) {
        const isFormatUpdate = this.stateManager.currentStreamFormat !== null;
        this.stateManager.currentStreamFormat = message.payload.player;
        console.log(isFormatUpdate
            ? "Sendspin: Stream format updated"
            : "Sendspin: Stream started", this.stateManager.currentStreamFormat);
        console.log(`Sendspin: Codec=${this.stateManager.currentStreamFormat.codec.toUpperCase()}, ` +
            `SampleRate=${this.stateManager.currentStreamFormat.sample_rate}Hz, ` +
            `Channels=${this.stateManager.currentStreamFormat.channels}, ` +
            `BitDepth=${this.stateManager.currentStreamFormat.bit_depth}bit`);
        this.streamHandler.handleStreamStart(this.stateManager.currentStreamFormat, isFormatUpdate);
        this.stateManager.isPlaying = true;
        // Explicitly set playbackState for Android (if mediaSession available)
        if (typeof navigator !== "undefined" && navigator.mediaSession) {
            navigator.mediaSession.playbackState = "playing";
        }
    }
    handleStreamClear(message) {
        const roles = message.payload.roles;
        if (!roles || roles.includes("player")) {
            console.log("Sendspin: Stream clear (seek)");
            this.streamHandler.handleStreamClear();
        }
    }
    handleStreamEnd(message) {
        const roles = message.payload?.roles;
        if (!roles || roles.includes("player")) {
            console.log("Sendspin: Stream ended");
            this.streamHandler.handleStreamEnd();
            this.stateManager.currentStreamFormat = null;
            this.stateManager.isPlaying = false;
            if (typeof navigator !== "undefined" && navigator.mediaSession) {
                navigator.mediaSession.playbackState = "paused";
            }
            this.sendStateUpdate();
        }
    }
    // Handle server commands
    handleServerCommand(message) {
        const playerCommand = message.payload.player;
        if (!playerCommand)
            return;
        switch (playerCommand.command) {
            case "volume":
                // Set volume command
                if (playerCommand.volume !== undefined) {
                    this.stateManager.volume = playerCommand.volume;
                    this.streamHandler.handleVolumeUpdate();
                    // Notify external handler for hardware volume
                    if (this.useHardwareVolume && this.onVolumeCommand) {
                        this.onVolumeCommand(playerCommand.volume, this.stateManager.muted);
                    }
                }
                break;
            case "mute":
                // Mute/unmute command - uses boolean mute field
                if (playerCommand.mute !== undefined) {
                    this.stateManager.muted = playerCommand.mute;
                    this.streamHandler.handleVolumeUpdate();
                    // Notify external handler for hardware volume
                    if (this.useHardwareVolume && this.onVolumeCommand) {
                        this.onVolumeCommand(this.stateManager.volume, playerCommand.mute);
                    }
                }
                break;
            case "set_static_delay": {
                const delay = playerCommand.static_delay_ms;
                if (typeof delay === "number" && isFinite(delay)) {
                    const clamped = clampSyncDelayMs(delay);
                    this.streamHandler.handleSyncDelayChange(clamped);
                    this.onDelayCommand?.(clamped);
                }
                break;
            }
        }
        // Reset periodic timer first, then send state with commanded values.
        // Skip hardware read to avoid race where hardware hasn't applied the volume yet.
        this.restartStateUpdateInterval();
        this.sendStateUpdate(true);
    }
    // Send client hello with player identification
    sendClientHello() {
        const hello = {
            type: "client/hello",
            payload: {
                client_id: this.playerId,
                name: this.clientName,
                version: 1,
                supported_roles: ["player@v1", "controller@v1", "metadata@v1"],
                device_info: {
                    product_name: "Web Browser",
                    manufacturer: (typeof navigator !== "undefined" && navigator.vendor) || "Unknown",
                    software_version: (typeof navigator !== "undefined" && navigator.userAgent) ||
                        "Unknown",
                },
                "player@v1_support": {
                    supported_formats: getSupportedFormats(this.codecs),
                    buffer_capacity: this.bufferCapacity,
                    supported_commands: ["volume", "mute"],
                },
            },
        };
        this.wsManager.send(hello);
    }
    setRequiredLeadTimeMs(leadTimeMs) {
        assertBufferMs(leadTimeMs, "requiredLeadTimeMs");
        this.requiredLeadTimeMs = leadTimeMs;
        this.sendStateUpdate();
    }
    setMinBufferMs(minBufferMs) {
        assertBufferMs(minBufferMs, "minBufferMs");
        this.minBufferMs = minBufferMs;
        this.sendStateUpdate();
    }
    // Send state update
    // When skipHardwareRead is true, use stateManager values instead of reading from hardware.
    // This avoids race conditions when responding to volume commands.
    sendStateUpdate(skipHardwareRead = false) {
        let volume = this.stateManager.volume;
        let muted = this.stateManager.muted;
        if (!skipHardwareRead && this.useHardwareVolume && this.getExternalVolume) {
            const externalVol = this.getExternalVolume();
            volume = externalVol.volume;
            muted = externalVol.muted;
        }
        const syncDelayMs = this.streamHandler.getSyncDelayMs();
        const staticDelayMs = clampSyncDelayMs(syncDelayMs);
        const message = {
            type: "client/state",
            payload: {
                player: {
                    state: this.stateManager.playerState,
                    volume,
                    muted,
                    static_delay_ms: staticDelayMs,
                    required_lead_time_ms: this.requiredLeadTimeMs,
                    min_buffer_ms: this.minBufferMs,
                    supported_commands: ["set_static_delay"],
                },
            },
        };
        this.wsManager.send(message);
    }
    // Send goodbye message before disconnecting
    sendGoodbye(reason) {
        this.wsManager.send({
            type: "client/goodbye",
            payload: {
                reason,
            },
        });
    }
    // Send controller command to server
    sendCommand(command, params) {
        this.wsManager.send({
            type: "client/command",
            payload: {
                controller: {
                    command,
                    ...params,
                },
            },
        });
    }
}
//# sourceMappingURL=protocol-handler.js.map
