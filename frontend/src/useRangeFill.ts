import { useEffect, type RefObject } from "react";

/**
 * Paints a purple-fill gradient on every `<input type="range">` mounted
 * inside the given ref's subtree. Reacts to sliders being added to the
 * DOM, but not to attribute changes (none of our sliders reshape their
 * min/max dynamically).
 *
 * This replaces a MutationObserver that used to watch document.body's
 * entire subtree — that was a huge CPU hog during encoding because every
 * progress-tick text-node update fired the callback.
 *
 * Callers must attach the returned ref to the container whose sliders
 * they want to style. If the container is conditionally rendered (e.g.
 * a modal), the hook is still safe — it just returns early when the ref
 * is null.
 */
export function useRangeFill(containerRef: RefObject<HTMLElement | null>) {
  useEffect(() => {
    const root = containerRef.current;
    if (!root) return;

    const paint = (el: HTMLInputElement) => {
      const min = parseFloat(el.min) || 0;
      const max = parseFloat(el.max) || 100;
      const val = parseFloat(el.value) || 0;
      const pct = ((val - min) / (max - min)) * 100;
      el.style.background = `linear-gradient(to right, #6860fe ${pct}%, #212533 ${pct}%)`;
      el.style.borderRadius = "3px";
    };

    const init = () => {
      root.querySelectorAll<HTMLInputElement>('input[type="range"]').forEach(paint);
    };

    // Initial pass.
    init();

    // Only re-init when a range input actually enters the subtree.
    const obs = new MutationObserver((mutations) => {
      let hasRangeInput = false;
      for (const m of mutations) {
        for (const n of Array.from(m.addedNodes)) {
          if (n instanceof HTMLElement) {
            if (n.matches?.('input[type="range"]') || n.querySelector?.('input[type="range"]')) {
              hasRangeInput = true;
              break;
            }
          }
        }
        if (hasRangeInput) break;
      }
      if (hasRangeInput) init();
    });
    obs.observe(root, { childList: true, subtree: true });
    return () => obs.disconnect();
  }, [containerRef]);
}
