import { useSyncExternalStore } from "react";
import type { JobProgress } from "./types";

// Live progress of running jobs, from the WebSocket (FE#4, v0.10.0). As App
// state, every job_progress message — up to two a second per running job —
// re-rendered the whole app: the Scanner, Settings, the Dashboard's charts,
// in a hidden tab too. Now only the components that show progress subscribe,
// and while the tab is hidden they aren't told: the map keeps updating and
// they get one notification when the tab is shown again.

let map = new Map<number, JobProgress>();
const listeners = new Set<() => void>();
let missed = false;

function emit() {
  if (document.hidden) {
    missed = true;
    return;
  }
  missed = false;
  listeners.forEach(listener => listener());
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && missed) emit();
});

function update(change: (next: Map<number, JobProgress>) => boolean) {
  const next = new Map(map);
  if (!change(next)) return;
  map = next;
  emit();
}

export const jobProgressStore = {
  set(progress: JobProgress) {
    update(next => { next.set(progress.job_id, { ...progress, received_at: Date.now() }); return true; });
  },
  delete(jobId: number) {
    if (map.has(jobId)) update(next => next.delete(jobId));
  },
  clear() {
    if (map.size) update(next => { next.clear(); return true; });
  },
  /** Drop entries that stopped updating (a job that ended unseen). */
  dropStale(maxAgeMs: number) {
    const now = Date.now();
    const stale = [...map.values()].filter(p => now - (p.received_at ?? now) > maxAgeMs);
    if (stale.length) update(next => { stale.forEach(p => next.delete(p.job_id)); return true; });
  },
  subscribe(listener: () => void) {
    listeners.add(listener);
    return () => { listeners.delete(listener); };
  },
  get: () => map,
};

export function useJobProgressMap(): Map<number, JobProgress> {
  return useSyncExternalStore(jobProgressStore.subscribe, jobProgressStore.get);
}
