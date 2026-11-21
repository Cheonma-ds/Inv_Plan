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
    <div className="fixed bottom-5 right-5 z-50 flex flex-col gap-2.5 max-w-md w-full pointer-events-none">
      {toasts.map((t) => {
        const tone =
          t.kind === 'error'
            ? 'border-[#C9342D]/25 bg-white/92 text-[#1D1D1F]'
            : t.kind === 'success'
              ? 'border-[#1F7A4D]/25 bg-white/92 text-[#1D1D1F]'
              : 'border-[#0066CC]/25 bg-white/92 text-[#1D1D1F]';

        const iconTone =
          t.kind === 'error'
            ? 'text-[#B82923]'
            : t.kind === 'success'
              ? 'text-[#1B6B43]'
              : 'text-[#0066CC]';

        const Icon =
          t.kind === 'error'
            ? AlertCircle
            : t.kind === 'success'
              ? CheckCircle2
              : Info;

        return (
          <div
            key={t.id}
            className={`anim-toast-in pointer-events-auto flex items-start gap-2.5 rounded-2xl border backdrop-blur-xl px-4 py-3 shadow-[0_12px_32px_rgba(0,0,0,0.10)] ${tone}`}
          >
            <Icon className={`h-4 w-4 mt-0.5 shrink-0 ${iconTone}`} />
            <div className="flex-1 min-w-0">
              <p className="text-sm font-semibold text-[#1D1D1F]">{t.title}</p>
              {t.detail && (
                <p className="text-xs text-[#6E6E73] mt-0.5 break-words">{t.detail}</p>
              )}
            </div>
            <button
              type="button"
              onClick={() => onDismiss(t.id)}
              className="apple-press rounded-full p-1 text-[#86868B] hover:bg-black/[0.05] hover:text-[#1D1D1F]"
              aria-label="Close notification"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        );
      })}
    </div>
  );
}
