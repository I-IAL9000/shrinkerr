// Natural-sort comparator: digits compare numerically ("Season 2" <
// "Season 11") and case / accents don't matter. One shared collator (FE#6,
// v0.10.0): `a.localeCompare(b, undefined, options)` builds one per call —
// 166 ms to sort 5,000 titles, against about 6 ms.
export const naturalCompare = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" }).compare;
