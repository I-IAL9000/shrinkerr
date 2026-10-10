"""Nodes → "Add a remote worker" (v0.10.0): a copy-ready command with the
server's address, API key and the image matching its version — docker run
or compose, for NVIDIA, Intel/AMD and CPU-only machines."""
import json
import shlex
import shutil
import subprocess
from pathlib import Path

import aiosqlite
import pytest

from backend.routes.nodes import worker_image_tags

ROOT = Path(__file__).resolve().parents[2]


def test_the_image_matches_the_server():
    assert worker_image_tags("0.10.0", "n8.1") == {"cpu": "v0.10.0", "intel_amd": "v0.10.0", "nvidia": "v0.10.0-nvenc"}
    assert worker_image_tags("0.10.0-dev.42+1a2b3c4", "n8.1")["nvidia"] == "develop-nvenc"
    assert worker_image_tags("0.10.0-dev.42+1a2b3c4", "master") == {
        "cpu": "develop-edge", "intel_amd": "develop-edge", "nvidia": "develop-edge-nvenc"}
    assert worker_image_tags("0.10.0-rc.1", "") ["cpu"] == "v0.10.0-rc.1"


@pytest.mark.asyncio
async def test_the_library_is_mounted_where_the_server_sees_it(test_db, monkeypatch):
    from backend.routes.nodes import worker_setup
    monkeypatch.setenv("SHRINKERR_FFMPEG_BUILD", "master")
    assert (await worker_setup())["media_root"] == "/media"  # no media folders yet
    async with aiosqlite.connect(test_db) as db:
        await db.executemany("INSERT INTO media_dirs (path, enabled) VALUES (?, ?)",
                             [("/data/media/Movies/", 1), ("/data/media/TV", 1), ("/elsewhere", 0)])
        await db.commit()
    got = await worker_setup()
    assert got["media_root"] == "/data/media" and got["tags"]["cpu"].endswith("-edge")
    assert got["image"] == "ghcr.io/i-ial9000/shrinkerr"


def _node_runs_typescript() -> bool:
    node = shutil.which("node")
    return bool(node) and subprocess.run([node, "--experimental-strip-types", "--no-warnings", "-e", "1"],
                                         capture_output=True).returncode == 0


needs_node = pytest.mark.skipif(not _node_runs_typescript(), reason="needs Node 22.6+")


def _command(**opts) -> str:
    base = {"gpu": "cpu", "format": "run", "image": "ghcr.io/i-ial9000/shrinkerr", "tag": "v0.10.0",
            "serverUrl": "http://192.168.1.5:6680", "apiKey": "k$ey", "workerName": "", "hostMediaPath": "",
            "mediaRoot": "/media"}
    script = (f"import({json.dumps(str(ROOT / 'frontend/src/workerCommand.ts'))})"
              f".then(m => process.stdout.write(m.workerCommand({json.dumps({**base, **opts})})))")
    return subprocess.run(["node", "--experimental-strip-types", "--no-warnings", "-e", script],
                          capture_output=True, text=True, check=True).stdout


@needs_node
def test_docker_run_is_one_valid_shell_command():
    cmd = _command(gpu="nvidia", workerName="gaming pc", hostMediaPath="/mnt/my media")
    words = shlex.split(cmd.replace("\\\n", " "))
    assert words[:3] == ["docker", "run", "-d"] and words[-1] == "ghcr.io/i-ial9000/shrinkerr:v0.10.0"
    assert {"API_KEY=k$ey", "WORKER_NAME=gaming pc", "SERVER_URL=http://192.168.1.5:6680",
            "SHRINKERR_MODE=worker", "/mnt/my media:/media"} <= set(words)
    assert "--gpus" in words and "/dev/dri:/dev/dri" not in words
    intel = shlex.split(_command(gpu="intel_amd").replace("\\\n", " "))
    assert "/dev/dri:/dev/dri" in intel and "--gpus" not in intel and "render" in intel


@needs_node
@pytest.mark.parametrize("gpu", ["nvidia", "intel_amd", "cpu"])
def test_compose_is_valid_yaml(gpu):
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(_command(format="compose", gpu=gpu, workerName="nas: box", hostMediaPath="/mnt/my media"))
    svc = doc["services"]["shrinkerr-worker"]
    assert svc["image"] == "ghcr.io/i-ial9000/shrinkerr:v0.10.0"
    assert "WORKER_NAME=nas: box" in svc["environment"] and "API_KEY=k$ey" in svc["environment"]
    assert svc["volumes"] == ["/mnt/my media:/media", "shrinkerr-worker-data:/app/data"]
    assert "shrinkerr-worker-data" in doc["volumes"]
    assert ("deploy" in svc) == (gpu == "nvidia")
    assert ("devices" in svc) == (gpu == "intel_amd")
