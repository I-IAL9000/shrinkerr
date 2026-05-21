"""Unit tests for backend.disc_metadata. v0.6.5+."""

import io as _io
import struct
from pathlib import Path

import pycdlib
import pytest

from backend.disc_metadata import (
    _find_main_bdmv_playlist,
    _iso639_1_to_2,
    _parse_bdmv_mpls,
    _parse_dvd_ifo,
)


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
    def _write_fixture(self, tmp_path: Path, content: bytes) -> Path:
        ifo = tmp_path / "VTS_01_0.IFO"
        ifo.write_bytes(content)
        return ifo

    def test_single_english_audio_no_subs(self, tmp_path):
        fixture = _build_dvd_ifo(audio_langs=[b"en"], subp_langs=[])
        path = self._write_fixture(tmp_path, fixture)
        assert _parse_dvd_ifo(path) == {"audio": ["eng"], "subtitle": []}

    def test_multi_language_audio_and_subs(self, tmp_path):
        fixture = _build_dvd_ifo(
            audio_langs=[b"en", b"de"],
            subp_langs=[b"ja", b"fr"],
        )
        path = self._write_fixture(tmp_path, fixture)
        assert _parse_dvd_ifo(path) == {
            "audio": ["eng", "ger"],
            "subtitle": ["jpn", "fre"],
        }

    def test_unused_slots_produce_empty_strings(self, tmp_path):
        # Declare 2 audio streams; first valid, second has zeroed lang_code
        fixture = _build_dvd_ifo(audio_langs=[b"en", b"\x00\x00"], subp_langs=[])
        path = self._write_fixture(tmp_path, fixture)
        result = _parse_dvd_ifo(path)
        assert result["audio"] == ["eng", ""]

    def test_unknown_language_code_returns_empty(self, tmp_path):
        fixture = _build_dvd_ifo(audio_langs=[b"xx"], subp_langs=[])
        path = self._write_fixture(tmp_path, fixture)
        assert _parse_dvd_ifo(path) == {"audio": [""], "subtitle": []}

    def test_malformed_magic_returns_empty_lists(self, tmp_path):
        fixture = bytearray(0x320)
        fixture[0:12] = b"NOTADVD-VTS-"  # wrong magic
        path = self._write_fixture(tmp_path, bytes(fixture))
        assert _parse_dvd_ifo(path) == {"audio": [], "subtitle": []}

    def test_truncated_file_returns_empty_lists(self, tmp_path):
        # Valid magic but file is too short
        fixture = b"DVDVIDEO-VTS" + b"\x00" * 100
        path = self._write_fixture(tmp_path, fixture)
        assert _parse_dvd_ifo(path) == {"audio": [], "subtitle": []}

    def test_missing_file_returns_empty_lists(self):
        # No fixture needed
        nonexistent = Path("/tmp/does_not_exist_for_test.IFO")
        assert _parse_dvd_ifo(nonexistent) == {"audio": [], "subtitle": []}

    def test_max_streams_8_audio_32_subp(self, tmp_path):
        # DVD spec caps: 8 audio, 32 subp. Build a full-cap fixture.
        audio = [b"en"] * 8
        subp = [b"en"] * 32
        fixture = _build_dvd_ifo(audio, subp)
        path = self._write_fixture(tmp_path, fixture)
        result = _parse_dvd_ifo(path)
        assert len(result["audio"]) == 8
        assert len(result["subtitle"]) == 32

    def test_clamps_when_count_exceeds_spec(self, tmp_path):
        # Defensive: if nr_of_streams reports >8 (corrupt IFO), clamp.
        fixture = bytearray(_build_dvd_ifo(audio_langs=[b"en"], subp_langs=[]))
        fixture[0x202] = 200  # nonsensical
        path = self._write_fixture(tmp_path, bytes(fixture))
        result = _parse_dvd_ifo(path)
        assert len(result["audio"]) <= 8

    def test_count_zero_but_attr_populated_fallback(self, tmp_path):
        """Real-world quirk: some DVD authoring tools write a valid
        audio_attr[0] but leave nr_of_vts_audio_streams = 0 (the count
        byte). Parser must fall back to scanning attrs in that case.
        Fast-Walking (1982) is one such disc. v0.6.6+."""
        # Build a fixture with the exact byte pattern from Fast-Walking's
        # VTS_01_0.IFO: count=0, audio_attr[0] = 04c1656e00000000 (lang
        # 'en' at offset +2), audio_attr[1] = all zeros (gap sentinel).
        buf = bytearray(0x320)
        buf[0:12] = b"DVDVIDEO-VTS"
        buf[0x202] = 0  # n_audio reports 0
        # audio_attr[0]: bytes 0,1 = packed format/etc, bytes 2,3 = 'en'
        buf[0x204:0x20C] = bytes.fromhex("04c1656e00000000")
        # audio_attr[1..7] stay all-zero (sentinel gap)
        buf[0x254] = 0  # n_subp also 0
        # subp_attr[0..N] stay all-zero — Fast-Walking has no subs
        path = tmp_path / "VTS_01_0.IFO"
        path.write_bytes(bytes(buf))
        assert _parse_dvd_ifo(path) == {"audio": ["eng"], "subtitle": []}

    def test_count_zero_with_multiple_populated_attrs(self, tmp_path):
        """Fallback should keep collecting until the first all-zero
        sentinel. Synthetic case: count=0 but audio_attr[0]='en',
        audio_attr[1]='de', audio_attr[2]=zero gap."""
        buf = bytearray(0x320)
        buf[0:12] = b"DVDVIDEO-VTS"
        buf[0x202] = 0
        buf[0x204 + 2:0x204 + 4] = b"en"
        buf[0x20c + 2:0x20c + 4] = b"de"
        # audio_attr[2] is the zero gap
        buf[0x254] = 0
        path = tmp_path / "VTS_01_0.IFO"
        path.write_bytes(bytes(buf))
        result = _parse_dvd_ifo(path)
        assert result["audio"] == ["eng", "ger"]
        assert result["subtitle"] == []

    def test_count_positive_still_trusted(self, tmp_path):
        """When n_declared > 0, trust it exactly — don't run the
        fallback even if attrs beyond the declared count are populated."""
        buf = bytearray(0x320)
        buf[0:12] = b"DVDVIDEO-VTS"
        buf[0x202] = 1  # declares 1 audio stream
        buf[0x204 + 2:0x204 + 4] = b"en"
        # audio_attr[1] is populated (would be picked up by fallback)
        # but the declared count is 1, so trust it
        buf[0x20c + 2:0x20c + 4] = b"de"
        buf[0x254] = 0
        path = tmp_path / "VTS_01_0.IFO"
        path.write_bytes(bytes(buf))
        result = _parse_dvd_ifo(path)
        assert result["audio"] == ["eng"]  # NOT ["eng", "ger"]


