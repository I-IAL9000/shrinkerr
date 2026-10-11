"""Sets of scan_results row ids as Python ints (bit n = row id n)."""
from backend.filter_index.bits import from_ids, ids_of


def test_round_trip():
    ids = [0, 1, 7, 8, 9, 63, 64, 1000, 165_741]
    bits = from_ids(ids)
    assert bits.bit_count() == len(ids)
    assert ids_of(bits) == ids
    assert ids_of(from_ids(reversed(ids))) == ids  # any order in, ascending out


def test_empty_and_duplicates():
    assert from_ids([]) == 0
    assert ids_of(0) == []
    assert ids_of(from_ids([5, 5, 5])) == [5]


def test_set_algebra_is_int_algebra():
    a, b = from_ids([1, 2, 3, 500]), from_ids([2, 3, 4])
    assert ids_of(a & b) == [2, 3]
    assert ids_of(a | b) == [1, 2, 3, 4, 500]
    assert ids_of(a & ~b) == [1, 500]  # what an exclusion does
