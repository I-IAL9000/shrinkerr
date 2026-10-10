"""Prometheus metrics and the dashboard widget (v0.10.0)."""
import re

import aiosqlite
import pytest
import pytest_asyncio

LINE = re.compile(r'^(# (HELP|TYPE) \w+ .+|[a-z_]+(\{[a-z_]+="(?:[^"\\]|\\.)*"\})? -?[0-9.]+)$')


@pytest_asyncio.fixture
async def data(test_db):
    async with aiosqlite.connect(test_db) as db:
        for status, saved, orig, vmaf in (("completed", 600, 1000, 94.0), ("completed", 400, 1000, 90.0),
                                          ("pending", 0, 0, None), ("failed", 0, 0, None)):
            await db.execute("INSERT INTO jobs (file_path, job_type, status, created_at, space_saved, original_size, "
                             "vmaf_score) VALUES (?, 'convert', ?, 'x', ?, ?, ?)", (f"/m/{status}{saved}", status, saved, orig, vmaf))
        await db.execute("UPDATE jobs SET cleared = 1 WHERE space_saved = 400")  # cleared from the Queue: still counts
        await db.executemany("INSERT INTO scan_results (file_path, file_size, needs_conversion, video_conv_savings_bytes, "
                             "scan_timestamp) VALUES (?, ?, ?, ?, 'x')",
                             [("/m/a.mkv", 5000, 1, 2000), ("/m/b.mkv", 3000, 0, 0)])
        await db.executemany("INSERT INTO worker_nodes (id, name, status, registered_at) VALUES (?, ?, ?, 'x')",
                             [("n1", 'gpu "box"', "online"), ("n2", "old", "offline")])
        await db.commit()
    return test_db


@pytest.mark.asyncio
async def test_prometheus_text(data):
    from backend.routes.metrics import collect, render
    text = render(await collect())
    for line in text.strip().splitlines():
        assert LINE.match(line), line
    assert 'shrinkerr_jobs{status="completed"} 2' in text and 'shrinkerr_jobs{status="pending"} 1' in text
    assert "shrinkerr_saved_bytes_total 1000\n" in text and "shrinkerr_original_bytes_total 2000\n" in text
    assert "shrinkerr_vmaf_average 92.0\n" in text and "shrinkerr_encode_fps 0\n" in text
    assert "shrinkerr_library_files 2\n" in text and "shrinkerr_files_to_convert 1\n" in text
    assert "shrinkerr_estimated_savings_bytes 2000\n" in text
    assert 'shrinkerr_node_up{node="gpu \\"box\\""} 1' in text and 'shrinkerr_node_up{node="old"} 0' in text


@pytest.mark.asyncio
async def test_the_widget(data):
    from backend.routes.metrics import widget
    got = await widget()
    assert (got["pending"], got["completed"], got["failed"], got["saved_bytes"], got["saved_percent"]) == (1, 2, 1, 1000, 50.0)
    assert (got["library_files"], got["to_convert"], got["nodes_online"], got["nodes"]) == (2, 1, 1, 2)
