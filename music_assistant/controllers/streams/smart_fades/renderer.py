"""
Smart Fades - Transition renderer.

The renderer is the DJ's hands: it picks the tools from the filter toolset
(``filters.py``) that realize a ``TransitionPlan``, and produces the
``CrossfadeTimingInfo`` breakdown.  This is the only place where bytes
re-enter: the plan is sized in seconds, the renderer reconciles it with the
actual buffer lengths and drives both the acrossfade overlap and the timing
bookkeeping from that one reconciled value.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from music_assistant.controllers.streams.smart_fades.filters import (
    CrossfadeFilter,
    FadeInTrimFilter,
    FadeOutTrimFilter,
    Filter,
    GradualTimeStretchFilter,
    PeakFilter,
    ShelfFilter,
    ShelfType,
)
from music_assistant.controllers.streams.smart_fades.models import (
    CrossfadeTimingInfo,
    ShelfSchedule,
    TransitionPlan,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat


class TransitionRenderer:
    """Renders a ``TransitionPlan`` into an FFmpeg filter chain and timing info."""

    def __init__(self, logger: logging.Logger) -> None:
        """Initialize the renderer."""
        self.logger = logger

    def render(
        self,
        plan: TransitionPlan,
        pcm_format: AudioFormat,
        fade_in_bytes_len: int,
    ) -> tuple[list[Filter], CrossfadeTimingInfo]:
        """
        Build the filter chain and timing info for a transition plan.

        :param plan: The transition plan to render.
        :param pcm_format: PCM format of both input buffers and the output.
        :param fade_in_bytes_len: Length in bytes of the incoming track's head buffer.
        """
        fade_out_seconds = plan.fade_out_window - plan.tempo_plan.savings_until(
            plan.fade_out_window
        )
        fade_in_seconds = fade_in_bytes_len / pcm_format.pcm_sample_size
        fadein_trimmed = plan.fadein_trim_start or 0.0
        # clamp CF to fit shorter inputs (defensive — normally full buffers)
        crossfade_samples = int(
            min(
                plan.crossfade_duration,
                fade_out_seconds,
                max(0.0, fade_in_seconds - fadein_trimmed),
            )
            * pcm_format.sample_rate
        )
        crossfade_seconds = crossfade_samples / pcm_format.sample_rate
        filters = self._build_filters(plan, crossfade_samples)
        timing = CrossfadeTimingInfo(
            pre_crossfade_duration=max(0.0, fade_out_seconds - crossfade_seconds),
            crossfade_duration=crossfade_seconds,
            fadein_trimmed_duration=fadein_trimmed,
            post_crossfade_duration=max(0.0, fade_in_seconds - fadein_trimmed - crossfade_seconds),
        )
        return filters, timing

    def _build_filters(self, plan: TransitionPlan, crossfade_samples: int) -> list[Filter]:
        """Assemble the ordered filter chain from the plan."""
        filters: list[Filter] = []
        # FadeOutTrim first: its cut point is on the untrimmed input timeline
        if plan.fadeout_trim is not None:
            filters.append(
                FadeOutTrimFilter(
                    logger=self.logger,
                    fadeout_end_pos=plan.fadeout_trim.end_pos,
                    trimmed_seconds=plan.fadeout_trim.trimmed_seconds,
                )
            )
        # outgoing shelves before the stretch keep their schedules in musical input time
        self._append_shelf(filters, plan.eq_plan.low_out, "fadeout")
        self._append_shelf(filters, plan.eq_plan.high_out, "fadeout")
        self._append_shelf(filters, plan.eq_plan.mid_out, "fadeout")
        if plan.tempo_plan:
            filters.append(GradualTimeStretchFilter(self.logger, plan.tempo_plan.steps))
        if plan.fadein_trim_start is not None:
            filters.append(
                FadeInTrimFilter(logger=self.logger, fadein_start_pos=plan.fadein_trim_start)
            )
        self._append_shelf(filters, plan.eq_plan.low_in, "fadein")
        self._append_shelf(filters, plan.eq_plan.high_in, "fadein")
        self._append_shelf(filters, plan.eq_plan.mid_in, "fadein")
        filters.append(CrossfadeFilter(logger=self.logger, crossfade_samples=crossfade_samples))
        return filters

    def _append_shelf(
        self, filters: list[Filter], schedule: ShelfSchedule | None, stream_type: str
    ) -> None:
        """Append the matching filter for ``schedule``, or nothing when bypassed (None)."""
        if schedule is None:
            return
        if schedule.shelf_type is ShelfType.PEAK:
            filters.append(
                PeakFilter(
                    logger=self.logger,
                    frequency=schedule.frequency,
                    width_oct=schedule.width_oct,
                    gain_steps=schedule.steps,
                    stream_type=stream_type,
                )
            )
            return
        filters.append(
            ShelfFilter(
                logger=self.logger,
                shelf_type=schedule.shelf_type,
                frequency=schedule.frequency,
                gain_steps=schedule.steps,
                stream_type=stream_type,
            )
        )
