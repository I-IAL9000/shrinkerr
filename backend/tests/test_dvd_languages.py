"""v0.10.0 (SC-21): a DVD's IFO languages were applied by stream position,
but ffprobe lists streams in the order their first packet appears — a
track that starts later swapped labels with the one before it."""
import shutil
import subprocess

import pytest

from backend.scanner import _dvd_logical_index, probe_file
from backend.tests.test_disc_release_folder import _dvd


@pytest.mark.parametrize("sid, kind, expected", [
    ("0x80", "audio", 0), ("0x81", "audio", 1), ("0x89", "audio", 1), ("0xa2", "audio", 2),
    ("0x1c1", "audio", 1), ("0x20", "subtitle", 0), ("0x23", "subtitle", 3), ("0x1e0", "audio", None),
    (None, "audio", None),
])
def test_dvd_logical_index(sid, kind, expected):
    assert _dvd_logical_index(sid, kind) == expected


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
async def test_languages_follow_the_stream_id(tmp_path, monkeypatch):
    marker = _dvd(tmp_path / "Film (1999)")
    # Logical audio 0 (stream 0x80) only starts at 2 s, so ffprobe meets 0x81 first.
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=720x480:rate=25:duration=4",
         "-itsoffset", "2", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=880:duration=4",
         "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "mpeg2video", "-c:a", "ac3",
         "-f", "vob", str(marker.parent / "VTS_01_1.VOB")], check=True)
    import backend.disc_metadata as disc_metadata
    monkeypatch.setattr(disc_metadata, "parse_disc_languages",
                        lambda folder, kind: {"audio": ["eng", "fre"], "subtitle": []})
    probe = await probe_file(str(marker))
    ids = sorted((t["language"], t["stream_index"]) for t in probe["audio_tracks"])
    assert [lang for lang, _ in ids] == ["eng", "fre"]
    by_index = {t["stream_index"]: t["language"] for t in probe["audio_tracks"]}
    assert "_stream_id" not in probe["audio_tracks"][0]
    # whichever comes first in the probe, the 0x80 stream is English
    import json
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index,id",
                          "-of", "json", "-analyzeduration", "200M", "-probesize", "200M",
                          str(marker.parent / "VTS_01_1.VOB")], capture_output=True, text=True).stdout
    id_of = {s["index"]: s["id"] for s in json.loads(out)["streams"]}
    assert {by_index[i] for i, sid in id_of.items() if sid == "0x80"} == {"eng"}
