import { TimeSyncManager } from "./time-sync-manager.js";
import { getDefaultBufferCapacity, getSupportedFormats } from "./codec-support.js";
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
    constructor(sender, helloContext, streamHandler, stateManager, timeFilter, config = {}) {
        this.sender = sender;
        this.helloContext = helloContext;
        this.streamHandler = streamHandler;
        this.stateManager = stateManager;
        this.timeFilter = timeFilter;
        this.activated = false;
        this.activeRoles = null;
        this.pairingSuspended = false;
        // Last player payload sent to the current server connection, or null when no
        // full state has been sent yet. Cleared on (re)connect so the first send is
        // full again.
        this.lastSentPlayer = null;
        this.clientName = config.clientName ?? "Sendspin Player";
        this.productName = config.productName;
        this.codecs = config.codecs ?? ["opus", "flac", "pcm"];
        // Left undefined so the capacity is derived from the formats actually
        // advertised in client/hello (see sendClientHello).
        this.bufferCapacity = config.bufferCapacity;
        this.requiredLeadTimeMs =
            config.requiredLeadTimeMs ?? DEFAULT_REQUIRED_LEAD_TIME_MS;
        assertBufferMs(this.requiredLeadTimeMs, "requiredLeadTimeMs");
        this.minBufferMs = config.minBufferMs ?? DEFAULT_MIN_BUFFER_MS;
        assertBufferMs(this.minBufferMs, "minBufferMs");
        this.useHardwareVolume = config.useHardwareVolume ?? false;
        this.onVolumeCommand = config.onVolumeCommand;
        this.onDelayCommand = config.onDelayCommand;
        this.getExternalVolume = config.getExternalVolume;
        this.timeSyncManager = new TimeSyncManager(sender, stateManager, timeFilter);
    }
    // Handle server messages
    handleServerMessage(message) {
        switch (message.type) {
            case "server/hello":
                this.handleServerHello();
                break;
            case "server/activate":
                this.handleServerActivate(message);
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
    // Handle server hello: reply with client/hello. client/state and time-sync
    // are deferred to server/activate.
    handleServerHello() {
        console.log("Sendspin: Connected to server");
        this.sendClientHello();
    }
    // Handle server/activate: start the initial client/state, time-sync, and
    // periodic state updates. Guarded so a repeat activate is a no-op.
    handleServerActivate(message) {
        this.pairingSuspended = false;
        let rolesChanged = false;
        if (message.payload.active_roles !== undefined) {
            const nextRoles = new Set(message.payload.active_roles);
            rolesChanged =
                this.activeRoles === null ||
                    nextRoles.size !== this.activeRoles.size ||
                    [...nextRoles].some((role) => !this.activeRoles.has(role));
            this.activeRoles = nextRoles;
        }
        if (this.activated) {
            if (rolesChanged)
                this.sendStateUpdate();
            return;
        }
        this.activated = true;
        this.sendStateUpdate();
        this.timeSyncManager.startAndSchedule();
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
    suspendForPairing() {
        this.pairingSuspended = true;
        this.activated = false;
        this.activeRoles = new Set();
        this.timeSyncManager.stop();
        this.stateManager.clearStateUpdateInterval();
    }
    /**
     * Clear the activate guard so the next server/activate (e.g. after a reconnect on a
     * reused handler) restarts time-sync and state updates.
     * @internal called by SendspinCore on transport close, not part of the public API.
     */
    resetActivation(preserveActiveRoles = false) {
        this.pairingSuspended = false;
        this.activated = false;
        if (!preserveActiveRoles)
            this.activeRoles = null;
        this.timeSyncManager.stop();
        this.stateManager.clearStateUpdateInterval();
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
    // client_id and version live in client/init, not the hello.
    sendClientHello() {
        const supportedFormats = getSupportedFormats(this.codecs);
        const hello = {
            type: "client/hello",
            payload: {
                name: this.clientName,
                supported_roles: ["player@v1", "controller@v1", "metadata@v1"],
                trust_level: this.helloContext.trustLevel(),
                supported_pair_methods: this.helloContext.pairMethods(),
                unpaired_access: { enabled: this.helloContext.unpairedAccess },
                device_info: {
                    product_name: this.productName,
                    manufacturer: (typeof navigator !== "undefined" && navigator.vendor) || "Unknown",
                    software_version: (typeof navigator !== "undefined" && navigator.userAgent) ||
                        "Unknown",
                },
                "player@v1_support": {
                    supported_formats: supportedFormats,
                    buffer_capacity: this.bufferCapacity ?? getDefaultBufferCapacity(supportedFormats),
                    supported_commands: ["volume", "mute"],
                },
            },
        };
        // Reset so the first client/state after connect is a full snapshot.
        this.lastSentPlayer = null;
        this.sender.sendControl(hello);
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
    // Send state update. The first send after a (re)connect is a full snapshot;
    // later sends are deltas carrying only changed fields, which the server merges.
    // When skipHardwareRead is true, use stateManager values instead of reading from hardware.
    // This avoids race conditions when responding to volume commands.
    sendStateUpdate(skipHardwareRead = false) {
        if (this.pairingSuspended)
            return;
        let volume = this.stateManager.volume;
        let muted = this.stateManager.muted;
        if (!skipHardwareRead && this.useHardwareVolume && this.getExternalVolume) {
            const externalVol = this.getExternalVolume();
            volume = externalVol.volume;
            muted = externalVol.muted;
        }
        const syncDelayMs = this.streamHandler.getSyncDelayMs();
        const staticDelayMs = clampSyncDelayMs(syncDelayMs);
        const payload = {
            available: true,
        };
        if (this.activeRoles === null || this.activeRoles.has("player@v1")) {
            const current = {
                volume,
                muted,
                static_delay_ms: staticDelayMs,
                required_lead_time_ms: this.requiredLeadTimeMs,
                min_buffer_ms: this.minBufferMs,
            };
            const last = this.lastSentPlayer;
            if (last === null) {
                // Full state: every field plus the static supported_commands.
                payload.player = {
                    ...current,
                    supported_commands: ["set_static_delay"],
                };
            }
            else {
                // Delta: only changed fields.
                const player = {};
                if (current.static_delay_ms !== last.static_delay_ms)
                    player.static_delay_ms = current.static_delay_ms;
                if (current.volume !== last.volume)
                    player.volume = current.volume;
                if (current.muted !== last.muted)
                    player.muted = current.muted;
                if (current.required_lead_time_ms !== last.required_lead_time_ms)
                    player.required_lead_time_ms = current.required_lead_time_ms;
                if (current.min_buffer_ms !== last.min_buffer_ms)
                    player.min_buffer_ms = current.min_buffer_ms;
                payload.player = player;
            }
            this.lastSentPlayer = current;
        }
        const message = {
            type: "client/state",
            payload,
        };
        this.sender.sendControl(message);
    }
    // Send goodbye message before disconnecting
    sendGoodbye(reason) {
        this.sender.sendControl({
            type: "client/goodbye",
            payload: {
                reason,
            },
        });
    }
    // Send controller command to server
    sendCommand(command, params) {
        if (this.pairingSuspended || !this.activeRoles?.has("controller@v1"))
            return;
        this.sender.sendControl({
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
