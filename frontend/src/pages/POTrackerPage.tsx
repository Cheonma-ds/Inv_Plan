import { useCallback, useEffect, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  ArrowRightLeft,
  CheckCircle2,
  ClipboardCheck,
  Clock,
  FileText,
  Mail,
  PackageCheck,
  RefreshCw,
  Search,
  ShieldAlert,
  Truck,
  X,
} from 'lucide-react';
import { api } from '../api/client';
import type {
  EscalateProcurementResponse,
  PODetail,
  POListItem,
} from '../api/types';
import { DataTable, type ColumnDef } from '../components/DataTable';
import { OrderTrackingStepper } from '../components/OrderTrackingStepper';
import { ProcurementEscalationModal } from '../components/ProcurementEscalationModal';
import {
  formatInr,
  formatSimDate,
  formatSimDateTime,
  personaLabel,
  poStatusBadgeClass,
} from '../utils/format';

interface POTrackerPageProps {
  refreshNonce: number;
  focusPoId: number | null;
  onClearFocus: () => void;
  onMutated: () => void;
  onNotify: (kind: 'success' | 'error' | 'info', title: string, detail?: string) => void;
}

const TERMINAL_STATUSES = new Set(['CLOSED', 'CANCELLED']);
const GRN_ALLOWED_STATUSES = new Set([
  'ACKNOWLEDGED',
  'DELAYED_CRITICAL',
  'OVERDUE',
]);

