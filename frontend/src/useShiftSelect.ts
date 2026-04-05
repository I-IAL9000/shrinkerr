import { useState, useCallback, useRef } from "react";

export function useShiftSelect<T extends string | number>(items: T[]) {
  const [selected, setSelected] = useState<Set<T>>(new Set());
  const lastClickedRef = useRef<number | null>(null);

  const handleClick = useCallback((index: number, id: T, e: { shiftKey: boolean }) => {
    setSelected(prev => {
      const next = new Set(prev);
      if (e.shiftKey && lastClickedRef.current !== null) {
        const start = Math.min(lastClickedRef.current, index);
        const end = Math.max(lastClickedRef.current, index);
        for (let i = start; i <= end; i++) {
          next.add(items[i]);
        }
      } else {
        if (next.has(id)) {
          next.delete(id);
        } else {
          next.add(id);
        }
      }
      lastClickedRef.current = index;
      return next;
    });
  }, [items]);

  const selectAll = useCallback(() => {
    setSelected(new Set(items));
  }, [items]);

  const deselectAll = useCallback(() => {
    setSelected(new Set());
  }, [items]);

  return { selected, setSelected, handleClick, selectAll, deselectAll };
}
