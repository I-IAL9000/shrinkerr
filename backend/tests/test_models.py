import pytest
import aiosqlite

@pytest.mark.asyncio
async def test_db_tables_created(test_db):
    db = await aiosqlite.connect(test_db)
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row["name"] for row in await cursor.fetchall()]
    await db.close()
    assert "media_dirs" in tables
    assert "scan_results" in tables
    assert "jobs" in tables
    assert "settings" in tables
    assert "schedule" in tables

from backend.models import AudioTrack, ScannedFile, JobCreate

def test_audio_track_defaults():
    track = AudioTrack(stream_index=1, language="eng", codec="ac3", channels=6)
    assert track.keep is True
    assert track.locked is False
    assert track.title == ""

def test_scanned_file():
    sf = ScannedFile(
        file_path="/media/test.mkv", file_name="test.mkv", folder_name="Test",
        file_size=4_500_000_000, file_size_gb=4.19, video_codec="h264",
        needs_conversion=True, audio_tracks=[], native_language="eng",
        has_removable_tracks=False, estimated_savings_bytes=1_350_000_000,
        estimated_savings_gb=1.26,
    )
    assert sf.needs_conversion is True

def test_job_create_defaults():
    job = JobCreate(file_path="/media/test.mkv", job_type="convert")
    assert job.encoder == "nvenc"
    assert job.audio_tracks_to_remove == []
