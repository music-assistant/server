"""Tests for cleaning radio streamtitle."""

from music_assistant.common.helpers.util import clean_stream_title


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

    line = 'title="Thirty Seconds To Mars - Closer to the Edge",artist="Thirty Seconds To Mars - Closer to the Edge",url="https://nowplaying.scahw.com.au/c/fd8ee07bed6a5e4e9824a11aa02dd34a.jpg?t=1714568458&l=250"'  # noqa: E501
    stream_title = clean_stream_title(line)
    assert stream_title == tstm

    line = 'title="https://listenapi.planetradio.co.uk/api9.2/eventdata/247801912",url="https://listenapi.planetradio.co.uk/api9.2/eventdata/247801912"'
    stream_title = clean_stream_title(line)
    assert stream_title == ""

    line = 'title="Thirty Seconds To Mars - Closer to the Edge https://nowplaying.scahw.com.au/",artist="Thirty Seconds To Mars - Closer to the Edge",url="https://nowplaying.scahw.com.au/c/fd8ee07bed6a5e4e9824a11aa02dd34a.jpg?t=1714568458&l=250"'  # noqa: E501
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
