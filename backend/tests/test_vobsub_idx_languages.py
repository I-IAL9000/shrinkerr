"""A VobSub .idx names its streams' languages ("id: it, index: 0"), as players
read them (v0.10.0). The Consequences of Love's .idx/.sub holds Italian,
English and French: Shrinkerr listed one UND track, OCR'd it for minutes and
found nothing, and a conversion merged all three streams with the first
re-tagged und."""
from backend.audio import build_remux_cmd
from backend.converter import _build_ffmpeg_cmd_impl, delete_merged_external_subs
from backend.scanner import detect_external_subtitles, merge_external_subs, vobsub_stream_languages

HEADER = "# VobSub index file, v7 (do not modify this line!)\nsize: 720x576\npalette: 000000, f0f0f0\n"
THREE = HEADER + """id: it, index: 0
timestamp: 00:00:01:000, filepos: 000000000
id: en, index: 1
timestamp: 00:00:01:000, filepos: 000000800
id: fr, index: 2
timestamp: 00:00:01:000, filepos: 000001000
"""


def _pair(folder, name="Film", idx=THREE):
    folder.mkdir(exist_ok=True)
    (folder / f"{name}.mkv").write_bytes(b"")
    (folder / f"{name}.idx").write_text(idx)
    (folder / f"{name}.sub").write_bytes(b"\x00")
    return folder / f"{name}.mkv", str(folder / f"{name}.idx")


def test_the_idx_names_its_streams(tmp_path):
    _, idx = _pair(tmp_path)
    assert vobsub_stream_languages(idx) == ["ita", "eng", "fre"]
    _, unnamed = _pair(tmp_path, "Other", HEADER + "id: --, index: 0\n")
    assert vobsub_stream_languages(unnamed) == ["und"]
    _, none = _pair(tmp_path, "None", HEADER)
    assert vobsub_stream_languages(none) == []


def test_one_track_per_stream(tmp_path):
    video, idx = _pair(tmp_path / "Film (2004)")
    found = detect_external_subtitles(str(video))
    assert sorted((t["language"], t["idx_stream"]) for t in found) == [("eng", 1), ("fre", 2), ("ita", 0)]
    assert {t["external_path"] for t in found} == {idx} and {t["codec"] for t in found} == {"dvd_subtitle"}


def test_the_file_name_names_an_unnamed_stream(tmp_path):
    video, _ = _pair(tmp_path / "Film (2004)", idx=HEADER + "id: --, index: 0\n")
    (tmp_path / "Film (2004)" / "Film.idx").rename(tmp_path / "Film (2004)" / "Film.eng.idx")
    (tmp_path / "Film (2004)" / "Film.sub").rename(tmp_path / "Film (2004)" / "Film.eng.sub")
    [track] = detect_external_subtitles(str(video))
    assert (track["language"], track["idx_stream"]) == ("eng", 0)


def test_a_stored_single_track_becomes_three(tmp_path):
    video, idx = _pair(tmp_path / "Film (2004)")
    stored = [{"stream_index": -1, "language": "und", "codec": "dvd_subtitle", "external": True, "external_path": idx}]
    changed, subs, has_ext, _ = merge_external_subs(stored, "ita", detect_external_subtitles(str(video)))
    assert changed and has_ext
    assert sorted((t["language"], t["idx_stream"]) for t in subs) == [("eng", 1), ("fre", 2), ("ita", 0)]
    assert len({t["stream_index"] for t in subs}) == 3
    unchanged, *_ = merge_external_subs(subs, "ita", detect_external_subtitles(str(video)))
    assert not unchanged


def test_the_merge_maps_each_stream_once():
    ext = [{"path": "/m/Film.idx", "codec": "dvd_subtitle", "language": lang, "idx_stream": n}
           for n, lang in ((0, "ita"), (2, "fre"))]  # English not kept
    ext.append({"path": "/m/Film.eng.srt", "codec": "subrip", "language": "eng"})
    convert = " ".join(_build_ffmpeg_cmd_impl("/m/in.mkv", "/m/out.mkv", encoder="libx265", external_subtitle_files=ext))
    remux = " ".join(build_remux_cmd("/m/in.mkv", "/m/out.mkv", [1], external_subtitle_files=ext))
    for cmd, optional in ((convert, "?"), (remux, "")):
        assert cmd.count("-i /m/Film.idx") == 1 and "-i /m/Film.eng.srt" in cmd
        assert f"-map 1:s:0{optional} " in cmd and f"-map 1:s:2{optional} " in cmd and "1:s:1" not in cmd
        assert f"-map 2:s{optional} " in cmd
        assert "-metadata:s:s:0 language=ita" in cmd and "-metadata:s:s:1 language=fre" in cmd
        assert "-metadata:s:s:2 language=eng" in cmd


def test_merged_pairs_go_whole(tmp_path):
    _, idx = _pair(tmp_path)
    merged = [{"path": idx, "codec": "dvd_subtitle", "language": lang, "idx_stream": n}
              for n, lang in enumerate(("ita", "eng"))]
    delete_merged_external_subs(merged, delete=True)
    assert not (tmp_path / "Film.idx").exists() and not (tmp_path / "Film.sub").exists()
