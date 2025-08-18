"""Recommendations configuration category for Nicovideo provider."""

from __future__ import annotations

from music_assistant.providers.nicovideo.config.categories.base import ConfigCategoryBase
from music_assistant.providers.nicovideo.config.factory import ConfigFactory


class RecommendationsConfigCategory(ConfigCategoryBase):
    """Recommendations settings category."""

    _rec = ConfigFactory("Recommendations")

    recommendation_filter_tags = _rec.str_list_config(
        key="recommendation_filter_tags",
        label="Tag filter for main recommendations / similar tracks",
        description=(
            "Comma-separated list of tags that tracks must have at least one of "
            "to appear in main recommendations and similar tracks.\n"
            "Leave empty to disable tag filtering.\n"
            "Not used for tag-based recommendations.\n"
            "Example: 'VOCALOID,音楽,ボカロ'"
        ),
    )

    main_recommendation_count = _rec.int_config(
        key="main_recommendation_count",
        label="Number of main recommendations",
        default=25,
        min_val=1,
        max_val=100,
        description=(
            "Number of tracks to fetch for recommendations.\n"
            "If tag filtering is enabled, the system will automatically "
            "fetch additional tracks to meet this target count."
        ),
    )

    tag_based_recommendation_tags = _rec.str_list_config(
        key="tag_based_recommendation_tags",
        label="Tags for tag-based recommendations",
        description=(
            "Comma-separated list of tags to search for recommended tracks.\n"
            "Tracks with these tags will be shown in 'Tag-based Recommendations' section.\n"
            "Leave empty to disable tag-based recommendations.\n"
            "Example: 'VOCALOID,音楽,ボカロ'"
        ),
    )
    tag_based_recommendation_count = _rec.int_config(
        key="tag_based_recommendation_count",
        label="Number of tag-based recommendations",
        default=25,
        min_val=1,
        max_val=100,
        description="Number of tracks to fetch for tag-based recommendations.",
    )

    tag_new_tracks_tags = _rec.str_list_config(
        key="tag_new_tracks_tags",
        label="Tags for new track recommendations",
        description=(
            "Comma-separated list of tags to search for new tracks.\n"
            "Latest tracks with these tags will be shown in 'New Tracks by Tags' section.\n"
            "Leave empty to disable tag-based new tracks.\n"
            "Example: 'VOCALOID,音楽,ボカロ'"
        ),
    )
    tag_new_tracks_count = _rec.int_config(
        key="tag_new_tracks_count",
        label="Number of new track recommendations",
        default=25,
        min_val=1,
        max_val=100,
        description="Number of latest tracks to fetch for the 'New Tracks by Tags' section.",
    )

    history_count = _rec.int_config(
        key="history_count",
        label="Number of history tracks",
        default=50,
        min_val=1,
        max_val=100,
        description="Number of recently watched tracks to show in recommendations.",
    )

    like_history_count = _rec.int_config(
        key="like_history_count",
        label="Number of like history tracks",
        default=50,
        min_val=1,
        max_val=100,
        description="Number of recently liked tracks to show in recommendations.",
    )

    following_activities_count = _rec.int_config(
        key="following_activities_count",
        label="Number of following activity tracks",
        default=30,
        min_val=1,
        max_val=100,
        description="Number of tracks from following activities to show in recommendations.",
    )
