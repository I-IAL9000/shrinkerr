"""v0.9.43: manual track-language override endpoint."""
import pytest
from fastapi import HTTPException
import backend.routes.scan as scan_mod


@pytest.mark.asyncio
async def test_set_track_language_rejects_blank_or_und():
    for bad in ("", "und", "   "):
        with pytest.raises(HTTPException):
            await scan_mod.set_track_language(scan_mod.SetTrackLanguageRequest(
                file_path="/m/x.mkv", track_type="audio", stream_index=1, language=bad))
