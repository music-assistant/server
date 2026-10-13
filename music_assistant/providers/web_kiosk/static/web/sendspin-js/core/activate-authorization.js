/** Is this activity set allowed at all for the matched PSK category? */
function isAllowedActivitySet(category, set, unpairedAccess) {
    if (category === "long_term") {
        // ['pairing'] alone, or any subset of {playback, management}.
        if (set.has("pairing"))
            return set.size === 1;
        for (const a of set)
            if (a !== "playback" && a !== "management")
                return false;
        return true;
    }
    if (category === "pairing") {
        return set.size === 1 && set.has("pairing");
    }
    // sentinel
    if (set.size === 0)
        return true;
    if (set.size === 1 && set.has("pairing"))
        return true;
    if (set.size === 1 && set.has("playback"))
        return unpairedAccess;
    return false;
}
/**
 * Is the WHOLE activation admissible for the given unpairedAccess: activities allowed AND
 * (if active_roles present) the connection is playback-capable (activities + 'playback' allowed)?
 */
function isAdmissible(category, set, hasActiveRoles, unpairedAccess) {
    if (!isAllowedActivitySet(category, set, unpairedAccess))
        return false;
    if (hasActiveRoles) {
        const withPlayback = new Set(set);
        withPlayback.add("playback");
        if (!isAllowedActivitySet(category, withPlayback, unpairedAccess)) {
            return false;
        }
    }
    return true;
}
export function authorizeActivate(category, activities, activeRoles, unpairedAccess) {
    // The activation's `pairing` object is ignored unless 'pairing' is in
    // activities, so it never bears on authorization.
    const set = new Set(activities);
    const hasActiveRoles = !!activeRoles && activeRoles.length > 0;
    if (isAdmissible(category, set, hasActiveRoles, unpairedAccess)) {
        return { ok: true };
    }
    // pairing_required exactly when the matched PSK is Sentinel and enabling unpaired
    // access would make the whole activation admissible. Covers both a ['playback']
    // activation AND an active_roles-gated one blocked solely by unpairedAccess=false.
    if (category === "sentinel" &&
        !unpairedAccess &&
        isAdmissible(category, set, hasActiveRoles, true)) {
        return { ok: false, goodbye: "pairing_required" };
    }
    return { ok: false, goodbye: "unauthorized" };
}
//# sourceMappingURL=activate-authorization.js.map
