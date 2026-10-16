import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { createPortal } from "react-dom";
import Chevron from "./Chevron";

export interface ActionMenuItem {
  label: string;
  onSelect: () => void;
  danger?: boolean;
}

/**
 * A button that opens a list of actions (FE#28, v0.10.0). The Queue's bulk
 * actions were <select>s that applied on change — with the keyboard every
 * arrow press applied a preset to all selected jobs, and arrowing back to the
 * placeholder applied the first one. Here nothing happens until an item is
 * chosen: click, or arrows then Enter. Escape / Tab / a click outside close.
 */
export default function ActionMenu({ label, items, buttonStyle, icon, buttonClassName, align = "left", floating = false }: {
  label: string;
  items: ActionMenuItem[];
  buttonStyle?: CSSProperties;
  // An icon button instead of the label (`label` names it): a poster's ⋯ (v0.10.0).
  icon?: ReactNode;
  buttonClassName?: string;
  align?: "left" | "right";
  // Over the page, not inside the button's box (v0.10.0): a poster clips
  // what overflows it, and the menu was cut off.
  floating?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [at, setAt] = useState<CSSProperties>({});
  const wrapRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([]);

  useEffect(() => {
    if (!open) return;
    itemRefs.current[0]?.focus();
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (!wrapRef.current?.contains(target) && !menuRef.current?.contains(target)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    // A floating menu stays where the button was: it closes when the page moves.
    const onMove = (e: Event) => { if (!menuRef.current?.contains(e.target as Node)) setOpen(false); };
    if (floating) {
      window.addEventListener("scroll", onMove, true);
      window.addEventListener("resize", onMove);
    }
    return () => {
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("scroll", onMove, true);
      window.removeEventListener("resize", onMove);
    };
  }, [open, floating]);

  const toggle = () => {
    if (!open && floating && buttonRef.current) {
      const box = buttonRef.current.getBoundingClientRect();
      const tall = items.length * 30 + 12;  // about: opens upwards when it wouldn't fit below
      const vertical = box.bottom + 4 + tall > window.innerHeight
        ? { bottom: window.innerHeight - box.top + 4 } : { top: box.bottom + 4 };
      const horizontal = align === "right" ? { right: window.innerWidth - box.right } : { left: box.left };
      setAt({ position: "fixed", ...vertical, ...horizontal, zIndex: 1000 });
    }
    setOpen(o => !o);
  };

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
        onClick={toggle}
        onKeyDown={(e) => { if (e.key === "ArrowDown" && !open) { e.preventDefault(); setOpen(true); } }}
        className={buttonClassName}
        aria-label={icon ? label : undefined}
        title={icon ? label : undefined}
        style={icon ? buttonStyle : { ...buttonStyle, display: "inline-flex", alignItems: "center", gap: 6 }}
      >
        {icon ?? <>{label}<Chevron /></>}
      </button>
      {open && ((menu: ReactNode) => (floating ? createPortal(menu, document.body) : menu))(
        <div
          ref={menuRef}
          role="menu"
          aria-label={label}
          onKeyDown={onMenuKey}
          style={{
            ...(floating ? at : { position: "absolute", top: "calc(100% + 4px)", [align]: 0, zIndex: 50, minWidth: "100%" }),
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
              className={`action-menu-item${item.danger ? " danger" : ""}`}
              onClick={() => { close(true); item.onSelect(); }}
            >
              {item.label}
            </button>
          ))}
        </div>,
      )}
    </div>
  );
}
