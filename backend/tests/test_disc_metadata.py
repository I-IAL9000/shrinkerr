"""Unit tests for backend.disc_metadata. v0.6.5+."""

import struct
import tempfile
from pathlib import Path

import pytest

from backend.disc_metadata import _iso639_1_to_2, _parse_dvd_ifo


class TestIso639Mapping:
    @pytest.mark.parametrize("code,expected", [
        ("en", "eng"),
        ("de", "ger"),
        ("fr", "fre"),
        ("ja", "jpn"),
        ("is", "ice"),
    ])
    def test_known_codes_map_to_three_letter(self, code, expected):
        assert _iso639_1_to_2(code) == expected

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

    @pytest.mark.parametrize("code", ["e", "eng", ""])
    def test_too_short_or_too_long_returns_empty(self, code):
        assert _iso639_1_to_2(code) == ""


def _build_dvd_ifo(audio_langs: list[bytes], subp_langs: list[bytes]) -> bytes:
    """Build a minimal VTS IFO fixture with the given audio and subpicture
    language codes. Pads to 0x320 bytes. Each lang code MUST be 2 bytes.
    Empty audio/subp lists mean nr_of_streams = 0."""
    buf = bytearray(0x320)
    # Magic
    buf[0:12] = b"DVDVIDEO-VTS"
    # nr_of_vts_audio_streams at 0x202
    buf[0x202] = len(audio_langs)
    # audio_attrs[] at 0x204 — 8 entries × 8 bytes
    for i, lang in enumerate(audio_langs[:8]):
        assert len(lang) == 2
        # bytes +0 and +1 are packed format/lang_type/etc — leave 0
        # bytes +2,+3 = lang_code
        buf[0x204 + 8*i + 2] = lang[0]
        buf[0x204 + 8*i + 3] = lang[1]
    # nr_of_vts_subp_streams at 0x254
    buf[0x254] = len(subp_langs)
    # subp_attrs[] at 0x256 — 32 entries × 6 bytes
    for i, lang in enumerate(subp_langs[:32]):
        assert len(lang) == 2
        buf[0x256 + 6*i + 2] = lang[0]
        buf[0x256 + 6*i + 3] = lang[1]
    return bytes(buf)


class TestDvdIfoParser:
    def _write_fixture(self, content: bytes) -> Path:
        td = Path(tempfile.mkdtemp())
        ifo = td / "VTS_01_0.IFO"
        ifo.write_bytes(content)
        return ifo

    def test_single_english_audio_no_subs(self):
        fixture = _build_dvd_ifo(audio_langs=[b"en"], subp_langs=[])
        path = self._write_fixture(fixture)
        assert _parse_dvd_ifo(path) == {"audio": ["eng"], "subtitle": []}

    def test_multi_language_audio_and_subs(self):
        fixture = _build_dvd_ifo(
            audio_langs=[b"en", b"de"],
            subp_langs=[b"ja", b"fr"],
        )
        path = self._write_fixture(fixture)
        assert _parse_dvd_ifo(path) == {
            "audio": ["eng", "ger"],
            "subtitle": ["jpn", "fre"],
        }

    def test_unused_slots_produce_empty_strings(self):
        # Declare 2 audio streams; first valid, second has zeroed lang_code
        fixture = _build_dvd_ifo(audio_langs=[b"en", b"\x00\x00"], subp_langs=[])
        path = self._write_fixture(fixture)
        result = _parse_dvd_ifo(path)
        assert result["audio"] == ["eng", ""]

    def test_unknown_language_code_returns_empty(self):
        fixture = _build_dvd_ifo(audio_langs=[b"xx"], subp_langs=[])
        path = self._write_fixture(fixture)
        assert _parse_dvd_ifo(path) == {"audio": [""], "subtitle": []}

    def test_malformed_magic_returns_empty_lists(self):
        fixture = bytearray(0x320)
        fixture[0:12] = b"NOTADVD-VTS-"  # wrong magic
        path = self._write_fixture(bytes(fixture))
        assert _parse_dvd_ifo(path) == {"audio": [], "subtitle": []}

    def test_truncated_file_returns_empty_lists(self):
        # Valid magic but file is too short
        fixture = b"DVDVIDEO-VTS" + b"\x00" * 100
        path = self._write_fixture(fixture)
        assert _parse_dvd_ifo(path) == {"audio": [], "subtitle": []}

    def test_missing_file_returns_empty_lists(self):
        nonexistent = Path("/tmp/does_not_exist_for_test.IFO")
        assert _parse_dvd_ifo(nonexistent) == {"audio": [], "subtitle": []}

    def test_max_streams_8_audio_32_subp(self):
        # DVD spec caps: 8 audio, 32 subp. Build a full-cap fixture.
        audio = [b"en"] * 8
        subp = [b"en"] * 32
        fixture = _build_dvd_ifo(audio, subp)
        path = self._write_fixture(fixture)
        result = _parse_dvd_ifo(path)
        assert len(result["audio"]) == 8
        assert len(result["subtitle"]) == 32

    def test_clamps_when_count_exceeds_spec(self):
        # Defensive: if nr_of_streams reports >8 (corrupt IFO), clamp.
        fixture = bytearray(_build_dvd_ifo(audio_langs=[b"en"], subp_langs=[]))
        fixture[0x202] = 200  # nonsensical
        path = self._write_fixture(bytes(fixture))
        result = _parse_dvd_ifo(path)
        assert len(result["audio"]) <= 8
