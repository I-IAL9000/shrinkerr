import { useEffect, useRef, type RefObject } from "react";

// Accessible dialogs (FE#9, v0.10.0). No dialog had a role, set focus,
// trapped it or handled Escape (only the changelog did), so focus stayed on
// the button behind the scrim — Enter fired it again — and screen readers
// didn't know a dialog was open.

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

// Open dialogs, innermost last: only the top one answers Escape and Tab.
const stack: HTMLElement[] = [];

/** True while any dialog is open (keyboard shortcuts stay out of the way). */
export const dialogOpen = () => stack.length > 0;

/**
 * Make `panel` a modal dialog while mounted: role and label, focus moved in
 * (to `[data-autofocus]`, else the first control), Tab kept inside, Escape
 * calls `onClose`, and focus returns to where it was on close. Spread the
 * returned props onto the panel. `active`: for a dialog that stays mounted
 * while closed.
 */
export function useDialog(panel: RefObject<HTMLElement | null>, onClose: (() => void) | undefined, label: string, active = true) {
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    const el = panel.current;
    if (!active || !el) return;
    const previous = document.activeElement as HTMLElement | null;
    stack.push(el);
    const focusables = () =>
      [...el.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(f => f.offsetParent !== null || f === document.activeElement);
    (el.querySelector<HTMLElement>("[data-autofocus]") || focusables()[0] || el).focus();

    const onKey = (e: KeyboardEvent) => {
      if (stack[stack.length - 1] !== el) return;
      if (e.key === "Escape" && closeRef.current) {
        e.preventDefault();
        e.stopPropagation();
        closeRef.current();
        return;
      }
      if (e.key !== "Tab") return;
      const items = focusables();
      if (items.length === 0) {
        e.preventDefault();
        el.focus();
        return;
      }
      const first = items[0], last = items[items.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && (active === first || !el.contains(active))) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (active === last || !el.contains(active))) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      stack.splice(stack.indexOf(el), 1);
      if (previous && document.contains(previous)) previous.focus();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);

  return { role: "dialog" as const, "aria-modal": true, "aria-label": label, tabIndex: -1 };
}
