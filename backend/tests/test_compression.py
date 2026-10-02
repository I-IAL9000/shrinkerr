"""v0.9.139: gzip API/text responses, but not already-compressed downloads.

The pending-queue list for 5,000 jobs was 4.3 MB of uncompressed JSON and
is re-fetched whenever the queue changes.
"""
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from fastapi.testclient import TestClient

from backend.compression import SelectiveGZipMiddleware

BIG_JSON = [{"id": i, "file_path": f"/media/show/{i}.mkv", "error_log": None} for i in range(500)]


def _client() -> TestClient:
    app = FastAPI()
    app.add_middleware(SelectiveGZipMiddleware)

    @app.get("/api/jobs/")
    async def jobs():
        return JSONResponse(BIG_JSON)

    @app.get("/api/tiny")
    async def tiny():
        return {"ok": True}

    @app.get("/api/settings/backup/download/{name}")
    async def backup(name: str):
        return Response(b"PK" + b"x" * 5000, media_type="application/zip")

    @app.get("/logo.png")
    async def logo():
        return Response(b"\x89PNG" + b"x" * 5000, media_type="image/png")

    return TestClient(app)


def test_json_list_is_gzipped_and_round_trips():
    r = _client().get("/api/jobs/", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip"
    assert r.json() == BIG_JSON  # httpx decompresses transparently


def test_small_responses_are_left_alone():
    r = _client().get("/api/tiny", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in r.headers


def test_already_compressed_downloads_are_not_recompressed():
    c = _client()
    for path in ("/api/settings/backup/download/shrinkerr-backup.zip", "/logo.png"):
        r = c.get(path, headers={"Accept-Encoding": "gzip"})
        assert "content-encoding" not in r.headers, path


def test_clients_without_gzip_get_plain_json():
    r = _client().get("/api/jobs/", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in r.headers
    assert r.json() == BIG_JSON
