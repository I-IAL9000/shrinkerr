"""The index on disk: what's loaded is what was saved, and a damaged or
foreign file loads as nothing (the index is then rebuilt)."""
from backend.filter_index.bits import from_ids
from backend.filter_index.index import FilterIndex
from backend.scan_filters import _FILTER_LIST


def _index() -> FilterIndex:
    index = FilterIndex(version="v1", db_id="db", seq=42, counters={"jobs": 3, "ignored": 1}, time_at=1700000000.5)
    index.alive = from_ids(range(0, 300_000, 3))
    index.bits = {f.id: from_ids(range(n, 300_000, 97 + n)) for n, f in enumerate(_FILTER_LIST)}
    index.bits["x264"] = 0  # an empty one
    index.savings = {3: 1_000_000_000, 299_997: 7}
    return index


def test_saving_and_loading(tmp_path):
    path = str(tmp_path / "filter-index.bin")
    saved = _index()
    saved.save(path)
    assert FilterIndex.load(path) == saved


def test_a_damaged_or_foreign_file_loads_as_nothing(tmp_path):
    path = tmp_path / "filter-index.bin"
    assert FilterIndex.load(str(path)) is None  # none yet
    _index().save(str(path))
    data = path.read_bytes()
    for broken in (data[:-10], data[:len(data) // 2], data[:20] + b"x" + data[21:],
                   data[:-1] + bytes([data[-1] ^ 1]), b"something else entirely\n" + data):
        path.write_bytes(broken)
        assert FilterIndex.load(str(path)) is None