export function POTrackerPage({
  refreshNonce,
  focusPoId,
  onClearFocus,
  onMutated,
  onNotify,
}: POTrackerPageProps) {
  const [pos, setPos] = useState<POListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState<string>('OPEN');
  const [search, setSearch] = useState('');
  const [selectedPoId, setSelectedPoId] = useState<number | null>(null);
  const [poDetail, setPoDetail] = useState<PODetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  // Action states inside drawer
  const [grnQty, setGrnQty] = useState('');
  const [grnCondition, setGrnCondition] = useState('GOOD');
  const [noteInput, setNoteInput] = useState('');
  const [busyAction, setBusyAction] = useState(false);
  const [showEscalationModal, setShowEscalationModal] = useState(false);

  const loadPos = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.listPos();
      setPos(data);
    } catch (err) {
      onNotify(
        'error',
        'Failed to load purchase orders',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setLoading(false);
    }
  }, [onNotify]);

  const loadDetail = useCallback(
    async (poId: number) => {
      setDetailLoading(true);
      try {
        const detail = await api.getPoDetail(poId);
        setPoDetail(detail);
        setGrnQty(String(detail.qty));
        setGrnCondition('GOOD');
      } catch (err) {
        onNotify(
          'error',
          `Failed to load PO #${poId}`,
          err instanceof Error ? err.message : String(err),
        );
      } finally {
        setDetailLoading(false);
      }
    },
    [onNotify],
  );

  useEffect(() => {
    void loadPos();
  }, [loadPos, refreshNonce]);

  useEffect(() => {
    if (focusPoId !== null) {
      setStatusFilter('ALL');
      setSelectedPoId(focusPoId);
      onClearFocus();
    }
  }, [focusPoId, onClearFocus]);

  useEffect(() => {
    setShowEscalationModal(false);
    if (selectedPoId !== null) {
      void loadDetail(selectedPoId);
    } else {
      setPoDetail(null);
    }
  }, [selectedPoId, loadDetail, refreshNonce]);

  useEffect(() => {
    if (selectedPoId === null || showEscalationModal) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setSelectedPoId(null);
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [selectedPoId, showEscalationModal]);

  const statusOptions = useMemo(() => {
    const distinct = Array.from(new Set(pos.map((p) => p.status))).sort();
    return ['OPEN', 'ALL', ...distinct];
  }, [pos]);

  const filteredPos = useMemo(() => {
    const q = search.trim().toLowerCase();
    return pos.filter((po) => {
      if (statusFilter === 'OPEN' && TERMINAL_STATUSES.has(po.status)) return false;
      if (statusFilter !== 'OPEN' && statusFilter !== 'ALL' && po.status !== statusFilter) {
        return false;
      }
      if (!q) return true;
      return (
        po.po_number.toLowerCase().includes(q) ||
        (po.part_sku ?? '').toLowerCase().includes(q) ||
        (po.part_name ?? '').toLowerCase().includes(q) ||
        (po.supplier_name ?? '').toLowerCase().includes(q) ||
        (po.carrier_name ?? '').toLowerCase().includes(q) ||
        (po.tracking_number ?? '').toLowerCase().includes(q)
      );
    });
  }, [pos, statusFilter, search]);

  const handleRecordGrn = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!poDetail) return;
    const qty = Number.parseInt(grnQty, 10);
    if (!Number.isFinite(qty) || qty <= 0) {
      onNotify('error', 'Invalid GRN quantity', 'Enter a positive integer.');
      return;
    }
    setBusyAction(true);
    try {
      const res = await api.recordGrn(poDetail.id, qty, grnCondition);
      onNotify(
        'success',
        `Logged GRN for ${poDetail.po_number} (${res.received_qty} units)`,
        `PO status → ${res.po_status}${
          res.match_result ? ` · Match: ${res.match_result.match_status}` : ''
        }`,
      );
      onMutated();
      await Promise.all([loadPos(), loadDetail(poDetail.id)]);
    } catch (err) {
      onNotify(
        'error',
        'GRN logging failed',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setBusyAction(false);
    }
  };

  const handleAddNote = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!poDetail || !noteInput.trim()) return;
    setBusyAction(true);
    try {
      await api.updatePo(poDetail.id, { note: noteInput.trim() });
      setNoteInput('');
      onNotify('success', `Note saved on ${poDetail.po_number}`);
      onMutated();
      await Promise.all([loadPos(), loadDetail(poDetail.id)]);
    } catch (err) {
      onNotify(
        'error',
        'Failed to save note',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setBusyAction(false);
    }
  };

  const handleSwitchBackup = async () => {
    if (!poDetail) return;
    setBusyAction(true);
    try {
      const res = await api.switchPoSupplier(poDetail.id);
      onNotify(
        'success',
        `Switched ${poDetail.po_number} to backup supplier`,
        `Created ${res.new_po.po_number} with ${res.new_po.supplier_name ?? 'backup supplier'}.`,
      );
      onMutated();
      setSelectedPoId(res.new_po.id);
      await loadPos();
    } catch (err) {
      onNotify(
        'error',
        'Switch to backup supplier failed',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setBusyAction(false);
    }
  };

  const handleEscalated = async (res: EscalateProcurementResponse) => {
    setShowEscalationModal(false);
    onNotify(
      'success',
      `Escalated ${res.po.po_number} to Procurement`,
      `Internal escalation email delivered to Procurement inbox and alert #${res.alert_id} created.`,
    );
    onMutated();
    await Promise.all([loadPos(), loadDetail(res.po.id)]);
  };

  const poColumns = useMemo<ColumnDef<POListItem>[]>(
    () => [
      {
        key: 'po_number',
        header: 'PO Number',
        tooltip:
          'Unique Purchase Order identifier assigned when replenishment is triggered. Click any row to open the full lifecycle, GRN, invoice match, and linked email drawer.',
        defaultWidth: 105,
        minWidth: 90,
        cellClassName: 'font-mono font-bold text-[#1D1D1F]',
        renderCell: (po) => po.po_number,
      },
      {
        key: 'part',
        header: 'Part',
        tooltip: 'SKU code and part description ordered on this Purchase Order.',
        defaultWidth: 205,
        minWidth: 145,
        renderCell: (po) => (
          <>
            <div className="font-mono font-semibold text-[#1D1D1F]">
              {po.part_sku ?? `Part #${po.part_id}`}
            </div>
            <div className="text-[#6E6E73]">{po.part_name}</div>
          </>
        ),
      },
      {
        key: 'supplier',
        header: 'Supplier',
        tooltip: 'Assigned vendor fulfilling this Purchase Order (Primary or Backup supplier).',
        defaultWidth: 175,
        minWidth: 125,
        cellClassName: 'font-medium text-[#1D1D1F]',
        renderCell: (po) => po.supplier_name ?? `Supplier #${po.supplier_id}`,
      },
      {
        key: 'qty',
        header: 'Quantity',
        tooltip:
          "Total order quantity in units, rounded up to meet the supplier's Minimum Order Quantity (MOQ).",
        defaultWidth: 85,
        minWidth: 75,
        align: 'right',
        cellClassName: 'font-semibold tabular-nums text-[#1D1D1F]',
        renderCell: (po) => po.qty,
      },
      {
        key: 'unit_price',
        header: 'Unit Price',
        tooltip:
          'Agreed contract price per unit (INR) used as the baseline during 3-way invoice matching.',
        defaultWidth: 100,
        minWidth: 85,
        align: 'right',
        cellClassName: 'tabular-nums text-[#6E6E73]',
        renderCell: (po) => formatInr(po.unit_price),
      },
      {
        key: 'total_amount',
        header: 'Total Value',
        tooltip: 'Total PO commitment value (Quantity × Agreed Unit Price in INR).',
        defaultWidth: 110,
        minWidth: 90,
        align: 'right',
        cellClassName: 'font-semibold tabular-nums text-[#1D1D1F]',
        renderCell: (po) => formatInr(po.total_amount),
      },
      {
        key: 'status',
        header: 'Status',
        tooltip:
          'Current PO workflow state: PENDING_DISPATCH_APPROVAL (draft awaiting Manager send), SENT_AWAITING_ACK, ACKNOWLEDGED, EXCEPTION_REVIEW, UNRESPONSIVE_ESCALATED, DELAYED_CRITICAL, OVERDUE, RECEIVED_PENDING_INVOICE, QUARANTINED_INVOICE_MISMATCH, CLOSED, or CANCELLED.',
        defaultWidth: 195,
        minWidth: 140,
        renderCell: (po) => (
          <span
            className={`inline-block max-w-full truncate align-middle rounded-full border px-2.5 py-0.5 text-[11px] font-semibold ${poStatusBadgeClass(
              po.status,
            )}`}
            title={po.status}
          >
            {po.status}
          </span>
        ),
      },
      {
        key: 'eta',
        header: 'Promised / Revised ETA',
        tooltip:
          'Original promised delivery date based on quoted lead time, plus any revised ETA communicated by the supplier in email updates.',
        defaultWidth: 145,
        minWidth: 125,
        cellClassName: 'text-[#6E6E73] tabular-nums',
        renderCell: (po) => (
          <>
            <div>{formatSimDate(po.promised_eta)}</div>
            {po.revised_eta && (
              <div className="text-[#945600] font-medium">
                Rev: {formatSimDate(po.revised_eta)}
              </div>
            )}
          </>
        ),
      },
      {
        key: 'dispatch',
        header: 'Dispatch',
        tooltip:
          'Promised or actual shipment dispatch date from the supplier, plus missing dispatch details status (2-day waiting or overdue nudge).',
        defaultWidth: 145,
        minWidth: 120,
        cellClassName: 'text-[#6E6E73] tabular-nums',
        renderCell: (po) => (
          <div className="space-y-0.5">
            <div>
              {po.actual_dispatch_date
                ? `Shipped: ${formatSimDate(po.actual_dispatch_date)}`
                : po.promised_dispatch_date
                  ? `Prom: ${formatSimDate(po.promised_dispatch_date)}`
                  : '—'}
            </div>
            {po.dispatch_details_status === 'DEFERRED_2D' && (
              <span className="inline-block rounded-full border border-[#B46900]/25 bg-[#B46900]/10 px-1.5 py-0.5 text-[10px] font-semibold text-[#945600]">
                2d Wait
              </span>
            )}
            {po.dispatch_details_status === 'AWAITING_IMMEDIATE' && (
              <span className="inline-block rounded-full border border-[#0066CC]/22 bg-[#0066CC]/10 px-1.5 py-0.5 text-[10px] font-semibold text-[#0058B3]">
                Details Needed
              </span>
            )}
            {po.dispatch_details_status === 'TIMED_OUT' && (
              <span className="inline-block rounded-full border border-[#C9342D]/25 bg-[#C9342D]/10 px-1.5 py-0.5 text-[10px] font-semibold text-[#B82923]">
                Details Overdue
              </span>
            )}
          </div>
        ),
      },
      {
        key: 'carrier',
        header: 'Carrier',
        tooltip: 'Delivery courier or logistics service assigned by the supplier (e.g., BlueDart, Delhivery, DTDC).',
        defaultWidth: 110,
        minWidth: 88,
        cellClassName: 'text-[#1D1D1F] font-medium',
        renderCell: (po) => po.carrier_name ?? '—',
      },
      {
        key: 'tracking',
        header: 'Tracking',
        tooltip: 'Shipment tracking number provided by the supplier for live transit verification.',
        defaultWidth: 135,
        minWidth: 105,
        cellClassName: 'font-mono text-[#6E6E73] tabular-nums',
        renderCell: (po) => po.tracking_number ?? '—',
      },
    ],
    [],
  );

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold tracking-tight text-[#1D1D1F]">Purchase Order Tracker</h1>
          <p className="text-sm text-[#6E6E73]">
            Track open &amp; historical POs, log goods receipts (GRN), inspect 3-way invoice matches, and switch suppliers.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void loadPos()}
          className="apple-press inline-flex items-center gap-1.5 rounded-full border border-black/[0.08] bg-white px-3.5 py-1.5 text-xs font-medium text-[#1D1D1F] hover:bg-[#FBFBFD] shadow-2xs"
        >
          <RefreshCw className={`h-3.5 w-3.5 text-[#6E6E73] ${loading ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <div className="relative flex-1 min-w-60 max-w-sm">
          <Search className="pointer-events-none absolute left-3.5 top-2.5 h-4 w-4 text-[#86868B]" />
          <input
            type="text"
            placeholder="Search PO#, SKU, supplier, or tracking..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full rounded-xl border border-black/[0.08] bg-white py-2 pl-9 pr-3 text-sm text-[#1D1D1F] placeholder:text-[#86868B] shadow-2xs focus:border-[#0066CC] focus:outline-none"
          />
        </div>

        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          aria-label="Filter PO status"
          className="rounded-xl border border-black/[0.08] bg-white px-3.5 py-2 text-xs font-semibold text-[#1D1D1F] shadow-2xs focus:border-[#0066CC] focus:outline-none"
        >
          {statusOptions.map((st) => (
            <option key={st} value={st}>
              {st === 'OPEN' ? 'Open POs' : st === 'ALL' ? 'All POs' : st}
            </option>
          ))}
        </select>
      </div>

      <DataTable
        storageKey="invplan_col_widths_po_tracker"
        ariaLabel="Purchase Orders"
        columns={poColumns}
        rows={filteredPos}
        rowKey={(po) => po.id}
        loading={loading}
        maxHeight="calc(100vh - 280px)"
        emptyMessage="No purchase orders match this filter."
        onRowClick={(po) => setSelectedPoId(po.id)}
        rowClassName={(po) =>
          selectedPoId === po.id ? 'bg-[#0066CC]/[0.07]' : 'hover:bg-black/[0.02]'
        }
      />

      {/* Slide-over detail drawer (portaled to body so page animations never clip fixed overlay) */}
      {selectedPoId !== null &&
        typeof document !== 'undefined' &&
        createPortal(
          <div
            onClick={() => setSelectedPoId(null)}
            className="anim-backdrop fixed inset-0 z-40 flex justify-end bg-black/28 backdrop-blur-xs"
          >
            <div
              role="dialog"
              aria-modal="true"
              aria-label={poDetail ? poDetail.po_number : `PO #${selectedPoId}`}
              onClick={(e) => e.stopPropagation()}
              className="anim-drawer-slide h-full w-full max-w-2xl overflow-y-auto bg-white shadow-[var(--shadow-drawer)] border-l border-black/[0.08] flex flex-col"
            >
              <div className="sticky top-0 z-10 flex items-center justify-between border-b border-black/[0.08] bg-white/88 backdrop-blur-xl px-6 py-4">
                <div>
                  <h2 className="text-lg font-bold tracking-tight text-[#1D1D1F]">
                    {poDetail ? poDetail.po_number : `PO #${selectedPoId}`}
                  </h2>
                  {poDetail && (
                    <p className="text-xs text-[#6E6E73]">
                      {poDetail.part_sku} · {poDetail.part_name} · {poDetail.supplier_name}
                    </p>
                  )}
                </div>
                <button
                  type="button"
                  aria-label="Close PO detail drawer"
                  onClick={() => setSelectedPoId(null)}
                  className="apple-press rounded-full p-1.5 text-[#86868B] hover:bg-black/[0.05] hover:text-[#1D1D1F]"
                >
                  <X className="h-5 w-5" />
                </button>
              </div>

              {detailLoading || !poDetail ? (
                <div className="p-6 text-sm text-[#6E6E73]">Loading PO details...</div>
              ) : (
                <div className="p-6 space-y-6">
                  {/* Summary KPIs */}
                  <div className="grid grid-cols-2 sm:grid-cols-[minmax(195px,1.6fr)_0.8fr_1fr_1fr] gap-3 text-xs tabular-nums">
                    <div className="min-w-0 rounded-xl border border-black/[0.07] bg-[#FBFBFD] p-3">
                      <div className="text-[#86868B]">Status</div>
                      <div className="mt-1.5">
                        <span
                          className={`inline-block max-w-full truncate align-middle rounded-full border px-2 py-0.5 text-[10px] font-semibold ${poStatusBadgeClass(
                            poDetail.status,
                          )}`}
                          title={poDetail.status}
                        >
                          {poDetail.status}
                        </span>
                      </div>
                    </div>
                  <div className="rounded-xl border border-black/[0.07] bg-[#FBFBFD] p-3">
                    <div className="text-[#86868B]">Order Qty</div>
                    <div className="mt-1 text-sm font-bold text-[#1D1D1F]">
                      {poDetail.qty} units
                    </div>
                  </div>
                  <div className="rounded-xl border border-black/[0.07] bg-[#FBFBFD] p-3">
                    <div className="text-[#86868B]">Unit Price</div>
                    <div className="mt-1 text-sm font-bold text-[#1D1D1F]">
                      {formatInr(poDetail.unit_price)}
                    </div>
                  </div>
                  <div className="rounded-xl border border-black/[0.07] bg-[#FBFBFD] p-3">
                    <div className="text-[#86868B]">Total Value</div>
                    <div className="mt-1 text-sm font-bold text-[#1D1D1F]">
                      {formatInr(poDetail.total_amount)}
                    </div>
                  </div>
                </div>

                {/* Horizontal Order Tracking Stepper & Shipment Logistics Details */}
                <section className="space-y-3">
                  <div className="flex items-center justify-between gap-2">
                    <h3 className="text-xs font-bold uppercase tracking-wider text-[#86868B]">
                      Order Tracking
                    </h3>
                    {poDetail.dispatch_details_status === 'DEFERRED_2D' && (
                      <span className="inline-flex items-center gap-1 rounded-full border border-[#B46900]/25 bg-[#B46900]/10 px-2.5 py-0.5 text-[11px] font-semibold text-[#945600]">
                        <Clock className="h-3 w-3" />
                        2-Day Waiting (Due {formatSimDate(poDetail.dispatch_details_due_at)})
                      </span>
                    )}
                    {poDetail.dispatch_details_status === 'AWAITING_IMMEDIATE' && (
                      <span className="inline-flex items-center gap-1 rounded-full border border-[#0066CC]/22 bg-[#0066CC]/10 px-2.5 py-0.5 text-[11px] font-semibold text-[#0058B3]">
                        <Clock className="h-3 w-3" />
                        Awaiting Dispatch Details
                      </span>
                    )}
                    {poDetail.dispatch_details_status === 'TIMED_OUT' && (
                      <span className="inline-flex items-center gap-1 rounded-full border border-[#C9342D]/25 bg-[#C9342D]/10 px-2.5 py-0.5 text-[11px] font-semibold text-[#B82923]">
                        <Clock className="h-3 w-3" />
                        Dispatch Details Overdue
                      </span>
                    )}
                    {poDetail.dispatch_details_status === 'COMPLETE' && (
                      <span className="inline-flex items-center gap-1 rounded-full border border-[#1F7A4D]/22 bg-[#1F7A4D]/10 px-2.5 py-0.5 text-[11px] font-semibold text-[#1B6B43]">
                        <CheckCircle2 className="h-3 w-3" />
                        Dispatch Details Complete
                      </span>
                    )}
                  </div>

                  <OrderTrackingStepper po={poDetail} />

                  {/* Shipment & Logistics Details Card */}
                  <div
                    data-testid="shipment-logistics-card"
                    className="rounded-2xl border border-black/[0.08] bg-white p-4 space-y-3"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <h4 className="text-xs font-bold uppercase tracking-wider text-[#1D1D1F] flex items-center gap-1.5">
                        <Truck className="h-3.5 w-3.5 text-[#0066CC]" />
                        Shipment &amp; Logistics Details
                      </h4>
                      {poDetail.missing_dispatch_fields.length > 0 && (
                        <span className="text-[11px] text-[#945600] font-medium">
                          Missing: {poDetail.missing_dispatch_fields.join(', ')}
                        </span>
                      )}
                    </div>

                    <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs tabular-nums">
                      <div className="rounded-xl border border-black/[0.06] bg-[#FBFBFD] px-3 py-2">
                        <div className="text-[11px] text-[#86868B]">Promised Dispatch</div>
                        <div className="mt-0.5 font-semibold text-[#1D1D1F]">
                          {formatSimDate(poDetail.promised_dispatch_date)}
                        </div>
                      </div>
                      <div className="rounded-xl border border-black/[0.06] bg-[#FBFBFD] px-3 py-2">
                        <div className="text-[11px] text-[#86868B]">Actual Dispatch</div>
                        <div className="mt-0.5 font-semibold text-[#1D1D1F]">
                          {formatSimDate(poDetail.actual_dispatch_date)}
                        </div>
                      </div>
                      <div className="rounded-xl border border-black/[0.06] bg-[#FBFBFD] px-3 py-2">
                        <div className="text-[11px] text-[#86868B]">Carrier</div>
                        <div className="mt-0.5 font-semibold text-[#1D1D1F]">
                          {poDetail.carrier_name ?? '—'}
                        </div>
                      </div>
                      <div className="rounded-xl border border-black/[0.06] bg-[#FBFBFD] px-3 py-2">
                        <div className="text-[11px] text-[#86868B]">Tracking Number</div>
                        <div className="mt-0.5 font-mono font-semibold text-[#1D1D1F] truncate" title={poDetail.tracking_number ?? undefined}>
                          {poDetail.tracking_number ?? '—'}
                        </div>
                      </div>
                      <div className="rounded-xl border border-black/[0.06] bg-[#FBFBFD] px-3 py-2">
                        <div className="text-[11px] text-[#86868B]">Promised ETA</div>
                        <div className="mt-0.5 font-medium text-[#1D1D1F]">
                          {formatSimDate(poDetail.promised_eta)}
                        </div>
                      </div>
                      <div className="rounded-xl border border-black/[0.06] bg-[#FBFBFD] px-3 py-2">
                        <div className="text-[11px] text-[#86868B]">Revised ETA</div>
                        <div className="mt-0.5 font-medium text-[#945600]">
                          {formatSimDate(poDetail.revised_eta)}
                        </div>
                      </div>
                      <div className="rounded-xl border border-black/[0.06] bg-[#FBFBFD] px-3 py-2">
                        <div className="text-[11px] text-[#86868B]">Follow-ups Sent</div>
                        <div className="mt-0.5 font-semibold text-[#1D1D1F]">
                          {poDetail.follow_up_count}
                        </div>
                      </div>
                      <div className="rounded-xl border border-black/[0.06] bg-[#FBFBFD] px-3 py-2">
                        <div className="text-[11px] text-[#86868B]">Pre-ETA Check</div>
                        <div className="mt-0.5 font-semibold text-[#1D1D1F]">
                          {poDetail.pre_eta_checked ? 'Done' : 'Pending'}
                        </div>
                      </div>
                    </div>

                    {poDetail.escalated_to_procurement_at && (
                      <div
                        data-testid="procurement-escalation-banner"
                        className="rounded-xl border border-[#C9342D]/25 bg-[#C9342D]/[0.05] px-3.5 py-2.5 text-xs space-y-1"
                      >
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <span className="inline-flex items-center gap-1.5 font-semibold text-[#B82923]">
                            <ShieldAlert className="h-3.5 w-3.5" />
                            Escalated to Procurement
                          </span>
                          <span className="text-[11px] text-[#6E6E73] tabular-nums">
                            {formatSimDateTime(poDetail.escalated_to_procurement_at)}
                          </span>
                        </div>
                        {poDetail.procurement_escalation_note && (
                          <p className="text-[#1D1D1F]">
                            {poDetail.procurement_escalation_note}
                          </p>
                        )}
                      </div>
                    )}
                  </div>
                </section>

                {/* Log GRN + Switch Backup + Escalate to Procurement */}
                {!TERMINAL_STATUSES.has(poDetail.status) && (
                  <section className="rounded-2xl border border-black/[0.08] bg-white p-4 space-y-4">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <h3 className="text-xs font-bold uppercase tracking-wider text-[#1D1D1F] flex items-center gap-1.5">
                        <PackageCheck className="h-4 w-4 text-[#1B6B43]" />
                        Warehouse Goods Receipt (GRN)
                      </h3>

                      <div className="flex flex-wrap items-center gap-2">
                        <button
                          type="button"
                          disabled={busyAction}
                          onClick={() => void handleSwitchBackup()}
                          className="apple-press inline-flex items-center gap-1.5 rounded-xl border border-[#B46900]/25 bg-[#B46900]/[0.08] px-3 py-1.5 text-xs font-semibold text-[#945600] hover:bg-[#B46900]/[0.14] disabled:opacity-45"
                        >
                          <ArrowRightLeft className="h-3.5 w-3.5" />
                          Switch to Backup Supplier
                        </button>
                        <button
                          type="button"
                          disabled={busyAction}
                          onClick={() => setShowEscalationModal(true)}
                          className="apple-press inline-flex items-center gap-1.5 rounded-xl border border-[#C9342D]/25 bg-[#C9342D]/[0.08] px-3 py-1.5 text-xs font-semibold text-[#B82923] hover:bg-[#C9342D]/[0.14] disabled:opacity-45"
                        >
                          <ShieldAlert className="h-3.5 w-3.5" />
                          Escalate to Procurement
                        </button>
                      </div>
                    </div>

                    {!GRN_ALLOWED_STATUSES.has(poDetail.status) ? (
                      <p className="text-xs text-[#6E6E73]">
                        GRN logging is unavailable while PO is in{' '}
                        <span className="font-semibold text-[#1D1D1F]">{poDetail.status}</span>{' '}
                        (requires ACKNOWLEDGED, DELAYED_CRITICAL, or OVERDUE).
                      </p>
                    ) : (
                      <form onSubmit={handleRecordGrn} className="flex flex-wrap items-end gap-3">
                        <div>
                          <label className="block text-[11px] font-semibold text-[#6E6E73] mb-1">
                            Received Qty
                          </label>
                          <input
                            type="number"
                            min={1}
                            value={grnQty}
                            onChange={(e) => setGrnQty(e.target.value)}
                            required
                            className="w-28 rounded-xl border border-black/[0.12] px-3 py-1.5 text-xs text-[#1D1D1F] tabular-nums focus:border-[#0066CC] focus:outline-none"
                          />
                        </div>
                        <div>
                          <label className="block text-[11px] font-semibold text-[#6E6E73] mb-1">
                            Condition
                          </label>
                          <select
                            value={grnCondition}
                            onChange={(e) => setGrnCondition(e.target.value)}
                            className="rounded-xl border border-black/[0.12] px-3 py-1.5 text-xs text-[#1D1D1F] focus:border-[#0066CC] focus:outline-none"
                          >
                            <option value="GOOD">GOOD</option>
                            <option value="DAMAGED">DAMAGED</option>
                          </select>
                        </div>
                        <button
                          type="submit"
                          disabled={busyAction}
                          className="apple-press inline-flex items-center gap-1.5 rounded-xl bg-[#1F7A4D] px-4 py-1.5 text-xs font-semibold text-white hover:bg-[#1B6B43] disabled:opacity-45"
                        >
                          <ClipboardCheck className="h-3.5 w-3.5" />
                          Log GRN
                        </button>
                      </form>
                    )}
                  </section>
                )}

                {/* Goods Receipts */}
                {poDetail.goods_receipts.length > 0 && (
                  <section className="space-y-2">
                    <h3 className="text-xs font-bold uppercase tracking-wider text-[#86868B]">
                      Goods Receipts
                    </h3>
                    <div className="space-y-2">
                      {poDetail.goods_receipts.map((g) => (
                        <div
                          key={g.id}
                          className="flex items-center justify-between rounded-xl border border-black/[0.08] bg-white px-3.5 py-2.5 text-xs tabular-nums"
                        >
                          <div>
                            <span className="font-semibold text-[#1D1D1F]">
                              GRN #{g.id}: {g.received_qty} units
                            </span>{' '}
                            <span className="text-[#6E6E73]">({g.condition})</span>
                          </div>
                          <div className="flex items-center gap-2">
                            <span
                              className={`rounded-full border px-2.5 py-0.5 text-[11px] font-semibold ${
                                g.status === 'RELEASED'
                                  ? 'bg-[#1F7A4D]/10 text-[#1B6B43] border-[#1F7A4D]/22'
                                  : 'bg-[#B46900]/10 text-[#945600] border-[#B46900]/24'
                              }`}
                            >
                              {g.status}
                            </span>
                            <span className="text-[#86868B]">
                              {formatSimDateTime(g.received_at)}
                            </span>
                          </div>
                        </div>
                      ))}
                    </div>
                  </section>
                )}

                {/* Invoices & 3-Way Match Breakdown */}
                <section className="space-y-2">
                  <h3 className="text-xs font-bold uppercase tracking-wider text-[#86868B] flex items-center gap-1.5">
                    <FileText className="h-3.5 w-3.5" />
                    Invoice &amp; 3-Way Match Breakdown ({poDetail.invoices.length})
                  </h3>
                  {poDetail.invoices.length === 0 ? (
                    <p className="text-xs text-[#6E6E73]">No invoices submitted yet.</p>
                  ) : (
                    <div className="space-y-2">
                      {poDetail.invoices.map((inv) => {
                        const isMatched = inv.match_status === 'MATCHED';
                        return (
                          <div
                            key={inv.id}
                            className={`rounded-xl border p-3.5 text-xs space-y-1.5 tabular-nums ${
                              isMatched
                                ? 'border-[#1F7A4D]/25 bg-[#1F7A4D]/[0.05]'
                                : inv.match_status === 'PENDING_GRN'
                                  ? 'border-[#0066CC]/22 bg-[#0066CC]/[0.04]'
                                  : 'border-[#C9342D]/25 bg-[#C9342D]/[0.05]'
                            }`}
                          >
                            <div className="flex items-center justify-between gap-2">
                              <div className="font-semibold text-[#1D1D1F] flex items-center gap-1.5">
                                {isMatched && (
                                  <CheckCircle2 className="h-3.5 w-3.5 text-[#1B6B43]" />
                                )}
                                <span>
                                  {inv.invoice_number} (v{inv.version})
                                </span>
                              </div>
                              <span className="rounded-full bg-white px-2.5 py-0.5 text-[11px] font-bold border border-black/[0.08] text-[#1D1D1F]">
                                {inv.match_status}
                              </span>
                            </div>
                            <div className="flex flex-wrap gap-4 text-[#6E6E73]">
                              <span>
                                Extracted Qty: <strong className="text-[#1D1D1F]">{inv.extracted_qty}</strong> (PO:{' '}
                                {poDetail.qty})
                              </span>
                              <span>
                                Unit Price:{' '}
                                <strong className="text-[#1D1D1F]">{formatInr(inv.extracted_unit_price)}</strong> (PO:{' '}
                                {formatInr(poDetail.unit_price)})
                              </span>
                              <span>
                                Total: <strong className="text-[#1D1D1F]">{formatInr(inv.extracted_total)}</strong>
                              </span>
                            </div>
                            {inv.discrepancy_summary && (
                              <p className="text-[#B82923] font-medium">
                                {inv.discrepancy_summary}
                              </p>
                            )}
                            <div className="text-[11px] text-[#86868B]">
                              PDF: {inv.pdf_path} · Received {formatSimDateTime(inv.received_at)}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </section>

                {/* Notes */}
                <section className="space-y-2">
                  <h3 className="text-xs font-bold uppercase tracking-wider text-[#86868B]">
                    PO Notes
                  </h3>
                  {poDetail.notes && (
                    <pre className="whitespace-pre-wrap rounded-xl border border-black/[0.07] bg-[#FBFBFD] p-3 text-xs text-[#1D1D1F] font-sans">
                      {poDetail.notes}
                    </pre>
                  )}
                  {!TERMINAL_STATUSES.has(poDetail.status) && (
                    <form onSubmit={handleAddNote} className="flex gap-2">
                      <input
                        type="text"
                        placeholder="Add operational note to PO..."
                        value={noteInput}
                        onChange={(e) => setNoteInput(e.target.value)}
                        className="flex-1 rounded-xl border border-black/[0.10] bg-[#FBFBFD] px-3 py-1.5 text-xs text-[#1D1D1F] focus:border-[#0066CC] focus:bg-white focus:outline-none"
                      />
                      <button
                        type="submit"
                        disabled={busyAction || !noteInput.trim()}
                        className="apple-press rounded-xl bg-[#0066CC] px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-[#0071E3] disabled:opacity-45"
                      >
                        Add Note
                      </button>
                    </form>
                  )}
                </section>

                {/* Linked Email Thread */}
                <section className="space-y-2">
                  <h3 className="text-xs font-bold uppercase tracking-wider text-[#86868B] flex items-center gap-1.5">
                    <Mail className="h-3.5 w-3.5" />
                    Linked Email Thread ({poDetail.communications.length})
                  </h3>
                  {poDetail.communications.length === 0 ? (
                    <p className="text-xs text-[#6E6E73]">No communications linked yet.</p>
                  ) : (
                    <div className="space-y-2.5">
                      {poDetail.communications.map((c) => (
                        <div
                          key={c.id}
                          className="rounded-xl border border-black/[0.08] bg-white p-3.5 text-xs space-y-1.5"
                        >
                          <div className="flex flex-wrap items-center justify-between gap-2">
                            <div className="font-semibold text-[#1D1D1F]">
                              {personaLabel(c.from_persona)} →{' '}
                              {c.to_personas.map(personaLabel).join(', ')}
                            </div>
                            <div className="flex items-center gap-1.5">
                              {c.ai_classification && (
                                <span className="rounded-full bg-[#5E5CE6]/10 border border-[#5E5CE6]/22 px-2 py-0.5 text-[11px] font-semibold text-[#4B48C4]">
                                  Lex: {c.ai_classification}
                                  {c.ai_confidence !== null
                                    ? ` (${Math.round(c.ai_confidence * 100)}%)`
                                    : ''}
                                </span>
                              )}
                              <span className="rounded-full bg-black/[0.05] px-2 py-0.5 text-[11px] font-medium text-[#6E6E73]">
                                {c.status}
                              </span>
                            </div>
                          </div>
                          <div className="font-semibold text-[#1D1D1F]">{c.subject}</div>
                          <p className="whitespace-pre-wrap text-[#6E6E73]">{c.body}</p>
                          <div className="text-[11px] text-[#86868B] tabular-nums">
                            {formatSimDateTime(c.timestamp)}
                            {c.attachment_path ? ` · Attachment: ${c.attachment_path}` : ''}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </section>
                </div>
              )}
            </div>
          </div>,
          document.body,
        )}

      {showEscalationModal && poDetail && (
        <ProcurementEscalationModal
          po={poDetail}
          onClose={() => setShowEscalationModal(false)}
          onEscalated={(res) => void handleEscalated(res)}
        />
      )}
    </div>
  );
}
