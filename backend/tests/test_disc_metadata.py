"""Unit tests for backend.disc_metadata. v0.6.5+."""

import pytest

from backend.disc_metadata import _iso639_1_to_2


class TestIso639Mapping:
    def test_known_codes_map_to_three_letter(self):
        assert _iso639_1_to_2("en") == "eng"
        assert _iso639_1_to_2("de") == "ger"
        assert _iso639_1_to_2("fr") == "fre"
        assert _iso639_1_to_2("ja") == "jpn"
        assert _iso639_1_to_2("is") == "ice"

    def test_uppercase_input_normalized(self):
        assert _iso639_1_to_2("EN") == "eng"
        assert _iso639_1_to_2("De") == "ger"

    def test_unknown_code_returns_empty(self):
        assert _iso639_1_to_2("xx") == ""
        assert _iso639_1_to_2("zz") == ""

    def test_zeroed_bytes_returns_empty(self):
        # DVD IFO unused audio_attr slots have lang_code = b"\x00\x00"
        # which decodes to "\x00\x00" — must NOT match anything in the table
        assert _iso639_1_to_2("\x00\x00") == ""

    def test_whitespace_returns_empty(self):
        # Some discs pad codes with spaces
        assert _iso639_1_to_2("  ") == ""

    def test_too_short_or_too_long_returns_empty(self):
        assert _iso639_1_to_2("e") == ""
        assert _iso639_1_to_2("eng") == ""  # 3-letter input not allowed here
        assert _iso639_1_to_2("") == ""
