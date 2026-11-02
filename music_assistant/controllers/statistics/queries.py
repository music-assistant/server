"""Shared SQL fragments for completed-play statistics."""

from music_assistant_models.enums import MediaType

from music_assistant.constants import (
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_ALBUMS,
    DB_TABLE_PLAY_HISTORY,
    DB_TABLE_PROVIDER_MAPPINGS,
)

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
    played_pairs AS (
        SELECT provider, item_id, media_type, COUNT(*) AS play_count
        FROM {DB_TABLE_PLAY_HISTORY}
        WHERE userid = :user_id
            AND media_type = :media_type
            AND timestamp >= :cutoff_timestamp
        GROUP BY provider, item_id, media_type
    ), history_tracks AS (
        SELECT provider_mappings.item_id AS track_id,
            SUM(played_pairs.play_count) AS play_count
        FROM played_pairs
        INNER JOIN {DB_TABLE_PROVIDER_MAPPINGS} AS provider_mappings
            ON {
    PROVIDER_MAPPING_JOIN.format(
        mapping="provider_mappings",
        media_type="played_pairs.media_type",
        item_id="played_pairs.item_id",
        provider="played_pairs.provider",
    )
}
        WHERE played_pairs.provider != 'library'
        GROUP BY provider_mappings.item_id
        UNION ALL
        SELECT CAST(item_id AS INTEGER) AS track_id, play_count
        FROM played_pairs
        WHERE provider = 'library'
    )
"""
TRACK_ALBUMS_CTE = f"""
    track_albums AS (
        SELECT
            history_tracks.play_count,
            albums.item_id,
            albums.name,
            json_extract(albums.metadata, '$.images[0]') AS image
        FROM history_tracks
        INNER JOIN {DB_TABLE_ALBUM_TRACKS} AS album_tracks
            ON album_tracks.track_id = history_tracks.track_id
        INNER JOIN {DB_TABLE_ALBUMS} AS albums
            ON albums.item_id = album_tracks.album_id
    )
"""
# Selecting an artist is not a listening event; artist statistics come from the
# credits stored on completed track plays, not from artist/container history rows.
# Aliases with different display metadata still represent one artist credit per play.
TRACK_ARTISTS_CTE = f"""
    played_groups AS (
        SELECT provider, item_id, media_type, artists, COUNT(*) AS play_count
        FROM {DB_TABLE_PLAY_HISTORY}
        WHERE artists IS NOT NULL
            AND media_type = '{MediaType.TRACK.value}'
            AND userid = :user_id
            AND timestamp >= :cutoff_timestamp
        GROUP BY provider, item_id, media_type, artists
    ), normalized_artist_credits AS (
        SELECT
            played_groups.provider AS played_provider,
            played_groups.item_id AS played_item_id,
            played_groups.artists AS played_artists,
            played_groups.play_count,
            CASE
                WHEN played_groups.provider = 'library'
                    THEN CAST(played_groups.item_id AS INTEGER)
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
        FROM played_groups
        CROSS JOIN json_each(played_groups.artists) AS artist_data
        LEFT JOIN {DB_TABLE_PROVIDER_MAPPINGS} AS track_mappings
            ON {
    PROVIDER_MAPPING_JOIN.format(
        mapping="track_mappings",
        media_type="played_groups.media_type",
        item_id="played_groups.item_id",
        provider="played_groups.provider",
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
    ), track_artists AS (
        SELECT
            track_id,
            item_id,
            provider,
            MAX(play_count) AS play_count,
            MAX(name) AS name,
            MAX(image) AS image
        FROM normalized_artist_credits
        GROUP BY played_provider, played_item_id, played_artists, track_id, item_id, provider
    )
"""
