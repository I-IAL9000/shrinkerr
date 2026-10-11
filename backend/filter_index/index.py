"""The filter index: for every Scanner filter, the listed scan_results rows
it matches, as a bitset over row ids (bits.py). The pills' counts under any
filter are then a few ANDs and popcounts instead of a pass over the library."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterable, Optional

from backend.filter_index.bits import from_ids, ids_of
from backend.scan_filters import FILTERS_VERSION, _FILTER_LIST, Filter, _group_key


def definitions_version() -> str:
    """Changes when a filter does: a saved index for other definitions is rebuilt."""
    text = repr([FILTERS_VERSION] + [(f.id, f.group, f.sql, f.pre_sql, f.py is not None, f.inputs)
                                     for f in _FILTER_LIST])
    return hashlib.sha1(text.encode()).hexdigest()[:16]


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

    def replace(self, filters: Iterable[Filter], members: dict[str, list[int]],
                savings: Optional[dict[int, int]] = None) -> None:
        """Take these filters' rows over the whole library (and the savings, when given)."""
        for f in filters:
            self.bits[f.id] = from_ids(members[f.id])
        if savings is not None:
            self.savings = dict(savings)

    def patch(self, ids: list[int], alive: list[int], members: dict[str, list[int]],
              savings: Optional[dict[int, int]]) -> None:
        """Take these rows' answers for every filter: `ids` were evaluated,
        `alive` are the ones still listed, `members` and `savings` theirs."""
        keep = ~from_ids(ids)
        self.alive = (self.alive & keep) | from_ids(alive)
        for fid, bits in list(self.bits.items()):
            self.bits[fid] = (bits & keep) | from_ids(members.get(fid, ()))
        for i in ids:
            self.savings.pop(i, None)
        self.savings.update(savings or {})

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
