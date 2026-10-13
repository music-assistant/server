import { SendspinCore } from "./core/core.js";
import { AudioScheduler } from "./audio/scheduler.js";
import { SILENT_AUDIO_SRC } from "./silent-audio.generated.js";
// Platform detection utilities
function detectIsAndroid() {
    if (typeof navigator === "undefined")
        return false;
    return /Android/i.test(navigator.userAgent);
}
function detectIsIOS() {
    if (typeof navigator === "undefined")
        return false;
    return (/iPad|iPhone|iPod/.test(navigator.userAgent) ||
        (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1));
}
function detectIsMobile() {
    return detectIsAndroid() || detectIsIOS();
}
function detectIsCastRuntime() {
    if (typeof navigator === "undefined")
        return false;
    return /CrKey/i.test(navigator.userAgent);
}
// Add a small cushion beyond the measured buffered runway so delayed timer
// delivery does not cut playback off just before the last scheduled audio ends.
const DISCONNECT_PLAYBACK_RESET_GRACE_MS = 250;
export class SendspinPlayer {
    constructor(config) {
        this.ownsAudioElement = false;
        this.disconnectPlaybackResetTimeout = null;
        this.suppressDisconnectPlaybackReset = false;
        // Auto-detect platform
        const isAndroid = detectIsAndroid();
        const isCastRuntime = detectIsCastRuntime();
        const isMobile = detectIsMobile();
        // Determine output mode
        const outputMode = config.audioElement || isMobile ? "media-element" : "direct";
        this.ownsAudioElement =
            outputMode === "media-element" && !config.audioElement;
        if (this.ownsAudioElement && typeof document === "undefined") {
            throw new Error("SendspinPlayer requires a DOM document to use media-element output without a provided audioElement.");
        }
        let storage = null;
        if (config.storage !== undefined) {
            storage = config.storage;
        }
        else if (typeof localStorage !== "undefined") {
            storage = localStorage;
        }
        // Create core (protocol + decoding). It resolves the effective initial
        // delay, so read it back below for the scheduler's starting value.
        this.core = new SendspinCore({
            baseUrl: config.baseUrl,
            clientName: config.clientName,
            productName: config.productName,
            webSocket: config.webSocket,
            codecs: config.codecs,
            bufferCapacity: config.bufferCapacity,
            syncDelay: config.syncDelay,
            defaultSyncDelay: config.defaultSyncDelay,
            storage,
            requiredLeadTimeMs: config.requiredLeadTimeMs,
            minBufferMs: config.minBufferMs,
            useHardwareVolume: config.useHardwareVolume,
            onVolumeCommand: config.onVolumeCommand,
            onDelayCommand: config.onDelayCommand,
            getExternalVolume: config.getExternalVolume,
            reconnect: config.reconnect,
            onStateChange: config.onStateChange,
            onPairing: config.onPairing,
            onPairingPin: config.onPairingPin,
            pinOutChannels: config.pinOutChannels,
            minPinLength: config.minPinLength,
            staticPin: config.staticPin,
            staticPinLocations: config.staticPinLocations,
            pairingPskLocations: config.pairingPskLocations,
            suite: config.suite,
            unpairedAccess: config.unpairedAccess,
            longTermPsks: config.longTermPsks,
        });
        const syncDelay = this.core.getSyncDelayMs();
        // Create scheduler (Web Audio playback)
        this.scheduler = new AudioScheduler({
            stateManager: this.core._stateManager,
            timeFilter: this.core._timeFilter,
            outputMode,
            audioElement: config.audioElement,
            isAndroid,
            isCastRuntime,
            ownsAudioElement: this.ownsAudioElement,
            silentAudioSrc: isAndroid ? SILENT_AUDIO_SRC : undefined,
            syncDelayMs: syncDelay,
            useHardwareVolume: config.useHardwareVolume ?? false,
            correctionMode: config.correctionMode ?? "sync",
            storage,
            useOutputLatencyCompensation: config.useOutputLatencyCompensation ?? true,
            correctionThresholds: config.correctionThresholds,
        });
        // Wire core events to scheduler
        this.core.onAudioData = (chunk) => {
            this.scheduler.handleDecodedChunk(chunk);
        };
        this.core.onStreamStart = (format, isFormatUpdate) => {
            this.scheduler.initAudioContext();
            void this.scheduler.resumeAudioContext().catch((error) => {
                console.warn("Sendspin: Failed to resume AudioContext:", error);
            });
            if (!isFormatUpdate) {
                this.scheduler.clearBuffers();
            }
            this.scheduler.startAudioElement();
        };
        this.core.onStreamClear = () => {
            this.scheduler.clearBuffers();
        };
        this.core.onStreamEnd = () => {
            this.scheduler.clearBuffers();
            this.scheduler.stopAudioElement();
        };
        this.core.onVolumeUpdate = () => {
            this.scheduler.updateVolume();
        };
        this.core.onSyncDelayChange = (delayMs) => {
            this.scheduler.setSyncDelay(delayMs);
        };
        // Wire connection lifecycle for disconnect playback deferral
        this.core.onConnectionOpen = () => {
            this.cancelPendingDisconnectPlaybackReset();
        };
        this.core.onConnectionClose = () => {
            if (this.suppressDisconnectPlaybackReset) {
                return;
            }
            this.scheduleDisconnectPlaybackReset();
        };
    }
    cancelPendingDisconnectPlaybackReset() {
        if (this.disconnectPlaybackResetTimeout !== null) {
            clearTimeout(this.disconnectPlaybackResetTimeout);
            this.disconnectPlaybackResetTimeout = null;
        }
    }
    resetPlaybackStateAfterDisconnect() {
        this.disconnectPlaybackResetTimeout = null;
        if (this.core.isConnected) {
            return;
        }
        this.scheduler.clearBuffers();
        this.core.resetPlaybackState();
        this.scheduler.stopAudioElement();
        if (typeof navigator !== "undefined" && navigator.mediaSession) {
            navigator.mediaSession.playbackState = "paused";
        }
    }
    scheduleDisconnectPlaybackReset() {
        this.cancelPendingDisconnectPlaybackReset();
        const runwaySec = this.scheduler.measureBufferedPlaybackRunwaySec();
        if (runwaySec <= 0) {
            this.resetPlaybackStateAfterDisconnect();
            return;
        }
        this.disconnectPlaybackResetTimeout = setTimeout(() => {
            this.resetPlaybackStateAfterDisconnect();
        }, runwaySec * 1000 + DISCONNECT_PLAYBACK_RESET_GRACE_MS);
    }
    /**
     * Initialize and resume audio playback. Call this directly from a click or
     * tap handler, before any other await, to satisfy browser autoplay policies.
     */
    async unlock() {
        this.scheduler.initAudioContext();
        await this.scheduler.resumeAudioContext();
    }
    // Connect to Sendspin server
    async connect() {
        this.suppressDisconnectPlaybackReset = false;
        return this.core.connect();
    }
    /**
     * Disconnect from Sendspin server
     * @param reason - Optional reason for disconnecting (default: 'restart')
     */
    disconnect(reason = "restart") {
        this.cancelPendingDisconnectPlaybackReset();
        this.suppressDisconnectPlaybackReset = true;
        this.core.disconnect(reason);
        // Close scheduler
        this.scheduler.close();
        // Reset MediaSession playbackState (if available)
        if (typeof navigator !== "undefined" && navigator.mediaSession) {
            navigator.mediaSession.playbackState = "none";
            navigator.mediaSession.metadata = null;
        }
    }
    // Set volume (0-100)
    setVolume(volume) {
        this.core.setVolume(volume);
    }
    // Set muted state
    setMuted(muted) {
        this.core.setMuted(muted);
    }
    // Set static delay (in milliseconds, 0-5000)
    setSyncDelay(delayMs) {
        this.core.setSyncDelay(delayMs);
    }
    /**
     * Update the reported startup lead time at runtime (ms). Reported to the
     * server via client/state. Debounce calls to avoid reacting to transient
     * fluctuations. Throws RangeError if not a non-negative finite number.
     */
    setRequiredLeadTimeMs(leadTimeMs) {
        this.core.setRequiredLeadTimeMs(leadTimeMs);
    }
    /**
     * Update the reported minimum ongoing buffer duration at runtime (ms).
     * Reported to the server via client/state. Debounce calls to avoid reacting
     * to transient fluctuations. Throws RangeError if not a non-negative finite
     * number.
     */
    setMinBufferMs(minBufferMs) {
        this.core.setMinBufferMs(minBufferMs);
    }
    /**
     * Set the sync correction mode at runtime.
     */
    setCorrectionMode(mode) {
        this.scheduler.setCorrectionMode(mode);
    }
    // ========================================
    // Controller Commands (sent to server)
    // ========================================
    /**
     * Send a controller command to the server.
     */
    sendCommand(command, params) {
        this.core.sendCommand(command, params);
    }
    // Getters for reactive state
    get isPlaying() {
        return this.core.isPlaying;
    }
    get volume() {
        return this.core.volume;
    }
    get muted() {
        return this.core.muted;
    }
    get playerState() {
        return this.core.playerState;
    }
    get currentFormat() {
        return this.core.currentFormat;
    }
    get isConnected() {
        return this.core.isConnected;
    }
    /** The client's stable identity id (base64url X25519 public key). */
    get clientId() {
        return this.core.clientId;
    }
    /** The client's Pairing PSK (base64url) for the operator to enter server-side. Null without storage. */
    get pairingPsk() {
        return this.core.pairingPsk;
    }
    get pairingToken() {
        return this.core.pairingToken;
    }
    /** Rotate the Pairing PSK, returning the new value (null without storage). */
    rotatePairingPsk() {
        return this.core.rotatePairingPsk();
    }
    /**
     * Operator gesture that opens the pairing window (~5 minutes, admits one
     * attempt). Required before each gesture-gated attempt: every static PIN
     * attempt, and dynamic PIN when escalated or the PIN is shorter than 6.
     * The "pending" pairing event fires when an attempt is waiting on this.
     */
    openPairingWindow() {
        this.core.openPairingWindow();
    }
    /** Cancel an in-progress pairing attempt (sends pair/abort user_cancelled). */
    cancelPairing() {
        this.core.cancelPairing();
    }
    /** Whether dynamic PIN has escalated to gesture-gating (10 failures). */
    isDynamicPinEscalated() {
        return this.core.isDynamicPinEscalated();
    }
    // Get current correction mode
    get correctionMode() {
        return this.scheduler.correctionMode;
    }
    // Time sync info for debugging
    get timeSyncInfo() {
        return this.core.timeSyncInfo;
    }
    /** Get current server time in microseconds using synchronized clock */
    getCurrentServerTimeUs() {
        return this.core.getCurrentServerTimeUs();
    }
    /** Get current track progress with real-time position calculation */
    get trackProgress() {
        return this.core.trackProgress;
    }
    // Sync info for debugging/display
    get syncInfo() {
        return this.scheduler.syncInfo;
    }
}
// Re-export types for convenience
export * from "./types.js";
export { SendspinTimeFilter } from "./core/time-filter.js";
export { SendspinCore } from "./core/core.js";
export { loadSendspinClientIdentity } from "./client-identity.js";
export { SendspinDecoder } from "./audio/decoder.js";
export { AudioScheduler } from "./audio/scheduler.js";
// Export platform detection utilities
export { detectIsAndroid, detectIsIOS, detectIsMobile, detectIsCastRuntime };
//# sourceMappingURL=index.js.map
