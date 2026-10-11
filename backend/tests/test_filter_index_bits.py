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


def test_bit_n_is_row_id_n():
    assert from_ids([0]) == 1
    assert from_ids([3]) == 8
    assert from_ids([0, 3, 9]) == 0b10_0000_1001
    assert from_ids([5, 5, 6, 6]) == 0b110_0000


def test_byte_boundaries():
    for top in (0, 7, 8, 9, 15, 16, 64, 1000):
        ids = sorted({0, top, max(top - 1, 0)})
        assert ids_of(from_ids(ids)) == ids
        assert from_ids(ids) == sum(1 << i for i in ids)
