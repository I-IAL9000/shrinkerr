"""The scan process's writes must land whatever the server's connections do.

A forked scan process inherited SQLite's record of the server's locks without
the locks themselves. When the server's last connection closed mid-scan it
checkpointed and deleted the WAL, and every write the scan made from then on
was lost: a folder rescan logged "Written N results" and left the old rows in
place (stale track lists failed clean-ups as "tracks changed"). With the server
busy, the scan's writes into the server's new WAL corrupted the database.
"""
import sqlite3

import backend.routes.scan as scan_mod


def _write_once_told(db_path, attached, go):
    con = sqlite3.connect(db_path, timeout=10)
    con.execute("SELECT count(*) FROM t").fetchone()   # attached, like the scan's first batch
    attached.set()
    go.wait(30)                                        # the probing
    con.execute("INSERT INTO t VALUES ('scan')")
    con.commit()
    con.close()


def test_the_scan_process_keeps_its_writes_when_the_servers_connections_close(tmp_path):
    db_path = str(tmp_path / "shrinkerr.db")
    setup = sqlite3.connect(db_path)
    setup.execute("PRAGMA journal_mode=WAL")
    setup.execute("CREATE TABLE t (v TEXT)")
    setup.commit()
    setup.close()

    ctx = scan_mod._SCAN_MP
    attached, go = ctx.Event(), ctx.Event()
    server = sqlite3.connect(db_path)                  # a request mid-flight as the scan starts
    server.execute("SELECT count(*) FROM t").fetchone()
    proc = ctx.Process(target=_write_once_told, args=(db_path, attached, go), daemon=True)
    proc.start()
    try:
        assert attached.wait(60)
        server.close()                                 # the server's last connection closes
        later = sqlite3.connect(db_path)               # and the server carries on
        later.execute("INSERT INTO t VALUES ('server')")
        later.commit()
        later.close()
        go.set()
        proc.join(60)
        assert proc.exitcode == 0
    finally:
        if proc.is_alive():
            proc.kill()

    check = sqlite3.connect(db_path)
    rows = sorted(v for (v,) in check.execute("SELECT v FROM t"))
    check.close()
    assert rows == ["scan", "server"]
