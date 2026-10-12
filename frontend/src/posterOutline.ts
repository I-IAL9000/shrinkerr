/** A box's edges, in the poster grid's coordinates. */
export interface Box { x1: number; y1: number; x2: number; y2: number }

/**
 * One outline around the open poster and its row below (v0.10.0): the
 * poster's sides run down to the row and meet it in inward curves, every
 * outer corner rounded; a poster at the row's edge runs straight down it.
 * `inset`: half the stroke, so the line covers the boxes' own edges.
 */
export function posterOutline(card: Box, row: Box, radius: number, inset: number): string {
  const r = Math.max(0, radius - inset);  // the boxes' corners, at the line's middle
  const c = { x1: card.x1 + inset, y1: card.y1 + inset, x2: card.x2 - inset };
  const p = { x1: row.x1 + inset, y1: row.y1 + inset, x2: row.x2 - inset, y2: row.y2 - inset };
  const join = radius;  // the inward curves
  const flushLeft = c.x1 - p.x1 < join + r;
  const flushRight = p.x2 - c.x2 < join + r;
  const arc = (rad: number, outward: boolean, x: number, y: number) => `A ${rad} ${rad} 0 0 ${outward ? 1 : 0} ${x} ${y}`;
  const d = [`M ${c.x1 + r} ${c.y1}`, `H ${c.x2 - r}`, arc(r, true, c.x2, c.y1 + r)];
  if (flushRight) d.push(`V ${p.y2 - r}`);
  else d.push(`V ${p.y1 - join}`, arc(join, false, c.x2 + join, p.y1), `H ${p.x2 - r}`,
              arc(r, true, p.x2, p.y1 + r), `V ${p.y2 - r}`);
  d.push(arc(r, true, flushRight ? c.x2 - r : p.x2 - r, p.y2), `H ${(flushLeft ? c.x1 : p.x1) + r}`,
         arc(r, true, flushLeft ? c.x1 : p.x1, p.y2 - r));
  if (flushLeft) d.push(`V ${c.y1 + r}`);
  else d.push(`V ${p.y1 + r}`, arc(r, true, p.x1 + r, p.y1), `H ${c.x1 - join}`,
              arc(join, false, c.x1, p.y1 - join), `V ${c.y1 + r}`);
  d.push(arc(r, true, c.x1 + r, c.y1), "Z");
  return d.join(" ");
}
