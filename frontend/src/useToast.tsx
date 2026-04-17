import { useState, useCallback, createContext, useContext } from "react";

interface Toast {
  id: number;
  message: string;
  type?: "info" | "success" | "error";
}

let nextId = 0;

export function useToastState() {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const addToast = useCallback((message: string, type: "info" | "success" | "error" = "info") => {
    const id = nextId++;
    setToasts((prev) => [...prev, { id, message, type }]);
    setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), 3000);
  }, []);

  return { toasts, addToast };
}

const ToastContext = createContext<(message: string, type?: "info" | "success" | "error") => void>(() => {});

export const ToastProvider = ToastContext.Provider;
export const useToast = () => useContext(ToastContext);

export function ToastContainer({ toasts }: { toasts: Toast[] }) {
  if (toasts.length === 0) return null;
  return (
    <div className="toast-container">
      {toasts.map((t) => (
        <div key={t.id} className={`toast ${t.type === "success" ? "success" : t.type === "error" ? "error" : ""}`}>
          {t.message}
        </div>
      ))}
    </div>
  );
}
