"""Tests for the candidate scoring policies: vetoes and soft penalties."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from music_assistant.controllers.streams.smart_fades.models import (
    Deck,
    TransitionTier,
)
from music_assistant.controllers.streams.smart_fades.planner.context import (
    TransitionContext,
)
from music_assistant.controllers.streams.smart_fades.planner.policies import (
    AnchorAlignmentPolicy,
    AudibleTrimPolicy,
    OverlapPreferencePolicy,
    Verdict,
    VocalCollisionPolicy,
    VocalTruncationPolicy,
    default_policies,
)
from music_assistant.controllers.streams.smart_fades.vocal import (
    COLLISION_SECONDS_LIMIT,
    SHORT_FADE_SECONDS,
    WEIGHTED_COLLISION_LIMIT,
    VocalMask,
)
from music_assistant.models.audio_analysis import AudioAnalysisData
from tests.controllers.streams.smart_fades.conftest import build_test_candidate as _candidate


def _ctx(
    *,
    tier: TransitionTier = TransitionTier.FULL_BLEND,
    vocal_out_scoring: VocalMask | None = None,
    vocal_in_scoring: VocalMask | None = None,
) -> TransitionContext:
    deck = Deck(
        analysis=AudioAnalysisData(),
        bpm=120.0,
        beats=np.array([], dtype=np.float32),
        downbeats=np.array([], dtype=np.float32),
    )
    return TransitionContext(
        outgoing=deck,
        incoming=deck,
        outgoing_profile=None,
        incoming_profile=None,
        buffer_duration=45.0,
        buffer_offset=0.0,
        audio_end=45.0,
        default_anchor=45.0,
        mix_out_anchor=None,
        kick_anchor=None,
        fade_onset=None,
        coda_zone=None,
        tier=tier,
        cross_meter=False,
        bpm_diff_percent=0.0,
        vocal_out_placement=None,
        vocal_in_placement=None,
        vocal_out_scoring=vocal_out_scoring,
        vocal_in_scoring=vocal_in_scoring,
        natural_entry=0.0,
        protective_downbeats=(),
    )


# a mask presence-only fixture: contents never matter to any policy, only
# whether the field is None or not
_MASK = VocalMask(windows=[(0.0, 1.0)])


class TestVerdict:
    """The Verdict value object's two constructors."""

    def test_veto_sets_vetoed_true(self) -> None:
        """A vetoed verdict reports vetoed=True with its reason."""
        verdict = Verdict.veto("some reason")

        assert verdict.vetoed is True
        assert verdict.reason == "some reason"

    def test_ok_defaults_to_no_penalty_not_vetoed(self) -> None:
        """An accepted verdict defaults to zero penalty and vetoed=False."""
        verdict = Verdict.ok()

        assert verdict.vetoed is False
        assert verdict.penalty == 0.0

    def test_ok_carries_penalty(self) -> None:
        """An accepted verdict can still carry a soft penalty."""
        verdict = Verdict.ok(7.5, "reason")

        assert verdict.vetoed is False
        assert verdict.penalty == 7.5
        assert verdict.reason == "reason"


