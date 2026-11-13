"""Tests for the Rotation field catalog, matching and validation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from music_assistant_models.enums import AlbumType, ContentType, MediaType
from music_assistant_models.media_items import Album, Artist, AudioFormat, ItemMapping, Track
from music_assistant_models.media_items.provider_mapping import ProviderMapping
from music_assistant_models.unique_list import UniqueList

from music_assistant.providers.rotation_playlist.fields import (
    FIELDS,
    OPERATORS,
    FieldType,
    describe,
    matches,
    sort_items,
    sort_value,
)
from music_assistant.providers.rotation_playlist.models import (
    Condition,
    ExpandStep,
    FilterStep,
    ItemKind,
    SortKey,
    SortStep,
    SourceStep,
    Step,
)
from music_assistant.providers.rotation_playlist.validation import validate_steps


def _artist() -> ItemMapping:
    return ItemMapping(
        item_id="7", provider="library", name="Die drei ???", media_type=MediaType.ARTIST
    )


def _track(name: str = "Böser die Glocken nie klingen (Titelmusik)", **kwargs: Any) -> Track:
    return Track(
        item_id="1",
        provider="library",
        name=name,
        provider_mappings={
            ProviderMapping(
                item_id="1", provider_domain="x", provider_instance="x", in_library=True
            )
        },
        artists=UniqueList([_artist()]),
        **kwargs,
    )


def _album(**kwargs: Any) -> Album:
    return Album(
        item_id="2",
        provider="library",
        name="Discovery",
        provider_mappings=kwargs.pop("provider_mappings", set()),
        **kwargs,
    )


def test_title_contains_casefolded() -> None:
    """Text comparison ignores case."""
    condition = Condition("title", "contains", "titelmusik")
    assert matches(_track(), ItemKind.TRACK, condition)
    assert not matches(_track("Tag 01 - Unbekannter Herkunft (Teil 01)"), ItemKind.TRACK, condition)


def test_ref_artist_matches_uri_or_name() -> None:
    """An artist reference matches by uri or by plain name."""
    track = _track()
    assert matches(track, ItemKind.TRACK, Condition("artist", "is", "library://artist/7"))
    assert matches(track, ItemKind.TRACK, Condition("artist", "is", "die drei ???"))
    assert matches(track, ItemKind.TRACK, Condition("artist", "one_of", ["x", "Die drei ???"]))
    assert matches(track, ItemKind.TRACK, Condition("artist", "is_not", "Justice"))
    assert not matches(track, ItemKind.TRACK, Condition("artist", "is_not", "die drei ???"))


def test_ref_provider_and_album_type() -> None:
    """Providers match by instance or domain, album types by value."""
    assert matches(_track(), ItemKind.TRACK, Condition("provider", "is", "x"))
    album = _album(album_type=AlbumType.LIVE)
    assert matches(album, ItemKind.ALBUM, Condition("album_type", "is", "Live"))


def test_between_and_in_last_days() -> None:
    """Number ranges are inclusive and date windows count back from now."""
    album = _album(year=2001, date_added=datetime.now(UTC) - timedelta(days=3))
    assert matches(album, ItemKind.ALBUM, Condition("year", "between", [2000, 2001]))
    assert not matches(album, ItemKind.ALBUM, Condition("year", "between", [2002, 2005]))
    assert matches(album, ItemKind.ALBUM, Condition("date_added", "in_last_days", 7))
    assert not matches(album, ItemKind.ALBUM, Condition("date_added", "in_last_days", 2))
    assert matches(album, ItemKind.ALBUM, Condition("date_added", "more_than", "2000-01-01"))


def test_bool_fields() -> None:
    """Favorite counts as false when unset; explicit can be missing."""
    track = _track()
    assert matches(track, ItemKind.TRACK, Condition("favorite", "is", False))
    assert not matches(track, ItemKind.TRACK, Condition("explicit", "is", False))


def test_missing_value_never_matches() -> None:
    """Items without a value fail every condition, apart from negative text operators."""
    album = _album()
    assert not matches(album, ItemKind.ALBUM, Condition("year", "more_than", 2000))
    assert not matches(album, ItemKind.ALBUM, Condition("genre", "contains", "rock"))
    assert not matches(album, ItemKind.ALBUM, Condition("genre", "is", "rock"))


def test_genre_negative_operators_keep_items_without_genres() -> None:
    """Like mood or style, an item without genres has none that contains the text."""
    album = _album()
    assert matches(album, ItemKind.ALBUM, Condition("genre", "not_contains", "rock"))
    assert matches(album, ItemKind.ALBUM, Condition("genre", "is_not", "rock"))


def test_sort_value_none_last() -> None:
    """Items without a value come last in both directions."""
    albums = [_album(year=None), _album(year=1999), _album(year=2005)]
    assert sort_value(albums[0], ItemKind.ALBUM, "year") > sort_value(
        albums[2], ItemKind.ALBUM, "year"
    )
    for descending, expected in ((False, [1999, 2005, None]), (True, [2005, 1999, None])):
        ordered = sort_items(albums, ItemKind.ALBUM, [SortKey("year", descending)])
        assert [album.year for album in ordered] == expected


def test_track_number_sorts_by_disc_first() -> None:
    """A track number sort keeps the discs apart, like the album page; a filter sees the number."""
    tracks = [
        _track(f"{disc}-{number}", disc_number=disc, track_number=number)
        for disc, number in ((2, 1), (1, 2), (2, 2), (1, 1))
    ]
    tracks.append(_track("no number", disc_number=1, track_number=0))
    tracks.append(_track("single disc", disc_number=0, track_number=3))
    for descending, expected in (
        (False, ["1-1", "1-2", "single disc", "2-1", "2-2", "no number"]),
        (True, ["2-2", "2-1", "single disc", "1-2", "1-1", "no number"]),
    ):
        ordered = sort_items(tracks, ItemKind.TRACK, [SortKey("track_number", descending)])
        assert [track.name for track in ordered] == expected
    condition = Condition("track_number", "is", 2)
    assert [t.name for t in tracks if matches(t, ItemKind.TRACK, condition)] == ["1-2", "2-2"]


def test_validate_track_field_on_albums() -> None:
    """A track field on albums names the field, the kind and the fix."""
    errors = validate_steps(
        [
            SourceStep(ItemKind.ALBUM),
            SortStep([SortKey("track_number")]),
        ]
    )
    assert len(errors) == 1
    assert errors[0].step == 2
    message = errors[0].message
    assert "track_number" in message
    assert "albums" in message
    assert "add a 'Play each album's tracks' step" in message


def test_validate_fields_follow_the_expanded_kind() -> None:
    """After an expand the steps are checked against the new kind."""
    steps: list[Step] = [
        SourceStep(ItemKind.ALBUM),
        ExpandStep(ItemKind.TRACK),
        SortStep([SortKey("track_number")]),
        FilterStep("keep", "all", [Condition("year", "more_than", 2000)]),
    ]
    assert validate_steps(steps) == []


def test_validate_operator_type_mismatch() -> None:
    """An operator that does not fit lists the allowed ones."""
    errors = validate_steps(
        [
            SourceStep(ItemKind.ALBUM),
            FilterStep("keep", "all", [Condition("year", "contains", "20")]),
        ]
    )
    assert len(errors) == 1
    for operator in OPERATORS[FieldType.NUMBER]:
        assert operator in errors[0].message


def test_validate_value_shapes() -> None:
    """Values must have the shape the operator needs."""
    conditions = [
        Condition("year", "between", [1]),
        Condition("artist", "one_of", "die drei ???"),
        Condition("date_added", "in_last_days", 0),
        Condition("title", "is", None),
        Condition("nope", "is", 1),
        Condition("random", "is", 1),
    ]
    errors = validate_steps([SourceStep(ItemKind.ALBUM), FilterStep("keep", "all", conditions)])
    assert len(errors) == len(conditions)


def test_validate_sort_field_not_sortable() -> None:
    """Fields without an order cannot be sort keys."""
    errors = validate_steps([SourceStep(ItemKind.ALBUM), SortStep([SortKey("genre")])])
    assert len(errors) == 1
    assert "genre" in errors[0].message


def test_describe_album_fields() -> None:
    """The description lists fields and sort keys per kind."""
    result = describe(ItemKind.ALBUM, []).to_dict()
    album = result["media_types"]["album"]
    assert "album_type" in [item["name"] for item in album["fields"]]
    assert "random" in album["sort_keys"]
    assert list(result["media_types"]) == ["album"]
    assert set(describe(None, []).to_dict()["media_types"]) == {"artist", "album", "track"}


def test_question_marks_match_literally() -> None:
    """Characters such as question marks are plain text, not patterns."""
    track = _track("Die drei ??? und der Superpapagei")
    assert matches(track, ItemKind.TRACK, Condition("title", "contains", "die drei ???"))
    assert matches(track, ItemKind.TRACK, Condition("title", "contains", "die drei"))
    assert not matches(track, ItemKind.TRACK, Condition("title", "contains", "die drei ?!?"))


def test_casefold_with_umlaut() -> None:
    """Casefolding also covers umlauts."""
    track = _track("Böser die Glocken nie klingen (Titelmusik)")
    assert matches(track, ItemKind.TRACK, Condition("title", "contains", "BÖSER"))


def test_remove_filter_keeps_the_episode_parts_in_order() -> None:
    """Removing summary and theme tracks leaves the two episode parts."""
    titles = [
        "Böser die Glocken nie klingen (Inhaltsangabe)",
        "Böser die Glocken nie klingen (Titelmusik)",
        "Tag 01 - Unbekannter Herkunft (Teil 01)",
        "Tag 01 - Unbekannter Herkunft (Teil 02)",
    ]
    unwanted = [
        Condition("title", "contains", "Inhaltsangabe"),
        Condition("title", "contains", "Titelmusik"),
    ]
    kept = [
        title
        for title in titles
        if not any(matches(_track(title), ItemKind.TRACK, cond) for cond in unwanted)
    ]
    assert kept == titles[2:]


def test_play_count_filters_and_orders() -> None:
    """Play count and last played filter and order albums and artists too."""
    for kind in (ItemKind.ARTIST, ItemKind.ALBUM, ItemKind.TRACK):
        assert not validate_steps(
            [
                SourceStep(kind),
                FilterStep("keep", "all", [Condition("play_count", "more_than", 3)]),
                FilterStep("keep", "all", [Condition("last_played", "in_last_days", 30)]),
                SortStep([SortKey("play_count")]),
            ]
        )
    fields = {
        item["name"]: item
        for item in describe(ItemKind.ALBUM, []).to_dict()["media_types"]["album"]["fields"]
    }
    assert fields["play_count"]["operators"] == list(OPERATORS[FieldType.NUMBER])
    assert fields["play_count"]["label"] == "Play count (all users)"
    assert fields["last_played"]["operators"] == list(OPERATORS[FieldType.DATE])
    assert (
        "play_count" in describe(ItemKind.TRACK, []).to_dict()["media_types"]["track"]["sort_keys"]
    )


def test_unknown_filter_field_lists_only_filterable_fields() -> None:
    """An unknown field in a filter suggests only fields a filter can use."""
    errors = validate_steps(
        [
            SourceStep(ItemKind.ALBUM),
            FilterStep("keep", "all", [Condition("composer", "is", "Europa")]),
        ]
    )
    assert len(errors) == 1
    choices = errors[0].message.split("choose one of: ")[1].split(", ")
    assert "name" in choices
    assert "random" not in choices
    sort_errors = validate_steps([SourceStep(ItemKind.ALBUM), SortStep([SortKey("composer")])])
    assert "random" in sort_errors[0].message.split("choose one of: ")[1].split(", ")


def _track_by(*names: str, **kwargs: Any) -> Track:
    track = _track(**kwargs)
    track.artists = UniqueList(
        ItemMapping(item_id=str(index), provider="library", name=name, media_type=MediaType.ARTIST)
        for index, name in enumerate(names)
    )
    return track


def test_artist_contains_matches_the_name_only() -> None:
    """Text operators on a reference compare the name, ignoring case and literal characters."""
    fuchs = _track_by("Die Fuchsbande")
    assert matches(fuchs, ItemKind.TRACK, Condition("artist", "contains", "fuchsbande"))
    assert not matches(fuchs, ItemKind.TRACK, Condition("artist", "contains", "library://"))
    assert matches(_track(), ItemKind.TRACK, Condition("artist", "contains", "drei ???"))
    fragen = _track_by("Die drei Fragezeichen")
    assert not matches(fragen, ItemKind.TRACK, Condition("artist", "contains", "drei ???"))


def test_artist_starts_with_and_ends_with() -> None:
    """Starts with and ends with look at the referenced name."""
    track = _track_by("Die Fuchsbande")
    assert matches(track, ItemKind.TRACK, Condition("artist", "starts_with", "die fuchs"))
    assert not matches(track, ItemKind.TRACK, Condition("artist", "starts_with", "fuchs"))
    assert matches(track, ItemKind.TRACK, Condition("artist", "ends_with", "BANDE"))
    assert not matches(track, ItemKind.TRACK, Condition("artist", "ends_with", "die"))


def test_artist_not_contains_needs_no_name_to_contain_it() -> None:
    """Does not contain matches only when none of several artists contains the text."""
    track = _track_by("Die Fuchsbande", "Justice")
    assert matches(track, ItemKind.TRACK, Condition("artist", "contains", "justice"))
    assert not matches(track, ItemKind.TRACK, Condition("artist", "not_contains", "justice"))
    assert matches(track, ItemKind.TRACK, Condition("artist", "not_contains", "drei"))


def test_genre_and_album_text_operators() -> None:
    """Genres and albums also match by text."""
    track = _track(album=_album())
    track.metadata.genres = {"Hörspiel", "Kinder"}
    assert matches(track, ItemKind.TRACK, Condition("genre", "contains", "hörsp"))
    assert matches(track, ItemKind.TRACK, Condition("genre", "not_contains", "rock"))
    assert not matches(track, ItemKind.TRACK, Condition("genre", "not_contains", "kind"))
    assert matches(track, ItemKind.TRACK, Condition("album", "starts_with", "disc"))


def test_validate_text_operators_on_references() -> None:
    """Text operators need a non-empty text; is keeps taking a uri."""

    def errors_for(op: str, value: Any) -> list[Any]:
        return validate_steps(
            [
                SourceStep(ItemKind.TRACK),
                FilterStep("keep", "all", [Condition("artist", op, value)]),
            ]
        )

    assert errors_for("contains", "")
    assert errors_for("contains", None)
    assert not errors_for("contains", "fuchs")
    assert not errors_for("is", "library://artist/7")
    assert validate_steps(
        [
            SourceStep(ItemKind.TRACK),
            FilterStep("keep", "all", [Condition("provider", "contains", "x")]),
        ]
    )


def test_describe_lists_text_operators_for_names_only() -> None:
    """Artist, album and genre offer text operators; album type and provider do not."""
    fields = {
        item["name"]: item["operators"]
        for item in describe(ItemKind.TRACK, []).to_dict()["media_types"]["track"]["fields"]
    }
    for name in ("artist", "album", "genre"):
        assert "contains" in fields[name]
        assert "one_of" in fields[name]
    assert "contains" not in fields["provider"]
    album_fields = {
        item["name"]: item["operators"]
        for item in describe(ItemKind.ALBUM, []).to_dict()["media_types"]["album"]["fields"]
    }
    assert "contains" not in album_fields["album_type"]


def test_version_contains_matches_a_remix() -> None:
    """The version of a track can be matched by text, apart from its title."""
    condition = Condition("version", "contains", "mix")
    assert matches(_track("Burnin'", version="Slam Mix"), ItemKind.TRACK, condition)
    assert not matches(_track("Burnin'"), ItemKind.TRACK, condition)
    assert not matches(_track("Mix Tape"), ItemKind.TRACK, Condition("version", "contains", "mix"))


def test_version_is_a_text_field_for_albums_and_tracks() -> None:
    """Albums and tracks list a filterable version with the text operators."""
    for kind in (ItemKind.ALBUM, ItemKind.TRACK):
        definition = FIELDS[kind]["version"]
        assert definition.type == FieldType.TEXT
        assert definition.label == "Version"
    assert "version" not in FIELDS[ItemKind.ARTIST]


def _artist_item(**metadata: Any) -> Artist:
    artist = Artist(item_id="3", provider="library", name="Justice", provider_mappings=set())
    for key, value in metadata.items():
        setattr(artist.metadata, key, value)
    return artist


def test_metadata_text_fields_on_every_kind() -> None:
    """Mood, style, label and language are text fields of artists, albums and tracks."""
    labels = {"mood": "Mood", "style": "Style", "label": "Label", "language": "Language"}
    for kind in ItemKind:
        for name, label in labels.items():
            definition = FIELDS[kind][name]
            assert definition.type == FieldType.TEXT
            assert definition.label == label
            assert not definition.sortable
    album = _album()
    album.metadata.mood = "Dreamy"
    album.metadata.style = "French House"
    album.metadata.label = "Ed Banger Records"
    assert matches(album, ItemKind.ALBUM, Condition("mood", "is", "dreamy"))
    assert matches(album, ItemKind.ALBUM, Condition("style", "contains", "house"))
    assert matches(album, ItemKind.ALBUM, Condition("label", "starts_with", "ed banger"))
    assert matches(album, ItemKind.ALBUM, Condition("label", "ends_with", "RECORDS"))
    assert not matches(album, ItemKind.ALBUM, Condition("mood", "is_not", "Dreamy"))
    artist = _artist_item(style="Electro")
    assert matches(artist, ItemKind.ARTIST, Condition("style", "is", "electro"))
    track = _track()
    track.metadata.mood = "Spannend"
    assert matches(track, ItemKind.TRACK, Condition("mood", "not_contains", "ruhig"))


def test_language_matches_any_of_several_values() -> None:
    """A positive operator needs one language to match, a negative one needs none to match."""
    track = _track()
    track.metadata.languages = UniqueList(["deu", "eng"])
    assert matches(track, ItemKind.TRACK, Condition("language", "is", "ENG"))
    assert matches(track, ItemKind.TRACK, Condition("language", "starts_with", "de"))
    assert not matches(track, ItemKind.TRACK, Condition("language", "is_not", "eng"))
    assert not matches(track, ItemKind.TRACK, Condition("language", "not_contains", "en"))
    assert matches(track, ItemKind.TRACK, Condition("language", "is_not", "fra"))
    assert matches(track, ItemKind.TRACK, Condition("language", "not_contains", "fr"))


def test_metadata_text_without_a_value() -> None:
    """An item without the value matches only the negative operators."""
    track = _track()
    for name in ("mood", "style", "label", "language"):
        for op in ("is", "contains", "starts_with", "ends_with"):
            assert not matches(track, ItemKind.TRACK, Condition(name, op, "x")), (name, op)
        for op in ("is_not", "not_contains"):
            assert matches(track, ItemKind.TRACK, Condition(name, op, "x")), (name, op)


def test_popularity_filters_and_orders() -> None:
    """Popularity is a number that filters and orders; 0 is a value, not a missing one."""
    for kind in ItemKind:
        definition = FIELDS[kind]["popularity"]
        assert definition.type == FieldType.NUMBER
        assert definition.label == "Popularity"
        assert definition.sortable
    albums = [_album(), _album(), _album()]
    albums[0].metadata.popularity = 40
    albums[1].metadata.popularity = 0
    assert matches(albums[0], ItemKind.ALBUM, Condition("popularity", "more_than", 30))
    assert matches(albums[1], ItemKind.ALBUM, Condition("popularity", "less_than", 10))
    assert not matches(albums[2], ItemKind.ALBUM, Condition("popularity", "less_than", 10))
    ordered = sort_items(albums, ItemKind.ALBUM, [SortKey("popularity", descending=True)])
    assert [album.metadata.popularity for album in ordered] == [40, 0, None]


def _mapping(
    content_type: ContentType, bit_depth: int = 16, sample_rate: int = 44100, instance: str = "x"
) -> ProviderMapping:
    return ProviderMapping(
        item_id=f"{instance}{content_type.value}{bit_depth}{sample_rate}",
        provider_domain=instance,
        provider_instance=instance,
        audio_format=AudioFormat(
            content_type=content_type, bit_depth=bit_depth, sample_rate=sample_rate
        ),
    )


def test_audio_quality_counts_the_best_mapping() -> None:
    """The audio quality is lossy, lossless or hi-res, taken from the best mapping."""

    def quality_is(item: Album | Track, kind: ItemKind, value: str) -> bool:
        return matches(item, kind, Condition("audio_quality", "is", value))

    assert quality_is(
        _album(provider_mappings={_mapping(ContentType.MP3)}), ItemKind.ALBUM, "lossy"
    )
    assert quality_is(
        _album(provider_mappings={_mapping(ContentType.FLAC)}), ItemKind.ALBUM, "lossless"
    )
    hi_res_depth = _album(provider_mappings={_mapping(ContentType.FLAC, bit_depth=24)})
    assert quality_is(hi_res_depth, ItemKind.ALBUM, "hi_res")
    hi_res_rate = _album(provider_mappings={_mapping(ContentType.FLAC, sample_rate=96000)})
    assert quality_is(hi_res_rate, ItemKind.ALBUM, "hi_res")
    lossy_high_rate = _album(provider_mappings={_mapping(ContentType.AAC, 24, 96000)})
    assert quality_is(lossy_high_rate, ItemKind.ALBUM, "lossy")
    both = _track(album=None)
    both.provider_mappings = {
        _mapping(ContentType.OGG, instance="spotify"),
        _mapping(ContentType.FLAC, instance="filesystem"),
    }
    assert quality_is(both, ItemKind.TRACK, "lossless")
    assert not quality_is(both, ItemKind.TRACK, "lossy")
    assert matches(both, ItemKind.TRACK, Condition("audio_quality", "is_not", "lossy"))
    assert matches(
        both, ItemKind.TRACK, Condition("audio_quality", "one_of", ["hi_res", "lossless"])
    )


def test_audio_quality_unknown_format_has_no_value() -> None:
    """A mapping without a known format tells nothing about the quality."""
    album = _album(provider_mappings={_mapping(ContentType.UNKNOWN)})
    for value in ("lossy", "lossless", "hi_res"):
        assert not matches(album, ItemKind.ALBUM, Condition("audio_quality", "is", value))


def test_audio_quality_field_and_values() -> None:
    """Audio quality is a reference with fixed values that describe lists."""
    assert "audio_quality" not in FIELDS[ItemKind.ARTIST]
    for kind in (ItemKind.ALBUM, ItemKind.TRACK):
        definition = FIELDS[kind]["audio_quality"]
        assert definition.type == FieldType.REF
        assert definition.label == "Audio quality"
        assert not definition.sortable
    fields = {
        item["name"]: item
        for item in describe(ItemKind.ALBUM, []).to_dict()["media_types"]["album"]["fields"]
    }
    assert fields["audio_quality"]["operators"] == ["is", "is_not", "one_of"]
    assert fields["audio_quality"]["values"] == ["lossy", "lossless", "hi_res"]
    assert "values" not in fields["year"]


def test_validate_audio_quality_values() -> None:
    """Values other than the fixed ones are refused with the allowed ones."""

    def errors_for(op: str, value: Any) -> list[Any]:
        return validate_steps(
            [
                SourceStep(ItemKind.ALBUM),
                FilterStep("keep", "all", [Condition("audio_quality", op, value)]),
            ]
        )

    assert not errors_for("is", "hi_res")
    assert not errors_for("one_of", ["lossy", "lossless"])
    errors = errors_for("is", "hd")
    assert len(errors) == 1
    assert errors[0].field == "conditions[0].value"
    assert "use one of: lossy, lossless, hi_res" in errors[0].message
    assert errors_for("is_not", "flac")
    assert errors_for("one_of", ["lossless", "cd"])
    assert errors_for("contains", "loss")


def test_loaded_values_filter_and_order() -> None:
    """Values the engine loads for an item filter and order like the others."""
    album = _album()
    recent = datetime.now(UTC) - timedelta(days=2)
    loaded = {"play_count": 4, "last_played": recent, "track_count": 12, "date_favorited": recent}
    assert matches(album, ItemKind.ALBUM, Condition("play_count", "more_than", 3), loaded)
    assert matches(album, ItemKind.ALBUM, Condition("last_played", "in_last_days", 7), loaded)
    assert matches(album, ItemKind.ALBUM, Condition("track_count", "between", [10, 20]), loaded)
    assert matches(album, ItemKind.ALBUM, Condition("date_favorited", "in_last_days", 7), loaded)
    assert not matches(album, ItemKind.ALBUM, Condition("play_count", "less_than", 3), loaded)
    never = {"play_count": 0, "last_played": None, "track_count": 0, "date_favorited": None}
    assert matches(album, ItemKind.ALBUM, Condition("play_count", "is", 0), never)
    assert not matches(
        album, ItemKind.ALBUM, Condition("last_played", "less_than", "2100-01-01"), never
    )
    assert not matches(
        album, ItemKind.ALBUM, Condition("date_favorited", "more_than", "2000-01-01"), never
    )
    # an item that is not in the library was never played here
    assert matches(album, ItemKind.ALBUM, Condition("play_count", "is", 0))
    assert not matches(album, ItemKind.ALBUM, Condition("track_count", "is", 0))
    others = [_album(), _album(), album]
    values = {id(others[0]): {"track_count": 3}, id(album): {"track_count": 12}}
    ordered = sort_items(
        others,
        ItemKind.ALBUM,
        [SortKey("track_count", descending=True)],
        lambda item: values.get(id(item), {}),
    )
    assert [id(item) for item in ordered] == [id(album), id(others[0]), id(others[1])]


def test_track_count_and_date_favorited_fields() -> None:
    """Albums have a track count; every kind has the date it was favorited."""
    definition = FIELDS[ItemKind.ALBUM]["track_count"]
    assert definition.type == FieldType.NUMBER
    assert definition.label == "Number of tracks"
    assert definition.sortable
    assert "track_count" not in FIELDS[ItemKind.TRACK]
    for kind in ItemKind:
        favorited = FIELDS[kind]["date_favorited"]
        assert favorited.type == FieldType.DATE
        assert favorited.label == "Date favorited"
        assert FIELDS[kind]["play_count"].type == FieldType.NUMBER
        assert not validate_steps(
            [
                SourceStep(kind),
                FilterStep("keep", "all", [Condition("date_favorited", "in_last_days", 30)]),
            ]
        )


def test_unavailable_mappings_tell_nothing_about_quality() -> None:
    """A mapping that is not available does not count for the audio quality."""
    track = _track(album=None)
    gone = _mapping(ContentType.FLAC, bit_depth=24, instance="gone")
    gone.available = False
    track.provider_mappings = {gone, _mapping(ContentType.MP3)}
    assert matches(track, ItemKind.TRACK, Condition("audio_quality", "is", "lossy"))
    album = _album(provider_mappings={gone})
    assert not matches(album, ItemKind.ALBUM, Condition("audio_quality", "is", "hi_res"))


def test_album_quality_comes_from_the_loaded_value() -> None:
    """A library album counts with the quality loaded from its tracks, others by their own."""
    album = _album(provider_mappings={_mapping(ContentType.MP3)})
    hi_res = Condition("audio_quality", "is", "hi_res")
    assert matches(album, ItemKind.ALBUM, hi_res, {"audio_quality": [("hi_res",)]})
    assert not matches(album, ItemKind.ALBUM, hi_res, {"audio_quality": []})
    assert matches(album, ItemKind.ALBUM, Condition("audio_quality", "is", "lossy"))


def test_never_played_sorts_as_the_oldest_moment() -> None:
    """Items that were never played come first by last played, and last when descending."""
    tracks = [_track("never"), _track("old", last_played=1000), _track("new", last_played=9000)]
    for descending, expected in ((False, ["never", "old", "new"]), (True, ["new", "old", "never"])):
        ordered = sort_items(tracks, ItemKind.TRACK, [SortKey("last_played", descending)])
        assert [track.name for track in ordered] == expected
    albums = [_album(), _album(), _album()]
    recent = datetime.now(UTC)
    values: dict[int, dict[str, Any]] = {
        id(albums[1]): {"last_played": None},
        id(albums[2]): {"last_played": recent},
    }
    by_moment = sort_items(
        albums, ItemKind.ALBUM, [SortKey("last_played")], lambda item: values.get(id(item), {})
    )
    assert id(by_moment[-1]) == id(albums[2])
    never = _track("never")
    assert not matches(never, ItemKind.TRACK, Condition("last_played", "less_than", "2100-01-01"))


def test_ref_matches_the_uri_of_another_mapping() -> None:
    """A reference matches the uri of every provider mapping of the referenced item."""
    artist = Artist(
        item_id="7",
        provider="library",
        name="Die drei ???",
        provider_mappings={
            ProviderMapping(
                item_id="abc", provider_domain="spotify", provider_instance="spotify--1"
            )
        },
    )
    track = _track()
    track.artists = UniqueList([artist])
    condition = Condition("artist", "is", "spotify--1://artist/abc")
    assert matches(track, ItemKind.TRACK, condition)
    assert matches(track, ItemKind.TRACK, Condition("artist", "is", "library://artist/7"))
    assert not matches(track, ItemKind.TRACK, Condition("artist", "is", "artist://spotify--1/abc"))


def test_dates_compare_by_their_utc_day() -> None:
    """A date-only value covers its whole UTC day, for every operator."""
    late = _album(date_added=datetime(2024, 1, 31, 23, 30, tzinfo=UTC))
    early = _album(date_added=datetime(2024, 1, 1, 0, 0, tzinfo=UTC))
    for album in (late, early):
        assert matches(
            album, ItemKind.ALBUM, Condition("date_added", "between", ["2024-01-01", "2024-01-31"])
        )
    assert not matches(late, ItemKind.ALBUM, Condition("date_added", "more_than", "2024-01-31"))
    assert matches(late, ItemKind.ALBUM, Condition("date_added", "more_than", "2024-01-30"))
    assert not matches(late, ItemKind.ALBUM, Condition("date_added", "less_than", "2024-01-31"))
    assert matches(late, ItemKind.ALBUM, Condition("date_added", "less_than", "2024-02-01"))
    assert not matches(early, ItemKind.ALBUM, Condition("date_added", "less_than", "2024-01-01"))
    assert matches(late, ItemKind.ALBUM, Condition("date_added", "is", "2024-01-31"))
    assert not matches(
        early, ItemKind.ALBUM, Condition("date_added", "between", ["2024-01-02", "2024-01-31"])
    )


def test_dates_count_in_utc() -> None:
    """A moment in another time zone and a naive moment are compared on their UTC day."""
    berlin = timezone(timedelta(hours=1))
    album = _album(date_added=datetime(2024, 2, 1, 0, 30, tzinfo=berlin))
    assert matches(album, ItemKind.ALBUM, Condition("date_added", "is", "2024-01-31"))
    assert not matches(album, ItemKind.ALBUM, Condition("date_added", "more_than", "2024-01-31"))
    naive = _album(date_added=datetime(2024, 1, 31, 23, 30, tzinfo=UTC).replace(tzinfo=None))
    assert matches(naive, ItemKind.ALBUM, Condition("date_added", "is", "2024-01-31"))


def test_date_values_with_a_time_compare_the_moment() -> None:
    """A value with a time is one moment; only 'is' compares its UTC day."""
    album = _album(date_added=datetime(2024, 5, 31, 12, 0, tzinfo=UTC))
    assert matches(album, ItemKind.ALBUM, Condition("date_added", "more_than", "2024-05-31T11:00"))
    assert not matches(
        album, ItemKind.ALBUM, Condition("date_added", "more_than", "2024-05-31T12:00:00+00:00")
    )
    assert matches(album, ItemKind.ALBUM, Condition("date_added", "less_than", "2024-05-31T13:00"))
    assert matches(
        album,
        ItemKind.ALBUM,
        Condition("date_added", "between", ["2024-05-31T12:00", "2024-05-31"]),
    )
    assert not matches(
        album,
        ItemKind.ALBUM,
        Condition("date_added", "between", ["2024-05-30", "2024-05-31T11:59"]),
    )
    assert matches(album, ItemKind.ALBUM, Condition("date_added", "is", "2024-05-31T23:00"))


def test_provider_of_a_library_item_needs_it_in_that_library() -> None:
    """A library item is on a provider only when the provider has it in its library."""
    track = _track()
    track.provider_mappings.add(
        ProviderMapping(
            item_id="9", provider_domain="spotify", provider_instance="spotify--1", in_library=False
        )
    )
    assert matches(track, ItemKind.TRACK, Condition("provider", "is", "x"))
    assert not matches(track, ItemKind.TRACK, Condition("provider", "is", "spotify"))
    assert not matches(track, ItemKind.TRACK, Condition("provider", "is", "spotify--1"))
    assert matches(track, ItemKind.TRACK, Condition("provider", "is_not", "spotify"))


def test_provider_of_a_provider_item_is_its_own() -> None:
    """An item straight from a provider is on that provider and on its mapped providers."""
    track = Track(
        item_id="9",
        provider="spotify--1",
        name="Titelmusik",
        provider_mappings={
            ProviderMapping(item_id="9", provider_domain="spotify", provider_instance="spotify--1")
        },
    )
    assert matches(track, ItemKind.TRACK, Condition("provider", "is", "spotify"))
    assert matches(track, ItemKind.TRACK, Condition("provider", "is", "spotify--1"))
    assert not matches(track, ItemKind.TRACK, Condition("provider", "is", "x"))


def test_validate_artist_tracks_cannot_come_from_the_providers() -> None:
    """Artist tracks with the providers scope are refused with the way to reach the providers."""
    errors = validate_steps(
        [SourceStep(ItemKind.ARTIST), ExpandStep(ItemKind.TRACK, scope="providers")]
    )
    assert [(error.step, error.field, error.translation_key) for error in errors] == [
        (2, "scope", "expand_artist_tracks_from_providers")
    ]
    assert "expand the artists to albums with the providers scope" in errors[0].message
    assert not validate_steps(
        [
            SourceStep(ItemKind.ARTIST),
            ExpandStep(ItemKind.ALBUM, scope="providers"),
            ExpandStep(ItemKind.TRACK),
        ]
    )
    assert not validate_steps([SourceStep(ItemKind.ARTIST), ExpandStep(ItemKind.TRACK)])
