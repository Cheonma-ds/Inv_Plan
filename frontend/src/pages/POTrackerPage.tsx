import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ArrowRightLeft,
  CheckCircle2,
  ClipboardCheck,
  FileText,
  Mail,
  PackageCheck,
  RefreshCw,
  Search,
  X,
} from 'lucide-react';
import { api } from '../api/client';
import type { PODetail, POListItem } from '../api/types';
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
    if (selectedPoId !== null) {
      void loadDetail(selectedPoId);
    } else {
      setPoDetail(null);
    }
  }, [selectedPoId, loadDetail, refreshNonce]);

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
        (po.supplier_name ?? '').toLowerCase().includes(q)
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

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold text-slate-900">Purchase Order Tracker</h1>
          <p className="text-sm text-slate-500">
            Track open &amp; historical POs, log goods receipts (GRN), inspect 3-way invoice matches, and switch suppliers.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void loadPos()}
          className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <div className="relative flex-1 min-w-60 max-w-sm">
          <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-slate-400" />
          <input
            type="text"
            placeholder="Search PO#, SKU, or supplier..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full rounded-lg border border-slate-200 bg-white py-2 pl-9 pr-3 text-sm text-slate-800 focus:border-slate-400 focus:outline-none"
          />
        </div>

        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          aria-label="Filter PO status"
          className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-700 focus:border-slate-400 focus:outline-none"
        >
          {statusOptions.map((st) => (
            <option key={st} value={st}>
              {st === 'OPEN' ? 'Open POs' : st === 'ALL' ? 'All POs' : st}
            </option>
          ))}
        </select>
      </div>

      <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white shadow-sm">
        <table className="min-w-full divide-y divide-slate-200 text-left text-xs">
          <thead className="bg-slate-50 text-slate-600 font-semibold uppercase tracking-wider">
            <tr>
              <th className="px-4 py-3">PO #</th>
              <th className="px-4 py-3">Part</th>
              <th className="px-4 py-3">Supplier</th>
              <th className="px-3 py-3 text-right">Qty</th>
              <th className="px-3 py-3 text-right">Unit Price</th>
              <th className="px-3 py-3 text-right">Total</th>
              <th className="px-4 py-3">Status</th>
              <th className="px-4 py-3">Promised / Revised ETA</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {filteredPos.map((po) => {
              const isSelected = selectedPoId === po.id;
              return (
                <tr
                  key={po.id}
                  onClick={() => setSelectedPoId(po.id)}
                  className={`cursor-pointer transition ${
                    isSelected ? 'bg-sky-50' : 'hover:bg-slate-50'
                  }`}
                >
                  <td className="px-4 py-3 font-mono font-bold text-slate-900">
                    {po.po_number}
                  </td>
                  <td className="px-4 py-3">
                    <div className="font-mono font-semibold text-slate-800">
                      {po.part_sku ?? `Part #${po.part_id}`}
                    </div>
                    <div className="text-slate-500">{po.part_name}</div>
                  </td>
                  <td className="px-4 py-3 font-medium text-slate-800">
                    {po.supplier_name ?? `Supplier #${po.supplier_id}`}
                  </td>
                  <td className="px-3 py-3 text-right font-semibold tabular-nums">
                    {po.qty}
                  </td>
                  <td className="px-3 py-3 text-right tabular-nums">
                    {formatInr(po.unit_price)}
                  </td>
                  <td className="px-3 py-3 text-right font-semibold tabular-nums">
                    {formatInr(po.total_amount)}
                  </td>
                  <td className="px-4 py-3">
                    <span
                      className={`inline-flex rounded-full border px-2.5 py-0.5 text-[11px] font-semibold ${poStatusBadgeClass(
                        po.status,
                      )}`}
                    >
                      {po.status}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-slate-600 tabular-nums">
                    <div>{formatSimDate(po.promised_eta)}</div>
                    {po.revised_eta && (
                      <div className="text-amber-700 font-medium">
                        Rev: {formatSimDate(po.revised_eta)}
                      </div>
                    )}
                  </td>
                </tr>
              );
            })}
            {filteredPos.length === 0 && (
              <tr>
                <td colSpan={8} className="px-4 py-8 text-center text-slate-500">
                  No purchase orders match this filter.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {/* Slide-over detail drawer */}
      {selectedPoId !== null && (
        <div className="fixed inset-0 z-40 flex justify-end bg-slate-900/35">
          <div className="h-full w-full max-w-2xl overflow-y-auto bg-white shadow-2xl border-l border-slate-200 flex flex-col">
            <div className="sticky top-0 z-10 flex items-center justify-between border-b border-slate-200 bg-white px-6 py-4">
              <div>
                <h2 className="text-lg font-bold text-slate-900">
                  {poDetail ? poDetail.po_number : `PO #${selectedPoId}`}
                </h2>
                {poDetail && (
                  <p className="text-xs text-slate-500">
                    {poDetail.part_sku} · {poDetail.part_name} · {poDetail.supplier_name}
                  </p>
                )}
              </div>
              <button
                type="button"
                onClick={() => setSelectedPoId(null)}
                className="rounded-lg p-1.5 text-slate-500 hover:bg-slate-100 hover:text-slate-900"
              >
                <X className="h-5 w-5" />
              </button>
            </div>

            {detailLoading || !poDetail ? (
              <div className="p-6 text-sm text-slate-500">Loading PO details...</div>
            ) : (
              <div className="p-6 space-y-6">
                {/* Summary KPIs */}
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
                  <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
                    <div className="text-slate-500">Status</div>
                    <div className="mt-1">
                      <span
                        className={`inline-flex rounded-full border px-2 py-0.5 text-[10px] font-bold ${poStatusBadgeClass(
                          poDetail.status,
                        )}`}
                      >
                        {poDetail.status}
                      </span>
                    </div>
                  </div>
                  <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
                    <div className="text-slate-500">Order Qty</div>
                    <div className="mt-1 text-sm font-bold text-slate-900">
                      {poDetail.qty} units
                    </div>
                  </div>
                  <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
                    <div className="text-slate-500">Unit Price</div>
                    <div className="mt-1 text-sm font-bold text-slate-900">
                      {formatInr(poDetail.unit_price)}
                    </div>
                  </div>
                  <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
                    <div className="text-slate-500">Total Value</div>
                    <div className="mt-1 text-sm font-bold text-slate-900">
                      {formatInr(poDetail.total_amount)}
                    </div>
                  </div>
                </div>

                {/* State Timeline */}
                <section className="space-y-2">
                  <h3 className="text-xs font-bold uppercase tracking-wider text-slate-500">
                    Lifecycle Timeline
                  </h3>
                  <div className="rounded-lg border border-slate-200 bg-slate-50 p-3 grid grid-cols-2 gap-3 text-xs">
                    <div>
                      <span className="text-slate-500">Created:</span>{' '}
                      <span className="font-medium text-slate-800">
                        {formatSimDateTime(poDetail.created_at)}
                      </span>
                    </div>
                    <div>
                      <span className="text-slate-500">Dispatched:</span>{' '}
                      <span className="font-medium text-slate-800">
                        {formatSimDateTime(poDetail.sent_at)}
                      </span>
                    </div>
                    <div>
                      <span className="text-slate-500">Promised ETA:</span>{' '}
                      <span className="font-medium text-slate-800">
                        {formatSimDate(poDetail.promised_eta)}
                      </span>
                    </div>
                    <div>
                      <span className="text-slate-500">Revised ETA:</span>{' '}
                      <span className="font-medium text-amber-700">
                        {formatSimDate(poDetail.revised_eta)}
                      </span>
                    </div>
                    <div>
                      <span className="text-slate-500">Follow-ups Sent:</span>{' '}
                      <span className="font-semibold text-slate-900">
                        {poDetail.follow_up_count}
                      </span>
                    </div>
                    <div>
                      <span className="text-slate-500">Pre-ETA Check:</span>{' '}
                      <span className="font-semibold text-slate-900">
                        {poDetail.pre_eta_checked ? 'Done' : 'Pending'}
                      </span>
                    </div>
                  </div>
                </section>

                {/* Log GRN + Switch Backup */}
                {!TERMINAL_STATUSES.has(poDetail.status) && (
                  <section className="rounded-xl border border-slate-200 p-4 space-y-4">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <h3 className="text-xs font-bold uppercase tracking-wider text-slate-700 flex items-center gap-1.5">
                        <PackageCheck className="h-4 w-4 text-emerald-600" />
                        Warehouse Goods Receipt (GRN)
                      </h3>

                      <button
                        type="button"
                        disabled={busyAction}
                        onClick={() => void handleSwitchBackup()}
                        className="inline-flex items-center gap-1.5 rounded-lg border border-amber-300 bg-amber-50 px-3 py-1.5 text-xs font-semibold text-amber-800 hover:bg-amber-100 disabled:opacity-50"
                      >
                        <ArrowRightLeft className="h-3.5 w-3.5" />
                        Switch to Backup Supplier
                      </button>
                    </div>

                    {!GRN_ALLOWED_STATUSES.has(poDetail.status) ? (
                      <p className="text-xs text-slate-500">
                        GRN logging is unavailable while PO is in{' '}
                        <span className="font-semibold">{poDetail.status}</span>{' '}
                        (requires ACKNOWLEDGED, DELAYED_CRITICAL, or OVERDUE).
                      </p>
                    ) : (
                      <form onSubmit={handleRecordGrn} className="flex flex-wrap items-end gap-3">
                        <div>
                          <label className="block text-[11px] font-semibold text-slate-600 mb-1">
                            Received Qty
                          </label>
                          <input
                            type="number"
                            min={1}
                            value={grnQty}
                            onChange={(e) => setGrnQty(e.target.value)}
                            required
                            className="w-28 rounded-lg border border-slate-300 px-3 py-1.5 text-xs focus:border-slate-500 focus:outline-none"
                          />
                        </div>
                        <div>
                          <label className="block text-[11px] font-semibold text-slate-600 mb-1">
                            Condition
                          </label>
                          <select
                            value={grnCondition}
                            onChange={(e) => setGrnCondition(e.target.value)}
                            className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs focus:border-slate-500 focus:outline-none"
                          >
                            <option value="GOOD">GOOD</option>
                            <option value="DAMAGED">DAMAGED</option>
                          </select>
                        </div>
                        <button
                          type="submit"
                          disabled={busyAction}
                          className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-700 px-4 py-1.5 text-xs font-semibold text-white hover:bg-emerald-800 disabled:opacity-50"
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
                    <h3 className="text-xs font-bold uppercase tracking-wider text-slate-500">
                      Goods Receipts
                    </h3>
                    <div className="space-y-2">
                      {poDetail.goods_receipts.map((g) => (
                        <div
                          key={g.id}
                          className="flex items-center justify-between rounded-lg border border-slate-200 px-3 py-2 text-xs"
                        >
                          <div>
                            <span className="font-semibold text-slate-900">
                              GRN #{g.id}: {g.received_qty} units
                            </span>{' '}
                            <span className="text-slate-500">({g.condition})</span>
                          </div>
                          <div className="flex items-center gap-2">
                            <span
                              className={`rounded px-2 py-0.5 font-semibold ${
                                g.status === 'RELEASED'
                                  ? 'bg-emerald-100 text-emerald-800'
                                  : 'bg-amber-100 text-amber-800'
                              }`}
                            >
                              {g.status}
                            </span>
                            <span className="text-slate-500">
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
                  <h3 className="text-xs font-bold uppercase tracking-wider text-slate-500 flex items-center gap-1.5">
                    <FileText className="h-3.5 w-3.5" />
                    Invoice &amp; 3-Way Match Breakdown ({poDetail.invoices.length})
                  </h3>
                  {poDetail.invoices.length === 0 ? (
                    <p className="text-xs text-slate-500">No invoices submitted yet.</p>
                  ) : (
                    <div className="space-y-2">
                      {poDetail.invoices.map((inv) => {
                        const isMatched = inv.match_status === 'MATCHED';
                        return (
                          <div
                            key={inv.id}
                            className={`rounded-lg border p-3 text-xs space-y-1.5 ${
                              isMatched
                                ? 'border-emerald-200 bg-emerald-50/40'
                                : inv.match_status === 'PENDING_GRN'
                                  ? 'border-sky-200 bg-sky-50/40'
                                  : 'border-red-200 bg-red-50/50'
                            }`}
                          >
                            <div className="flex items-center justify-between gap-2">
                              <div className="font-semibold text-slate-900 flex items-center gap-1.5">
                                {isMatched && (
                                  <CheckCircle2 className="h-3.5 w-3.5 text-emerald-600" />
                                )}
                                <span>
                                  {inv.invoice_number} (v{inv.version})
                                </span>
                              </div>
                              <span className="rounded bg-white px-2 py-0.5 font-bold border border-slate-200">
                                {inv.match_status}
                              </span>
                            </div>
                            <div className="flex flex-wrap gap-4 text-slate-700">
                              <span>
                                Extracted Qty: <strong>{inv.extracted_qty}</strong> (PO:{' '}
                                {poDetail.qty})
                              </span>
                              <span>
                                Unit Price:{' '}
                                <strong>{formatInr(inv.extracted_unit_price)}</strong> (PO:{' '}
                                {formatInr(poDetail.unit_price)})
                              </span>
                              <span>
                                Total: <strong>{formatInr(inv.extracted_total)}</strong>
                              </span>
                            </div>
                            {inv.discrepancy_summary && (
                              <p className="text-red-800 font-medium">
                                {inv.discrepancy_summary}
                              </p>
                            )}
                            <div className="text-[11px] text-slate-500">
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
                  <h3 className="text-xs font-bold uppercase tracking-wider text-slate-500">
                    PO Notes
                  </h3>
                  {poDetail.notes && (
                    <pre className="whitespace-pre-wrap rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs text-slate-700 font-sans">
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
                        className="flex-1 rounded-lg border border-slate-200 px-3 py-1.5 text-xs focus:border-slate-400 focus:outline-none"
                      />
                      <button
                        type="submit"
                        disabled={busyAction || !noteInput.trim()}
                        className="rounded-lg bg-slate-900 px-3 py-1.5 text-xs font-semibold text-white hover:bg-slate-800 disabled:opacity-50"
                      >
                        Add Note
                      </button>
                    </form>
                  )}
                </section>

                {/* Linked Email Thread */}
                <section className="space-y-2">
                  <h3 className="text-xs font-bold uppercase tracking-wider text-slate-500 flex items-center gap-1.5">
                    <Mail className="h-3.5 w-3.5" />
                    Linked Email Thread ({poDetail.communications.length})
                  </h3>
                  {poDetail.communications.length === 0 ? (
                    <p className="text-xs text-slate-500">No communications linked yet.</p>
                  ) : (
                    <div className="space-y-2.5">
                      {poDetail.communications.map((c) => (
                        <div
                          key={c.id}
                          className="rounded-lg border border-slate-200 bg-white p-3 text-xs space-y-1.5"
                        >
                          <div className="flex flex-wrap items-center justify-between gap-2">
                            <div className="font-semibold text-slate-900">
                              {personaLabel(c.from_persona)} →{' '}
                              {c.to_personas.map(personaLabel).join(', ')}
                            </div>
                            <div className="flex items-center gap-1.5">
                              {c.ai_classification && (
                                <span className="rounded bg-indigo-100 px-2 py-0.5 font-semibold text-indigo-800">
                                  Lex: {c.ai_classification}
                                  {c.ai_confidence !== null
                                    ? ` (${Math.round(c.ai_confidence * 100)}%)`
                                    : ''}
                                </span>
                              )}
                              <span className="rounded bg-slate-100 px-2 py-0.5 font-medium text-slate-700">
                                {c.status}
                              </span>
                            </div>
                          </div>
                          <div className="font-semibold text-slate-800">{c.subject}</div>
                          <p className="whitespace-pre-wrap text-slate-600">{c.body}</p>
                          <div className="text-[11px] text-slate-400">
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
        </div>
      )}
    </div>
  );
}
