import { useState, useRef, useCallback, memo, type ReactNode } from "react";

interface VirtualListProps<T> {
  items: T[];
  itemHeight: number;
  overscan?: number;
  renderItem: (item: T, index: number) => ReactNode;
  getKey: (item: T) => string | number;
}

function VirtualListInner<T>({
  items,
  itemHeight,
  overscan = 5,
  renderItem,
  getKey,
}: VirtualListProps<T>) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);

  const handleScroll = useCallback(() => {
    if (containerRef.current) {
      setScrollTop(containerRef.current.scrollTop);
    }
  }, []);

  // Use CSS for height — no ResizeObserver, no getBoundingClientRect, no layout thrashing
  const containerHeight = 800; // fallback for calculation only
  const totalHeight = items.length * itemHeight;
  const startIndex = Math.max(0, Math.floor(scrollTop / itemHeight) - overscan);
  const endIndex = Math.min(
    items.length,
    Math.ceil((scrollTop + containerHeight) / itemHeight) + overscan
  );

  const visibleItems = items.slice(startIndex, endIndex);

  return (
    <div
      ref={containerRef}
      onScroll={handleScroll}
      style={{
        overflowY: "auto",
        height: "calc(100vh - 200px)",
        minHeight: 300,
        background: "var(--bg-primary)",
        borderRadius: 6,
      }}
    >
      <div style={{ height: totalHeight, position: "relative" }}>
        <div
          style={{
            position: "absolute",
            top: startIndex * itemHeight,
            left: 0,
            right: 0,
          }}
        >
          {visibleItems.map((item, i) => (
            <div key={getKey(item)} style={{ height: itemHeight }}>
              {renderItem(item, startIndex + i)}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// Memo to prevent re-renders from parent state changes (like jobProgress)
export default memo(VirtualListInner) as typeof VirtualListInner;
