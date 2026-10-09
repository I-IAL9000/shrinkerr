import { useEffect, useRef, useState, type CSSProperties } from "react";

export interface ActionMenuItem {
  label: string;
  onSelect: () => void;
}

/**
 * A button that opens a list of actions (FE#28, v0.10.0). The Queue's bulk
 * actions were <select>s that applied on change — with the keyboard every
 * arrow press applied a preset to all selected jobs, and arrowing back to the
 * placeholder applied the first one. Here nothing happens until an item is
 * chosen: click, or arrows then Enter. Escape / Tab / a click outside close.
 */
export default function ActionMenu({ label, items, buttonStyle }: {
  label: string;
  items: ActionMenuItem[];
  buttonStyle?: CSSProperties;
}) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([]);

  useEffect(() => {
    if (!open) return;
    itemRefs.current[0]?.focus();
    const onDown = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const close = (refocus: boolean) => {
    setOpen(false);
    if (refocus) buttonRef.current?.focus();
  };

  const onMenuKey = (e: React.KeyboardEvent) => {
    const list = itemRefs.current.filter(Boolean) as HTMLButtonElement[];
    const at = list.indexOf(document.activeElement as HTMLButtonElement);
    const move = (i: number) => { e.preventDefault(); list[(i + list.length) % list.length]?.focus(); };
    if (e.key === "ArrowDown") move(at + 1);
    else if (e.key === "ArrowUp") move(at - 1);
    else if (e.key === "Home") move(0);
    else if (e.key === "End") move(list.length - 1);
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(true); }
    else if (e.key === "Tab") close(false);
  };

  return (
    <div ref={wrapRef} style={{ position: "relative", display: "inline-block" }}>
      <button
        ref={buttonRef}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen(o => !o)}
        onKeyDown={(e) => { if (e.key === "ArrowDown" && !open) { e.preventDefault(); setOpen(true); } }}
        style={{ ...buttonStyle, display: "inline-flex", alignItems: "center", gap: 6 }}
      >
        {label}
        <span aria-hidden="true" style={{ fontSize: 8 }}>{"▼"}</span>
      </button>
      {open && (
        <div
          role="menu"
          aria-label={label}
          onKeyDown={onMenuKey}
          style={{
            position: "absolute", top: "calc(100% + 4px)", left: 0, zIndex: 50, minWidth: "100%",
            background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 6,
            boxShadow: "0 6px 20px rgba(0,0,0,0.35)", padding: 4,
            display: "flex", flexDirection: "column",
          }}
        >
          {items.map((item, i) => (
            <button
              key={i}
              ref={el => { itemRefs.current[i] = el; }}
              type="button"
              role="menuitem"
              className="action-menu-item"
              onClick={() => { close(true); item.onSelect(); }}
            >
              {item.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
