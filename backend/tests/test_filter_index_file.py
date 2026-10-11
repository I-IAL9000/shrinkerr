"""The index on disk: what's loaded is what was saved, and a damaged or
foreign file loads as nothing (the index is then rebuilt)."""
import json

from backend.filter_index.bits import from_ids
from backend.filter_index.index import _MAGIC, FilterIndex
from backend.scan_filters import _FILTER_LIST


def _index() -> FilterIndex:
    index = FilterIndex(version="v1", db_id="db", seq=42, counters={"jobs": 3, "ignored": 1}, time_at=1700000000.5)
    index.alive = from_ids(range(0, 300_000, 3))
    index.bits = {f.id: from_ids(range(n, 300_000, 97 + n)) for n, f in enumerate(_FILTER_LIST)}
    index.bits["x264"] = 0  # an empty one
    index.savings = {3: 30_000_000_001, 299_997: 7}
    return index


def test_saving_and_loading(tmp_path):
    path = str(tmp_path / "filter-index.bin")
    saved = _index()
    saved.save(path)
    assert FilterIndex.load(path) == saved


def test_a_damaged_or_foreign_file_loads_as_nothing(tmp_path):
    path = tmp_path / "filter-index.bin"
    assert FilterIndex.load(str(path)) is None  # none yet
    assert FilterIndex.load(str(tmp_path)) is None  # a directory
    _index().save(str(path))
    data = path.read_bytes()
    cases = {"cut at the end": data[:-10], "cut in half": data[:len(data) // 2],
             "magic changed": data[:20] + b"x" + data[21:], "last byte flipped": data[:-1] + bytes([data[-1] ^ 1]),
             "something before it": b"something else entirely\n" + data}
    for name, broken in cases.items():
        path.write_bytes(broken)
        assert FilterIndex.load(str(path)) is None, name


def test_a_damaged_header_loads_as_nothing(tmp_path):
    path = tmp_path / "filter-index.bin"
    _index().save(str(path))
    data = path.read_bytes()
    end = data.index(b"\n", len(_MAGIC))
    header = json.loads(data[len(_MAGIC):end])
    payload = data[end + 1:]
    sizes = [list(s) for s in header["sizes"]]
    sizes[0][1] = -1

    def with_header(**changes) -> bytes:
        return _MAGIC + json.dumps({**header, **changes}).encode() + b"\n" + payload

    cases = {"cut inside the header": data[:len(_MAGIC) + 10],
             "not JSON": _MAGIC + b"{not json}\n" + payload,
             "keys missing": _MAGIC + b'{"version": "v1"}\n' + payload,
             "nested too deep": _MAGIC + b"[" * 100_000 + b"\n" + payload,
             "sizes of the wrong type": with_header(sizes=5),
             "a negative size": with_header(sizes=sizes),
             "an odd savings count": with_header(savings=header["savings"] - 1)}
    for name, broken in cases.items():
        path.write_bytes(broken)
        assert FilterIndex.load(str(path)) is None, name