def _build_mpls_with_duration(seconds: float) -> bytes:
    """Build a minimal valid .mpls fixture whose total PlayItem duration
    is `seconds` seconds. Single PlayItem, no multi-angle, no STN_table
    streams (just enough header bytes for duration parsing).
    Used to test the longest-playlist picker without needing full mpls."""
    ticks = int(seconds * 45000)
    # Header (40 bytes)
    pl_start = 40
    plm_start = 0  # not used in selection
    ext_start = 0
    header = (
        b"MPLS"
        + b"0200"
        + struct.pack(">I", pl_start)
        + struct.pack(">I", plm_start)
        + struct.pack(">I", ext_start)
        + b"\x00" * 20  # reserved
    )
    # PlayList @ pl_start: length(4) + reserved(2) + n_playitems(2) + n_subpaths(2)
    # then PlayItem[]
    # Minimal PlayItem:
    #   length(2)
    #   clip_id(5) + codec_id(4)
    #   flags(2) + ref_to_STC_id(1)
    #   IN_time(4) + OUT_time(4)
    #   UO_mask(8) + flags(1) + still_mode(1) + still_time(2)
    #   = 34 bytes payload (length covers bytes 2 onwards = 32)
    # NO multi_clip, NO STN_table (Task 3 doesn't need it, Task 4 will)
    in_time = 0
    out_time = ticks
    playitem = (
        struct.pack(">H", 32)  # length (after this field)
        + b"00000"
        + b"M2TS"
        + b"\x00\x00"  # flags (no multi_angle)
        + b"\x00"
        + struct.pack(">I", in_time)
        + struct.pack(">I", out_time)
        + b"\x00" * 8  # UO_mask
        + b"\x00"  # flags
        + b"\x00"  # still_mode = none
        + b"\x00\x00"  # still_time
    )
    playlist_body = (
        b"\x00" * 2  # reserved
        + struct.pack(">H", 1)  # number_of_PlayItems
        + struct.pack(">H", 0)  # number_of_SubPaths
        + playitem
    )
    playlist = struct.pack(">I", len(playlist_body)) + playlist_body
    return header + playlist


