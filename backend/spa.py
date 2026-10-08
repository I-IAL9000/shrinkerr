"""Static-file lookup for the SPA catch-all route (v0.9.155).

The route sits outside the /api auth gate, and it used to join the raw
request path onto frontend/dist — "/..%2f..%2fdata%2fshrinkerr.db" served
the database to anyone. Only files that resolve inside dist are served.
"""
from pathlib import Path
from typing import Optional


def spa_file(dist: Path, full_path: str) -> Optional[Path]:
    """The file under `dist` to serve for `full_path`, or None (→ index.html)."""
    if not full_path:
        return None
    root = dist.resolve()
    candidate = (root / full_path).resolve()
    if candidate.is_relative_to(root) and candidate.is_file():
        return candidate
    return None
