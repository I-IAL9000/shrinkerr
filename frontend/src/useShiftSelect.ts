import { useState, useCallback, useRef } from "react";

export function useShiftSelect<T extends string | number>(items: T[]) {
  const [selected, setSelected] = useState<Set<T>>(new Set());
  // Memoized rows can hold an old copy of handleClick (and the row index it
  // was rendered with), so positions are resolved from the current list at
  // click time and the last click is remembered by id, not index — mixing
  // indices from different renders made a shift-select range cover the
  // wrong jobs for bulk Remove/Ignore (v0.10.0).
  const itemsRef = useRef(items);
  itemsRef.current = items;
  const lastClickedRef = useRef<T | null>(null);

  const handleClick = useCallback((_index: number, id: T, e: { shiftKey: boolean }) => {
    const list = itemsRef.current;
    const from = lastClickedRef.current === null ? -1 : list.indexOf(lastClickedRef.current);
    const to = list.indexOf(id);
    lastClickedRef.current = id;
    setSelected(prev => {
      const next = new Set(prev);
      if (e.shiftKey && from >= 0 && to >= 0) {
        for (let i = Math.min(from, to); i <= Math.max(from, to); i++) {
          next.add(list[i]);
        }
      } else if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }, []);

  const selectAll = useCallback(() => {
    setSelected(new Set(itemsRef.current));
  }, []);

  const deselectAll = useCallback(() => {
    setSelected(new Set());
  }, []);

  return { selected, setSelected, handleClick, selectAll, deselectAll };
}
