"""The filter index: for every Scanner filter, the listed scan_results rows
it matches, as a bitset over row ids (bits.py). The pills' counts under any
filter are then a few ANDs and popcounts instead of a pass over the library."""
from __future__ import annotations

import hashlib
import json
import zlib
from array import array
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Optional

from backend.filter_index.bits import from_ids, ids_of
from backend.scan_filters import (FACETS_VERSION, FILTERS_VERSION, _FACET_LIST, LISTED, PY_COLUMNS, _FILTER_LIST,
                                  Filter, _group_key)

TOP_VALUES = 50  # a list's values in the counts response
# Searched lists (Facet.searched: actors, directors, studios) aren't kept as
# bitsets — ~150K values, and a bitset is as long as its highest row id. A
# title's credits are its files', so each row points at its set of values (a
# "combo", shared by a title's files): a row's change is one number, and a
# count is one pass over the rows it counts.
SEARCHED = frozenset(f.key for f in _FACET_LIST if f.searched)
# One input change replaces them all (replace_facets), never some.
assert len({f.inputs for f in _FACET_LIST if f.searched}) <= 1


def _searched(fid: str) -> bool:
    return fid.partition(":")[0] in SEARCHED


def definitions_version() -> str:
    """Changes when a filter or an indexed list does, or the rows they run
    over (LISTED) or the columns the Python ones read: a saved index for
    other definitions is rebuilt."""
    text = repr([FILTERS_VERSION, FACETS_VERSION, LISTED, PY_COLUMNS]
                + [(f.id, f.group, f.sql, f.pre_sql, f.py is not None, f.inputs) for f in _FILTER_LIST]
                + [(f.key, f.pairs_sql, f.values is not None, f.inputs) for f in _FACET_LIST])
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
    combos: list[tuple[str, ...]] = field(default_factory=list)  # the searched lists' value sets ("actor:X", ...)
    row_combo: array = field(default_factory=lambda: array("i"))  # row id -> its combo (-1: none)
    _combo_at: dict = field(default_factory=dict, compare=False, repr=False)  # combo -> its number
    _rows_of: dict = field(default_factory=dict, compare=False, repr=False)  # list_bits(), until a change

    @classmethod
    def of(cls, alive: list[int], members: dict[str, list[int]], savings: dict[int, int], **meta) -> FilterIndex:
        """An index from evaluate()'s answer for every filter over every row."""
        index = cls(**meta)
        index.alive = from_ids(alive)
        index.bits = {f.id: from_ids(members[f.id]) for f in _FILTER_LIST}
        index.bits.update((key, from_ids(rows)) for key, rows in members.items() if ":" in key and not _searched(key))
        index._set_combos(members, rebuild=True)
        index.savings = dict(savings)
        return index

    def _set_combos(self, members: dict[str, list[int]], rebuild: bool = False, ids: Iterable[int] = ()) -> None:
        """The rows in the searched lists' `members` point at their value
        sets; `ids` (evaluated, with none) at none. `rebuild`: every row's."""
        if rebuild:
            self.combos, self.row_combo, self._combo_at = [], array("i"), {}
        elif not self._combo_at:
            self._combo_at = {c: n for n, c in enumerate(self.combos)}
        self._rows_of = {}
        by_row: dict[int, list[str]] = defaultdict(list)
        for fid, rows in members.items():
            if _searched(fid):
                for i in rows:
                    by_row[i].append(fid)
        top = max([*by_row, *ids], default=-1)
        if top >= len(self.row_combo):
            self.row_combo.extend([-1] * (top + 1 - len(self.row_combo)))
        for i in ids:
            self.row_combo[i] = -1
        for i, fids in by_row.items():
            combo = tuple(sorted(fids))
            n = self._combo_at.get(combo)
            if n is None:
                n = self._combo_at[combo] = len(self.combos)
                self.combos.append(combo)
            self.row_combo[i] = n

    def _combo_counts(self, rows: int, want) -> dict[str, int]:
        """The searched lists' values `want(fid)` is true of, with their rows
        among `rows`."""
        per = Counter(map(self.row_combo.__getitem__, [i for i in ids_of(rows) if i < len(self.row_combo)]))
        counts: dict[str, int] = defaultdict(int)
        for n, k in per.items():
            if n >= 0:
                for fid in self.combos[n]:
                    if want(fid):
                        counts[fid] += k
        return counts

    def replace(self, filters: Iterable[Filter], members: dict[str, list[int]]) -> None:
        """Take these filters' rows over the whole library. (Savings belong to
        the row, not to an input, so only patch() touches them.)"""
        for f in filters:
            self.bits[f.id] = from_ids(members[f.id])

    def replace_facets(self, keys: Iterable[str], members: dict[str, list[int]]) -> None:
        """Take these lists' values over the whole library (an input of theirs changed)."""
        prefixes = tuple(f"{key}:" for key in keys)
        for fid in [k for k in self.bits if k.startswith(prefixes)]:
            del self.bits[fid]
        self.bits.update((k, from_ids(rows)) for k, rows in members.items()
                         if k.startswith(prefixes) and rows and not _searched(k))
        if SEARCHED & set(keys):
            self._set_combos(members, rebuild=True)

    def patch(self, ids: list[int], alive: list[int], members: dict[str, list[int]],
              savings: dict[int, int]) -> None:
        """Take these rows' answers for every filter: `ids` were evaluated,
        `alive` are the ones still listed, `members` and `savings` theirs."""
        keep = ~from_ids(ids)
        self.alive = (self.alive & keep) | from_ids(alive)
        for fid, bits in list(self.bits.items()):
            self.bits[fid] = (bits & keep) | from_ids(members.get(fid, ()))
        for fid in [k for k in members.keys() - self.bits.keys() if not _searched(k)]:  # first seen in these rows
            self.bits[fid] = from_ids(members[fid])
        self._set_combos(members, ids=ids)
        for fid in [k for k, bits in self.bits.items() if ":" in k and not bits]:
            del self.bits[fid]  # a value no row has any more
        for i in ids:
            self.savings.pop(i, None)
        self.savings.update(savings)

    def count_all(self) -> dict[str, int]:
        """scan_filters.count_all's counts."""
        counts = {"all": self.alive.bit_count()}
        counts.update((f.id, self.bits[f.id].bit_count()) for f in _FILTER_LIST)
        return counts

    def list_bits(self, fid: str) -> int:
        """A searched list's value's rows (0: none), kept until a change."""
        if fid not in self._rows_of:
            combos = {n for n, c in enumerate(self.combos) if fid in c}
            rc = self.row_combo
            self._rows_of[fid] = from_ids(i for i in range(len(rc)) if rc[i] in combos) & self.alive if combos else 0
        return self._rows_of[fid]

    def savings_of(self, fid: str) -> int:
        """The estimated savings of the files a filter matches."""
        return sum(self.savings.get(i, 0) for i in ids_of(self.bits[fid]))

    def _bases(self, groups: list[tuple[str, int]], excluded: list[tuple[str, int]]):
        """(group key -> constraint number, filter id -> constraint number,
        base(skip): the rows meeting every constraint but those in `skip`)."""
        constraints = [bits for _, bits in groups] + [self.alive & ~bits for _, bits in excluded]
        group_at = {key: n for n, (key, _) in enumerate(groups)}
        excluded_at = {fid: len(groups) + n for n, (fid, _) in enumerate(excluded)}
        cache: dict[frozenset, int] = {}

        def base(skip: frozenset) -> int:
            if skip not in cache:
                rows = self.alive
                for n, bits in enumerate(constraints):
                    if n not in skip:
                        rows &= bits
                cache[skip] = rows
            return cache[skip]
        return group_at, excluded_at, base

    def facet_counts(self, groups: list[tuple[str, int]], excluded: list[tuple[str, int]]) -> dict[str, int]:
        """scan_filters.facet_counts's counts: each pill's with the
        constraints its click replaces (its group's, its own exclusion) set
        aside; "matching": every constraint. `groups`: (group key, the rows
        any of its filters match); `excluded`: (filter id, its rows)."""
        group_at, excluded_at, base = self._bases(groups, excluded)
        counts = {}
        for f in _FILTER_LIST:
            skip = frozenset(n for n in (group_at.get(_group_key(f)), excluded_at.get(f.id)) if n is not None)
            counts[f.id] = (base(skip) & self.bits[f.id]).bit_count()
        counts["matching"] = base(frozenset()).bit_count()
        return counts

    def facet_values(self, groups: list[tuple[str, int]], excluded: list[tuple[str, int]],
                     selected: frozenset = frozenset(), facet: Optional[str] = None,
                     top: Optional[int] = TOP_VALUES, unkept: Optional[dict[str, int]] = None,
                     q: str = "") -> dict[str, list[tuple[str, int]]]:
        """Each indexed list's values (or only `facet`'s) with what adding
        each would give: the files meeting every constraint but its own
        list's and its own exclusion, that have it. The most first (then by
        value), `top` of them (None: all), without zeros; values in
        `selected` ("<facet>:<value>") always, at 0 when no file has them.
        `unkept`: selected values the index doesn't keep, with their rows (a
        genre saved combined). A searched list (SEARCHED) only when it's
        `facet`; `q`: only values with it in them (any case), and the selected."""
        group_at, excluded_at, base = self._bases(groups, excluded)
        by_facet: dict[str, list[tuple[str, int]]] = defaultdict(list)
        needle = q.strip().casefold()
        for fid, bits in [*self.bits.items(), *(unkept or {}).items()]:
            key, sep, value = fid.partition(":")
            if sep and (facet is None or key == facet) and (needle in value.casefold() or fid in selected):
                skip = frozenset(n for n in (group_at.get(key), excluded_at.get(fid)) if n is not None)
                by_facet[key].append((value, (base(skip) & bits).bit_count()))
        if facet in SEARCHED:
            prefix = f"{facet}:"
            counts = self._combo_counts(base(frozenset(n for n in (group_at.get(facet),) if n is not None)),
                                        lambda fid: fid.startswith(prefix) and (
                                            needle in fid[len(prefix):].casefold() or fid in selected))
            for fid, n in excluded_at.items():  # a value excluded: its count without its exclusion
                if fid.startswith(prefix):
                    rows = base(frozenset(x for x in (group_at.get(facet), n) if x is not None))
                    counts[fid] = self._combo_counts(rows, lambda f, fid=fid: f == fid).get(fid, 0)
            by_facet[facet] += [(fid[len(prefix):], n) for fid, n in counts.items()]
        out = {}
        for key in [f.key for f in _FACET_LIST if (facet is None and not f.searched) or f.key == facet]:
            rows = sorted(by_facet.get(key, []), key=lambda vc: (-vc[1], vc[0]))
            kept = [vc for vc in (rows if top is None else rows[:top]) if vc[1] > 0]
            have = {v for v, _ in kept}
            counts = dict(rows)
            for token in sorted(selected):
                k, _, v = token.partition(":")
                if k == key and v not in have:
                    kept.append((v, counts.get(v, 0)))
            out[key] = kept
        return out

    def save(self, path: str) -> None:
        """Written in place: see _MAGIC."""
        blobs = [("alive", _to_bytes(self.alive))] + [(fid, _to_bytes(bits)) for fid, bits in self.bits.items()]
        pairs = array("q")
        for row_id, saving in self.savings.items():
            pairs.extend((row_id, saving))
        combo_rows, combos = self.row_combo.tobytes(), json.dumps(self.combos).encode()
        payload = b"".join(blob for _, blob in blobs) + pairs.tobytes() + combo_rows + combos
        header = {"version": self.version, "db_id": self.db_id, "seq": self.seq, "counters": self.counters,
                  "time_at": self.time_at, "sizes": [[name, len(blob)] for name, blob in blobs],
                  "savings": len(pairs), "combo_rows": len(combo_rows), "combos": len(combos),
                  "length": len(payload), "crc": zlib.crc32(payload)}
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
            end = at + header["savings"] * pairs.itemsize
            if end + header["combo_rows"] + header["combos"] != len(payload):
                return None
            pairs.frombytes(payload[at:end])
            index.savings = dict(zip(pairs[0::2], pairs[1::2]))
            index.row_combo.frombytes(payload[end:end + header["combo_rows"]])
            index.combos = [tuple(c) for c in json.loads(payload[end + header["combo_rows"]:])]
            return index
        except (ValueError, KeyError, TypeError, RecursionError):
            return None
