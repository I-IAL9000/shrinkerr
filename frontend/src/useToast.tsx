import { useState, useCallback, createContext, useContext } from "react";
import { useTranslation } from "react-i18next";

interface Toast {
  id: number;
  message: string;
  type?: "info" | "success" | "error";
}

let nextId = 0;

// FE#27 (v0.10.0): toasts were silent to screen readers and every one —
// errors included — vanished after 3 s with no way to keep it. Now they're
// announced, info / success stay 5 s, errors stay until dismissed, and each
// has a close button. At most five at a time (the oldest go).
const AUTO_DISMISS_MS = 5000;
const MAX_TOASTS = 5;

export function useToastState() {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const addToast = useCallback((message: string, type: "info" | "success" | "error" = "info") => {
    const id = nextId++;
    setToasts((prev) => [...prev, { id, message, type }].slice(-MAX_TOASTS));
    if (type !== "error") setTimeout(() => dismiss(id), AUTO_DISMISS_MS);
  }, [dismiss]);

  return { toasts, addToast, dismiss };
}

const ToastContext = createContext<(message: string, type?: "info" | "success" | "error") => void>(() => {});

export const ToastProvider = ToastContext.Provider;
export const useToast = () => useContext(ToastContext);

export function ToastContainer({ toasts, onDismiss }: { toasts: Toast[]; onDismiss: (id: number) => void }) {
  const { t } = useTranslation("common");
  // Always rendered: a live region must exist before content is added to it
  // for screen readers to announce that content.
  return (
    <div className="toast-container" aria-live="polite">
      {toasts.map((toast) => (
        <div
          key={toast.id}
          role={toast.type === "error" ? "alert" : "status"}
          className={`toast ${toast.type === "success" ? "success" : toast.type === "error" ? "error" : ""}`}
        >
          <span>{toast.message}</span>
          <button className="toast-close" onClick={() => onDismiss(toast.id)} aria-label={t("actions.close")}>×</button>
        </div>
      ))}
    </div>
  );
}
