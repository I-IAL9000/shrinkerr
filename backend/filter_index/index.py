"""The filter index: for every Scanner filter, the listed scan_results rows
it matches, as a bitset over row ids (bits.py). The pills' counts under any
filter are then a few ANDs and popcounts instead of a pass over the library."""
from __future__ import annotations

import hashlib
import json
import zlib
from array import array
from dataclasses import dataclass, field
from typing import Iterable, Optional

from backend.filter_index.bits import from_ids, ids_of
from backend.scan_filters import FILTERS_VERSION, LISTED, PY_COLUMNS, _FILTER_LIST, Filter, _group_key


def definitions_version() -> str:
    """Changes when a filter does, or the rows they run over (LISTED) or the
    columns the Python ones read: a saved index for other definitions is rebuilt."""
    text = repr([FILTERS_VERSION, LISTED, PY_COLUMNS] + [(f.id, f.group, f.sql, f.pre_sql, f.py is not None, f.inputs)
                                                         for f in _FILTER_LIST])
    return hashlib.sha1(text.encode()).hexdigest()[:16]


# The file: this line, a JSON header line, then the bitsets and the savings.
# Written in place, not renamed into place (renaming over a file on a NAS
# share can send the old one to its recycle bin): a torn write fails the
# checksum and the index is rebuilt.
_MAGIC = b"SHRINKERR FILTER INDEX 1\n"


def _to_bytes(bits: int) -> bytes:
    return bits.to_bytes((bits.bit_length() + 7) // 8, "little")


@dataclass
class FilterIndex:
    version: str  # definitions_version() it was built with
    db_id: str    # the database it was built from (service._db_id)
    seq: int = 0  # the last filter_index_changes entry applied
    counters: dict[str, int] = field(default_factory=dict)  # the input counters applied ("jobs": n, ...)
    time_at: float = 0.0  # when the time-based filters were evaluated
    alive: int = 0  # the listed rows
    bits: dict[str, int] = field(default_factory=dict)  # filter id -> the rows it matches
    savings: dict[int, int] = field(default_factory=dict)  # row id -> estimated savings, when > 0

    @classmethod
    def of(cls, alive: list[int], members: dict[str, list[int]], savings: dict[int, int], **meta) -> FilterIndex:
        """An index from evaluate()'s answer for every filter over every row."""
        index = cls(**meta)
        index.alive = from_ids(alive)
        index.bits = {f.id: from_ids(members[f.id]) for f in _FILTER_LIST}
        index.savings = dict(savings)
        return index

    def replace(self, filters: Iterable[Filter], members: dict[str, list[int]]) -> None:
        """Take these filters' rows over the whole library. (Savings belong to
        the row, not to an input, so only patch() touches them.)"""
        for f in filters:
            self.bits[f.id] = from_ids(members[f.id])

    def patch(self, ids: list[int], alive: list[int], members: dict[str, list[int]],
              savings: dict[int, int]) -> None:
        """Take these rows' answers for every filter: `ids` were evaluated,
        `alive` are the ones still listed, `members` and `savings` theirs."""
        keep = ~from_ids(ids)
        self.alive = (self.alive & keep) | from_ids(alive)
        for fid, bits in list(self.bits.items()):
            self.bits[fid] = (bits & keep) | from_ids(members.get(fid, ()))
        for i in ids:
            self.savings.pop(i, None)
        self.savings.update(savings)

    def count_all(self) -> dict[str, int]:
        """scan_filters.count_all's counts."""
        counts = {"all": self.alive.bit_count()}
        counts.update((fid, bits.bit_count()) for fid, bits in self.bits.items())
        return counts

    def savings_of(self, fid: str) -> int:
        """The estimated savings of the files a filter matches."""
        return sum(self.savings.get(i, 0) for i in ids_of(self.bits[fid]))

    def facet_counts(self, groups: list[tuple[str, int]], excluded: list[tuple[str, int]]) -> dict[str, int]:
        """scan_filters.facet_counts's counts: each pill's with the
        constraints its click replaces (its group's, its own exclusion) set
        aside; "matching": every constraint. `groups`: (group key, the rows
        any of its filters match); `excluded`: (filter id, its rows)."""
        constraints = [bits for _, bits in groups] + [self.alive & ~bits for _, bits in excluded]
        group_at = {key: n for n, (key, _) in enumerate(groups)}
        excluded_at = {fid: len(groups) + n for n, (fid, _) in enumerate(excluded)}
        bases: dict[frozenset, int] = {}

        def base(skip: frozenset) -> int:
            if skip not in bases:
                rows = self.alive
                for n, bits in enumerate(constraints):
                    if n not in skip:
                        rows &= bits
                bases[skip] = rows
            return bases[skip]

        counts = {}
        for f in _FILTER_LIST:
            skip = frozenset(n for n in (group_at.get(_group_key(f)), excluded_at.get(f.id)) if n is not None)
            counts[f.id] = (base(skip) & self.bits[f.id]).bit_count()
        counts["matching"] = base(frozenset()).bit_count()
        return counts

    def save(self, path: str) -> None:
        """Written in place: see _MAGIC."""
        blobs = [("alive", _to_bytes(self.alive))] + [(fid, _to_bytes(bits)) for fid, bits in self.bits.items()]
        pairs = array("q")
        for row_id, saving in self.savings.items():
            pairs.extend((row_id, saving))
        payload = b"".join(blob for _, blob in blobs) + pairs.tobytes()
        header = {"version": self.version, "db_id": self.db_id, "seq": self.seq, "counters": self.counters,
                  "time_at": self.time_at, "sizes": [[name, len(blob)] for name, blob in blobs],
                  "savings": len(pairs), "length": len(payload), "crc": zlib.crc32(payload)}
        with open(path, "wb") as fh:
            fh.write(_MAGIC + json.dumps(header).encode() + b"\n" + payload)

    @classmethod
    def load(cls, path: str) -> Optional[FilterIndex]:
        """The saved index, or None (none saved, damaged, not one)."""
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            return None
        if not data.startswith(_MAGIC):
            return None
        try:
            end = data.index(b"\n", len(_MAGIC))
            header = json.loads(data[len(_MAGIC):end])
            payload = data[end + 1:]
            if len(payload) != header["length"] or zlib.crc32(payload) != header["crc"]:
                return None
            index = cls(version=header["version"], db_id=header["db_id"], seq=header["seq"],
                        counters=header["counters"], time_at=header["time_at"])
            at = 0
            for name, size in header["sizes"]:
                if not isinstance(size, int) or size < 0:
                    return None
                value = int.from_bytes(payload[at:at + size], "little")
                at += size
                if name == "alive":
                    index.alive = value
                else:
                    index.bits[name] = value
            pairs = array("q")
            if at + header["savings"] * pairs.itemsize != len(payload):
                return None
            pairs.frombytes(payload[at:])
            index.savings = dict(zip(pairs[0::2], pairs[1::2]))
            return index
        except (ValueError, KeyError, TypeError, RecursionError):
            return None
