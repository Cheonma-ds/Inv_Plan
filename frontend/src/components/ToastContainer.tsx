import { AlertCircle, CheckCircle2, Info, X } from 'lucide-react';

export interface ToastMessage {
  id: number;
  kind: 'success' | 'error' | 'info';
  title: string;
  detail?: string;
}

interface ToastContainerProps {
  toasts: ToastMessage[];
  onDismiss: (id: number) => void;
}

export function ToastContainer({ toasts, onDismiss }: ToastContainerProps) {
  if (toasts.length === 0) return null;

  return (
    <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 max-w-md w-full pointer-events-none">
      {toasts.map((t) => {
        const tone =
          t.kind === 'error'
            ? 'border-red-300 bg-red-50 text-red-900'
            : t.kind === 'success'
              ? 'border-emerald-300 bg-emerald-50 text-emerald-900'
              : 'border-sky-300 bg-sky-50 text-sky-900';

        const Icon =
          t.kind === 'error'
            ? AlertCircle
            : t.kind === 'success'
              ? CheckCircle2
              : Info;

        return (
          <div
            key={t.id}
            className={`pointer-events-auto flex items-start gap-2.5 rounded-lg border p-3 shadow-lg ${tone}`}
          >
            <Icon className="h-4 w-4 mt-0.5 shrink-0" />
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium">{t.title}</p>
              {t.detail && (
                <p className="text-xs opacity-90 mt-0.5 break-words">{t.detail}</p>
              )}
            </div>
            <button
              type="button"
              onClick={() => onDismiss(t.id)}
              className="rounded p-0.5 opacity-70 hover:opacity-100"
              aria-label="Close notification"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        );
      })}
    </div>
  );
}