class TestVocalCollisionPolicy:
    """Veto/penalize simultaneous outgoing/incoming vocal overlap."""

    policy = VocalCollisionPolicy()

    def test_neutral_when_out_scoring_mask_missing(self) -> None:
        """No outgoing vocal timeline disables the guard entirely."""
        candidate = _candidate(collision=999.0, weighted=999.0)
        ctx = _ctx(vocal_out_scoring=None, vocal_in_scoring=_MASK)

        verdict = self.policy.evaluate(candidate, ctx)

        assert verdict.vetoed is False
        assert verdict.penalty == 0.0

    def test_neutral_when_in_scoring_mask_missing(self) -> None:
        """No incoming vocal timeline disables the guard entirely."""
        candidate = _candidate(collision=999.0, weighted=999.0)
        ctx = _ctx(vocal_out_scoring=_MASK, vocal_in_scoring=None)

        verdict = self.policy.evaluate(candidate, ctx)

        assert verdict.vetoed is False
        assert verdict.penalty == 0.0

    def test_vetoes_at_collision_seconds_limit(self) -> None:
        """Raw collision seconds at the limit vetoes the candidate."""
        candidate = _candidate(collision=COLLISION_SECONDS_LIMIT, weighted=0.0)
        ctx = _ctx(vocal_out_scoring=_MASK, vocal_in_scoring=_MASK)

        assert self.policy.evaluate(candidate, ctx).vetoed is True

    def test_no_veto_just_below_collision_seconds_limit(self) -> None:
        """Raw collision seconds just under the limit does not veto."""
        candidate = _candidate(collision=COLLISION_SECONDS_LIMIT - 0.01, weighted=0.0)
        ctx = _ctx(vocal_out_scoring=_MASK, vocal_in_scoring=_MASK)

        assert self.policy.evaluate(candidate, ctx).vetoed is False

    def test_vetoes_at_weighted_collision_limit(self) -> None:
        """Weighted (gain-integrated) collision at the limit vetoes the candidate."""
        candidate = _candidate(collision=0.0, weighted=WEIGHTED_COLLISION_LIMIT)
        ctx = _ctx(vocal_out_scoring=_MASK, vocal_in_scoring=_MASK)

        assert self.policy.evaluate(candidate, ctx).vetoed is True

    def test_no_veto_just_below_weighted_collision_limit(self) -> None:
        """Weighted collision just under the limit does not veto."""
        candidate = _candidate(collision=0.0, weighted=WEIGHTED_COLLISION_LIMIT - 0.01)
        ctx = _ctx(vocal_out_scoring=_MASK, vocal_in_scoring=_MASK)

        assert self.policy.evaluate(candidate, ctx).vetoed is False

    def test_penalty_proportional_to_weighted_collision(self) -> None:
        """Penalty scales linearly with weighted collision seconds, doubling with it."""
        ctx = _ctx(vocal_out_scoring=_MASK, vocal_in_scoring=_MASK)
        low = _candidate(weighted=0.1)
        high = _candidate(weighted=0.2)

        low_penalty = self.policy.evaluate(low, ctx).penalty
        high_penalty = self.policy.evaluate(high, ctx).penalty

        assert low_penalty == pytest.approx(0.1 / WEIGHTED_COLLISION_LIMIT * 20.0)
        assert high_penalty == pytest.approx(2 * low_penalty)


class TestVocalTruncationPolicy:
    """Veto a candidate that cuts off an audible outgoing vocal phrase."""

    policy = VocalTruncationPolicy()

    def test_neutral_when_out_scoring_mask_missing(self) -> None:
        """No outgoing vocal timeline disables the guard entirely."""
        candidate = _candidate(vocal_fade=999.0)
        ctx = _ctx(vocal_out_scoring=None)

        verdict = self.policy.evaluate(candidate, ctx)

        assert verdict.vetoed is False
        assert verdict.penalty == 0.0

    def test_vetoes_when_audible_vocal_extends_past_the_anchor(self) -> None:
        """A phrase cut off by the anchor trim (beyond 0.25s) vetoes the candidate."""
        candidate = _candidate(duration=20.0)
        ctx = _ctx(vocal_out_scoring=VocalMask(windows=[(18.0, 21.0)]))

        assert self.policy.evaluate(candidate, ctx).vetoed is True

    def test_no_veto_at_exactly_the_truncation_threshold(self) -> None:
        """Exactly 0.25s of cut vocal does not veto (strictly-greater test)."""
        candidate = _candidate(duration=20.0)
        ctx = _ctx(vocal_out_scoring=VocalMask(windows=[(20.0, 20.25)]))

        assert self.policy.evaluate(candidate, ctx).vetoed is False

    def test_no_veto_when_the_vocal_rides_the_fade_untruncated(self) -> None:
        """A long phrase entirely inside the fade window is normal, never a truncation."""
        candidate = _candidate(duration=20.0)
        ctx = _ctx(vocal_out_scoring=VocalMask(windows=[(5.0, 19.5)]))

        assert self.policy.evaluate(candidate, ctx).vetoed is False

    def test_inaudible_vocal_past_the_rms_boundary_never_counts(self) -> None:
        """Windows beyond audio_end are inaudible and cannot register as truncation."""
        candidate = _candidate(duration=20.0)
        ctx = _ctx(vocal_out_scoring=VocalMask(windows=[(46.0, 50.0)]))

        assert self.policy.evaluate(candidate, ctx).vetoed is False


