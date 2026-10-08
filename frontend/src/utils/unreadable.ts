// v0.9.153: a disc image no reader could open is stored with
// probe_status "unreadable" and probe_error = JSON {kind, detail}; the
// detail is the reader's own (untranslated) error text.

export interface UnreadableReason {
  kind: "dvd" | "bluray" | "unknown";
  detail: string;
}

export function unreadableReason(file: { probe_status?: string; probe_error?: string | null }): UnreadableReason | null {
  if (file.probe_status !== "unreadable") return null;
  try {
    const r = JSON.parse(file.probe_error || "{}");
    const kind = r.kind === "dvd" || r.kind === "bluray" ? r.kind : "unknown";
    return { kind, detail: String(r.detail || "") };
  } catch {
    return { kind: "unknown", detail: "" };
  }
}
