export const SYNC_DELAY_MAX_MS = 5000;
export function clampSyncDelayMs(delayMs) {
    if (!isFinite(delayMs))
        return 0;
    return Math.max(0, Math.min(SYNC_DELAY_MAX_MS, Math.round(delayMs)));
}
//# sourceMappingURL=sync-delay.js.map
