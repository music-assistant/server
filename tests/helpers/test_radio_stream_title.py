"""Tests for cleaning radio streamtitle."""

from music_assistant.helpers.util import clean_stream_title, parse_quoted_stream_title


def test_cleaning_streamtitle() -> None:
    """Tests for cleaning radio streamtitle."""
    tstm = "Thirty Seconds To Mars - Closer to the Edge"
    advert = "Advert"

    line = "Advertisement_Start_Length=00:00:29.960"
    stream_title = clean_stream_title(line)
    assert stream_title == advert

    line = "Advertisement_Stop"
    stream_title = clean_stream_title(line)
    assert stream_title == advert

    line = "START_AD_BREAK_6000"
    stream_title = clean_stream_title(line)
    assert stream_title == advert

    line = "STOP ADBREAK 1"
    stream_title = clean_stream_title(line)
    assert stream_title == advert

    line = "AD 2"
    stream_title = clean_stream_title(line)
    assert stream_title == advert

    line = 'title="Thirty Seconds To Mars - Closer to the Edge",artist="Thirty Seconds To Mars - Closer to the Edge",url="https://nowplaying.scahw.com.au/c/fd8ee07bed6a5e4e9824a11aa02dd34a.jpg?t=1714568458&l=250"'
    stream_title = clean_stream_title(line)
    assert stream_title == tstm

    line = 'title="https://listenapi.planetradio.co.uk/api9.2/eventdata/247801912",url="https://listenapi.planetradio.co.uk/api9.2/eventdata/247801912"'
    stream_title = clean_stream_title(line)
    assert stream_title == ""

    line = 'title="Thirty Seconds To Mars - Closer to the Edge https://nowplaying.scahw.com.au/",artist="Thirty Seconds To Mars - Closer to the Edge",url="https://nowplaying.scahw.com.au/c/fd8ee07bed6a5e4e9824a11aa02dd34a.jpg?t=1714568458&l=250"'
    stream_title = clean_stream_title(line)
    assert stream_title == tstm

    line = 'title="Closer to the Edge",artist="Thirty Seconds To Mars",url="https://nowplaying.scahw.com.au/c/fd8ee07bed6a5e4e9824a11aa02dd34a.jpg?t=1714568458&l=250"'
    stream_title = clean_stream_title(line)
    assert stream_title == tstm

    line = 'title="Thirty Seconds To Mars - Closer to the Edge"'
    stream_title = clean_stream_title(line)
    assert stream_title == tstm

    line = "Thirty Seconds To Mars - Closer to the Edge https://nowplaying.scahw.com.au/"
    stream_title = clean_stream_title(line)
    assert stream_title == tstm

    line = "Lonely Street  By: Andy Williams - WALMRadio.com"
    stream_title = clean_stream_title(line)
    assert stream_title == "Andy Williams - Lonely Street"

    line = "Bye Bye Blackbird  By: Sammy Davis Jr. - WALMRadio.com"
    stream_title = clean_stream_title(line)
    assert stream_title == "Sammy Davis Jr. - Bye Bye Blackbird"

    line = (
        "Asha Bhosle, Mohd Rafi (mp3yaar.com) - Gunguna Rahe Hain Bhanwre - Araadhna (mp3yaar.com)"
    )
    stream_title = clean_stream_title(line)
    assert stream_title == "Asha Bhosle, Mohd Rafi - Gunguna Rahe Hain Bhanwre - Araadhna"

    line = "Mohammed Rafi(Jatt.fm) - Rang Aur Noor Ki Baraat (Ghazal)(Jatt.fm)"
    stream_title = clean_stream_title(line)
    assert stream_title == "Mohammed Rafi - Rang Aur Noor Ki Baraat (Ghazal)"


def test_cleaning_streamtitle_german_format() -> None:
    """Test normalisation of the German "Track" von Artist format."""
    line = '"Cornflake Girl" von Tori Amos'
    stream_title = clean_stream_title(line)
    assert stream_title == "Tori Amos - Cornflake Girl"

    line = '"Closer to the Edge" von "Thirty Seconds To Mars"'
    stream_title = clean_stream_title(line)
    assert stream_title == "Thirty Seconds To Mars - Closer to the Edge"


def test_cleaning_streamtitle_english_format() -> None:
    """Test normalisation of the English "Track" by Artist from "Album" format."""
    line = '"Nickel Wound" by Texas Is The Reason from "Do You Know Who You Are?"'
    assert clean_stream_title(line) == "Texas Is The Reason - Nickel Wound"

    # album is optional
    line = '"Cornflake Girl" by Tori Amos'
    assert clean_stream_title(line) == "Tori Amos - Cornflake Girl"


def test_parse_quoted_stream_title() -> None:
    """Test structured parsing of quoted stream titles, including album extraction."""
    # English format with album
    assert parse_quoted_stream_title(
        '"Nickel Wound" by Texas Is The Reason from "Do You Know Who You Are?: '
        'The Complete Collection"'
    ) == (
        "Nickel Wound",
        "Texas Is The Reason",
        "Do You Know Who You Are?: The Complete Collection",
    )

    # English format without album
    assert parse_quoted_stream_title('"Cornflake Girl" by Tori Amos') == (
        "Cornflake Girl",
        "Tori Amos",
        None,
    )

    # "by" and "from" appearing inside the artist name must not be mis-split
    assert parse_quoted_stream_title('"Romeo" by Death From Above 1979 from "Heads Up"') == (
        "Romeo",
        "Death From Above 1979",
        "Heads Up",
    )
    assert parse_quoted_stream_title('"Romeo" by Death From Above 1979') == (
        "Romeo",
        "Death From Above 1979",
        None,
    )

    # German format yields no album
    assert parse_quoted_stream_title('"Cornflake Girl" von Tori Amos') == (
        "Cornflake Girl",
        "Tori Amos",
        None,
    )

    # conventional "Artist - Track" is not a quoted format
    assert parse_quoted_stream_title("Tori Amos - Cornflake Girl") is None