class TestAudibleTrimPolicy:
    """Veto (on a short fade) or penalize trimming audible outgoing material."""

    policy = AudibleTrimPolicy()

    def test_vetoes_when_short_fade_trims_past_its_own_duration(self) -> None:
        """A crossfade at the short-fade limit that trims more than it spans vetoes."""
        candidate = _candidate(duration=SHORT_FADE_SECONDS, trim=SHORT_FADE_SECONDS + 0.5)
        # the hard rule is vocal-protection-scoped: it needs a vocal timeline
        ctx = _ctx(vocal_out_scoring=VocalMask(windows=[]))

        assert self.policy.evaluate(candidate, ctx).vetoed is True

    def test_no_veto_when_trim_equals_short_fade_duration(self) -> None:
        """A short fade whose trim exactly equals its duration does not veto (strict >)."""
        candidate = _candidate(duration=SHORT_FADE_SECONDS, trim=SHORT_FADE_SECONDS)
        ctx = _ctx(vocal_out_scoring=VocalMask(windows=[]))

        assert self.policy.evaluate(candidate, ctx).vetoed is False

    def test_no_veto_when_fade_longer_than_short_fade_limit(self) -> None:
        """A fade longer than the short-fade limit never vetoes on trim, however large."""
        candidate = _candidate(duration=SHORT_FADE_SECONDS + 0.01, trim=SHORT_FADE_SECONDS + 5.0)
        ctx = _ctx()

        assert self.policy.evaluate(candidate, ctx).vetoed is False

    def test_penalty_proportional_to_trim(self) -> None:
        """Penalty scales linearly (1.0/s) with audible outgoing trim, doubling with it."""
        ctx = _ctx()
        low = _candidate(duration=20.0, trim=3.0)
        high = _candidate(duration=20.0, trim=6.0)

        low_penalty = self.policy.evaluate(low, ctx).penalty
        high_penalty = self.policy.evaluate(high, ctx).penalty

        assert low_penalty == pytest.approx(3.0)
        assert high_penalty == pytest.approx(2 * low_penalty)


class TestOverlapPreferencePolicy:
    """Prefer the tier's top rung, the context's chosen tier, and two-sided vocal relief."""

    policy = OverlapPreferencePolicy()

    def test_no_penalty_at_top_rung_and_matching_tier(self) -> None:
        """A candidate at its ideal bar count and the context's tier earns no penalty."""
        candidate = _candidate(bars=16, ideal=16, tier=TransitionTier.FULL_BLEND)
        ctx = _ctx(tier=TransitionTier.FULL_BLEND)

        assert self.policy.evaluate(candidate, ctx).penalty == pytest.approx(0.0)

    def test_penalty_scales_with_rung_gap(self) -> None:
        """Each ladder rung below the ideal adds 10.0 penalty."""
        ctx = _ctx(tier=TransitionTier.FULL_BLEND)
        one_rung = _candidate(bars=8, ideal=16, tier=TransitionTier.FULL_BLEND)
        two_rungs = _candidate(bars=4, ideal=16, tier=TransitionTier.FULL_BLEND)

        assert self.policy.evaluate(one_rung, ctx).penalty == pytest.approx(10.0)
        assert self.policy.evaluate(two_rungs, ctx).penalty == pytest.approx(20.0)

    def test_penalty_scales_with_tier_steps_below_context(self) -> None:
        """Each tier step below the context's chosen tier adds 15.0 penalty."""
        ctx = _ctx(tier=TransitionTier.FULL_BLEND)
        one_step = _candidate(bars=16, ideal=16, tier=TransitionTier.TEMPO_BLEND)
        two_steps = _candidate(bars=16, ideal=16, tier=TransitionTier.QUICK_FADE)

        assert self.policy.evaluate(one_step, ctx).penalty == pytest.approx(15.0)
        assert self.policy.evaluate(two_steps, ctx).penalty == pytest.approx(30.0)

    def test_no_negative_penalty_when_tier_exceeds_context(self) -> None:
        """A candidate whose tier is more ambitious than the context's earns no tier penalty."""
        candidate = _candidate(bars=16, ideal=16, tier=TransitionTier.FULL_BLEND)
        ctx = _ctx(tier=TransitionTier.QUICK_FADE)

        assert self.policy.evaluate(candidate, ctx).penalty == pytest.approx(0.0)

    def test_one_sided_vocal_asymmetry(self) -> None:
        """Relaxing on the outgoing side costs more (12.0) than the incoming side (5.0)."""
        ctx = _ctx(tier=TransitionTier.FULL_BLEND)
        incoming = _candidate(
            bars=16, ideal=16, tier=TransitionTier.FULL_BLEND, one_sided="incoming"
        )
        outgoing = _candidate(
            bars=16, ideal=16, tier=TransitionTier.FULL_BLEND, one_sided="outgoing"
        )

        incoming_penalty = self.policy.evaluate(incoming, ctx).penalty
        outgoing_penalty = self.policy.evaluate(outgoing, ctx).penalty

        assert incoming_penalty == pytest.approx(5.0)
        assert outgoing_penalty == pytest.approx(12.0)
        assert outgoing_penalty > incoming_penalty

    def test_never_vetoes(self) -> None:
        """This is a pure soft-scoring policy: it never disqualifies a candidate."""
        candidate = _candidate(
            bars=1, ideal=16, tier=TransitionTier.QUICK_FADE, one_sided="outgoing"
        )
        ctx = _ctx(tier=TransitionTier.FULL_BLEND)

        assert self.policy.evaluate(candidate, ctx).vetoed is False


