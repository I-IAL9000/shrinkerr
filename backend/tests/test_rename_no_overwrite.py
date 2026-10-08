"""v0.9.155: renaming must never replace an existing file or folder.

Two editions (or CD1/CD2, PROPER/non-PROPER) of a film produce the same new
name, and os.rename() silently replaced the first with the second — on the
NAS share that also sends the replaced file to the recycle bin.
"""
import pytest

from backend.rename import RenamePlan, apply_plan


@pytest.mark.asyncio
async def test_file_rename_refuses_to_overwrite(tmp_path):
    a = tmp_path / "Blade Runner (1982) Theatrical.mkv"
    b = tmp_path / "Blade Runner (1982) Final Cut.mkv"
    a.write_bytes(b"theatrical")
    b.write_bytes(b"final cut")
    target = tmp_path / "Blade Runner (1982).mkv"

    first = await apply_plan(RenamePlan(old_path=str(a), new_path=str(target)))
    second = await apply_plan(RenamePlan(old_path=str(b), new_path=str(target)))

    assert first["applied"] is True
    assert second["applied"] is False and second["error"]
    assert target.read_bytes() == b"theatrical"
    assert b.read_bytes() == b"final cut"  # left in place, not lost


@pytest.mark.asyncio
async def test_folder_rename_refuses_to_overwrite(tmp_path):
    show = tmp_path / "Show"
    (show / "Season 1").mkdir(parents=True)
    ep = show / "Season 1" / "ep.mkv"
    ep.write_bytes(b"x")
    (tmp_path / "Show (2019)").mkdir()  # an existing folder with the target name
    (tmp_path / "Show (2019)" / "keep.mkv").write_bytes(b"other show")

    result = await apply_plan(RenamePlan(
        old_path=str(ep), new_path=str(ep),
        old_folder=str(show), new_folder=str(tmp_path / "Show (2019)")))

    assert result["applied"] is False and result["error"]
    assert (tmp_path / "Show (2019)" / "keep.mkv").read_bytes() == b"other show"
    assert ep.exists()
