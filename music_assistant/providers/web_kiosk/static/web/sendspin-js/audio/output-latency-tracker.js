/**
 * Output latency tracker with EMA smoothing and persistence.
 *
 * Tracks AudioContext.baseLatency + outputLatency using exponential moving
 * average to filter browser jitter (especially Chrome). Persists the smoothed
 * value to storage for cross-session consistency.
 *
 * outputLatency is device-derived, so it follows the actual output path
 * (built-in speakers, USB DAC, Bluetooth) as it changes at runtime.
 */
/**
 * Stand-in for AudioContext.outputLatency on browsers that do not implement it
 * (Safari and iOS Safari before 18.4), which report baseLatency only.
 *
 * Estimated from the 40-50ms gap observed between Safari and browsers that do
 * report the property on comparable hardware; it is a stand-in, not a
 * measurement of the current device. Used only when the property is absent or
 * unusable, so a reported 0 is taken at face value. Exported for tests.
 */
export const UNREPORTED_OUTPUT_LATENCY_SEC = 0.04;
const OUTPUT_LATENCY_ALPHA = 0.01;
const OUTPUT_LATENCY_STORAGE_KEY = "sendspin-output-latency-us";
const OUTPUT_LATENCY_PERSIST_INTERVAL_MS = 10000;
/**
 * Resolve a latency the AudioContext reports, in seconds.
 *
 * Falls back when the property is missing or holds a value that cannot be a
 * latency, so a single bad reading cannot reach the smoother, where it would
 * stick for the lifetime of the stream.
 */
function resolveLatencySec(reportedSec, fallbackSec) {
    const usable = typeof reportedSec === "number" &&
        Number.isFinite(reportedSec) &&
        reportedSec >= 0;
    return usable ? reportedSec : fallbackSec;
}
export class OutputLatencyTracker {
    constructor(storage) {
        this.storage = storage;
        this.smoothedOutputLatencyUs = null;
        this.lastLatencyPersistAtMs = null;
        this.loadPersisted();
    }
    loadPersisted() {
        if (!this.storage)
            return;
        try {
            const stored = this.storage.getItem(OUTPUT_LATENCY_STORAGE_KEY);
            if (stored) {
                const latency = parseFloat(stored);
                if (!isNaN(latency) && latency >= 0) {
                    this.smoothedOutputLatencyUs = latency;
                }
            }
        }
        catch {
            // ignore
        }
    }
    persist() {
        if (!this.storage || this.smoothedOutputLatencyUs === null)
            return;
        try {
            this.storage.setItem(OUTPUT_LATENCY_STORAGE_KEY, this.smoothedOutputLatencyUs.toString());
        }
        catch {
            // ignore
        }
    }
    /** Get raw output latency in microseconds from AudioContext. */
    getRawUs(audioContext) {
        if (!audioContext)
            return 0;
        const baseLatency = resolveLatencySec(audioContext.baseLatency, 0);
        const outputLatency = resolveLatencySec(audioContext.outputLatency, UNREPORTED_OUTPUT_LATENCY_SEC);
        return (baseLatency + outputLatency) * 1000000;
    }
    /** Get EMA-smoothed output latency in microseconds. */
    getSmoothedUs(audioContext) {
        const rawLatencyUs = this.getRawUs(audioContext);
        if (rawLatencyUs <= 0 && this.smoothedOutputLatencyUs !== null) {
            return this.smoothedOutputLatencyUs;
        }
        if (this.smoothedOutputLatencyUs === null) {
            this.smoothedOutputLatencyUs = rawLatencyUs;
        }
        else {
            this.smoothedOutputLatencyUs =
                OUTPUT_LATENCY_ALPHA * rawLatencyUs +
                    (1 - OUTPUT_LATENCY_ALPHA) * this.smoothedOutputLatencyUs;
        }
        const nowMs = typeof performance !== "undefined" ? performance.now() : Date.now();
        if (this.lastLatencyPersistAtMs === null ||
            nowMs - this.lastLatencyPersistAtMs >= OUTPUT_LATENCY_PERSIST_INTERVAL_MS) {
            this.persist();
            this.lastLatencyPersistAtMs = nowMs;
        }
        return this.smoothedOutputLatencyUs;
    }
    /** Reset smoother (on stream change or audio context recreation). */
    reset() {
        this.smoothedOutputLatencyUs = null;
    }
}
//# sourceMappingURL=output-latency-tracker.js.map