class TestFindMainBdmvPlaylist:
    def test_picks_longest_among_multiple(self, tmp_path):
        playlist_dir = tmp_path / "PLAYLIST"
        playlist_dir.mkdir()
        (playlist_dir / "00000.mpls").write_bytes(_build_mpls_with_duration(120))   # 2 min
        (playlist_dir / "00100.mpls").write_bytes(_build_mpls_with_duration(5000))  # ~83 min (main feature)
        (playlist_dir / "00200.mpls").write_bytes(_build_mpls_with_duration(60))    # 1 min
        result = _find_main_bdmv_playlist(playlist_dir)
        assert result is not None
        assert result.name == "00100.mpls"

    def test_single_playlist_picked(self, tmp_path):
        playlist_dir = tmp_path / "PLAYLIST"
        playlist_dir.mkdir()
        (playlist_dir / "00000.mpls").write_bytes(_build_mpls_with_duration(4900))
        result = _find_main_bdmv_playlist(playlist_dir)
        assert result is not None
        assert result.name == "00000.mpls"

    def test_empty_dir_returns_none(self, tmp_path):
        playlist_dir = tmp_path / "PLAYLIST"
        playlist_dir.mkdir()
        assert _find_main_bdmv_playlist(playlist_dir) is None

    def test_missing_dir_returns_none(self, tmp_path):
        assert _find_main_bdmv_playlist(tmp_path / "does_not_exist") is None

    def test_invalid_mpls_skipped(self, tmp_path):
        playlist_dir = tmp_path / "PLAYLIST"
        playlist_dir.mkdir()
        (playlist_dir / "00000.mpls").write_bytes(b"not_a_real_mpls")
        (playlist_dir / "00001.mpls").write_bytes(_build_mpls_with_duration(1000))
        result = _find_main_bdmv_playlist(playlist_dir)
        assert result is not None
        assert result.name == "00001.mpls"


def _build_mpls_full(
    duration_sec: float,
    audio_langs: list[bytes],  # 3-byte ISO 639-2
    pg_langs: list[bytes],     # 3-byte ISO 639-2
) -> bytes:
    """Build a complete .mpls fixture with STN_table.

    Audio entries use stream_coding_type=0x81 (AC-3) so the audio
    StreamAttributes lang_code lives at offset +3 within the
    attributes block.

    PG (subtitle) entries use stream_coding_type=0x90 (PG); lang_code
    at offset +2 within the attributes block.
    """
    ticks = int(duration_sec * 45000)

    # --- Build StreamEntry + StreamAttributes pairs for audio ---
    # StreamEntry for PlayItem-resident audio (type 0x01):
    #   length(1) = 8 ; type(1)=0x01 ; PID (uint16 BE) ; reserved(5)
    def audio_stream_entry(pid: int) -> bytes:
        return (
            b"\x08"   # length
            + b"\x01" # type 0x01 = elementary stream in PlayItem clip
            + struct.pack(">H", pid)
            + b"\x00" * 5
        )

    def audio_stream_attr(coding_type: int, lang: bytes) -> bytes:
        # length(1) + coding_type(1) + audio_format_sample_rate(1) + lang(3)
        # = 5 payload bytes after the length byte
        # length field reports bytes AFTER the length byte
        body = bytes([coding_type]) + b"\x00" + lang
        return bytes([len(body)]) + body

    def pg_stream_entry(pid: int) -> bytes:
        return (
            b"\x08"
            + b"\x01"
            + struct.pack(">H", pid)
            + b"\x00" * 5
        )

    def pg_stream_attr(lang: bytes) -> bytes:
        # length(1) + coding_type(1) + lang(3)
        body = b"\x90" + lang
        return bytes([len(body)]) + body

    # Build streams blob for STN_table body
    streams_blob = b""
    for i, lang in enumerate(audio_langs):
        streams_blob += audio_stream_entry(0x1100 + i)
        streams_blob += audio_stream_attr(0x81, lang)
    for i, lang in enumerate(pg_langs):
        streams_blob += pg_stream_entry(0x1200 + i)
        streams_blob += pg_stream_attr(lang)

    # STN_table:
    #   uint16 BE   : length (after this field)
    #   uint16 BE   : reserved
    #   uint8       : n_primary_video
    #   uint8       : n_primary_audio
    #   uint8       : n_primary_pg
    #   uint8       : n_primary_ig
    #   uint8       : n_secondary_audio
    #   uint8       : n_secondary_video
    #   uint8       : n_pip_pg
    #   bytes[5]    : reserved
    #   streams_blob (video then audio then pg then ig in that order; we
    #                 emit only audio and pg here)
    stn_body = (
        b"\x00\x00"  # reserved
        + bytes([0, len(audio_langs), len(pg_langs), 0, 0, 0, 0])
        + b"\x00" * 5
        + streams_blob
    )
    stn_table = struct.pack(">H", len(stn_body)) + stn_body

    # PlayItem:
    #   uint16 BE : length (after this field)
    #   bytes[5]  : clip_id
    #   bytes[4]  : codec_id
    #   uint16 BE : flags (no multi_angle)
    #   uint8     : ref_to_STC_id
    #   uint32 BE : IN_time
    #   uint32 BE : OUT_time
    #   bytes[8]  : UO_mask
    #   uint8     : flags
    #   uint8     : still_mode
    #   uint16 BE : still_time
    #   STN_table
    pi_payload = (
        b"00000"
        + b"M2TS"
        + b"\x00\x00"
        + b"\x00"
        + struct.pack(">I", 0)
        + struct.pack(">I", ticks)
        + b"\x00" * 8
        + b"\x00"
        + b"\x00"
        + b"\x00\x00"
        + stn_table
    )
    playitem = struct.pack(">H", len(pi_payload)) + pi_payload

    # PlayList:
    pl_body = (
        b"\x00\x00"  # reserved
        + struct.pack(">H", 1)
        + struct.pack(">H", 0)
        + playitem
    )
    playlist = struct.pack(">I", len(pl_body)) + pl_body

    # Header (40 bytes)
    pl_start = 40
    header = (
        b"MPLS" + b"0200"
        + struct.pack(">I", pl_start)
        + struct.pack(">I", 0)
        + struct.pack(">I", 0)
        + b"\x00" * 20
    )
    return header + playlist


