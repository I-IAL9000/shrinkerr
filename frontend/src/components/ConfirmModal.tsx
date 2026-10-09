import { useState, useCallback, useMemo, useRef, createContext, useContext } from "react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useDialog } from "../useDialog";

interface ConfirmOptions {
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
}

interface ConfirmContextType {
  confirm: (options: ConfirmOptions) => Promise<boolean>;
}

const ConfirmContext = createContext<ConfirmContextType | null>(null);

export function useConfirm(): (options: ConfirmOptions | string) => Promise<boolean> {
  const ctx = useContext(ConfirmContext);
  if (!ctx) throw new Error("useConfirm must be used within ConfirmProvider");
  return useCallback(
    (opts: ConfirmOptions | string) =>
      ctx.confirm(typeof opts === "string" ? { message: opts } : opts),
    [ctx]
  );
}

interface PendingConfirm extends ConfirmOptions {
  id: number;
  resolve: (v: boolean) => void;
}

export function ConfirmProvider({ children }: { children: ReactNode }) {
  // FE#9 (v0.10.0): a confirm asked while another is open waits its turn. It
  // used to replace the open one — e.g. the remux offer from a background
  // poll over "Trash N files?" — whose promise then never settled.
  const [queue, setQueue] = useState<PendingConfirm[]>([]);
  const nextId = useRef(0);

  const confirm = useCallback((options: ConfirmOptions): Promise<boolean> => {
    return new Promise<boolean>((resolve) => {
      setQueue(q => [...q, { ...options, resolve, id: nextId.current++ }]);
    });
  }, []);

  const current = queue[0];
  const answer = (value: boolean) => {
    current?.resolve(value);
    setQueue(q => q.slice(1));
  };

  // A stable value (FE#5): a new object each render re-rendered every
  // consumer — tree rows, job cards — on every progress tick.
  const value = useMemo(() => ({ confirm }), [confirm]);

  return (
    <ConfirmContext.Provider value={value}>
      {children}
      {current && <ConfirmDialog key={current.id} request={current} onAnswer={answer} />}
    </ConfirmContext.Provider>
  );
}

function ConfirmDialog({ request, onAnswer }: { request: ConfirmOptions; onAnswer: (v: boolean) => void }) {
  const { t } = useTranslation(["nav", "common"]);
  const panelRef = useRef<HTMLDivElement>(null);
  const dialog = useDialog(panelRef, () => onAnswer(false), request.message);
  return (
    <div
      onClick={() => onAnswer(false)}
      style={{
        position: "fixed", inset: 0, zIndex: 9999,
        background: "rgba(0, 0, 0, 0.6)",
        display: "flex", alignItems: "center", justifyContent: "center",
      }}
    >
      <div
        ref={panelRef}
        {...dialog}
        role="alertdialog"
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "var(--bg-card)",
          border: "1px solid var(--border)",
          borderRadius: 8,
          padding: "24px 28px",
          maxWidth: 420,
          width: "90%",
          boxShadow: "0 8px 32px rgba(0, 0, 0, 0.5)",
        }}
      >
        <div style={{ color: "var(--text-secondary)", fontSize: 14, lineHeight: 1.5, marginBottom: 20, whiteSpace: "pre-line" }}>
          {request.message}
        </div>
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <button
            onClick={() => onAnswer(false)}
            className="btn btn-secondary"
            data-autofocus={request.danger ? true : undefined}
            style={{ fontSize: 13, padding: "6px 16px" }}
          >
            {request.cancelLabel || t("common:actions.cancel")}
          </button>
          <button
            onClick={() => onAnswer(true)}
            className="btn btn-primary"
            data-autofocus={request.danger ? undefined : true}
            style={{
              fontSize: 13, padding: "6px 16px",
              ...(request.danger ? { background: "var(--danger)", borderColor: "var(--danger)" } : {}),
            }}
          >
            {request.confirmLabel || t("common:actions.confirm")}
          </button>
        </div>
      </div>
    </div>
  );
}
