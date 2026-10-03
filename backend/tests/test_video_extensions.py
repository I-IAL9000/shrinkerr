"""v0.9.147: MPEG program streams (.mpg/.mpeg) and other legacy containers
were never picked up by the scanner/watcher — only .mkv/.mp4/.avi/.m4v/.mov/
.ts/.m2ts were — even though the rest of Shrinkerr (untaggable-container
handling, MPEG-2/WMV source codecs) supports them. Reported: a .mpg title
in the library simply didn't show up."""
from backend.config import Settings
from backend.scanner import detect_external_subtitles


def test_default_extensions_include_legacy_containers():
    exts = set(Settings().video_extensions)
    for e in (".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".m2ts",
              ".mpg", ".mpeg", ".wmv", ".flv", ".webm", ".asf"):
        assert e in exts, e
    # DVD .vob files are handled as disc structures (VIDEO_TS), never one by one.
    assert ".vob" not in exts


def test_single_mpg_in_folder_gets_its_subtitle(tmp_path):
    """The external-sub matcher's "only one video in this folder" rule must
    count a .mpg as the folder's video (it used its own shorter list)."""
    video = tmp_path / "Aberne (1995) 480p MPEG.mpg"
    video.write_bytes(b"x")
    (tmp_path / "Aberne.ice.srt").write_bytes(b"1\n00:00:01,000 --> 00:00:02,000\nHallo\n")
    found = detect_external_subtitles(str(video))
    assert [s["language"] for s in found] == ["ice"]


def test_one_default_source_codec_list_everywhere():
    """Never-saved "Convert from" setting: every reader must agree (the full
    scan, add-by-path and webhooks used ["h264"]; Settings/watcher used this)."""
    import ast, pathlib
    from backend.scanner import DEFAULT_SOURCE_CODECS
    assert DEFAULT_SOURCE_CODECS == ["h264", "mpeg2", "mpeg4", "vc1"]
    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for path in list(root.glob("*.py")) + list((root / "routes").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "source_codecs" for t in node.targets
            ) and isinstance(node.value, ast.List):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], f"hard-coded source_codecs defaults: {offenders}"
