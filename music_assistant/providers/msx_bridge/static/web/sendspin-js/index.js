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
function detectIsSafari() {
    if (typeof navigator === "undefined")
        return false;
    const ua = navigator.userAgent;
    return /Safari/i.test(ua) && !/Chrome/i.test(ua);
}
function detectIsMac() {
    if (typeof navigator === "undefined")
        return false;
    return /Macintosh/i.test(navigator.userAgent);
}
function detectIsWindows() {
    if (typeof navigator === "undefined")
        return false;
    return /Windows/i.test(navigator.userAgent);
}
/**
 * Get platform-specific default static delay in milliseconds.
 * Based on testing across various platforms and browsers.
 */
function getDefaultSyncDelay() {
    if (detectIsIOS())
        return 250;
    if (detectIsAndroid())
        return 200;
    if (detectIsMac())
        return detectIsSafari() ? 190 : 150;
    if (detectIsWindows())
        return 250;
    // Linux and others
    return 200;
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
            playerId: config.playerId,
            baseUrl: config.baseUrl,
            clientName: config.clientName,
            webSocket: config.webSocket,
            codecs: config.codecs,
            bufferCapacity: config.bufferCapacity ??
                (outputMode === "media-element" ? 1024 * 1024 * 5 : 1024 * 1024 * 1.5),
            syncDelay: config.syncDelay,
            defaultSyncDelay: getDefaultSyncDelay(),
            storage,
            requiredLeadTimeMs: config.requiredLeadTimeMs,
            minBufferMs: config.minBufferMs,
            useHardwareVolume: config.useHardwareVolume,
            onVolumeCommand: config.onVolumeCommand,
            onDelayCommand: config.onDelayCommand,
            getExternalVolume: config.getExternalVolume,
            reconnect: config.reconnect,
            onStateChange: config.onStateChange,
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
            this.scheduler.resumeAudioContext();
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
export { SendspinDecoder } from "./audio/decoder.js";
export { AudioScheduler } from "./audio/scheduler.js";
// Export platform detection utilities
export { detectIsAndroid, detectIsIOS, detectIsMobile, detectIsCastRuntime, getDefaultSyncDelay, };
//# sourceMappingURL=index.js.map
