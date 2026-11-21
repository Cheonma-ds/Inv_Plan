import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { ShieldAlert, X } from 'lucide-react';
import { api } from '../api/client';
import type { EscalateProcurementResponse, PODetail } from '../api/types';
import { poStatusBadgeClass } from '../utils/format';

interface ProcurementEscalationModalProps {
  po: PODetail;
  onClose: () => void;
  onEscalated: (result: EscalateProcurementResponse) => void;
}

export function ProcurementEscalationModal({
  po,
  onClose,
  onEscalated,
}: ProcurementEscalationModalProps) {
  const [reason, setReason] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !submitting) {
        onClose();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose, submitting]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const res = await api.escalatePoToProcurement(po.id, reason.trim());
      onEscalated(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  };

  if (typeof document === 'undefined') return null;

  return createPortal(
    <div
      onClick={() => {
        if (!submitting) onClose();
      }}
      className="anim-backdrop fixed inset-0 z-50 flex items-center justify-center bg-black/32 backdrop-blur-xs p-4"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="procurement-escalation-title"
        onClick={(e) => e.stopPropagation()}
        className="anim-modal-pop w-full max-w-lg rounded-2xl border border-black/[0.08] bg-white shadow-[var(--shadow-elevated)] overflow-hidden"
      >
        <div className="flex items-center justify-between border-b border-black/[0.08] px-5 py-4">
          <div className="flex items-center gap-2">
            <span className="flex h-8 w-8 items-center justify-center rounded-full bg-[#C9342D]/10 text-[#B82923]">
              <ShieldAlert className="h-4 w-4" />
            </span>
            <div>
              <h3
                id="procurement-escalation-title"
                className="text-sm font-bold text-[#1D1D1F]"
              >
                Escalate to Procurement
              </h3>
              <p className="text-[11px] text-[#6E6E73]">
                Sends an internal escalation email to Procurement (CC Lex) and creates a Procurement alert.
              </p>
            </div>
          </div>
          <button
            type="button"
            aria-label="Close escalation modal"
            disabled={submitting}
            onClick={onClose}
            className="apple-press rounded-full p-1 text-[#86868B] hover:bg-black/[0.05] hover:text-[#1D1D1F] disabled:opacity-45"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-5 space-y-4">
          <div className="rounded-xl border border-black/[0.08] bg-[#FBFBFD] p-3.5 grid grid-cols-2 gap-3 text-xs tabular-nums">
            <div>
              <div className="text-[11px] text-[#86868B]">PO Number</div>
              <div className="mt-0.5 font-mono font-bold text-[#1D1D1F]">
                {po.po_number}
              </div>
            </div>
            <div>
              <div className="text-[11px] text-[#86868B]">Part SKU</div>
              <div className="mt-0.5 font-mono font-semibold text-[#1D1D1F]">
                {po.part_sku ?? `Part #${po.part_id}`}
              </div>
            </div>
            <div>
              <div className="text-[11px] text-[#86868B]">Supplier</div>
              <div className="mt-0.5 font-medium text-[#1D1D1F]">
                {po.supplier_name ?? `Supplier #${po.supplier_id}`}
              </div>
            </div>
            <div>
              <div className="text-[11px] text-[#86868B]">Follow-ups Sent</div>
              <div className="mt-0.5 font-semibold text-[#1D1D1F]">
                {po.follow_up_count}
              </div>
            </div>
            <div className="col-span-2">
              <div className="text-[11px] text-[#86868B]">Current Status</div>
              <div className="mt-1">
                <span
                  className={`inline-block rounded-full border px-2.5 py-0.5 text-[11px] font-semibold ${poStatusBadgeClass(
                    po.status,
                  )}`}
                >
                  {po.status}
                </span>
              </div>
            </div>
          </div>

          <div>
            <label
              htmlFor="procurement-escalation-reason"
              className="block text-xs font-semibold text-[#1D1D1F] mb-1.5"
            >
              Escalation Reason
            </label>
            <textarea
              id="procurement-escalation-reason"
              rows={3}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="e.g., Supplier unresponsive after follow-ups; missing dispatch & tracking details."
              className="w-full rounded-xl border border-black/[0.12] bg-white px-3 py-2 text-xs text-[#1D1D1F] placeholder:text-[#86868B] focus:border-[#0066CC] focus:outline-none"
            />
          </div>

          {error && (
            <div className="rounded-xl border border-[#C9342D]/25 bg-[#C9342D]/[0.06] px-3 py-2 text-xs text-[#B82923]">
              {error}
            </div>
          )}

          <div className="flex items-center justify-end gap-2 pt-1">
            <button
              type="button"
              disabled={submitting}
              onClick={onClose}
              className="apple-press rounded-xl border border-black/[0.08] bg-white px-3.5 py-1.5 text-xs font-semibold text-[#6E6E73] hover:bg-[#FBFBFD] disabled:opacity-45"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="apple-press inline-flex items-center gap-1.5 rounded-xl bg-[#C9342D] px-4 py-1.5 text-xs font-semibold text-white hover:bg-[#B82923] disabled:opacity-45"
            >
              <ShieldAlert className="h-3.5 w-3.5" />
              {submitting ? 'Escalating...' : 'Confirm Escalation'}
            </button>
          </div>
        </form>
      </div>
    </div>,
    document.body,
  );
}