class TestBdmvMplsParser:
    def test_two_audio_two_subtitle(self, tmp_path):
        mpls = tmp_path / "00100.mpls"
        mpls.write_bytes(_build_mpls_full(
            duration_sec=5000,
            audio_langs=[b"eng", b"ger"],
            pg_langs=[b"eng", b"jpn"],
        ))
        result = _parse_bdmv_mpls(mpls)
        assert result == {"audio": ["eng", "ger"], "subtitle": ["eng", "jpn"]}

    def test_french_first_english_second_load_bearing(self, tmp_path):
        # Mirrors the Elephant BD case: audio[0]=fre, audio[1]=eng.
        # Stream-order correlation MUST preserve this.
        mpls = tmp_path / "00800.mpls"
        mpls.write_bytes(_build_mpls_full(
            duration_sec=4900,
            audio_langs=[b"fre", b"eng"],
            pg_langs=[b"fre", b"eng"],
        ))
        result = _parse_bdmv_mpls(mpls)
        assert result["audio"][0] == "fre"
        assert result["audio"][1] == "eng"
        assert result["subtitle"][0] == "fre"
        assert result["subtitle"][1] == "eng"

    def test_three_byte_lang_passthrough(self, tmp_path):
        # 3-letter codes pass through after decode
        mpls = tmp_path / "test.mpls"
        mpls.write_bytes(_build_mpls_full(
            duration_sec=100,
            audio_langs=[b"ice"],
            pg_langs=[],
        ))
        assert _parse_bdmv_mpls(mpls)["audio"] == ["ice"]

    def test_whitespace_padded_lang_returns_empty(self, tmp_path):
        # Some discs pad with spaces; should strip and empty → ""
        mpls = tmp_path / "test.mpls"
        mpls.write_bytes(_build_mpls_full(
            duration_sec=100,
            audio_langs=[b"   "],
            pg_langs=[],
        ))
        assert _parse_bdmv_mpls(mpls)["audio"] == [""]

    def test_malformed_magic_returns_empty(self, tmp_path):
        mpls = tmp_path / "test.mpls"
        mpls.write_bytes(b"NOTAREAL" + b"\x00" * 1000)
        assert _parse_bdmv_mpls(mpls) == {"audio": [], "subtitle": []}

    def test_truncated_file_returns_empty(self, tmp_path):
        mpls = tmp_path / "test.mpls"
        mpls.write_bytes(b"MPLS0200" + b"\x00" * 4)
        assert _parse_bdmv_mpls(mpls) == {"audio": [], "subtitle": []}

    def test_missing_file_returns_empty(self):
        assert _parse_bdmv_mpls(Path("/tmp/nonexistent.mpls")) == {"audio": [], "subtitle": []}

    def test_version_0100_also_parses(self, tmp_path):
        mpls = tmp_path / "test.mpls"
        # Patch a v0200 fixture's version field
        data = bytearray(_build_mpls_full(
            duration_sec=100,
            audio_langs=[b"eng"],
            pg_langs=[],
        ))
        data[4:8] = b"0100"
        mpls.write_bytes(bytes(data))
        assert _parse_bdmv_mpls(mpls)["audio"] == ["eng"]


