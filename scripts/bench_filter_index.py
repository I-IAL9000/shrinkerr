"""How fast the Scanner's filter index is on a synthetic library (by hand,
not in CI):

    python scripts/bench_filter_index.py /tmp/shrinkerr-bench.db

The first run makes the library: the demo's files copied to ~166K. The
spec's targets: counts after a click < 150 ms, counts after a restart
< 300 ms, 1,000 changed files < 500 ms, a full build < 20 s."""
import asyncio
import os
import sqlite3
import sys
import time

DB = os.path.abspath(sys.argv[1])
os.environ["SHRINKERR_DB_PATH"] = DB
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.config  # noqa: E402
from backend.config import Settings  # noqa: E402

backend.config.settings = Settings(db_path=DB, media_root=os.path.dirname(DB))
import backend.database as database  # noqa: E402

database.DB_PATH = DB
from backend import demo  # noqa: E402
from backend.filter_index import service  # noqa: E402
from backend.scan_filters import parse_filter  # noqa: E402

FILES = 166_000


async def make() -> None:
    await database.init_db()
    n = await demo.seed(DB)
    con = sqlite3.connect(DB)
    cols = [r[1] for r in con.execute("PRAGMA table_info(scan_results)") if r[1] != "id"]
    pick = ", ".join("file_path || '.copy' || ?" if c == "file_path" else c for c in cols)
    for i in range(1, FILES // n):
        con.execute(f"INSERT INTO scan_results ({', '.join(cols)}) SELECT {pick} FROM scan_results WHERE id <= ?",
                    (i, n))
    con.commit()
    con.close()


def report(label: str, started: float, target: float) -> None:
    took = time.perf_counter() - started
    print(f"{label:40} {took * 1000:9.1f} ms   target {target * 1000:7.0f} ms   {'ok' if took <= target else 'SLOW'}")


async def main() -> None:
    if not os.path.exists(DB):
        print("Making the library…")
        await make()
    await database.init_db()
    if os.path.exists(service.index_path()):
        os.remove(service.index_path())
    service.reset()
    started = time.perf_counter()
    await service.current()
    await service.wait_rebuilt()
    report("full build", started, 20)
    for text in ("x264", "x264,res_1080p,!ignored", "needs_conversion,!ignored,plex_watched"):
        started = time.perf_counter()
        await service.counts(parse_filter(text))
        report(f"counts: {text}", started, 0.15)
    await service.save(force=True)
    service.reset()
    started = time.perf_counter()
    await service.load()
    await service.counts(parse_filter("x264"))
    report("restart: load, then counts", started, 0.3)
    con = sqlite3.connect(DB)
    con.execute("UPDATE scan_results SET file_size = file_size + 1 "
                "WHERE id IN (SELECT id FROM scan_results ORDER BY id LIMIT 1000 OFFSET 5000)")
    con.commit()
    con.close()
    started = time.perf_counter()
    await service.refresh()
    report("1,000 changed files", started, 0.5)


asyncio.run(main())
