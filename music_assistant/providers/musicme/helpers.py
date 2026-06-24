"""Helpers for the MusicMe provider (crypto, etc.)."""


def decrypt(encrypted: str) -> str:
    """
    Decrypt a MusicMe API response or ticket string.

    Reverse-engineered from audioFrame-bundle.min.js (apache_crypto module).
    Algorithm: strip padding, find marker, rearrange halves, hex-decode XOR 0xAA.
    """
    if len(encrypted) < 20:
        msg = f"Encrypted payload too short ({len(encrypted)} chars)"
        raise ValueError(msg)

    # Strip first 10 and last 8 padding characters
    encrypted = encrypted[10 : len(encrypted) - 8]

    # Find position marker ('8' or 'B' at an odd index within the first 10 chars)
    pos = -1
    for i in range(min(10, len(encrypted))):
        c = encrypted[i]
        if i % 2 == 1 and c in ("8", "B"):
            pos = i + 1
            break
    if pos == -1:
        pos = 10
    encrypted = encrypted[pos:]

    # Rearrange: swap two halves of the hex string
    test = list(encrypted)
    first_length = len(test) // 2 - (len(test) // 2) % 2
    sec_length = len(test) - first_length
    chars: list[str] = [""] * len(test)
    for j in range(first_length):
        chars[j + sec_length] = test[j]
    for j in range(sec_length):
        chars[j] = test[first_length + j]

    chars_len = len(chars)
    if chars_len == 0:
        raise ValueError("Encrypted payload is empty after processing")
    if chars_len % 2 != 0:
        msg = f"Encrypted payload has odd hex length after processing ({chars_len})"
        raise ValueError(msg)
    decrypted_chars: list[int] = []
    for i in range(chars_len // 2):
        hex_str = chars[2 * i] + chars[2 * i + 1]
        b = int(hex_str, 16)
        decrypted_chars.append(b ^ 0xAA)

    return "".join(chr(b) for b in decrypted_chars)