from backend.disc_metadata import parse_disc_languages


class TestParseDiscLanguages:
    def test_dvd_routes_to_ifo_parser(self, tmp_path):
        # Build a fake disc-root with VIDEO_TS/VTS_01_0.IFO
        disc_root = tmp_path / "Movie (1980)"
        video_ts = disc_root / "VIDEO_TS"
        video_ts.mkdir(parents=True)
        (video_ts / "VIDEO_TS.IFO").write_bytes(b"DVDVIDEO-VMG" + b"\x00" * 0x320)
        (video_ts / "VTS_01_0.IFO").write_bytes(
            _build_dvd_ifo(audio_langs=[b"en"], subp_langs=[b"fr"])
        )
        # Also create a single VOB so _dvd_main_title_vobs picks "01"
        (video_ts / "VTS_01_1.VOB").write_bytes(b"\x00" * 100)
        result = parse_disc_languages(disc_root, "dvd")
        assert result == {"audio": ["eng"], "subtitle": ["fre"]}

    def test_bdmv_routes_to_mpls_parser(self, tmp_path):
        disc_root = tmp_path / "Movie (1990)"
        playlist_dir = disc_root / "BDMV" / "PLAYLIST"
        playlist_dir.mkdir(parents=True)
        # Two playlists; longer one is the main feature
        (playlist_dir / "00000.mpls").write_bytes(
            _build_mpls_full(duration_sec=60, audio_langs=[b"jpn"], pg_langs=[])
        )
        (playlist_dir / "00100.mpls").write_bytes(
            _build_mpls_full(duration_sec=5000, audio_langs=[b"fre", b"eng"], pg_langs=[b"fre", b"eng"])
        )
        result = parse_disc_languages(disc_root, "bdmv")
        assert result == {"audio": ["fre", "eng"], "subtitle": ["fre", "eng"]}

    def test_unknown_disc_type_returns_empty(self, tmp_path):
        result = parse_disc_languages(tmp_path, "unknown")
        assert result == {"audio": [], "subtitle": []}

    def test_dvd_with_no_vobs_returns_empty(self, tmp_path):
        # No VOBs → _dvd_main_title_vobs returns empty → can't pick title set
        disc_root = tmp_path / "Movie"
        (disc_root / "VIDEO_TS").mkdir(parents=True)
        (disc_root / "VIDEO_TS" / "VIDEO_TS.IFO").write_bytes(b"\x00" * 100)
        result = parse_disc_languages(disc_root, "dvd")
        assert result == {"audio": [], "subtitle": []}

    def test_bdmv_with_no_playlist_returns_empty(self, tmp_path):
        disc_root = tmp_path / "Movie"
        (disc_root / "BDMV" / "PLAYLIST").mkdir(parents=True)  # empty dir
        result = parse_disc_languages(disc_root, "bdmv")
        assert result == {"audio": [], "subtitle": []}


from backend.disc_metadata import (
    _parse_dvd_ifo_bytes,
    _parse_bdmv_mpls_bytes,
    _mpls_total_duration_bytes,
)


class TestParserBytesAPIs:
    """v0.7.0+: smoke-test the new _bytes core functions. Most coverage
    is via the existing path-based tests; these just verify the new entry
    points are usable directly."""

    def test_parse_dvd_ifo_bytes_with_valid_fixture(self):
        fixture = _build_dvd_ifo(audio_langs=[b"en"], subp_langs=[])
        assert _parse_dvd_ifo_bytes(fixture) == {"audio": ["eng"], "subtitle": []}

    def test_parse_dvd_ifo_bytes_with_empty_bytes(self):
        assert _parse_dvd_ifo_bytes(b"") == {"audio": [], "subtitle": []}

    def test_parse_bdmv_mpls_bytes_with_valid_fixture(self):
        fixture = _build_mpls_full(
            duration_sec=100, audio_langs=[b"eng"], pg_langs=[],
        )
        assert _parse_bdmv_mpls_bytes(fixture)["audio"] == ["eng"]

    def test_mpls_total_duration_bytes(self):
        fixture = _build_mpls_with_duration(seconds=120.5)
        # Allow tiny floating-point drift from the 45 kHz round-trip
        assert abs(_mpls_total_duration_bytes(fixture) - 120.5) < 0.001


