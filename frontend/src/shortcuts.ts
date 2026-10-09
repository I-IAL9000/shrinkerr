// Single-key shortcuts can be turned off (WCAG 2.1.4, v0.10.0) — per
// browser, like the theme. On unless switched off.
const KEY = "shrinkerr_shortcuts";

export function shortcutsEnabled(): boolean {
  try {
    return localStorage.getItem(KEY) !== "off";
  } catch {
    return true;
  }
}

export function setShortcutsEnabled(on: boolean): void {
  try {
    if (on) localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, "off");
  } catch {
    /* not saved: fine for this session */
  }
}
