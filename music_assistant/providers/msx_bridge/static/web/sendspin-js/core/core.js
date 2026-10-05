/**
 * SendspinCore: Protocol + decoding layer.
 *
 * Manages the WebSocket connection, Sendspin protocol, time synchronization,
 * state management, and audio decoding. Emits decoded PCM audio chunks that
 * can be consumed by SendspinPlayer for playback, or by visualization/analysis
 * tools directly.
 */
import { SendspinDecoder } from "../audio/decoder.js";
import { ProtocolHandler } from "./protocol-handler.js";
import { StateManager } from "./state-manager.js";
import { WebSocketManager } from "./websocket-manager.js";
import { SendspinTimeFilter } from "./time-filter.js";
import { StaticDelayStore } from "./static-delay-store.js";
import { clampSyncDelayMs } from "../sync-delay.js";
function generateRandomId() {
    return Math.random().toString(36).substring(2, 6);
}
export class SendspinCore {
    constructor(config) {
        const randomId = generateRandomId();
        const playerId = config.playerId ?? `sendspin-js-${randomId}`;
        const clientName = config.clientName ?? `Sendspin JS Client (${randomId})`;
        this.config = { ...config, playerId, clientName };
        // Initial delay precedence: explicit config, then persisted, then default.
        this.delayStore = new StaticDelayStore(config.storage ?? null);
        const persisted = this.delayStore.load();
        const initialDelay = config.syncDelay ?? persisted ?? config.defaultSyncDelay ?? 0;
        this._syncDelayMs = clampSyncDelayMs(initialDelay);
        this.timeFilter = new SendspinTimeFilter(0, 1.1, 2.0, 1e-12);
        this.stateManager = new StateManager(config.onStateChange);
        this.decoder = new SendspinDecoder((chunk) => this._onAudioData?.(chunk), () => this.stateManager.streamGeneration);
        this.wsManager = new WebSocketManager(config.reconnect);
        this.protocolHandler = new ProtocolHandler(playerId, this.wsManager, this, // this class implements StreamHandler
        this.stateManager, this.timeFilter, {
            clientName,
            codecs: config.codecs,
            bufferCapacity: config.bufferCapacity,
            requiredLeadTimeMs: config.requiredLeadTimeMs,
            minBufferMs: config.minBufferMs,
            useHardwareVolume: config.useHardwareVolume,
            onVolumeCommand: config.onVolumeCommand,
            onDelayCommand: config.onDelayCommand,
            getExternalVolume: config.getExternalVolume,
        });
    }
    // ========================================
    // StreamHandler implementation
    // (called by ProtocolHandler)
    // ========================================
    handleBinaryMessage(data) {
        const format = this.stateManager.currentStreamFormat;
        if (!format) {
            console.warn("Sendspin: Received audio chunk but no stream format set");
            return;
        }
        const generation = this.stateManager.streamGeneration;
        this.decoder.handleBinaryMessage(data, format, generation);
    }
    handleStreamStart(format, isFormatUpdate) {
        if (!isFormatUpdate) {
            this.decoder.clearState();
        }
        this._onStreamStart?.(format, isFormatUpdate);
    }
    handleStreamClear() {
        this.decoder.clearState();
        this._onStreamClear?.();
    }
    handleStreamEnd() {
        this.decoder.clearState();
        this._onStreamEnd?.();
    }
    handleVolumeUpdate() {
        this._onVolumeUpdate?.();
    }
    applyDelay(delayMs) {
        this._syncDelayMs = clampSyncDelayMs(delayMs);
        this.delayStore.save(this._syncDelayMs);
        this._onSyncDelayChange?.(this._syncDelayMs);
    }
    handleSyncDelayChange(delayMs) {
        this.applyDelay(delayMs);
    }
    getSyncDelayMs() {
        return this._syncDelayMs;
    }
    // ========================================
    // Event registration
    // ========================================
    set onAudioData(cb) {
        this._onAudioData = cb;
    }
    set onStreamStart(cb) {
        this._onStreamStart = cb;
    }
    set onStreamClear(cb) {
        this._onStreamClear = cb;
    }
    set onStreamEnd(cb) {
        this._onStreamEnd = cb;
    }
    set onVolumeUpdate(cb) {
        this._onVolumeUpdate = cb;
    }
    set onSyncDelayChange(cb) {
        this._onSyncDelayChange = cb;
    }
    set onConnectionOpen(cb) {
        this._onConnectionOpen = cb;
    }
    set onConnectionClose(cb) {
        this._onConnectionClose = cb;
    }
    // ========================================
    // Connection
    // ========================================
    async connect() {
        const onOpen = () => {
            this._onConnectionOpen?.();
            console.log("Sendspin: Using player_id:", this.config.playerId);
            this.protocolHandler.sendClientHello();
        };
        const onMessage = (event) => {
            this.protocolHandler.handleMessage(event);
        };
        const onError = (error) => {
            console.error("Sendspin: WebSocket error", error);
        };
        const onClose = () => {
            this.protocolHandler.stopTimeSync();
            // Stop periodic state-update sends so they don't spam
            // "WebSocket not connected" warnings after the transport is gone.
            this.stateManager.clearStateUpdateInterval();
            console.log("Sendspin: Connection closed");
            this._onConnectionClose?.();
        };
        if (this.config.webSocket) {
            // Adopt externally-managed WebSocket
            await this.wsManager.adopt(this.config.webSocket, onOpen, onMessage, onError, onClose);
        }
        else {
            // Create connection from baseUrl
            if (!this.config.baseUrl) {
                throw new Error("SendspinCore requires either baseUrl or webSocket to be provided.");
            }
            // Preserve path from baseUrl for reverse proxy support
            const url = new URL(this.config.baseUrl, typeof window !== "undefined" ? window.location.href : undefined);
            const wsProtocol = url.protocol === "https:" ? "wss:" : "ws:";
            const basePath = url.pathname.replace(/\/$/, "");
            const wsUrl = basePath.endsWith("/sendspin")
                ? `${wsProtocol}//${url.host}${basePath}`
                : `${wsProtocol}//${url.host}${basePath}/sendspin`;
            await this.wsManager.connect(wsUrl, onOpen, onMessage, onError, onClose);
        }
    }
    /**
     * Reset playback-related state (isPlaying, currentStreamFormat) without
     * tearing down the connection. Intended for transport-loss cleanup after
     * any buffered audio has finished draining.
     */
    resetPlaybackState() {
        this.stateManager.isPlaying = false;
        this.stateManager.currentStreamFormat = null;
    }
    disconnect(reason = "restart") {
        if (this.wsManager.isConnected()) {
            this.protocolHandler.sendGoodbye(reason);
        }
        this.protocolHandler.stopTimeSync();
        this.stateManager.clearAllIntervals();
        this.wsManager.disconnect();
        this.decoder.close();
        this.timeFilter.reset();
        this.stateManager.reset();
    }
    // ========================================
    // Volume / Mute
    // ========================================
    setVolume(volume) {
        this.stateManager.volume = volume;
        this._onVolumeUpdate?.();
        this.protocolHandler.sendStateUpdate();
    }
    setMuted(muted) {
        this.stateManager.muted = muted;
        this._onVolumeUpdate?.();
        this.protocolHandler.sendStateUpdate();
    }
    // ========================================
    // Sync delay
    // ========================================
    setSyncDelay(delayMs) {
        this.applyDelay(delayMs);
        this.protocolHandler.sendStateUpdate();
    }
    // ========================================
    // Buffer timing
    // ========================================
    setRequiredLeadTimeMs(leadTimeMs) {
        this.protocolHandler.setRequiredLeadTimeMs(leadTimeMs);
    }
    setMinBufferMs(minBufferMs) {
        this.protocolHandler.setMinBufferMs(minBufferMs);
    }
    // ========================================
    // Controller commands
    // ========================================
    sendCommand(command, params) {
        const supportedCommands = this.stateManager.serverState.controller?.supported_commands;
        if (supportedCommands && !supportedCommands.includes(command)) {
            throw new Error(`Command '${command}' is not supported by the server. ` +
                `Supported commands: ${supportedCommands.join(", ")}`);
        }
        this.protocolHandler.sendCommand(command, params);
    }
    // ========================================
    // State getters
    // ========================================
    get isPlaying() {
        return this.stateManager.isPlaying;
    }
    get volume() {
        return this.stateManager.volume;
    }
    get muted() {
        return this.stateManager.muted;
    }
    get playerState() {
        return this.stateManager.playerState;
    }
    get currentFormat() {
        return this.stateManager.currentStreamFormat;
    }
    get isConnected() {
        return this.wsManager.isConnected();
    }
    get timeSyncInfo() {
        return {
            synced: this.timeFilter.is_synchronized,
            offset: Math.round(this.timeFilter.offset / 1000),
            error: Math.round(this.timeFilter.error / 1000),
        };
    }
    getCurrentServerTimeUs() {
        return this.timeFilter.computeServerTime(Math.floor(performance.now() * 1000));
    }
    get trackProgress() {
        const metadata = this.stateManager.serverState.metadata;
        if (!metadata?.progress || metadata.timestamp === undefined) {
            return null;
        }
        const serverTimeUs = this.getCurrentServerTimeUs();
        const elapsedUs = serverTimeUs - metadata.timestamp;
        const positionMs = metadata.progress.track_progress +
            (elapsedUs * metadata.progress.playback_speed) / 1000000;
        const trackDuration = metadata.progress.track_duration;
        return {
            // track_duration 0 means unbounded (live radio), so floor at 0 only.
            positionMs: trackDuration === 0
                ? Math.max(0, positionMs)
                : Math.max(0, Math.min(positionMs, trackDuration)),
            durationMs: trackDuration,
            playbackSpeed: metadata.progress.playback_speed / 1000,
        };
    }
    // ========================================
    // Internal accessors (for SendspinPlayer)
    // ========================================
    /** @internal */
    get _stateManager() {
        return this.stateManager;
    }
    /** @internal */
    get _timeFilter() {
        return this.timeFilter;
    }
}
//# sourceMappingURL=core.js.map
