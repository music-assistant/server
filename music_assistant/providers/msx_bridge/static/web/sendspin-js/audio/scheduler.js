/**
 * Audio scheduler for synchronized playback.
 *
 * Handles Web Audio API scheduling, sync correction, AudioContext management,
 * volume control, and output routing. Receives pre-decoded audio chunks
 * (DecodedAudioChunk) from SendspinCore and schedules them for playback.
 */
import { ClockSource } from "./clock-source.js";
import { RecorrectionMonitor, RECORRECTION_CUTOVER_GUARD_SEC, } from "./recorrection-monitor.js";
import { OutputLatencyTracker } from "./output-latency-tracker.js";
import { clampSyncDelayMs } from "../sync-delay.js";
// Sync correction constants
const SAMPLE_CORRECTION_FADE_LEN = 8;
const SAMPLE_CORRECTION_TARGET_BLEND_SUM = 1.0;
const SAMPLE_CORRECTION_FADE_STRENGTH = Math.min(1, (2 * SAMPLE_CORRECTION_TARGET_BLEND_SUM) / SAMPLE_CORRECTION_FADE_LEN);
const SAMPLE_CORRECTION_FADE_ALPHAS = new Float32Array(SAMPLE_CORRECTION_FADE_LEN);
for (let f = 0; f < SAMPLE_CORRECTION_FADE_LEN; f++) {
    SAMPLE_CORRECTION_FADE_ALPHAS[f] =
        ((SAMPLE_CORRECTION_FADE_LEN - f) / (SAMPLE_CORRECTION_FADE_LEN + 1)) *
            SAMPLE_CORRECTION_FADE_STRENGTH;
}
const SYNC_ERROR_ALPHA = 0.1;
const SCHEDULE_HEADROOM_SEC = 0.2;
const SCHEDULE_HORIZON_PRECISE_SEC = 20;
const SCHEDULE_HORIZON_GOOD_SEC = 8;
const SCHEDULE_HORIZON_POOR_SEC = 4;
const CAST_SCHEDULE_HORIZON_SEC = 1.5;
const SCHEDULE_HORIZON_PRECISE_ERROR_MS = 2;
const SCHEDULE_HORIZON_GOOD_ERROR_MS = 8;
const SCHEDULE_REFILL_THRESHOLD_FRACTION = 0.5;
const SCHEDULE_REFILL_MIN_THRESHOLD_SEC = 0.1;
const SCHEDULE_REFILL_MAX_THRESHOLD_SEC = 5;
const VOLUME_RAMP_TIME_CONSTANT_SEC = 0.015;
export function perceptualGain(volume) {
    return Math.pow(volume / 100, 1.5);
}
const DEFAULT_CORRECTION_THRESHOLDS = {
    sync: {
        resyncAboveMs: 200,
        rate2AboveMs: 35,
        rate1AboveMs: 8,
        samplesBelowMs: 8,
        deadbandBelowMs: 1,
        enableRecorrectionMonitor: true,
        immediateDelayCutover: true,
    },
    quality: {
        resyncAboveMs: 35,
        rate2AboveMs: Infinity,
        rate1AboveMs: Infinity,
        samplesBelowMs: 35,
        deadbandBelowMs: 1,
        enableRecorrectionMonitor: false,
        immediateDelayCutover: false,
    },
    "quality-local": {
        resyncAboveMs: 600,
        rate2AboveMs: Infinity,
        rate1AboveMs: Infinity,
        samplesBelowMs: 0,
        deadbandBelowMs: 5,
        enableRecorrectionMonitor: false,
        immediateDelayCutover: false,
    },
};
export class AudioScheduler {
    constructor(options) {
        this.audioContext = null;
        this.gainNode = null;
        this.streamDestination = null;
        this.audioBufferQueue = [];
        this.scheduledSources = [];
        this.nextPlaybackTime = 0;
        this.nextScheduleTime = 0;
        this.lastScheduledServerTime = 0;
        this.currentSyncErrorMs = 0;
        this.smoothedSyncErrorMs = 0;
        this.resyncCount = 0;
        this.currentPlaybackRate = 1.0;
        this.currentCorrectionMethod = "none";
        this.lastSamplesAdjusted = 0;
        this._correctionMode = "sync";
        this._lastStatusLogMs = 0;
        this._intervalResyncCount = 0;
        this.scheduleTimeout = null;
        this.refillTimeout = null;
        this.queueProcessScheduled = false;
        // Sub-modules
        this.clockSource = new ClockSource();
        this.stateManager = options.stateManager;
        this.timeFilter = options.timeFilter;
        this.outputMode = options.outputMode ?? "direct";
        this.audioElement = options.audioElement;
        this.isAndroid = options.isAndroid ?? false;
        this.isCastRuntime = options.isCastRuntime ?? false;
        this.ownsAudioElement = options.ownsAudioElement ?? false;
        this.silentAudioSrc = options.silentAudioSrc;
        this.syncDelayMs = clampSyncDelayMs(options.syncDelayMs ?? 0);
        this.useHardwareVolume = options.useHardwareVolume ?? false;
        this._correctionMode = options.correctionMode ?? "sync";
        this.useOutputLatencyCompensation =
            options.useOutputLatencyCompensation ?? true;
        // Merge user-provided threshold overrides with defaults
        this.correctionThresholds = { ...DEFAULT_CORRECTION_THRESHOLDS };
        const thresholdOverrides = options.correctionThresholds;
        if (thresholdOverrides) {
            for (const mode of Object.keys(thresholdOverrides)) {
                const overrides = thresholdOverrides[mode];
                if (overrides) {
                    this.correctionThresholds[mode] = {
                        ...DEFAULT_CORRECTION_THRESHOLDS[mode],
                        ...overrides,
                    };
                }
            }
        }
        this.latencyTracker = new OutputLatencyTracker(options.storage ?? null);
        if (this.isCastRuntime) {
            this.clockSource.disableTimestampPromotion();
        }
        this.clockSource.onPromotion(() => {
            if (this.audioBufferQueue.length > 0 ||
                this.scheduledSources.length > 0) {
                this.scheduleQueueProcessing();
            }
        });
        this.recorrectionMonitor = new RecorrectionMonitor(() => this.checkRecorrection());
    }
    get correctionMode() {
        return this._correctionMode;
    }
    setCorrectionMode(mode) {
        this._correctionMode = mode;
        if (!this.correctionThresholds[mode].enableRecorrectionMonitor) {
            this.recorrectionMonitor.stop();
        }
        else {
            this.recorrectionMonitor.start();
        }
    }
    get usesRecorrectionMonitor() {
        return this.correctionThresholds[this._correctionMode]
            .enableRecorrectionMonitor;
    }
    get usesImmediateDelayCutover() {
        return this.correctionThresholds[this._correctionMode]
            .immediateDelayCutover;
    }
    getTargetScheduledHorizonSec() {
        if (this.isCastRuntime) {
            return CAST_SCHEDULE_HORIZON_SEC;
        }
        const errorMs = this.timeFilter.error / 1000;
        if (errorMs < SCHEDULE_HORIZON_PRECISE_ERROR_MS)
            return SCHEDULE_HORIZON_PRECISE_SEC;
        if (errorMs <= SCHEDULE_HORIZON_GOOD_ERROR_MS)
            return SCHEDULE_HORIZON_GOOD_SEC;
        return SCHEDULE_HORIZON_POOR_SEC;
    }
    getScheduledAheadSec(currentTimeSec) {
        let farthest = this.nextScheduleTime;
        for (const entry of this.scheduledSources) {
            if (entry.endTime > farthest)
                farthest = entry.endTime;
        }
        return farthest <= 0 ? 0 : Math.max(0, farthest - currentTimeSec);
    }
    resetScheduledPlaybackState(_reason) {
        this.nextPlaybackTime = 0;
        this.nextScheduleTime = 0;
        this.lastScheduledServerTime = 0;
        this.recorrectionMonitor.clearMinScheduleTime();
        this.recorrectionMonitor.clearHardResyncCooldown();
        this.clockSource.pendingCutover = false;
        this.recorrectionMonitor.resetCheckState();
        this.resetSyncErrorEma();
        this.currentSyncErrorMs = 0;
        this.currentPlaybackRate = 1.0;
        this.currentCorrectionMethod = "none";
        this.lastSamplesAdjusted = 0;
        this._lastStatusLogMs = 0;
        this._intervalResyncCount = 0;
    }
    pruneExpiredScheduledSources(currentTimeSec) {
        if (this.scheduledSources.length === 0)
            return;
        this.scheduledSources = this.scheduledSources.filter((entry) => entry.endTime > currentTimeSec);
        if (this.scheduledSources.length === 0) {
            this.resetScheduledPlaybackState("no scheduled audio ahead");
        }
    }
    performGuardedCutover(_reason, options = {}) {
        if (!this.audioContext)
            return;
        const incrementResyncCount = options.incrementResyncCount ?? false;
        const markCooldown = options.markCooldown ?? true;
        const nowMs = performance.now();
        const cutoffTime = this.audioContext.currentTime + RECORRECTION_CUTOVER_GUARD_SEC;
        if (incrementResyncCount) {
            this.resyncCount++;
            this._intervalResyncCount++;
        }
        this.resetSyncErrorEma();
        this.currentCorrectionMethod = "resync";
        this.lastSamplesAdjusted = 0;
        this.currentPlaybackRate = 1.0;
        const cutResult = this.cutScheduledSources(cutoffTime);
        this.recorrectionMonitor.setMinScheduleTime(Math.max(cutoffTime, cutResult.keptTailEndTimeSec));
        this.nextPlaybackTime = 0;
        this.nextScheduleTime = 0;
        this.lastScheduledServerTime = 0;
        this.recorrectionMonitor.resetCheckState();
        if (markCooldown)
            this.recorrectionMonitor.markRecorrection(nowMs);
        this.recorrectionMonitor.noteHardResync(nowMs);
        this.processAudioQueue();
    }
    checkRecorrection() {
        if (!this.usesRecorrectionMonitor) {
            this.recorrectionMonitor.resetCheckState();
            return;
        }
        if (!this.audioContext || this.audioContext.state !== "running") {
            this.recorrectionMonitor.resetCheckState();
            return;
        }
        if (!this.stateManager.isPlaying ||
            this.nextPlaybackTime === 0 ||
            this.lastScheduledServerTime === 0) {
            this.recorrectionMonitor.resetCheckState();
            return;
        }
        const { audioContextTimeSec, audioContextRawTimeSec, nowMs, nowUs } = this.clockSource.getTimingSnapshot(this.audioContext);
        this.pruneExpiredScheduledSources(audioContextRawTimeSec);
        if (this.getScheduledAheadSec(audioContextRawTimeSec) <= 0) {
            this.recorrectionMonitor.resetCheckState();
            if (this.audioBufferQueue.length > 0)
                this.processAudioQueue();
            return;
        }
        const outputLatencySec = this.useOutputLatencyCompensation
            ? this.latencyTracker.getSmoothedUs(this.audioContext) / 1000000
            : 0;
        const targetPlaybackTime = this.computeTargetPlaybackTime(this.lastScheduledServerTime, audioContextTimeSec, nowUs, outputLatencySec);
        const syncErrorMs = (this.nextPlaybackTime - targetPlaybackTime) * 1000;
        const smoothedSyncErrorMs = this.applySyncErrorEma(syncErrorMs);
        if (this.recorrectionMonitor.shouldRecorrect(Math.abs(smoothedSyncErrorMs), syncErrorMs, nowMs)) {
            this.performGuardedCutover("recorrection", {
                incrementResyncCount: true,
                markCooldown: true,
            });
        }
    }
    getSyncDelayMs() {
        return this.syncDelayMs;
    }
    setSyncDelay(delayMs) {
        const sanitized = clampSyncDelayMs(delayMs);
        const delta = sanitized - this.syncDelayMs;
        this.syncDelayMs = sanitized;
        if (delta === 0 || !this.usesImmediateDelayCutover)
            return;
        if (!this.audioContext || this.audioContext.state !== "running")
            return;
        if (!this.stateManager.isPlaying)
            return;
        if (this.scheduledSources.length === 0 &&
            this.audioBufferQueue.length === 0 &&
            this.nextPlaybackTime === 0)
            return;
        this.performGuardedCutover("delay-change", {
            incrementResyncCount: false,
            markCooldown: true,
        });
    }
    get syncInfo() {
        return {
            clockDriftPercent: this.timeFilter.drift * 100,
            syncErrorMs: this.currentSyncErrorMs,
            resyncCount: this.resyncCount,
            outputLatencyMs: this.latencyTracker.getRawUs(this.audioContext) / 1000,
            playbackRate: this.currentPlaybackRate,
            correctionMethod: this.currentCorrectionMethod,
            samplesAdjusted: this.lastSamplesAdjusted,
            correctionMode: this._correctionMode,
        };
    }
    emitStatusLog(nowMs) {
        if (this._lastStatusLogMs !== 0 && nowMs - this._lastStatusLogMs < 10000)
            return;
        this._lastStatusLogMs = nowMs;
        let corr;
        switch (this.currentCorrectionMethod) {
            case "rate":
                corr = `rate@${this.currentPlaybackRate}`;
                break;
            case "samples":
                corr = `samples:${this.lastSamplesAdjusted}`;
                break;
            default:
                corr = this.currentCorrectionMethod;
        }
        const queueDepth = this.audioBufferQueue.length + this.scheduledSources.length;
        const aheadSec = this.audioContext
            ? this.getScheduledAheadSec(this.audioContext.currentTime)
            : 0;
        let clock;
        if (this.clockSource.timestampPromotionDisabled) {
            clock = "estimated(cast-disabled)";
        }
        else if (this.clockSource.active === "timestamp") {
            clock = `timestamp(good:${this.clockSource.timestampGoodSamples})`;
        }
        else if (this.clockSource.lastRejectReason) {
            clock = `estimated(reject:"${this.clockSource.lastRejectReason}")`;
        }
        else {
            clock = "estimated";
        }
        const tf = this.timeFilter.is_synchronized
            ? `synced(err=${(this.timeFilter.error / 1000).toFixed(1)}ms,drift=${this.timeFilter.drift.toFixed(3)},n=${this.timeFilter.count})`
            : `pending(n=${this.timeFilter.count})`;
        const smoothedLatUs = this.latencyTracker.getSmoothedUs(this.audioContext);
        const latMs = Math.round(smoothedLatUs / 1000);
        console.log(`Sendspin: sync=${this.smoothedSyncErrorMs >= 0 ? "+" : ""}${this.smoothedSyncErrorMs.toFixed(1)}ms` +
            ` corr=${corr} q=${queueDepth}/${aheadSec.toFixed(1)}s resyncs=${this._intervalResyncCount}` +
            ` clock=${clock} tf=${tf} lat=${latMs}ms mode=${this._correctionMode}` +
            ` ctx=${this.audioContext?.state ?? "null"} gen=${this.stateManager.streamGeneration}`);
        this._intervalResyncCount = 0;
    }
    applySyncErrorEma(inputMs) {
        this.currentSyncErrorMs = inputMs;
        this.smoothedSyncErrorMs =
            SYNC_ERROR_ALPHA * inputMs +
                (1 - SYNC_ERROR_ALPHA) * this.smoothedSyncErrorMs;
        return this.smoothedSyncErrorMs;
    }
    resetSyncErrorEma() {
        this.smoothedSyncErrorMs = 0;
    }
    copyBuffer(buffer) {
        if (!this.audioContext)
            return buffer;
        const newBuffer = this.audioContext.createBuffer(buffer.numberOfChannels, buffer.length, buffer.sampleRate);
        for (let ch = 0; ch < buffer.numberOfChannels; ch++) {
            newBuffer.getChannelData(ch).set(buffer.getChannelData(ch));
        }
        return newBuffer;
    }
    adjustBufferSamples(buffer, samplesToAdjust) {
        if (!this.audioContext || samplesToAdjust === 0 || buffer.length < 2)
            return this.copyBuffer(buffer);
        const channels = buffer.numberOfChannels;
        const len = buffer.length;
        const sampleRate = buffer.sampleRate;
        try {
            if (samplesToAdjust > 0) {
                const newBuffer = this.audioContext.createBuffer(channels, len + 1, sampleRate);
                for (let ch = 0; ch < channels; ch++) {
                    const oldData = buffer.getChannelData(ch);
                    const newData = newBuffer.getChannelData(ch);
                    newData[0] = oldData[0];
                    const insertedSample = (oldData[0] + oldData[1]) / 2;
                    newData[1] = insertedSample;
                    newData.set(oldData.subarray(1), 2);
                    for (let f = 0; f < SAMPLE_CORRECTION_FADE_LEN; f++) {
                        const pos = 2 + f;
                        if (pos >= newData.length)
                            break;
                        const alpha = SAMPLE_CORRECTION_FADE_ALPHAS[f];
                        newData[pos] = newData[pos] * (1 - alpha) + insertedSample * alpha;
                    }
                }
                return newBuffer;
            }
            else {
                const newBuffer = this.audioContext.createBuffer(channels, len - 1, sampleRate);
                for (let ch = 0; ch < channels; ch++) {
                    const oldData = buffer.getChannelData(ch);
                    const newData = newBuffer.getChannelData(ch);
                    newData.set(oldData.subarray(0, len - 2));
                    const replacementSample = (oldData[len - 2] + oldData[len - 1]) / 2;
                    newData[len - 2] = replacementSample;
                    for (let f = 0; f < SAMPLE_CORRECTION_FADE_LEN; f++) {
                        const pos = len - 3 - f;
                        if (pos < 0)
                            break;
                        const alpha = SAMPLE_CORRECTION_FADE_ALPHAS[f];
                        newData[pos] =
                            newData[pos] * (1 - alpha) + replacementSample * alpha;
                    }
                }
                return newBuffer;
            }
        }
        catch (e) {
            console.error("Sendspin: adjustBufferSamples error:", e);
            return buffer;
        }
    }
    initAudioContext() {
        if (this.audioContext)
            return;
        if (this.outputMode === "media-element" && this.ownsAudioElement) {
            this.audioElement = document.createElement("audio");
            this.audioElement.style.display = "none";
            document.body.appendChild(this.audioElement);
        }
        if (navigator.audioSession) {
            navigator.audioSession.type = "playback";
        }
        const streamSampleRate = this.stateManager.currentStreamFormat?.sample_rate || 48000;
        this.audioContext = new AudioContext({ sampleRate: streamSampleRate });
        this.gainNode = this.audioContext.createGain();
        const audioElement = this.audioElement;
        if (this.outputMode === "direct") {
            this.gainNode.connect(this.audioContext.destination);
        }
        else {
            if (!audioElement)
                throw new Error("Media-element output requires an audio element.");
            if (this.isAndroid && this.silentAudioSrc) {
                this.gainNode.connect(this.audioContext.destination);
                audioElement.src = this.silentAudioSrc;
                audioElement.loop = true;
                audioElement.muted = false;
                audioElement.volume = 1.0;
                audioElement.play().catch((e) => {
                    console.warn("Sendspin: Audio autoplay blocked:", e);
                });
            }
            else {
                this.streamDestination =
                    this.audioContext.createMediaStreamDestination();
                this.gainNode.connect(this.streamDestination);
                audioElement.srcObject = this.streamDestination.stream;
                audioElement.volume = 1.0;
                audioElement.play().catch((e) => {
                    console.warn("Sendspin: Audio autoplay blocked:", e);
                });
            }
        }
        this.updateVolume();
        if (this.usesRecorrectionMonitor)
            this.recorrectionMonitor.start();
    }
    async resumeAudioContext() {
        if (this.audioContext && this.audioContext.state === "suspended") {
            try {
                await this.audioContext.resume();
                console.log("Sendspin: AudioContext resumed");
            }
            catch (e) {
                console.warn("Sendspin: Failed to resume AudioContext:", e);
                return;
            }
            if (this.audioBufferQueue.length > 0)
                this.scheduleQueueProcessing();
            if (this.usesRecorrectionMonitor)
                this.recorrectionMonitor.start();
        }
    }
    cutScheduledSources(cutoffTime) {
        if (!this.audioContext)
            return { requeuedCount: 0, cutCount: 0, keptTailEndTimeSec: 0 };
        const stopTime = Math.max(cutoffTime, this.audioContext.currentTime);
        let requeued = 0, cutCount = 0, keptTailEndTimeSec = 0;
        this.scheduledSources = this.scheduledSources.filter((entry) => {
            if (entry.startTime < stopTime) {
                keptTailEndTimeSec = Math.max(keptTailEndTimeSec, entry.endTime);
                return true;
            }
            try {
                entry.source.onended = null;
                entry.source.stop(stopTime);
            }
            catch {
                /* ignore */
            }
            this.audioBufferQueue.push({
                buffer: entry.buffer,
                serverTime: entry.serverTime,
                generation: entry.generation,
            });
            requeued++;
            cutCount++;
            return false;
        });
        return { requeuedCount: requeued, cutCount, keptTailEndTimeSec };
    }
    updateVolume() {
        if (!this.gainNode)
            return;
        if (this.useHardwareVolume) {
            this.gainNode.gain.value = 1.0;
            return;
        }
        const target = this.stateManager.muted
            ? 0
            : perceptualGain(this.stateManager.volume);
        if (this.audioContext) {
            this.gainNode.gain.setTargetAtTime(target, this.audioContext.currentTime, VOLUME_RAMP_TIME_CONSTANT_SEC);
        }
        else {
            this.gainNode.gain.value = target;
        }
    }
    measureBufferedPlaybackRunwaySec() {
        if (!this.audioContext)
            return 0;
        const currentTimeSec = this.audioContext.currentTime;
        this.pruneExpiredScheduledSources(currentTimeSec);
        const scheduledAheadSec = this.getScheduledAheadSec(currentTimeSec);
        const queuedAheadSec = this.audioBufferQueue.reduce((totalSec, chunk) => totalSec + chunk.buffer.duration, 0);
        return Math.max(0, scheduledAheadSec + queuedAheadSec);
    }
    cancelScheduledRefill() {
        if (this.refillTimeout !== null) {
            clearTimeout(this.refillTimeout);
            this.refillTimeout = null;
        }
    }
    getScheduledRefillThresholdSec(targetScheduledHorizonSec) {
        return Math.max(SCHEDULE_REFILL_MIN_THRESHOLD_SEC, Math.min(SCHEDULE_REFILL_MAX_THRESHOLD_SEC, targetScheduledHorizonSec * SCHEDULE_REFILL_THRESHOLD_FRACTION));
    }
    scheduleQueueRefill(targetScheduledHorizonSec) {
        this.cancelScheduledRefill();
        if (!this.audioContext ||
            this.audioContext.state !== "running" ||
            !this.stateManager.isPlaying ||
            this.audioBufferQueue.length === 0)
            return;
        const currentTimeSec = this.audioContext.currentTime;
        this.pruneExpiredScheduledSources(currentTimeSec);
        const scheduledAheadSec = this.getScheduledAheadSec(currentTimeSec);
        const refillThresholdSec = this.getScheduledRefillThresholdSec(targetScheduledHorizonSec);
        if (scheduledAheadSec <= refillThresholdSec) {
            this.scheduleQueueProcessing();
            return;
        }
        const delayMs = (scheduledAheadSec - refillThresholdSec) * 1000;
        const runRefill = () => {
            this.refillTimeout = null;
            if (!this.audioContext ||
                this.audioContext.state !== "running" ||
                !this.stateManager.isPlaying ||
                this.audioBufferQueue.length === 0)
                return;
            this.scheduleQueueProcessing();
        };
        if (typeof globalThis.setTimeout === "function") {
            this.refillTimeout = globalThis.setTimeout(runRefill, delayMs);
            return;
        }
        this.refillTimeout = null;
        if (typeof globalThis
            .queueMicrotask === "function") {
            globalThis.queueMicrotask(runRefill);
            return;
        }
        void Promise.resolve().then(runRefill);
    }
    scheduleQueueProcessing() {
        this.cancelScheduledRefill();
        if (this.queueProcessScheduled)
            return;
        this.queueProcessScheduled = true;
        if (typeof globalThis.setTimeout === "function") {
            this.scheduleTimeout = globalThis.setTimeout(() => {
                this.scheduleTimeout = null;
                this.queueProcessScheduled = false;
                this.processAudioQueue();
            }, 15);
            return;
        }
        const run = () => {
            this.queueProcessScheduled = false;
            this.processAudioQueue();
        };
        if (typeof globalThis
            .queueMicrotask === "function") {
            globalThis.queueMicrotask(run);
        }
        else {
            Promise.resolve().then(run);
        }
    }
    handleDecodedChunk(chunk) {
        if (!this.audioContext || !this.gainNode) {
            console.warn("Sendspin: Received audio chunk but no audio context");
            return;
        }
        if (chunk.generation !== this.stateManager.streamGeneration)
            return;
        const numChannels = chunk.samples.length;
        const numFrames = chunk.samples[0].length;
        const audioBuffer = this.audioContext.createBuffer(numChannels, numFrames, chunk.sampleRate);
        for (let ch = 0; ch < numChannels; ch++)
            audioBuffer.getChannelData(ch).set(chunk.samples[ch]);
        this.audioBufferQueue.push({
            buffer: audioBuffer,
            serverTime: chunk.serverTimeUs,
            generation: chunk.generation,
        });
        this.scheduleQueueProcessing();
    }
    processAudioQueue() {
        this.cancelScheduledRefill();
        if (!this.audioContext || !this.gainNode)
            return;
        if (this.audioContext.state !== "running")
            return;
        const currentGeneration = this.stateManager.streamGeneration;
        this.audioBufferQueue = this.audioBufferQueue.filter((chunk) => chunk.generation === currentGeneration);
        this.audioBufferQueue.sort((a, b) => a.serverTime - b.serverTime);
        if (!this.timeFilter.is_synchronized)
            return;
        const { audioContextTimeSec: audioContextTime, audioContextRawTimeSec, nowMs, nowUs, } = this.clockSource.getTimingSnapshot(this.audioContext);
        this.pruneExpiredScheduledSources(audioContextRawTimeSec);
        const outputLatencySec = this.useOutputLatencyCompensation
            ? this.latencyTracker.getSmoothedUs(this.audioContext) / 1000000
            : 0;
        const syncDelaySec = this.syncDelayMs / 1000;
        const targetScheduledHorizonSec = this.getTargetScheduledHorizonSec();
        if (this.usesRecorrectionMonitor)
            this.recorrectionMonitor.start();
        if (this.clockSource.pendingCutover) {
            this.clockSource.pendingCutover = false;
            if (this.scheduledSources.length > 0 ||
                this.nextPlaybackTime !== 0 ||
                this.lastScheduledServerTime !== 0) {
                this.performGuardedCutover("delay-change", {
                    incrementResyncCount: false,
                    markCooldown: false,
                });
                return;
            }
        }
        while (this.audioBufferQueue.length > 0) {
            const scheduledAheadSec = this.getScheduledAheadSec(audioContextRawTimeSec);
            if (this.nextPlaybackTime > 0 &&
                scheduledAheadSec >= targetScheduledHorizonSec)
                break;
            const chunk = this.audioBufferQueue.shift();
            let playbackTime;
            let scheduleTime;
            let playbackRate;
            const targetPlaybackTime = this.computeTargetPlaybackTime(chunk.serverTime, audioContextTime, nowUs, outputLatencySec);
            const isTimestamp = this.clockSource.active === "timestamp";
            if (this.nextPlaybackTime === 0 || this.lastScheduledServerTime === 0) {
                this.recorrectionMonitor.armStartupGrace(nowMs, isTimestamp);
                playbackTime = targetPlaybackTime;
                scheduleTime = playbackTime - syncDelaySec;
                const minScheduleTimeSec = this.recorrectionMonitor.minScheduleTimeSec;
                if (minScheduleTimeSec !== null) {
                    scheduleTime = Math.max(scheduleTime, minScheduleTimeSec);
                    playbackTime = scheduleTime + syncDelaySec;
                }
                this.recorrectionMonitor.clearMinScheduleTime();
                playbackRate = 1.0;
                chunk.buffer = this.copyBuffer(chunk.buffer);
            }
            else {
                const serverGapUs = chunk.serverTime - this.lastScheduledServerTime;
                const serverGapSec = serverGapUs / 1000000;
                if (Math.abs(serverGapSec) < 0.1) {
                    const syncErrorSec = this.nextPlaybackTime - targetPlaybackTime;
                    const syncErrorMs = syncErrorSec * 1000;
                    const correctionErrorMs = this.applySyncErrorEma(syncErrorMs);
                    const thresholds = this.correctionThresholds[this._correctionMode];
                    const canHardResync = this.recorrectionMonitor.canUseHardResync(nowMs, isTimestamp);
                    if (Math.abs(correctionErrorMs) > thresholds.resyncAboveMs &&
                        canHardResync) {
                        this.recorrectionMonitor.noteHardResync(nowMs);
                        this.resyncCount++;
                        this._intervalResyncCount++;
                        this.resetSyncErrorEma();
                        this.cutScheduledSources(targetPlaybackTime - syncDelaySec);
                        playbackTime = targetPlaybackTime;
                        scheduleTime = playbackTime - syncDelaySec;
                        playbackRate = 1.0;
                        this.currentCorrectionMethod = "resync";
                        this.lastSamplesAdjusted = 0;
                        chunk.buffer = this.copyBuffer(chunk.buffer);
                    }
                    else if (Math.abs(correctionErrorMs) > thresholds.resyncAboveMs) {
                        playbackTime = this.nextPlaybackTime;
                        scheduleTime = this.nextScheduleTime;
                        playbackRate = Number.isFinite(thresholds.rate2AboveMs)
                            ? correctionErrorMs > 0
                                ? 1.02
                                : 0.98
                            : 1.0;
                        this.currentCorrectionMethod =
                            playbackRate === 1.0 ? "none" : "rate";
                        this.lastSamplesAdjusted = 0;
                        chunk.buffer = this.copyBuffer(chunk.buffer);
                    }
                    else if (Math.abs(correctionErrorMs) < thresholds.deadbandBelowMs) {
                        playbackTime = this.nextPlaybackTime;
                        scheduleTime = this.nextScheduleTime;
                        playbackRate = 1.0;
                        this.currentCorrectionMethod = "none";
                        this.lastSamplesAdjusted = 0;
                        chunk.buffer = this.copyBuffer(chunk.buffer);
                    }
                    else if (Math.abs(correctionErrorMs) <= thresholds.samplesBelowMs) {
                        playbackTime = this.nextPlaybackTime;
                        scheduleTime = this.nextScheduleTime;
                        playbackRate = 1.0;
                        const samplesToAdjust = correctionErrorMs > 0 ? -1 : 1;
                        chunk.buffer = this.adjustBufferSamples(chunk.buffer, samplesToAdjust);
                        this.currentCorrectionMethod = "samples";
                        this.lastSamplesAdjusted = samplesToAdjust;
                    }
                    else {
                        playbackTime = this.nextPlaybackTime;
                        scheduleTime = this.nextScheduleTime;
                        const absErrorMs = Math.abs(correctionErrorMs);
                        if (correctionErrorMs > 0) {
                            playbackRate =
                                absErrorMs >= thresholds.rate2AboveMs
                                    ? 1.02
                                    : absErrorMs >= thresholds.rate1AboveMs
                                        ? 1.01
                                        : 1.0;
                        }
                        else {
                            playbackRate =
                                absErrorMs >= thresholds.rate2AboveMs
                                    ? 0.98
                                    : absErrorMs >= thresholds.rate1AboveMs
                                        ? 0.99
                                        : 1.0;
                        }
                        this.currentCorrectionMethod =
                            playbackRate === 1.0 ? "none" : "rate";
                        this.lastSamplesAdjusted = 0;
                        chunk.buffer = this.copyBuffer(chunk.buffer);
                    }
                }
                else {
                    // Gap detected in server timestamps - hard resync (gated on cooldown)
                    if (this.recorrectionMonitor.canUseHardResync(nowMs, isTimestamp)) {
                        this.recorrectionMonitor.noteHardResync(nowMs);
                        this.resyncCount++;
                        this._intervalResyncCount++;
                        this.cutScheduledSources(targetPlaybackTime - syncDelaySec);
                    }
                    playbackTime = targetPlaybackTime;
                    scheduleTime = playbackTime - syncDelaySec;
                    playbackRate = 1.0;
                    this.currentCorrectionMethod = "resync";
                    this.lastSamplesAdjusted = 0;
                    chunk.buffer = this.copyBuffer(chunk.buffer);
                }
            }
            this.currentPlaybackRate = playbackRate;
            if (playbackTime < audioContextRawTimeSec) {
                this.nextPlaybackTime = 0;
                this.nextScheduleTime = 0;
                this.lastScheduledServerTime = 0;
                continue;
            }
            const effectiveScheduleTime = Math.max(scheduleTime, audioContextRawTimeSec);
            const effectivePlaybackTime = effectiveScheduleTime + (playbackTime - scheduleTime);
            const source = this.audioContext.createBufferSource();
            source.buffer = chunk.buffer;
            source.playbackRate.value = playbackRate;
            source.connect(this.gainNode);
            source.start(effectiveScheduleTime);
            const actualDuration = chunk.buffer.duration / playbackRate;
            this.nextPlaybackTime = effectivePlaybackTime + actualDuration;
            this.nextScheduleTime = effectiveScheduleTime + actualDuration;
            this.lastScheduledServerTime =
                chunk.serverTime + chunk.buffer.duration * 1000000;
            const scheduledEntry = {
                source,
                startTime: effectiveScheduleTime,
                endTime: effectiveScheduleTime + actualDuration,
                buffer: chunk.buffer,
                serverTime: chunk.serverTime,
                generation: chunk.generation,
            };
            this.scheduledSources.push(scheduledEntry);
            source.onended = () => {
                const idx = this.scheduledSources.indexOf(scheduledEntry);
                if (idx > -1)
                    this.scheduledSources.splice(idx, 1);
                if (this.scheduledSources.length === 0) {
                    this.resetScheduledPlaybackState("all scheduled audio ended");
                    if (this.audioBufferQueue.length > 0)
                        this.processAudioQueue();
                }
            };
        }
        this.scheduleQueueRefill(targetScheduledHorizonSec);
        this.emitStatusLog(nowMs);
    }
    computeTargetPlaybackTime(serverTimeUs, audioContextTime, nowUs, outputLatencySec) {
        const chunkClientTimeUs = this.timeFilter.computeClientTime(serverTimeUs);
        const deltaSec = (chunkClientTimeUs - nowUs) / 1000000;
        return (audioContextTime + deltaSec + SCHEDULE_HEADROOM_SEC - outputLatencySec);
    }
    startAudioElement() {
        if (this.outputMode === "media-element" && this.audioElement?.paused) {
            this.audioElement.play().catch((e) => {
                console.warn("Sendspin: Failed to start audio element:", e);
            });
        }
    }
    stopAudioElement() {
        if (this.outputMode === "media-element" &&
            this.audioElement &&
            !this.audioElement.paused) {
            this.audioElement.pause();
        }
    }
    clearBuffers() {
        this.recorrectionMonitor.fullReset();
        this.cancelScheduledRefill();
        this.scheduledSources.forEach((entry) => {
            try {
                entry.source.stop();
            }
            catch {
                /* ignore */
            }
        });
        this.scheduledSources = [];
        this.audioBufferQueue = [];
        if (this.scheduleTimeout !== null) {
            clearTimeout(this.scheduleTimeout);
            this.scheduleTimeout = null;
        }
        this.queueProcessScheduled = false;
        this.stateManager.resetStreamAnchors();
        this.resetScheduledPlaybackState();
        this.resyncCount = 0;
        this.latencyTracker.reset();
        this.clockSource.reset();
    }
    close() {
        this.clearBuffers();
        if (this.audioContext) {
            this.audioContext.close();
            this.audioContext = null;
        }
        this.gainNode = null;
        this.streamDestination = null;
        if (this.outputMode === "media-element" && this.audioElement) {
            this.audioElement.pause();
            this.audioElement.srcObject = null;
            this.audioElement.loop = false;
            this.audioElement.removeAttribute("src");
            this.audioElement.load();
            if (this.ownsAudioElement) {
                this.audioElement.remove();
                this.audioElement = undefined;
            }
        }
    }
    getAudioContext() {
        return this.audioContext;
    }
}
//# sourceMappingURL=scheduler.js.map