class TestAnchorAlignmentPolicy:
    """Prefer downbeat-anchored fades and groove-aligned incoming entries."""

    policy = AnchorAlignmentPolicy()

    def test_no_penalty_on_downbeat_with_natural_entry(self) -> None:
        """A downbeat-anchored fade with no pinned entry earns no penalty."""
        candidate = _candidate(on_downbeat=True)
        ctx = _ctx()

        assert self.policy.evaluate(candidate, ctx).penalty == pytest.approx(0.0)

    def test_penalty_when_not_on_downbeat(self) -> None:
        """A fade not anchored on a downbeat costs 4.0."""
        candidate = _candidate(on_downbeat=False)
        ctx = _ctx()

        assert self.policy.evaluate(candidate, ctx).penalty == pytest.approx(4.0)

    def test_penalty_when_pinned_entry_not_groove_aligned(self) -> None:
        """A pinned entry from a non-vocal-onset source costs 2.0."""
        candidate = _candidate(on_downbeat=True)
        candidate = dataclasses.replace(
            candidate, spec=dataclasses.replace(candidate.spec, entry_s=12.3, source="some-other")
        )
        ctx = _ctx()

        assert self.policy.evaluate(candidate, ctx).penalty == pytest.approx(2.0)

    def test_no_penalty_when_pinned_entry_is_vocal_onset(self) -> None:
        """A pinned entry from the vocal-onset-entry generator counts as aligned."""
        candidate = _candidate(on_downbeat=True)
        candidate = dataclasses.replace(
            candidate,
            spec=dataclasses.replace(candidate.spec, entry_s=12.3, source="vocal-onset-entry"),
        )
        ctx = _ctx()

        assert self.policy.evaluate(candidate, ctx).penalty == pytest.approx(0.0)

    def test_penalties_stack(self) -> None:
        """Both the downbeat and entry-alignment penalties can apply together."""
        candidate = _candidate(on_downbeat=False)
        candidate = dataclasses.replace(
            candidate, spec=dataclasses.replace(candidate.spec, entry_s=1.0, source="some-other")
        )
        ctx = _ctx()

        assert self.policy.evaluate(candidate, ctx).penalty == pytest.approx(6.0)


def test_default_policies_returns_the_five_standard_policies() -> None:
    """The standard policy tuple contains one instance of each of the five policies."""
    policies = default_policies()

    assert [type(p) for p in policies] == [
        VocalCollisionPolicy,
        VocalTruncationPolicy,
        AudibleTrimPolicy,
        OverlapPreferencePolicy,
        AnchorAlignmentPolicy,
    ]
