const TIME_SYNC_BURST_SIZE = 8;
const TIME_SYNC_BURST_INTERVAL_MS = 10000;
const TIME_SYNC_REQUEST_TIMEOUT_MS = 2000;
const TIME_SYNC_ROBUST_SELECTION_COUNT = 3;
export class TimeSyncManager {
    constructor(wsManager, stateManager, timeFilter) {
        this.wsManager = wsManager;
        this.stateManager = stateManager;
        this.timeFilter = timeFilter;
        this.timeSyncBurstActive = false;
        this.timeSyncBurstSentCount = 0;
        this.timeSyncInFlightClientTransmitted = null;
        this.timeSyncInFlightTimeout = null;
        this.timeSyncBurstSamples = [];
    }
    // Start an initial burst and schedule recurring bursts.
    startAndSchedule() {
        this.stop();
        this.startTimeSyncBurstIfIdle();
        this.scheduleNextTimeSyncBurstTick();
    }
    // Schedule the next fixed 10s burst tick.
    scheduleNextTimeSyncBurstTick() {
        const timeSyncTimeout = globalThis.setTimeout(() => {
            this.startTimeSyncBurstIfIdle();
            this.scheduleNextTimeSyncBurstTick();
        }, TIME_SYNC_BURST_INTERVAL_MS);
        this.stateManager.setTimeSyncInterval(timeSyncTimeout);
    }
    startTimeSyncBurstIfIdle() {
        if (this.timeSyncBurstActive || !this.wsManager.isConnected()) {
            return;
        }
        this.timeSyncBurstActive = true;
        this.timeSyncBurstSentCount = 0;
        this.timeSyncBurstSamples = [];
        this.timeSyncInFlightClientTransmitted = null;
        this.sendNextTimeSyncBurstProbe();
    }
    sendNextTimeSyncBurstProbe() {
        if (!this.timeSyncBurstActive ||
            this.timeSyncInFlightClientTransmitted !== null ||
            !this.wsManager.isConnected()) {
            return;
        }
        if (this.timeSyncBurstSentCount >= TIME_SYNC_BURST_SIZE) {
            this.finalizeTimeSyncBurst();
            return;
        }
        const clientTransmitted = this.sendTimeSync();
        this.timeSyncBurstSentCount += 1;
        this.timeSyncInFlightClientTransmitted = clientTransmitted;
        this.armTimeSyncProbeTimeout(clientTransmitted);
    }
    armTimeSyncProbeTimeout(expectedClientTransmitted) {
        this.clearTimeSyncProbeTimeout();
        this.timeSyncInFlightTimeout = globalThis.setTimeout(() => {
            this.handleTimeSyncProbeTimeout(expectedClientTransmitted);
        }, TIME_SYNC_REQUEST_TIMEOUT_MS);
    }
    clearTimeSyncProbeTimeout() {
        if (this.timeSyncInFlightTimeout !== null) {
            clearTimeout(this.timeSyncInFlightTimeout);
            this.timeSyncInFlightTimeout = null;
        }
    }
    handleTimeSyncProbeTimeout(expectedClientTransmitted) {
        if (!this.timeSyncBurstActive ||
            this.timeSyncInFlightClientTransmitted !== expectedClientTransmitted) {
            return;
        }
        console.warn("Sendspin: Time sync probe timed out, aborting current burst");
        this.abortTimeSyncBurst();
    }
    finalizeTimeSyncBurst() {
        this.clearTimeSyncProbeTimeout();
        const candidate = this.selectTimeSyncBurstCandidate();
        if (candidate) {
            this.timeFilter.update(candidate.measurement, candidate.maxError, candidate.t4);
        }
        this.timeSyncBurstActive = false;
        this.timeSyncBurstSentCount = 0;
        this.timeSyncInFlightClientTransmitted = null;
        this.timeSyncBurstSamples = [];
    }
    selectTimeSyncBurstCandidate() {
        if (this.timeSyncBurstSamples.length === 0) {
            return null;
        }
        const topRttSamples = [...this.timeSyncBurstSamples]
            .sort((a, b) => a.rttTerm - b.rttTerm)
            .slice(0, Math.min(TIME_SYNC_ROBUST_SELECTION_COUNT, this.timeSyncBurstSamples.length));
        const sortedByMeasurement = [...topRttSamples].sort((a, b) => a.measurement - b.measurement);
        return sortedByMeasurement[Math.floor(sortedByMeasurement.length / 2)];
    }
    abortTimeSyncBurst() {
        this.clearTimeSyncProbeTimeout();
        this.timeSyncBurstActive = false;
        this.timeSyncBurstSentCount = 0;
        this.timeSyncInFlightClientTransmitted = null;
        this.timeSyncBurstSamples = [];
    }
    // Stop all time sync activity (interval + in-flight burst).
    stop() {
        this.stateManager.clearTimeSyncInterval();
        this.abortTimeSyncBurst();
    }
    // Handle server/time response
    handleServerTime(message) {
        if (!this.timeSyncBurstActive ||
            this.timeSyncInFlightClientTransmitted === null) {
            return;
        }
        // Per spec: client_transmitted (T1), server_received (T2), server_transmitted (T3)
        const T1 = message.payload.client_transmitted;
        if (T1 !== this.timeSyncInFlightClientTransmitted) {
            console.warn("Sendspin: Ignoring out-of-order time response", T1, this.timeSyncInFlightClientTransmitted);
            return;
        }
        const T4 = Math.floor(performance.now() * 1000); // client received time
        const T2 = message.payload.server_received;
        const T3 = message.payload.server_transmitted;
        // NTP offset calculation: measurement = ((T2 - T1) + (T3 - T4)) / 2
        const measurement = (T2 - T1 + (T3 - T4)) / 2;
        // Max error (half of round-trip time): max_error = ((T4 - T1) - (T3 - T2)) / 2
        const rttTerm = Math.max(0, T4 - T1 - (T3 - T2));
        const maxError = Math.max(1000, rttTerm / 2);
        this.timeSyncBurstSamples.push({
            measurement,
            maxError,
            t4: T4,
            rttTerm,
        });
        this.clearTimeSyncProbeTimeout();
        this.timeSyncInFlightClientTransmitted = null;
        if (this.timeSyncBurstSentCount >= TIME_SYNC_BURST_SIZE) {
            this.finalizeTimeSyncBurst();
            return;
        }
        this.sendNextTimeSyncBurstProbe();
    }
    // Send time synchronization message
    sendTimeSync(clientTimeUs = Math.floor(performance.now() * 1000)) {
        const message = {
            type: "client/time",
            payload: {
                client_transmitted: clientTimeUs,
            },
        };
        this.wsManager.send(message);
        return clientTimeUs;
    }
}
//# sourceMappingURL=time-sync-manager.js.map
