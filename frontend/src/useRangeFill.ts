import { useEffect, type RefObject } from "react";

/**
 * Paints a purple-fill gradient on every `<input type="range">` mounted
 * inside the given ref's subtree. Reacts to sliders being added to the
 * DOM, and repaints after each render of the component that calls it: a
 * controlled slider's value can change without an input event (v0.10.0:
 * Add to Queue's quality moved to a rule's on Force re-encode, and the
 * fill stayed behind).
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
const paint = (el: HTMLInputElement) => {
  const min = parseFloat(el.min) || 0;
  const max = parseFloat(el.max) || 100;
  const val = parseFloat(el.value) || 0;
  const pct = ((val - min) / (max - min)) * 100;
  el.style.background = `linear-gradient(to right, var(--accent) ${pct}%, var(--border) ${pct}%)`;
  el.style.borderRadius = "3px";
};

export function useRangeFill(containerRef: RefObject<HTMLElement | null>) {
  useEffect(() => {
    const root = containerRef.current;
    if (!root) return;

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

  useEffect(() => {
    containerRef.current?.querySelectorAll<HTMLInputElement>('input[type="range"]').forEach(paint);
  });
}
