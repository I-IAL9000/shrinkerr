import type { KeyboardEvent } from "react";

/**
 * Props that make a clickable non-button element usable from the keyboard
 * (FE#25, v0.10.0): focusable, announced as a button, activated by Enter or
 * Space — only when it has the focus itself, so the checkboxes and buttons
 * inside it keep their own keys.
 */
export function pressable(onActivate: () => void, opts: { expanded?: boolean } = {}) {
  return {
    role: "button" as const,
    tabIndex: 0,
    "aria-expanded": opts.expanded,
    onKeyDown: (e: KeyboardEvent) => {
      if (e.target !== e.currentTarget) return;
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onActivate();
      }
    },
  };
}
