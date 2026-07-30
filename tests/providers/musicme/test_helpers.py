"""Test MusicMe provider helpers (decrypt)."""

import pytest

from music_assistant.providers.musicme.helpers import decrypt


class TestDecrypt:
    """Tests for the MusicMe XOR 0xAA decrypt function."""

    def test_decrypt_valid_response(self) -> None:
        """Test decrypting a real MusicMe API response."""
        # This is an actual encrypted error response from the MusicMe API:
        # {"code":2,"type":"OperationForbidden","message":"invalid credentials"}
        encrypted = (
            "4A29BFAE794933B19DF3C4888688C7CFD9D9CBCDCF889088C3C4DCCBC6C3CE"
            "8AC9D8CFCECFC4DEC3CBC6D988D7D188C9C5CECF8890988688DED3DACF8890"
            "88E5DACFD8CBDEC3C5C4ECC5D8C8C3CECECF8F6B9EA2"
        )
        result = decrypt(encrypted)
        parsed = __import__("json").loads(result)
        assert parsed["code"] == 2
        assert parsed["type"] == "OperationForbidden"
        assert parsed["message"] == "invalid credentials"

    def test_decrypt_too_short(self) -> None:
        """Test that short payloads raise ValueError."""
        with pytest.raises(ValueError, match="too short"):
            decrypt("ABC")

    def test_decrypt_exactly_20_chars(self) -> None:
        """Test edge case: exactly 20 characters results in empty payload after processing."""
        # After stripping 10+8=18 chars, only 2 chars remain.
        # The marker search + offset leaves an empty string, which is now rejected.
        with pytest.raises(ValueError, match="empty after processing"):
            decrypt("A" * 20)

    def test_decrypt_marker_at_odd_index(self) -> None:
        """Test that the marker detection works for '8' and 'B' at odd indices."""
        # Build a payload where position 1 (odd) has '8' as marker
        # Padding: 10 front + 8 back = 18 chars overhead
        # After front strip, first char at index 1 should be '8'
        # This is a synthetic test — the result may not be valid JSON
        # but the function should not crash
        payload = "0123456789" + "A8" + "CC" * 10 + "12345678"
        result = decrypt(payload)
        assert isinstance(result, str)

    def test_decrypt_returns_string(self) -> None:
        """Test that decrypt always returns a string."""
        # Minimal valid-ish payload (padded, with some hex content)
        payload = "0123456789" + "AA" * 20 + "12345678"
        result = decrypt(payload)
        assert isinstance(result, str)
        assert len(result) > 0
