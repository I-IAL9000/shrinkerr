// Splice a freshly fetched first page (`head`) over the loaded history rows.
// Rows past the head's last row are kept (the user's infinite-scroll pages);
// rows inside the head's range are replaced, so new entries appear and removed
// ones (e.g. a retried failure) disappear. If the head doesn't reach any loaded
// row (more new rows than a page), fall back to just the head. v0.9.136.
export function mergeHead<T extends { id: number }>(loaded: T[], head: T[]): { rows: T[]; reset: boolean } {
  if (head.length === 0) return { rows: [], reset: true };
  const lastId = head[head.length - 1].id;
  const k = loaded.findIndex(j => j.id === lastId);
  if (k === -1) {
    // Loaded rows that aren't in the head at all: either everything loaded
    // fits inside the head (short list) or there's a gap — both use the head.
    return { rows: head, reset: true };
  }
  const headIds = new Set(head.map(j => j.id));
  return { rows: [...head, ...loaded.slice(k + 1).filter(j => !headIds.has(j.id))], reset: false };
}