from backend.disc_metadata import _classify_disc_iso


def _build_minimal_dvd_iso(tmp_path):
    """Use pycdlib to write a tiny ISO containing /VIDEO_TS/VIDEO_TS.IFO
    (just enough for classification). UDF + ISO 9660 dual-format."""
    iso = pycdlib.PyCdlib()
    iso.new(udf="2.60")
    iso.add_directory(udf_path="/VIDEO_TS")
    iso.add_fp(
        _io.BytesIO(b"DVDVIDEO-VTS" + b"\x00" * 64),
        len(b"DVDVIDEO-VTS") + 64,
        udf_path="/VIDEO_TS/VIDEO_TS.IFO",
    )
    out = tmp_path / "fake_dvd.iso"
    iso.write(str(out))
    iso.close()
    return out


def _build_minimal_bdmv_iso(tmp_path):
    iso = pycdlib.PyCdlib()
    iso.new(udf="2.60")
    iso.add_directory(udf_path="/BDMV")
    iso.add_fp(
        _io.BytesIO(b"INDX0200" + b"\x00" * 32),
        len(b"INDX0200") + 32,
        udf_path="/BDMV/index.bdmv",
    )
    out = tmp_path / "fake_bdmv.iso"
    iso.write(str(out))
    iso.close()
    return out


def _build_empty_iso(tmp_path):
    """Non-video ISO — no VIDEO_TS, no BDMV."""
    iso = pycdlib.PyCdlib()
    iso.new(udf="2.60")
    iso.add_directory(udf_path="/READMES")
    out = tmp_path / "fake_other.iso"
    iso.write(str(out))
    iso.close()
    return out


class TestClassifyDiscIso:
    def test_dvd_iso_classified(self, tmp_path):
        iso = _build_minimal_dvd_iso(tmp_path)
        assert _classify_disc_iso(iso) == "dvd"

    def test_bdmv_iso_classified(self, tmp_path):
        iso = _build_minimal_bdmv_iso(tmp_path)
        assert _classify_disc_iso(iso) == "bdmv"

    def test_non_video_iso_returns_none(self, tmp_path):
        iso = _build_empty_iso(tmp_path)
        assert _classify_disc_iso(iso) is None

    def test_missing_iso_returns_none(self, tmp_path):
        assert _classify_disc_iso(tmp_path / "does_not_exist.iso") is None

    def test_garbage_file_returns_none(self, tmp_path):
        path = tmp_path / "not_an_iso.iso"
        path.write_bytes(b"definitely not an iso file")
        assert _classify_disc_iso(path) is None

    def test_combo_iso_prefers_bdmv(self, tmp_path):
        """If an ISO has both VIDEO_TS and BDMV (rare combo disc),
        BDMV wins — same priority as the folder-based _classify_disc."""
        iso = pycdlib.PyCdlib()
        iso.new(udf="2.60")
        iso.add_directory(udf_path="/VIDEO_TS")
        iso.add_fp(_io.BytesIO(b"x" * 100), 100, udf_path="/VIDEO_TS/VIDEO_TS.IFO")
        iso.add_directory(udf_path="/BDMV")
        iso.add_fp(_io.BytesIO(b"y" * 100), 100, udf_path="/BDMV/index.bdmv")
        out = tmp_path / "combo.iso"
        iso.write(str(out))
        iso.close()
        assert _classify_disc_iso(out) == "bdmv"


from backend.disc_metadata import (
    _extract_iso_file,
    _pick_main_vts_in_iso,
    _pick_main_mpls_in_iso,
)


