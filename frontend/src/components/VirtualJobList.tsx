import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from "react";
import {
  defaultRangeExtractor, useVirtualizer, useWindowVirtualizer, type Range, type Virtualizer,
} from "@tanstack/react-virtual";

// Virtualized list for the Queue tabs (v0.9.138). With thousands of jobs the
// page used to mount every row — ~58K DOM elements for 5K pending jobs, ~2s to
// show the tab and ~1s per checkbox click — and froze Firefox. Only the rows
// in view (plus `overscan`) are mounted; row heights are measured, so rows
// that expand (completed/failed details) just grow in place. Open rows stay
// mounted out of view (keepMounted): a row's details live in it, and an open
// one that came back showed "Loading…" (79 px) until they reloaded (387 px) —
// each change above the view moved the page, 308 px one way and back.
//
// The page scrolls inside `.main-content` on desktop but the window scrolls
// on mobile (theme.css ≤768px sets overflow-y: visible), so the scroll source
// is picked at runtime and re-picked when the layout flips.

interface Props {
  count: number;
  getKey: (index: number) => number | string;
  renderRow: (index: number) => ReactNode;
  estimateSize?: number;
  /** Rows to keep mounted out of view (the open ones). */
  keepMounted?: number[];
}

const OVERSCAN = 10;

function findScrollParent(el: HTMLElement | null): HTMLElement | null {
  const main = el?.closest(".main-content") as HTMLElement | null;
  if (!main) return null;
  const oy = getComputedStyle(main).overflowY;
  return oy === "auto" || oy === "scroll" ? main : null;
}

export default function VirtualJobList(props: Props) {
  const probeRef = useRef<HTMLDivElement>(null);
  // undefined = not measured yet, null = window scrolls, element = container scrolls
  const [scrollEl, setScrollEl] = useState<HTMLElement | null | undefined>(undefined);

  useLayoutEffect(() => {
    const update = () => setScrollEl(findScrollParent(probeRef.current));
    update();
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, []);

  return (
    <div ref={probeRef}>
      {scrollEl === undefined ? null
        : scrollEl ? <ElementList {...props} scrollEl={scrollEl} />
        : <WindowList {...props} />}
    </div>
  );
}

// Distance from the top of the scroll content to the list, which the
// virtualizer needs to map scroll offsets onto rows. Content above the list
// (toolbar, bulk-action panel, running cards) changes height whenever the
// Queue page re-renders — which re-renders this list too — so it's
// re-measured after every render, and on window resize (text reflow).
function useScrollMargin(listRef: RefObject<HTMLDivElement | null>, scrollEl: HTMLElement | null) {
  const [margin, setMargin] = useState(0);
  const measureRef = useRef(() => {});
  measureRef.current = () => {
    const list = listRef.current;
    if (!list) return;
    const top = list.getBoundingClientRect().top;
    const m = scrollEl
      ? top - scrollEl.getBoundingClientRect().top + scrollEl.scrollTop
      : top + window.scrollY;
    setMargin(prev => (Math.abs(prev - m) < 1 ? prev : m));
  };
  useLayoutEffect(() => measureRef.current());
  useEffect(() => {
    const onResize = () => measureRef.current();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return margin;
}

// The rows in view (plus overscan) and the ones kept mounted.
function useRangeExtractor(keep: number[] | undefined) {
  return useMemo(() => (range: Range) => {
    const shown = defaultRangeExtractor(range);
    if (!keep?.length) return shown;
    return [...new Set([...shown, ...keep.filter(i => i < range.count)])].sort((a, b) => a - b);
  }, [keep]);
}

function ElementList({ scrollEl, ...p }: Props & { scrollEl: HTMLElement }) {
  const listRef = useRef<HTMLDivElement>(null);
  const scrollMargin = useScrollMargin(listRef, scrollEl);
  const rangeExtractor = useRangeExtractor(p.keepMounted);
  const v = useVirtualizer({
    count: p.count,
    getScrollElement: () => scrollEl,
    estimateSize: () => p.estimateSize ?? 40,
    getItemKey: p.getKey,
    overscan: OVERSCAN,
    scrollMargin,
    rangeExtractor,
  });
  return <Rows listRef={listRef} v={v} scrollMargin={scrollMargin} renderRow={p.renderRow} />;
}

function WindowList(p: Props) {
  const listRef = useRef<HTMLDivElement>(null);
  const scrollMargin = useScrollMargin(listRef, null);
  const rangeExtractor = useRangeExtractor(p.keepMounted);
  const v = useWindowVirtualizer({
    count: p.count,
    estimateSize: () => p.estimateSize ?? 40,
    getItemKey: p.getKey,
    overscan: OVERSCAN,
    scrollMargin,
    rangeExtractor,
  });
  return <Rows listRef={listRef} v={v} scrollMargin={scrollMargin} renderRow={p.renderRow} />;
}

function Rows({ listRef, v, scrollMargin, renderRow }: {
  listRef: RefObject<HTMLDivElement | null>;
  v: Virtualizer<any, Element>;
  scrollMargin: number;
  renderRow: (index: number) => ReactNode;
}) {
  return (
    <div ref={listRef} style={{ height: v.getTotalSize(), position: "relative" }}>
      {v.getVirtualItems().map(item => (
        <div
          key={item.key}
          data-index={item.index}
          ref={v.measureElement}
          style={{
            position: "absolute", top: 0, left: 0, width: "100%",
            transform: `translateY(${item.start - scrollMargin}px)`,
          }}
        >
          {renderRow(item.index)}
        </div>
      ))}
    </div>
  );
}
