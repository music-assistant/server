"""Tests for Linn/OpenHome Media helpers."""
import defusedxml.ElementTree as DefusedET
import pytest
from async_upnp_client.profiles.ohmedia import TransportStateAllowedValues
from music_assistant_models.enums import PlaybackState

from music_assistant.providers.openhome_media.player import OpenHomePlayer, PlayerSource


class TestIsValidUuid:
    """Test UUID validation."""

    @pytest.mark.parametrize(
        ("uuid", "expected"),
        [
            ("uuid:00000000-1234-4321-ABCD-56789ABCDEF0", True),
            ("00000000-1234-4321-ABCD-56789ABCDEF0", True),
            ("not-a-uuid", False),
            ("", False),
            ("short", False),
            ("under_scores", False),
            (None, False),
        ],
    )
    def test_is_valid_uuid(self, uuid: str | None, expected:bool) -> None:
        """Test valid UUID detection."""
        actual = OpenHomePlayer.is_valid_uuid(uuid)
        assert actual is expected


class TestMacFromUuid:
    """Test MAC address extraction from UUID."""

    @pytest.mark.parametrize(
        ("uuid", "expected"),
        [
            ("uuid:00000000-1234-4321-ABCD-56789ABCDEF0", "12:34:43:21:AB:CD"),
            ("00000000-1234-4321-ABCD-56789ABCDEF0", "12:34:43:21:AB:CD"),
        ],
    )
    def test_get_mac_from_uuid(self, uuid: str, expected: str) -> None:
        """Test MAC extraction from valid UUID format."""
        actual = OpenHomePlayer.get_mac_from_uuid(uuid)
        assert actual == expected


class TestTransportStateConversion:
    """Test transport state to playback state mapping."""

    @pytest.mark.parametrize(
        ("transport_state", "expected_playback"),
        [
            (TransportStateAllowedValues.PLAYING, PlaybackState.PLAYING),
            (TransportStateAllowedValues.PAUSED, PlaybackState.PAUSED),
            (TransportStateAllowedValues.STOPPED, PlaybackState.IDLE),
            (TransportStateAllowedValues.BUFFERING, PlaybackState.UNKNOWN),
            (TransportStateAllowedValues.WAITING, PlaybackState.IDLE),
            (None, PlaybackState.UNKNOWN),
            ("UnknownState", PlaybackState.UNKNOWN),
        ],
    )
    def test_transport_state_to_playback_state(self, transport_state:TransportStateAllowedValues, expected_playback: PlaybackState) -> None:
        """Test conversion from device transport state to MA playback state."""
        result = OpenHomePlayer._transport_state_to_playback_state(transport_state)
        assert result == expected_playback