class TestIsoExtractors:
    def test_extract_iso_file_udf_path(self, tmp_path):
        # Build an ISO with a known file
        iso = pycdlib.PyCdlib()
        iso.new(udf="2.60")
        iso.add_directory(udf_path="/X")
        iso.add_fp(_io.BytesIO(b"HELLO_PAYLOAD"), 13, udf_path="/X/file.bin")
        out = tmp_path / "x.iso"
        iso.write(str(out))
        iso.close()

        iso2 = pycdlib.PyCdlib()
        iso2.open(str(out))
        try:
            data = _extract_iso_file(iso2, "/X/file.bin")
            assert data == b"HELLO_PAYLOAD"
        finally:
            iso2.close()

    def test_extract_iso_file_missing_raises(self, tmp_path):
        iso = pycdlib.PyCdlib()
        iso.new(udf="2.60")
        out = tmp_path / "empty.iso"
        iso.write(str(out))
        iso.close()

        iso2 = pycdlib.PyCdlib()
        iso2.open(str(out))
        try:
            with pytest.raises(FileNotFoundError):
                _extract_iso_file(iso2, "/does/not/exist")
        finally:
            iso2.close()

    def test_pick_main_vts_picks_largest(self, tmp_path):
        """Build an ISO with two title sets: VTS_01 (3 large VOBs) and
        VTS_02 (1 small VOB). Picker should return '01'."""
        iso = pycdlib.PyCdlib()
        iso.new(udf="2.60")
        iso.add_directory(udf_path="/VIDEO_TS")
        # VTS_01: 3 VOBs of 1000 bytes each
        for i in range(1, 4):
            iso.add_fp(
                _io.BytesIO(b"\x00" * 1000), 1000,
                udf_path=f"/VIDEO_TS/VTS_01_{i}.VOB",
            )
        # VTS_02: 1 VOB of 100 bytes
        iso.add_fp(
            _io.BytesIO(b"\x00" * 100), 100,
            udf_path="/VIDEO_TS/VTS_02_1.VOB",
        )
        # Menu chunks (should be ignored)
        iso.add_fp(
            _io.BytesIO(b"\x00" * 50), 50,
            udf_path="/VIDEO_TS/VTS_01_0.VOB",
        )
        out = tmp_path / "multi_vts.iso"
        iso.write(str(out))
        iso.close()

        iso2 = pycdlib.PyCdlib()
        iso2.open(str(out))
        try:
            assert _pick_main_vts_in_iso(iso2) == "01"
        finally:
            iso2.close()

    def test_pick_main_vts_no_vobs_returns_none(self, tmp_path):
        iso = pycdlib.PyCdlib()
        iso.new(udf="2.60")
        iso.add_directory(udf_path="/VIDEO_TS")
        # only IFO, no VOBs
        iso.add_fp(_io.BytesIO(b"x" * 100), 100, udf_path="/VIDEO_TS/VIDEO_TS.IFO")
        out = tmp_path / "novobs.iso"
        iso.write(str(out))
        iso.close()

        iso2 = pycdlib.PyCdlib()
        iso2.open(str(out))
        try:
            assert _pick_main_vts_in_iso(iso2) is None
        finally:
            iso2.close()

    def test_pick_main_mpls_picks_longest(self, tmp_path):
        """Build an ISO with three .mpls of different durations. Picker
        should return the bytes of the longest."""
        iso = pycdlib.PyCdlib()
        iso.new(udf="2.60")
        iso.add_directory(udf_path="/BDMV")
        iso.add_directory(udf_path="/BDMV/PLAYLIST")
        # Three playlists; middle one is longest
        for name, duration in [("00000.mpls", 60), ("00100.mpls", 5000), ("00200.mpls", 120)]:
            data = _build_mpls_with_duration(seconds=duration)
            iso.add_fp(_io.BytesIO(data), len(data), udf_path=f"/BDMV/PLAYLIST/{name}")
        out = tmp_path / "multi_mpls.iso"
        iso.write(str(out))
        iso.close()

        iso2 = pycdlib.PyCdlib()
        iso2.open(str(out))
        try:
            result = _pick_main_mpls_in_iso(iso2)
            assert result is not None
            # The picked bytes should be the longest playlist (~5000 sec)
            assert abs(_mpls_total_duration_bytes(result) - 5000) < 1
        finally:
            iso2.close()

    def test_pick_main_mpls_no_playlist_dir_returns_none(self, tmp_path):
        iso = pycdlib.PyCdlib()
        iso.new(udf="2.60")
        out = tmp_path / "empty.iso"
        iso.write(str(out))
        iso.close()

        iso2 = pycdlib.PyCdlib()
        iso2.open(str(out))
        try:
            assert _pick_main_mpls_in_iso(iso2) is None
        finally:
            iso2.close()


from backend.disc_metadata import parse_disc_languages_iso


