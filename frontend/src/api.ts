import { useEffect, useRef } from "react";
import type { WSMessage } from "./types";

const API_BASE = "/api";

async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

// Scan
export const startScan = (paths: string[]) =>
  apiFetch("/scan/start", { method: "POST", body: JSON.stringify({ paths }) });
export const getScanResults = () => apiFetch<{ results: any[] }>("/scan/results");
export const removeScanResult = (id: number) =>
  apiFetch(`/scan/results/${id}`, { method: "DELETE" });

// Jobs
export const addJob = (job: any) =>
  apiFetch("/jobs/add", { method: "POST", body: JSON.stringify(job) });
export const addBulkJobs = (jobs: any[]) =>
  apiFetch("/jobs/add-bulk", { method: "POST", body: JSON.stringify(jobs) });
export const getJobs = () => apiFetch<{ jobs: any[] }>("/jobs/");
export const getJobStats = () => apiFetch<any>("/jobs/stats");
export const startQueue = () => apiFetch("/jobs/start", { method: "POST" });
export const pauseQueue = () => apiFetch("/jobs/pause", { method: "POST" });
export const resumeQueue = () => apiFetch("/jobs/resume", { method: "POST" });
export const cancelJob = (id: number) =>
  apiFetch(`/jobs/${id}/cancel`, { method: "POST" });
export const removeJob = (id: number) =>
  apiFetch(`/jobs/${id}`, { method: "DELETE" });
export const retryJob = (id: number) =>
  apiFetch(`/jobs/${id}/retry`, { method: "POST" });
export const clearCompleted = () =>
  apiFetch("/jobs/clear-completed", { method: "POST" });

// Schedule
export const setSchedule = (startTime: string) =>
  apiFetch("/schedule/set", {
    method: "POST",
    body: JSON.stringify({ scheduled_start: startTime }),
  });
export const cancelSchedule = () =>
  apiFetch("/schedule/cancel", { method: "DELETE" });
export const getSchedule = () => apiFetch<any>("/schedule/");

// Settings
export const getMediaDirs = () => apiFetch<{ dirs: any[] }>("/settings/dirs");
export const addMediaDir = (path: string, label: string) =>
  apiFetch("/settings/dirs", {
    method: "POST",
    body: JSON.stringify({ path, label }),
  });
export const removeMediaDir = (id: number) =>
  apiFetch(`/settings/dirs/${id}`, { method: "DELETE" });
export const getEncodingSettings = () => apiFetch<any>("/settings/encoding");
export const updateEncodingSettings = (settings: any) =>
  apiFetch("/settings/encoding", {
    method: "PUT",
    body: JSON.stringify(settings),
  });

// WebSocket hook
export function useWebSocket(onMessage: (msg: WSMessage) => void) {
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${protocol}//${window.location.host}/ws`);
    wsRef.current = ws;

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data) as WSMessage;
        onMessage(msg);
      } catch {}
    };

    ws.onclose = () => {
      // Reconnect after 3 seconds
      setTimeout(() => {
        if (wsRef.current?.readyState === WebSocket.CLOSED) {
          const newWs = new WebSocket(`${protocol}//${window.location.host}/ws`);
          newWs.onmessage = ws.onmessage;
          newWs.onclose = ws.onclose;
          wsRef.current = newWs;
        }
      }, 3000);
    };

    return () => ws.close();
  }, [onMessage]);
}
