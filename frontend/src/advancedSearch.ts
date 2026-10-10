import type { SearchPredicate } from "./api";

/** Advanced Search's conditions, as the Scanner's filter carries them: an
 *  "adv:" token, base64url JSON {m, p} (backend/scan_filters.py). The server
 *  builds tokens (/scan/search); the page only reads them back. v0.10.0. */
export interface AdvancedSpec { m: "all" | "any"; p: SearchPredicate[] }

export function decodeAdvanced(token: string): AdvancedSpec | null {
  if (!token.startsWith("adv:")) return null;
  try {
    const b64 = token.slice(4).replace(/-/g, "+").replace(/_/g, "/");
    const bytes = Uint8Array.from(atob(b64 + "=".repeat((4 - (b64.length % 4)) % 4)), c => c.charCodeAt(0));
    const spec = JSON.parse(new TextDecoder().decode(bytes));
    if (!spec || !Array.isArray(spec.p)) return null;
    return { m: spec.m === "any" ? "any" : "all", p: spec.p };
  } catch {
    return null;
  }
}
