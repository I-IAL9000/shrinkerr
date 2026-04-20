import { useEffect, useRef } from "react";

/**
 * Like setInterval, but:
 *  - pauses while the tab is hidden (document.hidden === true)
 *  - fires once immediately when the tab becomes visible again (so stale data
 *    refreshes as soon as the user looks at it)
 *
 * Chrome/Edge will otherwise keep waking the tab up to run intervals even
 * when backgrounded, burning CPU for no visible benefit. This hook neatly
 * suspends the callback until the tab is active.
 */
export function useVisibleInterval(callback: () => void, delay: number | null) {
  // Keep latest callback in a ref so consumers don't have to memoize it.
  const cbRef = useRef(callback);
  useEffect(() => { cbRef.current = callback; }, [callback]);

  useEffect(() => {
    if (delay === null) return;

    let intervalId: ReturnType<typeof setInterval> | null = null;

    const start = () => {
      if (intervalId !== null) return;
      intervalId = setInterval(() => cbRef.current(), delay);
    };
    const stop = () => {
      if (intervalId === null) return;
      clearInterval(intervalId);
      intervalId = null;
    };

    const onVisibility = () => {
      if (document.hidden) {
        stop();
      } else {
        // Refresh immediately, then resume polling.
        try { cbRef.current(); } catch { /* ignore */ }
        start();
      }
    };

    // Kick off according to current visibility.
    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      stop();
    };
  }, [delay]);
}
