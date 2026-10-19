"""Shared SQL fragments for completed-play statistics."""

from music_assistant_models.enums import MediaType

from music_assistant.constants import DB_TABLE_PLAY_HISTORY, DB_TABLE_PROVIDER_MAPPINGS

PLAY_HISTORY_EVENT_TYPES = (
    MediaType.TRACK.value,
    MediaType.RADIO.value,
    MediaType.AUDIOBOOK.value,
    MediaType.PODCAST_EPISODE.value,
)
PLAY_HISTORY_EVENT_TYPE_SQL = ", ".join(
    f"'{media_type}'" for media_type in PLAY_HISTORY_EVENT_TYPES
)
# Domain-scoped history must resolve to one account mapping, while exact instances win.
# Several accounts can map the same domain ID; choosing one prevents a join from
# multiplying a single play into several events.
PROVIDER_MAPPING_JOIN = f"""
    {{mapping}}.media_type = {{media_type}}
    AND {{mapping}}.provider_item_id = {{item_id}}
    AND {{mapping}}.provider_instance = COALESCE(
        (
            SELECT exact_mapping.provider_instance
            FROM {DB_TABLE_PROVIDER_MAPPINGS} AS exact_mapping
            WHERE exact_mapping.media_type = {{media_type}}
                AND exact_mapping.provider_item_id = {{item_id}}
                AND exact_mapping.provider_instance = {{provider}}
            LIMIT 1
        ),
        (
            SELECT domain_mapping.provider_instance
            FROM {DB_TABLE_PROVIDER_MAPPINGS} AS domain_mapping
            WHERE domain_mapping.media_type = {{media_type}}
                AND domain_mapping.provider_item_id = {{item_id}}
                AND domain_mapping.provider_domain = {{provider}}
            ORDER BY domain_mapping.item_id, domain_mapping.provider_instance
            LIMIT 1
        )
    )
"""
TRACK_HISTORY_CTE = f"""
    history_tracks AS (
        SELECT play_history.id AS play_id, provider_mappings.item_id AS track_id
        FROM {DB_TABLE_PLAY_HISTORY} AS play_history
        INNER JOIN {DB_TABLE_PROVIDER_MAPPINGS} AS provider_mappings
            ON {
    PROVIDER_MAPPING_JOIN.format(
        mapping="provider_mappings",
        media_type="play_history.media_type",
        item_id="play_history.item_id",
        provider="play_history.provider",
    )
}
        WHERE play_history.userid = :user_id
            AND play_history.media_type = :media_type
            AND play_history.timestamp >= :cutoff_timestamp
        UNION
        SELECT play_history.id AS play_id, CAST(play_history.item_id AS INTEGER) AS track_id
        FROM {DB_TABLE_PLAY_HISTORY} AS play_history
        WHERE play_history.userid = :user_id
            AND play_history.media_type = :media_type
            AND play_history.provider = 'library'
            AND play_history.timestamp >= :cutoff_timestamp
    )
"""
# Selecting an artist is not a listening event; artist statistics come from the
# credits stored on completed track plays, not from artist/container history rows.
# Aliases with different display metadata still represent one artist credit per play.
TRACK_ARTISTS_CTE = f"""
    normalized_artist_credits AS (
        SELECT
            play_history.id AS play_id,
            CASE
                WHEN play_history.provider = 'library'
                    THEN CAST(play_history.item_id AS INTEGER)
                ELSE track_mappings.item_id
            END AS track_id,
            CASE
                WHEN json_extract(artist_data.value, '$.provider') = 'library'
                    THEN CAST(json_extract(artist_data.value, '$.item_id') AS TEXT)
                WHEN artist_mappings.item_id IS NOT NULL
                    THEN CAST(artist_mappings.item_id AS TEXT)
                ELSE json_extract(artist_data.value, '$.item_id')
            END AS item_id,
            CASE
                WHEN json_extract(artist_data.value, '$.provider') = 'library'
                    OR artist_mappings.item_id IS NOT NULL THEN 'library'
                ELSE json_extract(artist_data.value, '$.provider')
            END AS provider,
            json_extract(artist_data.value, '$.name') AS name,
            json_extract(artist_data.value, '$.image') AS image
        FROM {DB_TABLE_PLAY_HISTORY} AS play_history
        CROSS JOIN json_each(play_history.artists) AS artist_data
        LEFT JOIN {DB_TABLE_PROVIDER_MAPPINGS} AS track_mappings
            ON {
    PROVIDER_MAPPING_JOIN.format(
        mapping="track_mappings",
        media_type="play_history.media_type",
        item_id="play_history.item_id",
        provider="play_history.provider",
    )
}
        LEFT JOIN {DB_TABLE_PROVIDER_MAPPINGS} AS artist_mappings
            ON {
    PROVIDER_MAPPING_JOIN.format(
        mapping="artist_mappings",
        media_type=f"'{MediaType.ARTIST.value}'",
        item_id="json_extract(artist_data.value, '$.item_id')",
        provider="json_extract(artist_data.value, '$.provider')",
    )
}
        WHERE play_history.artists IS NOT NULL
            AND play_history.media_type = '{MediaType.TRACK.value}'
            AND play_history.userid = :user_id
            AND play_history.timestamp >= :cutoff_timestamp
    ), track_artists AS (
        SELECT play_id, track_id, item_id, provider, MAX(name) AS name, MAX(image) AS image
        FROM normalized_artist_credits
        GROUP BY play_id, track_id, item_id, provider
    )
"""
