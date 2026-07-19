"""Test the Tidal JSON:API document helper."""

from music_assistant.providers.tidal.jsonapi import JsonApiDocument


def test_data_and_data_list() -> None:
    """Test primary data accessors for single and collection documents."""
    single = JsonApiDocument({"data": {"id": "1", "type": "tracks"}})
    assert single.data == {"id": "1", "type": "tracks"}
    assert single.data_list == []

    collection = JsonApiDocument({"data": [{"id": "1"}, {"id": "2"}]})
    assert collection.data == {}
    assert len(collection.data_list) == 2


def test_related_resolves_included() -> None:
    """Test relationship linkages resolve against the included resources."""
    doc = JsonApiDocument(
        {
            "data": {
                "id": "t1",
                "type": "tracks",
                "relationships": {
                    "artists": {"data": [{"id": "a1", "type": "artists"}]},
                    "albums": {"data": {"id": "al1", "type": "albums"}},
                },
            },
            "included": [
                {"id": "a1", "type": "artists", "attributes": {"name": "Artist One"}},
                {"id": "al1", "type": "albums", "attributes": {"title": "Album One"}},
            ],
        }
    )

    artists = doc.related(doc.data, "artists")
    assert [a["attributes"]["name"] for a in artists] == ["Artist One"]

    album = doc.related_one(doc.data, "albums")
    assert album is not None
    assert album["attributes"]["title"] == "Album One"


def test_related_missing_and_unresolvable() -> None:
    """Test relationships that are absent or not included resolve to empty."""
    doc = JsonApiDocument(
        {
            "data": {
                "id": "t1",
                "type": "tracks",
                "relationships": {
                    "artists": {"data": [{"id": "missing", "type": "artists"}]},
                },
            },
            "included": [],
        }
    )
    assert doc.related(doc.data, "artists") == []
    assert doc.related(doc.data, "albums") == []
    assert doc.related_one(doc.data, "albums") is None


def test_next_cursor() -> None:
    """Test the next cursor is extracted from the links object."""
    assert JsonApiDocument({"links": {}}).next_cursor is None
    doc = JsonApiDocument({"links": {"next": "/tracks?countryCode=AT&page[cursor]=abc123&other=1"}})
    assert doc.next_cursor == "abc123"
    bare = JsonApiDocument({"links": {"next": "rawcursor"}})
    assert bare.next_cursor == "rawcursor"


def test_next_cursor_percent_encoded_brackets() -> None:
    """Test the cursor is extracted when the bracket key is percent-encoded."""
    # This is the real form Tidal returns: page%5Bcursor%5D=<value>.
    doc = JsonApiDocument(
        {"links": {"next": "/x?include=items.profileArt&page%5Bcursor%5D=eyJpZCI6Mzk3fQ"}}
    )
    assert doc.next_cursor == "eyJpZCI6Mzk3fQ"
