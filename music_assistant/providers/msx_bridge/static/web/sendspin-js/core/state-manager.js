/**
 * Apply a diff to an object, returning a new copy.
 * - Fields from diff are merged into the copy
 * - null values delete the key from the result
 * - Nested objects are merged recursively (one level deep)
 */
function applyDiff(existing, diff) {
    const result = { ...existing };
    for (const key of Object.keys(diff)) {
        const value = diff[key];
        if (value === null) {
            delete result[key];
        }
        else if (value !== undefined) {
            // If both existing and new value are plain objects, merge recursively
            const existingValue = result[key];
            if (typeof value === "object" &&
                !Array.isArray(value) &&
                typeof existingValue === "object" &&
                existingValue !== null &&
                !Array.isArray(existingValue)) {
                result[key] = applyDiff(existingValue, value);
            }
            else {
                result[key] = value;
            }
        }
    }
    return result;
}
export class StateManager {
    constructor(onStateChange) {
        this._volume = 100;
        this._muted = false;
        this._playerState = "synchronized";
        this._isPlaying = false;
        this._currentStreamFormat = null;
        this._streamStartServerTime = 0;
        this._streamStartAudioTime = 0;
        this._streamGeneration = 0;
        // Cached server state (from server/state messages)
        this._serverState = {};
        // Cached group state (from group/update messages)
        this._groupState = {};
        // Interval references for cleanup
        this.timeSyncInterval = null;
        this.stateUpdateInterval = null;
        this.onStateChangeCallback = onStateChange;
    }
    // Volume & Mute
    get volume() {
        return this._volume;
    }
    set volume(value) {
        this._volume = Math.max(0, Math.min(100, value));
        this.notifyStateChange();
    }
    get muted() {
        return this._muted;
    }
    set muted(value) {
        this._muted = value;
        this.notifyStateChange();
    }
    // Player State
    get playerState() {
        return this._playerState;
    }
    set playerState(value) {
        this._playerState = value;
        this.notifyStateChange();
    }
    // Playing State
    get isPlaying() {
        return this._isPlaying;
    }
    set isPlaying(value) {
        this._isPlaying = value;
        this.notifyStateChange();
    }
    // Stream Format
    get currentStreamFormat() {
        return this._currentStreamFormat;
    }
    set currentStreamFormat(value) {
        this._currentStreamFormat = value;
    }
    // Stream Anchoring (for timestamp-based scheduling)
    get streamStartServerTime() {
        return this._streamStartServerTime;
    }
    set streamStartServerTime(value) {
        this._streamStartServerTime = value;
    }
    get streamStartAudioTime() {
        return this._streamStartAudioTime;
    }
    set streamStartAudioTime(value) {
        this._streamStartAudioTime = value;
    }
    // Reset stream anchors (called on stream start)
    resetStreamAnchors() {
        this._streamStartServerTime = 0;
        this._streamStartAudioTime = 0;
        this._streamGeneration++;
    }
    // Get current stream generation
    get streamGeneration() {
        return this._streamGeneration;
    }
    // Interval management
    setTimeSyncInterval(interval) {
        this.clearTimeSyncInterval();
        this.timeSyncInterval = interval;
    }
    clearTimeSyncInterval() {
        if (this.timeSyncInterval !== null) {
            clearTimeout(this.timeSyncInterval);
            this.timeSyncInterval = null;
        }
    }
    setStateUpdateInterval(interval) {
        this.clearStateUpdateInterval();
        this.stateUpdateInterval = interval;
    }
    clearStateUpdateInterval() {
        if (this.stateUpdateInterval !== null) {
            clearInterval(this.stateUpdateInterval);
            this.stateUpdateInterval = null;
        }
    }
    clearAllIntervals() {
        this.clearTimeSyncInterval();
        this.clearStateUpdateInterval();
    }
    // Reset all state (called on disconnect)
    reset() {
        this._volume = 100;
        this._muted = false;
        this._playerState = "synchronized";
        this._isPlaying = false;
        this._currentStreamFormat = null;
        this._streamStartServerTime = 0;
        this._streamStartAudioTime = 0;
        this._serverState = {};
        this._groupState = {};
        this.clearAllIntervals();
    }
    // Notify callback of state changes
    notifyStateChange() {
        if (this.onStateChangeCallback) {
            this.onStateChangeCallback({
                isPlaying: this._isPlaying,
                volume: this._volume,
                muted: this._muted,
                playerState: this._playerState,
                serverState: this._serverState,
                groupState: this._groupState,
            });
        }
    }
    // Update server state (merges delta, null clears fields)
    updateServerState(update) {
        this._serverState = applyDiff(this._serverState, update);
        this.notifyStateChange();
    }
    // Update group state (merges delta, null clears fields)
    updateGroupState(update) {
        this._groupState = applyDiff(this._groupState, update);
        this.notifyStateChange();
    }
    // Getters for cached state
    get serverState() {
        return this._serverState;
    }
    get groupState() {
        return this._groupState;
    }
}
//# sourceMappingURL=state-manager.js.map
