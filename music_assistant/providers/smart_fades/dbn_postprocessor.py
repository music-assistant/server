"""Pure numpy DBN postprocessor for beat/downbeat tracking."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from numpy.typing import NDArray


class DBNDownBeatTracker:
    """
    Pure numpy DBN postprocessor for beat and downbeat tracking.

    Drop-in replacement for madmom's DBNDownBeatTrackingProcessor.

    :param beats_per_bar: List of time signatures to try (e.g. [3, 4]).
    :param min_bpm: Minimum tempo in BPM.
    :param max_bpm: Maximum tempo in BPM.
    :param fps: Frames per second of the input activations.
    :param transition_lambda: Tempo change penalty (higher = more stable).
    :param observation_lambda: Beat subdivision granularity.
    :param threshold: Minimum activation to consider.
    :param correct: Whether to snap beats to activation peaks.
    """

    def __init__(
        self,
        beats_per_bar: list[int] | None = None,
        min_bpm: float = 55.0,
        max_bpm: float = 215.0,
        fps: int = 50,
        transition_lambda: float = 100.0,
        observation_lambda: int = 16,
        threshold: float = 0.05,
        correct: bool = True,
    ) -> None:
        """Initialize the DBN tracker with the given parameters."""
        if beats_per_bar is None:
            beats_per_bar = [3, 4]
        self.fps = fps
        self.threshold = threshold
        self.correct = correct
        self.observation_lambda = observation_lambda

        self._hmms: list[dict[str, Any]] = []
        min_interval = round(60.0 * fps / max_bpm)
        max_interval = round(60.0 * fps / min_bpm)

        for num_beats in beats_per_bar:
            positions, intervals = self._build_bar_state_space(
                num_beats, min_interval, max_interval
            )
            tm_states, tm_pointers, tm_log_probs = self._build_transition_model(
                positions, intervals, num_beats, transition_lambda
            )
            om_pointers = self._build_observation_model(positions, observation_lambda)
            self._hmms.append(
                {
                    "num_beats": num_beats,
                    "positions": positions,
                    "om_pointers": om_pointers,
                    "tm_states": tm_states,
                    "tm_pointers": tm_pointers,
                    "tm_log_probs": tm_log_probs,
                }
            )

    def __call__(self, activations: NDArray[np.float64]) -> tuple[NDArray[np.float64], int]:
        """
        Run DBN decoding on beat/downbeat activations.

        :param activations: Shape (T, 2), columns [beat_act, downbeat_act].
            Values should be probabilities in (0, 1).
        :return: Tuple of (beats, num_beats).
            beats: Shape (M, 2) array of [time_seconds, beat_position].
                beat_position is 1 for downbeats, 2..num_beats for other beats.
            num_beats: Beats per bar of the winning meter hypothesis (0 if no beats found).
        """
        # Threshold: trim leading/trailing silence
        first = 0
        if self.threshold:
            above = np.nonzero(activations.max(axis=1) >= self.threshold)[0]
            if len(above) > 0:
                first = int(above[0])
                last = int(above[-1]) + 1
                activations = activations[first:last]

        log_dens = self._compute_log_densities(activations, self.observation_lambda)

        # Run Viterbi for each meter hypothesis, pick best
        best_path = None
        best_log_prob = -np.inf
        best_hmm = self._hmms[0]

        for hmm in self._hmms:
            path, log_prob = self._viterbi(
                log_dens,
                hmm["om_pointers"],
                hmm["tm_states"],
                hmm["tm_pointers"],
                hmm["tm_log_probs"],
            )
            if log_prob > best_log_prob:
                best_log_prob = log_prob
                best_path = path
                best_hmm = hmm

        assert best_path is not None
        positions = best_hmm["positions"][best_path]
        beat_numbers = positions.astype(int) + 1

        if self.correct:
            beats = self._correct_beats(best_path, best_hmm, activations, beat_numbers)
        else:
            beats = np.nonzero(np.diff(beat_numbers))[0] + 1

        if len(beats) == 0:
            return np.empty((0, 2), dtype=np.float64), 0

        beat_times = (beats + first) / self.fps
        beat_positions = beat_numbers[beats]

        return np.column_stack([beat_times, beat_positions]), int(best_hmm["num_beats"])

    @staticmethod
    def _build_bar_state_space(
        num_beats: int,
        min_interval: int,
        max_interval: int,
    ) -> tuple[NDArray[np.float32], NDArray[np.int32]]:
        """
        Build a bar-pointer state space for the given tempo range.

        Each state represents a position within a bar at a specific tempo.
        For each tempo interval i (in frames), there are i discrete positions
        per beat. A bar with B beats has B x i states for interval i.

        :param num_beats: Number of beats per bar (e.g. 4 for 4/4 time).
        :param min_interval: Minimum beat interval in frames.
        :param max_interval: Maximum beat interval in frames.
        :return: Tuple of (state_positions, state_intervals).
            state_positions: float64 array, position in bar [0, num_beats).
            state_intervals: int32 array, tempo interval for each state.
        """
        intervals_range = np.arange(min_interval, max_interval + 1)
        counts = intervals_range
        one_beat_intervals = np.repeat(intervals_range, counts)
        offsets = np.concatenate([np.arange(i, dtype=np.float32) / i for i in intervals_range])

        all_positions = np.concatenate([beat + offsets for beat in range(num_beats)]).astype(
            np.float32
        )
        all_intervals = np.tile(one_beat_intervals, num_beats)

        return all_positions, all_intervals.astype(np.int32)

    @staticmethod
    def _build_transition_model(
        positions: NDArray[np.float32],
        intervals: NDArray[np.int32],
        num_beats: int,
        transition_lambda: float,
    ) -> tuple[NDArray[np.int32], NDArray[np.int32], NDArray[np.float32]]:
        """
        Build a sparse CSR transition model for the bar state space.

        Within a beat, each state transitions deterministically to the next
        (constant tempo). At beat boundaries, transitions follow an exponential
        tempo-change distribution controlled by transition_lambda.

        :param positions: State positions from _build_bar_state_space.
        :param intervals: State intervals from _build_bar_state_space.
        :param num_beats: Number of beats per bar.
        :param transition_lambda: Penalty for tempo changes (higher = less change).
        :return: Tuple of (tm_states, tm_pointers, tm_log_probs) in CSR format.
            For state s, predecessors are tm_states[tm_pointers[s]:tm_pointers[s+1]]
            with log probabilities tm_log_probs[tm_pointers[s]:tm_pointers[s+1]].
        """
        num_states = len(positions)

        # Identify first and last states per (beat, interval)
        unique_intervals = np.arange(intervals.min(), intervals.max() + 1)
        num_intervals = len(unique_intervals)
        states_per_beat = num_states // num_beats

        # Build first/last state indices per beat
        first_states_per_beat: list[NDArray[np.int32]] = []
        last_states_per_beat: list[NDArray[np.int32]] = []
        for beat in range(num_beats):
            offset = beat * states_per_beat
            firsts = []
            lasts = []
            idx = 0
            for interval in unique_intervals:
                firsts.append(offset + idx)
                idx += interval
                lasts.append(offset + idx - 1)
            first_states_per_beat.append(np.array(firsts, dtype=np.int32))
            last_states_per_beat.append(np.array(lasts, dtype=np.int32))

        # Compute exponential transition probabilities between tempi
        from_intervals = unique_intervals.astype(np.float64)
        to_intervals = unique_intervals.astype(np.float64)
        ratio = to_intervals[np.newaxis, :] / from_intervals[:, np.newaxis]
        trans_prob = np.exp(-transition_lambda * np.abs(ratio - 1.0))
        # Normalize each row
        row_sums = trans_prob.sum(axis=1, keepdims=True)
        trans_prob = trans_prob / row_sums
        trans_log_prob = np.log(trans_prob).astype(np.float32)

        # Vectorized CSR construction: separate within-beat from boundary states
        frac = positions - np.floor(positions)
        within_mask = frac > 0
        within_states = np.nonzero(within_mask)[0]
        boundary_states = np.nonzero(~within_mask)[0]

        # Within-beat: predecessor is state - 1, log_prob = 0
        sources_w = within_states - 1
        dests_w = within_states
        logprobs_w = np.zeros(len(within_states), dtype=np.float32)

        # Boundary states: predecessors from last states of previous beat
        sources_b: list[int] = []
        dests_b: list[int] = []
        logprobs_b: list[float] = []
        min_interval_val = int(intervals.min())

        for state in boundary_states:
            beat = int(positions[state])
            prev_beat = (beat - 1) % num_beats
            prev_lasts = last_states_per_beat[prev_beat]
            cur_interval_idx = int(intervals[state]) - min_interval_val

            for from_idx in range(num_intervals):
                lp = trans_log_prob[from_idx, cur_interval_idx]
                if lp > -50:
                    sources_b.append(int(prev_lasts[from_idx]))
                    dests_b.append(int(state))
                    logprobs_b.append(float(lp))

        sources_all = np.concatenate([sources_w, np.array(sources_b, dtype=np.int32)]).astype(
            np.int32
        )
        dests_all = np.concatenate([dests_w, np.array(dests_b, dtype=np.int32)]).astype(np.int32)
        logprobs_all = np.concatenate([logprobs_w, np.array(logprobs_b, dtype=np.float32)]).astype(
            np.float32
        )

        return DBNDownBeatTracker._csr_from_arrays(sources_all, dests_all, logprobs_all, num_states)

    @staticmethod
    def _csr_from_arrays(
        sources_arr: NDArray[np.int32],
        dests_arr: NDArray[np.int32],
        log_probs_arr: NDArray[np.float32],
        num_states: int,
    ) -> tuple[NDArray[np.int32], NDArray[np.int32], NDArray[np.float32]]:
        """Convert transition arrays to CSR format indexed by destination."""
        order = np.argsort(dests_arr, kind="stable")
        sources_arr = sources_arr[order]
        dests_arr = dests_arr[order]
        log_probs_arr = log_probs_arr[order]

        tm_pointers = np.zeros(num_states + 1, dtype=np.int32)
        tm_pointers[1:] = np.cumsum(np.bincount(dests_arr, minlength=num_states))

        return sources_arr, tm_pointers, log_probs_arr

    @staticmethod
    def _build_observation_model(
        positions: NDArray[np.float32],
        observation_lambda: int = 16,
    ) -> NDArray[np.int32]:
        """
        Build observation model pointers mapping states to observation classes.

        Class 0 = no-beat, class 1 = beat, class 2 = downbeat.

        :param positions: State positions from _build_bar_state_space.
        :param observation_lambda: Beat subdivision granularity.
        :return: Array of observation class indices, one per state.
        """
        border = 1.0 / observation_lambda
        pointers = np.zeros(len(positions), dtype=np.int32)
        # Beat states: fractional position within beat < border
        frac = positions % 1.0
        pointers[frac < border] = 1
        # Downbeat states: absolute position < border (first beat of bar)
        pointers[positions < border] = 2
        return pointers

    @staticmethod
    def _compute_log_densities(
        activations: NDArray[np.float64],
        observation_lambda: int = 16,
    ) -> NDArray[np.float32]:
        """
        Compute log observation densities from beat/downbeat activations.

        :param activations: Shape (T, 2), columns [beat_act, downbeat_act].
        :param observation_lambda: Beat subdivision granularity.
        :return: Shape (T, 3) log densities for [no-beat, beat, downbeat].
        """
        act = activations.astype(np.float32, copy=False)
        beat_act = act[:, 0]
        downbeat_act = act[:, 1]
        no_beat_act = np.maximum(1.0 - beat_act - downbeat_act, 1e-7)

        log_dens = np.empty((len(activations), 3), dtype=np.float32)
        log_dens[:, 0] = np.log(no_beat_act / (observation_lambda - 1))
        log_dens[:, 1] = np.log(np.maximum(beat_act, 1e-7))
        log_dens[:, 2] = np.log(np.maximum(downbeat_act, 1e-7))
        return log_dens

    @staticmethod
    def _viterbi(
        log_densities: NDArray[np.float32],
        om_pointers: NDArray[np.int32],
        tm_states: NDArray[np.int32],
        tm_pointers: NDArray[np.int32],
        tm_log_probs: NDArray[np.float32],
    ) -> tuple[NDArray[np.int32], float]:
        """
        Run Viterbi decoding on the HMM.

        :param log_densities: Shape (T, 3), per-frame log observation densities.
        :param om_pointers: Shape (S,), observation class per state.
        :param tm_states: CSR source states for transitions.
        :param tm_pointers: CSR pointer array for transitions.
        :param tm_log_probs: CSR log probabilities for transitions.
        :return: Tuple of (path, log_probability).
            path: shape (T,) int32 array of state indices.
            log_probability: float, log prob of best path.
        """
        num_frames = len(log_densities)
        num_states = len(om_pointers)

        # Classify states: single-predecessor (within-beat) vs multi-predecessor (beat boundary)
        num_preds = np.diff(tm_pointers)
        single_mask = num_preds == 1
        multi_mask = ~single_mask
        single_states = np.nonzero(single_mask)[0]
        multi_states = np.nonzero(multi_mask)[0]

        # For single-predecessor states, precompute the single source
        single_sources = np.empty(num_states, dtype=np.int32)
        single_sources[single_states] = tm_states[tm_pointers[single_states]]

        # For multi-predecessor states, build padded arrays for vectorized max
        if len(multi_states) > 0:
            max_preds = num_preds[multi_states].max()
            multi_source_pad = np.zeros((len(multi_states), max_preds), dtype=np.int32)
            multi_logprob_pad = np.full((len(multi_states), max_preds), -np.inf, dtype=np.float32)
            for i, s in enumerate(multi_states):
                start = tm_pointers[s]
                end = tm_pointers[s + 1]
                n = end - start
                multi_source_pad[i, :n] = tm_states[start:end]
                multi_logprob_pad[i, :n] = tm_log_probs[start:end]

        # Initialize: uniform over all states, ping-pong buffers
        buf_a = np.empty(num_states, dtype=np.float32)
        buf_b = np.empty(num_states, dtype=np.float32)
        prev_v = buf_a
        prev_v[:] = np.float32(-np.log(num_states))
        bt = np.empty((num_frames, len(multi_states)), dtype=np.int32)

        # Pre-compute constant index arrays used every frame
        single_src_idx = single_sources[single_states]
        has_multi = len(multi_states) > 0
        if has_multi:
            arange_multi = np.arange(len(multi_states))

        for t in range(num_frames):
            cur_v = buf_b if prev_v is buf_a else buf_a
            cur_v[:] = -np.inf
            obs = log_densities[t, om_pointers]

            # Single-predecessor states: direct assignment (log_prob = 0)
            cur_v[single_states] = prev_v[single_src_idx] + obs[single_states]

            # Multi-predecessor states: max over predecessors
            if has_multi:
                scores = prev_v[multi_source_pad] + multi_logprob_pad
                best_idx = scores.argmax(axis=1)
                cur_v[multi_states] = scores[arange_multi, best_idx] + obs[multi_states]
                bt[t] = best_idx

            prev_v = cur_v

        # Backtrack — O(1) lookup instead of searchsorted
        multi_lookup = np.full(num_states, -1, dtype=np.int32)
        multi_lookup[multi_states] = np.arange(len(multi_states), dtype=np.int32)

        path = np.empty(num_frames, dtype=np.int32)
        best_state = int(np.argmax(prev_v))
        log_prob = float(prev_v[best_state])

        for t in range(num_frames - 1, -1, -1):
            path[t] = best_state
            mi = multi_lookup[best_state]
            if mi >= 0:
                best_state = int(multi_source_pad[mi, bt[t, mi]])
            else:
                best_state = int(single_sources[best_state])

        return path, log_prob

    def _correct_beats(
        self,
        path: NDArray[np.int32],
        hmm: dict[str, Any],
        activations: NDArray[np.float64],
        beat_numbers: NDArray[np.int64],
    ) -> NDArray[np.int64]:
        """Snap detected beats to activation peaks within beat regions."""
        om_pointers = hmm["om_pointers"]
        beat_range = om_pointers[path] >= 1

        transitions = np.diff(beat_range.astype(np.int32))
        idx = np.nonzero(transitions)[0] + 1

        if beat_range[0]:
            idx = np.concatenate([[0], idx])
        if beat_range[-1]:
            idx = np.concatenate([idx, [len(beat_range)]])

        if len(idx) % 2 != 0:
            idx = idx[:-1]

        beats = []
        for i in range(0, len(idx), 2):
            left, right = idx[i], idx[i + 1]
            # Find peak activation in region (sum both columns)
            region_act = activations[left:right].sum(axis=1)
            peak = int(np.argmax(region_act)) + left
            beats.append(peak)

        return np.array(beats, dtype=np.int64)
