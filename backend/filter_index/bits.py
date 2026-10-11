"""Sets of scan_results row ids as Python ints: bit n is row id n. AND, OR
and int.bit_count() run at C speed over a whole library's worth of bits."""
from typing import Iterable

_BITS_OF_BYTE = tuple(tuple(j for j in range(8) if byte >> j & 1) for byte in range(256))


def from_ids(ids: Iterable[int]) -> int:
    ids = list(ids)
    if not ids:
        return 0
    buf = bytearray(max(ids) // 8 + 1)
    for i in ids:
        buf[i >> 3] |= 1 << (i & 7)
    return int.from_bytes(buf, "little")


def ids_of(bits: int) -> list[int]:
    """The ids in `bits`, ascending."""
    out: list[int] = []
    for n, byte in enumerate(bits.to_bytes((bits.bit_length() + 7) // 8, "little")):
        if byte:
            out.extend((n << 3) + j for j in _BITS_OF_BYTE[byte])
    return out