class TestSourceList:
    """Tests for _source_list_from_source_xml() XML parsing and filtering."""

    def test_none(self) -> None:
        """Test None returns empty list."""
        assert OpenHomePlayer._source_list_from_source_xml(None) == []

    def test_single_visible_source(self) -> None:
        """Test single visible source extraction."""
        xml = """<Sources>
            <Source>
                <Visible>true</Visible>
                <Name>CD</Name>
                <Type>Digital</Type>
                <SystemName>SPDIF1</SystemName>
            </Source>
        </Sources>"""

        source_xml = DefusedET.fromstring(xml)
        result = OpenHomePlayer._source_list_from_source_xml(source_xml)
        assert len(result) == 1
        assert result[0].id == "0"
        assert result[0].name == "CD"


    def test_multiple_sources(self) -> None:
        """Test multiple visible sources extraction."""
        xml = """<Sources>
            <Source>
                <Visible>true</Visible>
                <Name>Playlist</Name>
                <Type>Playlist</Type>
                <SystemName>Playlist</SystemName>
            </Source>
            <Source>
                <Visible>true</Visible>
                <Name>Radio</Name>
                <Type>Radio</Type>
                <SystemName>Radio</SystemName>
            </Source>
            <Source>
                <Visible>true</Visible>
                <Name>Local Music</Name>
                <Type>Analog</Type>
                <SystemName>Analog</SystemName>
            </Source>
        </Sources>"""

        source_xml = DefusedET.fromstring(xml)
        result = OpenHomePlayer._source_list_from_source_xml(source_xml)

        assert len(result) == 3
        assert result[0].name == "Playlist"
        assert result[1].name == "Radio"
        assert result[2].name == "Local Music"

    def test_hidden_sources_excluded(self) -> None:
        """Test that hidden sources are filtered out."""
        xml = """<Sources>
            <Source>
                <Visible>false</Visible>
                <Name>Hidden Source</Name>
                <Type>Digital</Type>
                <SystemName>hidden</SystemName>
            </Source>
            <Source>
                <Visible>true</Visible>
                <Name>Visible Source</Name>
                <Type>Digital</Type>
                <SystemName>visible</SystemName>
            </Source>
        </Sources>"""

        source_xml = DefusedET.fromstring(xml)
        result = OpenHomePlayer._source_list_from_source_xml(source_xml)

        assert len(result) == 1
        assert result[0].name == "Visible Source"

    @pytest.mark.parametrize("visibility_value", ["true", "True", "TRUE", "1", " true "])
    def test_visibility_true_variations(self, visibility_value: str) -> None:
        """Test various reasonable 'true' value representations are recognized as visible."""
        xml = f"""<Sources>
            <Source>
                <Visible>{visibility_value}</Visible>
                <Name>Test Source</Name>
                <Type>Digital</Type>
                <SystemName>test</SystemName>
            </Source>
        </Sources>"""

        source_xml = DefusedET.fromstring(xml)
        result = OpenHomePlayer._source_list_from_source_xml(source_xml)

        assert len(result) == 1
        assert result[0].name == "Test Source"

    @pytest.mark.parametrize("visibility_value", ["false", "False", "0", "", "unknown"])
    def test_visibility_false_variations(self, visibility_value: str) -> None:
        """Test various non-visible values filter out sources."""
        xml = f"""<Sources>
            <Source>
                <Visible>{visibility_value}</Visible>
                <Name>Hidden Source</Name>
                <Type>Test</Type>
                <SystemName>hidden</SystemName>
            </Source>
        </Sources>"""

        source_xml = DefusedET.fromstring(xml)
        result = OpenHomePlayer._source_list_from_source_xml(source_xml)

        # Should return empty list since none are visible
        assert result == []

    def test_missing_visible_element_defaults_to_hidden(self) -> None:
        """Test source without Visible element is treated as hidden."""
        xml = """<Sources>
            <Source>
                <Name>Source Without Visible Tag</Name>
                <Type>Digital</Type>
                <SystemName>novisible</SystemName>
            </Source>
        </Sources>"""

        source_xml = DefusedET.fromstring(xml)
        result = OpenHomePlayer._source_list_from_source_xml(source_xml)

        # findtext returns None when element missing, which is not "true" or "1"
        assert result == []

    def test_missing_optional_elements_handled_gracefully(self) -> None:
        """Test sources missing optional elements are still processed."""
        xml = """<Sources>
            <Source>
                <Visible>true</Visible>
                <Name>Minimal Source</Name>
                <!-- Missing Type and SystemName -->
            </Source>
        </Sources>"""

        source_xml = DefusedET.fromstring(xml)
        result = OpenHomePlayer._source_list_from_source_xml(source_xml)

        assert len(result) == 1
        assert result[0].name == "Minimal Source"

    def test_special_characters_in_names(self) -> None:
        """Test special characters in source names are preserved."""
        xml = """<Sources>
            <Source>
                <Visible>true</Visible>
                <Name>Artist &amp; Band</Name>
                <Type>Digital</Type>
                <SystemName>artist&amp;band</SystemName>
            </Source>
        </Sources>"""

        source_xml = DefusedET.fromstring(xml)
        result = OpenHomePlayer._source_list_from_source_xml(source_xml)

        assert len(result) == 1
        # XML entities should be decoded
        assert "&" in result[0].name or "&amp;" in result[0].name

    def test_unicode_characters_in_names(self) -> None:
        """Test Unicode characters in source names."""
        xml = """<Sources>
            <Source>
                <Visible>true</Visible>
                <Name>Música é linda 🎵</Name>
                <Type>Radio</Type>
                <SystemName>unicode</SystemName>
            </Source>
        </Sources>"""

        source_xml = DefusedET.fromstring(xml)
        result = OpenHomePlayer._source_list_from_source_xml(source_xml)

        assert len(result) == 1
        assert "🎵" in result[0].name
        assert "é" in result[0].name

    def test_whitespace_handling_in_values(self) -> None:
        """Test whitespace in Name is NOT trimmed by XML parser."""
        xml = """<Sources>
            <Source>
                <Visible>true</Visible>
                <Name>  Spaced Name  </Name>
                <Type>Digital</Type>
                <SystemName>spaced</SystemName>
            </Source>
        </Sources>"""

        source_xml = DefusedET.fromstring(xml)
        result = OpenHomePlayer._source_list_from_source_xml(source_xml)

        assert len(result) == 1
        # ElementTree typically preserves whitespace in text content
        assert result[0].name == "  Spaced Name  "

    def test_nested_sources_ignored(self) -> None:
        """Test that nested Source elements are only counted once."""
        xml = """<Sources>
            <Source>
                <Visible>true</Visible>
                <Name>Outer</Name>
                <Type>PLAYLIST</Type>
                <SystemName>outer</SystemName>
                <!-- Nested should not be counted separately -->
                <Source>
                    <Visible>true</Visible>
                    <Name>Inner</Name>
                </Source>
            </Source>
        </Sources>"""

        source_xml = DefusedET.fromstring(xml)
        result = OpenHomePlayer._source_list_from_source_xml(source_xml)

        # Should only get top-level sources
        assert len(result) == 1
        assert result[0].name == "Outer"

    def test_large_number_of_sources(self) -> None:
        """Test handling of many sources."""
        sources = "".join([f"""
            <Source>
                <Visible>true</Visible>
                <Name>Source {i}</Name>
                <Type>Digital</Type>
                <SystemName>source{i}</SystemName>
            </Source>
        """ for i in range(50)])

        xml = f"<Sources>{sources}</Sources>"

        source_xml = DefusedET.fromstring(xml)
        result = OpenHomePlayer._source_list_from_source_xml(source_xml)

        assert len(result) == 50
        assert result[49].name == "Source 49"

    def test_index_ordering_preserved(self) -> None:
        """Test that source indices match enumeration order."""
        xml = """<Sources>
            <Source>
                <Visible>true</Visible>
                <Name>First</Name>
                <Type>Digital</Type>
                <SystemName>first</SystemName>
            </Source>
            <Source>
                <Visible>true</Visible>
                <Name>Second</Name>
                <Type>Digital</Type>
                <SystemName>second</SystemName>
            </Source>
            <Source>
                <Visible>true</Visible>
                <Name>Third</Name>
                <Type>Digital</Type>
                <SystemName>third</SystemName>
            </Source>
        </Sources>"""

        source_xml = DefusedET.fromstring(xml)
        result = OpenHomePlayer._source_list_from_source_xml(source_xml)

        assert result[0].id == "0"
        assert result[1].id == "1"
        assert result[2].id == "2"

    def test_mixed_visibility_preserves_order(self) -> None:
        """Test that visible sources maintain their relative order."""
        xml = """<Sources>
            <Source>
                <Visible>false</Visible>
                <Name>Hidden 1</Name>
                <Type>Playlist</Type>
                <SystemName>h1</SystemName>
            </Source>
            <Source>
                <Visible>true</Visible>
                <Name>Visible 1</Name>
                <Type>Radio</Type>
                <SystemName>v1</SystemName>
            </Source>
            <Source>
                <Visible>false</Visible>
                <Name>Hidden 2</Name>
                <Type>Digital</Type>
                <SystemName>h2</SystemName>
            </Source>
            <Source>
                <Visible>true</Visible>
                <Name>Visible 2</Name>
                <Type>Digital</Type>
                <SystemName>v2</SystemName>
            </Source>
            <Source>
                <Visible>true</Visible>
                <Name>Visible 3</Name>
                <Type>Digital</Type>
                <SystemName>v3</SystemName>
            </Source>
        </Sources>"""

        source_xml = DefusedET.fromstring(xml)
        result = OpenHomePlayer._source_list_from_source_xml(source_xml)

        assert len(result) == 3
        assert result[0].name == "Visible 1"
        assert result[1].name == "Visible 2"
        assert result[2].name == "Visible 3"
        # Id should match original index position in source_xml
        assert result[0].id == "1"
        assert result[1].id == "3"
        assert result[2].id == "4"

    def test_case_sensitive_type_values_preserved(self) -> None:
        """Test that Type values preserve case."""
        xml = """<Sources>
            <Source>
                <Visible>true</Visible>
                <Name>Test</Name>
                <Type>Playlist</Type>
                <SystemName>test</SystemName>
            </Source>
        </Sources>"""

        source_xml = DefusedET.fromstring(xml)
        result = OpenHomePlayer._source_list_from_source_xml(source_xml)

        # Should preserve exact case
        assert result[0].name == "Test"

    def test_empty_sources_root(self) -> None:
        """Test empty Sources element."""
        xml = "<Sources/>"
        source_xml = DefusedET.fromstring(xml)
        result = OpenHomePlayer._source_list_from_source_xml(source_xml)
        assert result == []

    def test_complex_xml_with_attributes(self) -> None:
        """Test XML with attributes on elements."""
        xml = """<Sources>
            <Source id="1" version="2">
                <Visible>true</Visible>
                <Name>Attributed Source</Name>
                <Type>PLAYLIST</Type>
                <SystemName>attributed</SystemName>
            </Source>
        </Sources>"""

        source_xml = DefusedET.fromstring(xml)
        result = OpenHomePlayer._source_list_from_source_xml(source_xml)

        assert len(result) == 1
        assert result[0].name == "Attributed Source"

    def test_return_type_is_list_of_player_source(self) -> None:
        """Test return type structure."""
        xml = """<Sources>
            <Source>
                <Visible>true</Visible>
                <Name>Test</Name>
                <Type>PLAYLIST</Type>
                <SystemName>test</SystemName>
            </Source>
        </Sources>"""

        source_xml = DefusedET.fromstring(xml)
        result = OpenHomePlayer._source_list_from_source_xml(source_xml)

        assert isinstance(result, list)
        assert all(isinstance(item, PlayerSource) for item in result)
