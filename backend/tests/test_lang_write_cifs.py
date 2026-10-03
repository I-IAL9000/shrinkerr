"""v0.9.145: writing a track language into a non-mkv file replaced the
original with `os.replace(tmp, original)`. On the user's CIFS/SMB share that
rename-over-an-existing-file fails with EACCES ("Permission denied"), so the
language was only kept as pending. Fall back to: move the original aside,
place the new file, then delete the original (restoring it on failure) —
the same staging the conversion/remux finalizers use since v0.9.126/127.
"""
import asyncio
import os
import shutil
import subprocess

import pytest

from backend import language_detection as ld

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def _make_mp4(path):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
         "-t", "1", "-c:v", "libx264", "-c:a", "aac", "-metadata:s:a:0", "language=und", str(path)],
        check=True,
    )


def _audio_lang(path):
    return subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream_tags=language", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    ).stdout.strip().strip(",")


def _refuse_replace_onto(target, monkeypatch):
    """Simulate CIFS: renaming ONTO the existing original is refused."""
    real = os.replace
    def fake(src, dst):
        if os.path.abspath(dst) == os.path.abspath(target) and os.path.exists(dst):
            raise PermissionError(13, "Permission denied", str(src), None, str(dst))
        return real(src, dst)
    monkeypatch.setattr(ld.os, "replace", fake)


def test_cifs_refused_replace_falls_back_to_staging(tmp_path, monkeypatch):
    f = tmp_path / "Movie (2018).mp4"
    _make_mp4(f)
    _refuse_replace_onto(f, monkeypatch)

    assert asyncio.run(ld.apply_track_languages_to_file(str(f), ["ice"], [])) is True
    assert _audio_lang(f) == "ice"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != f.name]
    assert leftovers == []  # no temp file, no staging dir left behind


def test_failed_placement_restores_the_original(tmp_path, monkeypatch):
    f = tmp_path / "Movie (2018).mp4"
    _make_mp4(f)
    original_bytes = f.read_bytes()
    _refuse_replace_onto(f, monkeypatch)
    # ...and placing the new file also fails (share fully read-only-ish).
    real_rename = os.rename
    def fake_rename(src, dst):
        if os.path.basename(str(src)).startswith(".shrinkerr_lang_"):
            raise PermissionError(13, "Permission denied")
        return real_rename(src, dst)
    monkeypatch.setattr(ld.os, "rename", fake_rename)

    assert asyncio.run(ld.apply_track_languages_to_file(str(f), ["ice"], [])) is False
    assert f.read_bytes() == original_bytes  # original back in place, untouched
    assert [p.name for p in tmp_path.iterdir()] == [f.name]


def test_file_in_use_leaves_everything_untouched(tmp_path, monkeypatch, capsys):
    """Open on the share (playing in Plex): even moving the original aside is
    refused. Nothing may change, and the log should say why."""
    f = tmp_path / "Movie (2018).mp4"
    _make_mp4(f)
    original_bytes = f.read_bytes()
    _refuse_replace_onto(f, monkeypatch)
    real_rename = os.rename
    def fake_rename(src, dst):
        if os.path.abspath(str(src)) == os.path.abspath(str(f)):
            raise PermissionError(13, "Permission denied")
        return real_rename(src, dst)
    monkeypatch.setattr(ld.os, "rename", fake_rename)

    assert asyncio.run(ld.apply_track_languages_to_file(str(f), ["ice"], [])) is False
    assert f.read_bytes() == original_bytes
    assert [p.name for p in tmp_path.iterdir()] == [f.name]
    assert "may be in use" in capsys.readouterr().out


def test_never_renames_onto_an_existing_file(tmp_path, monkeypatch):
    """v0.9.146: on the user's NAS a rename ONTO an existing file sent the
    original to the share's recycle bin and then failed — losing it. The
    writer must only ever rename into a name that is currently free."""
    f = tmp_path / "Movie (2018).mp4"
    _make_mp4(f)

    def no_replace(*a, **k):
        raise AssertionError("os.replace (rename-over-existing) must not be used")
    monkeypatch.setattr(ld.os, "replace", no_replace)
    real_rename = os.rename
    def checked_rename(src, dst):
        assert not os.path.exists(dst), f"rename onto existing file: {dst}"
        return real_rename(src, dst)
    monkeypatch.setattr(ld.os, "rename", checked_rename)

    assert asyncio.run(ld.apply_track_languages_to_file(str(f), ["ice"], [])) is True
    assert _audio_lang(f) == "ice"
    assert [p.name for p in tmp_path.iterdir()] == [f.name]


def test_leftover_in_staging_dir_is_never_touched(tmp_path, monkeypatch):
    """A same-named file already in .shrinkerr-replacing (e.g. stranded by a
    crash) must be neither overwritten nor deleted."""
    f = tmp_path / "Movie (2018).mp4"
    _make_mp4(f)
    stage = tmp_path / ".shrinkerr-replacing"
    stage.mkdir()
    (stage / f.name).write_bytes(b"stranded original")
    real_rename = os.rename
    def checked_rename(src, dst):
        assert not os.path.exists(dst), f"rename onto existing file: {dst}"
        return real_rename(src, dst)
    monkeypatch.setattr(ld.os, "rename", checked_rename)

    assert asyncio.run(ld.apply_track_languages_to_file(str(f), ["ice"], [])) is True
    assert (stage / f.name).read_bytes() == b"stranded original"
    assert _audio_lang(f) == "ice"