class TestParseDiscLanguagesIso:
    def test_dvd_iso_full_pipeline(self, tmp_path):
        """End-to-end: build a DVD-like ISO with a VTS_01_0.IFO containing
        a known audio language, run the full parse_disc_languages_iso
        pipeline, expect the extracted IFO bytes to flow through the
        bytes parser correctly."""
        iso = pycdlib.PyCdlib()
        iso.new(udf="2.60")
        iso.add_directory(udf_path="/VIDEO_TS")
        # Build a synthetic IFO with audio=['en']
        ifo_data = _build_dvd_ifo(audio_langs=[b"en"], subp_langs=[])
        iso.add_fp(_io.BytesIO(ifo_data), len(ifo_data), udf_path="/VIDEO_TS/VTS_01_0.IFO")
        # Add a VOB so _pick_main_vts_in_iso picks "01"
        iso.add_fp(_io.BytesIO(b"\x00" * 1000), 1000, udf_path="/VIDEO_TS/VTS_01_1.VOB")
        out = tmp_path / "test_dvd.iso"
        iso.write(str(out))
        iso.close()

        result = parse_disc_languages_iso(out, "dvd")
        assert result == {"audio": ["eng"], "subtitle": []}

    def test_bdmv_iso_full_pipeline(self, tmp_path):
        iso = pycdlib.PyCdlib()
        iso.new(udf="2.60")
        iso.add_directory(udf_path="/BDMV")
        iso.add_directory(udf_path="/BDMV/PLAYLIST")
        mpls_data = _build_mpls_full(
            duration_sec=5000,
            audio_langs=[b"fre", b"eng"],
            pg_langs=[b"fre", b"eng"],
        )
        iso.add_fp(_io.BytesIO(mpls_data), len(mpls_data), udf_path="/BDMV/PLAYLIST/00100.mpls")
        out = tmp_path / "test_bdmv.iso"
        iso.write(str(out))
        iso.close()

        result = parse_disc_languages_iso(out, "bdmv")
        assert result == {"audio": ["fre", "eng"], "subtitle": ["fre", "eng"]}

    def test_unknown_disc_type_returns_empty(self, tmp_path):
        iso = pycdlib.PyCdlib()
        iso.new(udf="2.60")
        out = tmp_path / "empty.iso"
        iso.write(str(out))
        iso.close()

        assert parse_disc_languages_iso(out, "unknown") == {"audio": [], "subtitle": []}

    def test_missing_iso_returns_empty(self, tmp_path):
        assert parse_disc_languages_iso(tmp_path / "nope.iso", "dvd") == {"audio": [], "subtitle": []}

    def test_dvd_iso_missing_ifo_returns_empty(self, tmp_path):
        iso = pycdlib.PyCdlib()
        iso.new(udf="2.60")
        iso.add_directory(udf_path="/VIDEO_TS")
        # VOB exists but no IFO
        iso.add_fp(_io.BytesIO(b"\x00" * 100), 100, udf_path="/VIDEO_TS/VTS_01_1.VOB")
        out = tmp_path / "no_ifo.iso"
        iso.write(str(out))
        iso.close()

        assert parse_disc_languages_iso(out, "dvd") == {"audio": [], "subtitle": []}


class TestParseDiscLanguagesDispatcher:
    def test_folder_path_routes_to_existing_logic(self, tmp_path):
        """Folder path should hit the v0.6.5 folder dispatcher unchanged."""
        disc_root = tmp_path / "Movie"
        video_ts = disc_root / "VIDEO_TS"
        video_ts.mkdir(parents=True)
        (video_ts / "VTS_01_0.IFO").write_bytes(
            _build_dvd_ifo(audio_langs=[b"en"], subp_langs=[])
        )
        (video_ts / "VTS_01_1.VOB").write_bytes(b"\x00" * 100)
        result = parse_disc_languages(disc_root, "dvd")
        assert result == {"audio": ["eng"], "subtitle": []}

    def test_iso_path_routes_to_iso_logic(self, tmp_path):
        iso = pycdlib.PyCdlib()
        iso.new(udf="2.60")
        iso.add_directory(udf_path="/VIDEO_TS")
        ifo_data = _build_dvd_ifo(audio_langs=[b"de"], subp_langs=[])
        iso.add_fp(_io.BytesIO(ifo_data), len(ifo_data), udf_path="/VIDEO_TS/VTS_01_0.IFO")
        iso.add_fp(_io.BytesIO(b"\x00" * 100), 100, udf_path="/VIDEO_TS/VTS_01_1.VOB")
        out = tmp_path / "test.iso"
        iso.write(str(out))
        iso.close()
        result = parse_disc_languages(out, "dvd")
        assert result == {"audio": ["ger"], "subtitle": []}

    def test_nonexistent_path_returns_empty(self, tmp_path):
        assert parse_disc_languages(tmp_path / "missing", "dvd") == {"audio": [], "subtitle": []}
