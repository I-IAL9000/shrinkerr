"""Tests for v0.9.7 external-subtitle language detection.

External subs encode their language in the filename, so "writing back" a
detected language means renaming the sidecar (Movie.srt -> Movie.eng.srt).
"""
from backend.routes.scan import _rename_external_sub_with_lang


def test_rename_inserts_lang_before_extension(tmp_path):
    srt = tmp_path / "1 (2013) 1080p WEBDL h265-Radarr.srt"
    srt.write_text("subtitle text")
    new_path = _rename_external_sub_with_lang(str(srt), "eng")
    assert new_path == str(tmp_path / "1 (2013) 1080p WEBDL h265-Radarr.eng.srt")
    assert not srt.exists()
    assert (tmp_path / "1 (2013) 1080p WEBDL h265-Radarr.eng.srt").exists()


def test_rename_preserves_ass_extension(tmp_path):
    ass = tmp_path / "Movie.ass"
    ass.write_text("[Script Info]")
    new_path = _rename_external_sub_with_lang(str(ass), "jpn")
    assert new_path == str(tmp_path / "Movie.jpn.ass")


def test_rename_refuses_to_clobber_existing(tmp_path):
    srt = tmp_path / "Movie.srt"
    srt.write_text("a")
    (tmp_path / "Movie.eng.srt").write_text("existing")
    assert _rename_external_sub_with_lang(str(srt), "eng") is None
    # Original left untouched.
    assert srt.exists()
    assert (tmp_path / "Movie.eng.srt").read_text() == "existing"


def test_rename_missing_file_returns_none(tmp_path):
    assert _rename_external_sub_with_lang(str(tmp_path / "nope.srt"), "eng") is None


def test_rename_vobsub_renames_idx_and_sub_pair(tmp_path):
    idx = tmp_path / "Movie.idx"
    sub = tmp_path / "Movie.sub"
    idx.write_text("idx data")
    sub.write_bytes(b"sub bitmap data")
    new_path = _rename_external_sub_with_lang(str(idx), "eng")
    assert new_path == str(tmp_path / "Movie.eng.idx")
    assert (tmp_path / "Movie.eng.idx").exists()
    assert (tmp_path / "Movie.eng.sub").exists()   # partner renamed too
    assert not idx.exists() and not sub.exists()


def test_rename_vobsub_refuses_if_partner_target_exists(tmp_path):
    idx = tmp_path / "Movie.idx"
    sub = tmp_path / "Movie.sub"
    idx.write_text("a"); sub.write_bytes(b"b")
    (tmp_path / "Movie.eng.sub").write_bytes(b"existing")   # partner collision
    assert _rename_external_sub_with_lang(str(idx), "eng") is None
    # Nothing moved.
    assert idx.exists() and sub.exists()
