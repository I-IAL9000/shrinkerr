/** Format a number with commas: 113498 → "113,498" */
export function fmtNum(n: number | null | undefined): string {
  if (n == null) return "0";
  return n.toLocaleString();
}
