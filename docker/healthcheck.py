"""Container health check (v0.10.0).

The server is healthy when /api/health answers. A remote worker
(SHRINKERR_MODE=worker) serves nothing on 6680, so it always showed as
unhealthy; it's healthy while its last successful heartbeat to the server
is recent (backend/worker_mode.py touches ALIVE_FILE after each one).
"""
import os
import sys
import tempfile
import time
import urllib.request

ALIVE_FILE = os.path.join(tempfile.gettempdir(), "shrinkerr-worker-alive")


def healthy() -> bool:
    if os.environ.get("SHRINKERR_MODE", "").strip().lower() == "worker":
        try:
            age = time.time() - os.path.getmtime(ALIVE_FILE)
        except OSError:
            return False
        try:
            interval = float(os.environ.get("HEARTBEAT_INTERVAL") or 30)
        except ValueError:
            interval = 30
        return age < max(180, 3 * interval)
    try:
        urllib.request.urlopen("http://localhost:6680/api/health", timeout=4)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(0 if healthy() else 1)
