import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  ArrowDownCircle,
  Edit3,
  Package,
  RefreshCw,
  Search,
  Sliders,
  X,
} from 'lucide-react';
import { api } from '../api/client';
import type { PartItem } from '../api/types';
import { criticalityBadgeClass, formatInr } from '../utils/format';

interface InventoryPageProps {
  refreshNonce: number;
  focusPartId: number | null;
  onClearFocus: () => void;
  onMutated: () => void;
  onNotify: (kind: 'success' | 'error' | 'info', title: string, detail?: string) => void;
}

type ModalState =
  | { kind: 'consume'; part: PartItem }
  | { kind: 'rop'; part: PartItem }
  | { kind: 'demand'; part: PartItem }
  | null;

export function InventoryPage({
  refreshNonce,
  focusPartId,
  onClearFocus,
  onMutated,
  onNotify,
}: InventoryPageProps) {
  const [parts, setParts] = useState<PartItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [criticality, setCriticality] = useState<string>('ALL');
  const [modal, setModal] = useState<ModalState>(null);
  const [submitting, setSubmitting] = useState(false);

  // Form fields
  const [withdrawQty, setWithdrawQty] = useState('1');
  const [withdrawNote, setWithdrawNote] = useState('');
  const [ropInput, setRopInput] = useState('');
  const [sInput, setSInput] = useState('');
  const [demandInput, setDemandInput] = useState('');

  const loadParts = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.listParts();
      setParts(data);
    } catch (err) {
      onNotify(
        'error',
        'Failed to load parts',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setLoading(false);
    }
  }, [onNotify]);

  useEffect(() => {
    void loadParts();
  }, [loadParts, refreshNonce]);

  const openModal = (next: ModalState) => {
    setModal(next);
    if (!next) return;
    if (next.kind === 'consume') {
      setWithdrawQty('10');
      setWithdrawNote('');
    } else if (next.kind === 'rop') {
      setRopInput(String(next.part.rop));
      setSInput(String(next.part.replenishment_point));
    } else if (next.kind === 'demand') {
      setDemandInput(
        next.part.manual_demand_override !== null
          ? String(next.part.manual_demand_override)
          : '',
      );
    }
  };

  const filteredParts = useMemo(() => {
    const q = search.trim().toLowerCase();
    return parts.filter((p) => {
      if (criticality !== 'ALL' && p.criticality !== criticality) return false;
      if (!q) return true;
      return (
        p.sku.toLowerCase().includes(q) ||
        p.name.toLowerCase().includes(q) ||
        p.suppliers.some((s) => (s.supplier_name ?? '').toLowerCase().includes(q))
      );
    });
  }, [parts, search, criticality]);

  const handleSubmitConsume = async (e: React.FormEvent) => {
    e.preventDefault();
    if (modal?.kind !== 'consume') return;
    const qty = Number.parseInt(withdrawQty, 10);
    if (!Number.isFinite(qty) || qty <= 0) {
      onNotify('error', 'Invalid quantity', 'Enter a positive integer.');
      return;
    }
    setSubmitting(true);
    try {
      const res = await api.consumeStock(modal.part.part_id, qty, withdrawNote.trim());
      if (res.created_po) {
        onNotify(
          'success',
          `Withdrew ${qty} ${modal.part.sku} → Triggered ${res.created_po.po_number}`,
          `Draft PO for ${res.created_po.qty} units @ ${formatInr(res.created_po.unit_price)} awaiting dispatch approval.`,
        );
      } else if (res.consume.accelerated_burn) {
        onNotify(
          'info',
          `Withdrew ${qty} ${modal.part.sku} — Accelerated Burn Alert`,
          `Open PO already exists; raised expedited review alert.`,
        );
      } else {
        onNotify(
          'success',
          `Withdrew ${qty} ${modal.part.sku}`,
          `On-hand: ${res.consume.snapshot.on_hand_stock}, Inventory Position: ${res.consume.snapshot.inventory_position}`,
        );
      }
      setModal(null);
      onMutated();
      await loadParts();
    } catch (err) {
      onNotify(
        'error',
        'Stock withdrawal failed',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setSubmitting(false);
    }
  };

  const handleSubmitRop = async (e: React.FormEvent) => {
    e.preventDefault();
    if (modal?.kind !== 'rop') return;
    const rop = Number.parseInt(ropInput, 10);
    const sVal = Number.parseInt(sInput, 10);
    if (!Number.isFinite(rop) || rop < 0 || !Number.isFinite(sVal) || sVal <= 0) {
      onNotify('error', 'Invalid ROP/S values', 'ROP must be >= 0 and S must be > 0.');
      return;
    }
    setSubmitting(true);
    try {
      await api.updatePartRop(modal.part.part_id, rop, sVal);
      onNotify('success', `Updated ROP/S for ${modal.part.sku}`, `ROP=${rop}, S=${sVal}`);
      setModal(null);
      onMutated();
      await loadParts();
    } catch (err) {
      onNotify(
        'error',
        'Failed to update ROP/S',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setSubmitting(false);
    }
  };

  const handleSubmitDemand = async (e: React.FormEvent) => {
    e.preventDefault();
    if (modal?.kind !== 'demand') return;
    const trimmed = demandInput.trim();
    const overrideVal = trimmed === '' ? null : Number.parseFloat(trimmed);
    if (overrideVal !== null && (!Number.isFinite(overrideVal) || overrideVal < 0)) {
      onNotify('error', 'Invalid demand override', 'Enter a non-negative number or clear.');
      return;
    }
    setSubmitting(true);
    try {
      await api.updateDemandOverride(modal.part.part_id, overrideVal);
      onNotify(
        'success',
        `Demand override ${overrideVal === null ? 'cleared' : 'updated'} for ${modal.part.sku}`,
        overrideVal === null
          ? 'Reverted to 12-month EWMA demand forecast.'
          : `Override set to ${overrideVal} units/day.`,
      );
      setModal(null);
      onMutated();
      await loadParts();
    } catch (err) {
      onNotify(
        'error',
        'Failed to update demand override',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setSubmitting(false);
    }
  };

  const totalQuarantined = parts.reduce((acc, p) => acc + p.quarantined_stock, 0);
  const belowRopCount = parts.filter((p) => p.inventory_position < p.rop).length;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold text-slate-900">Inventory &amp; Parts</h1>
          <p className="text-sm text-slate-500">
            15-SKU live stock ledger, inventory position (On-Hand + On-Order + Quarantined), ROP/S, and supplier terms.
          </p>
        </div>

        <div className="flex items-center gap-2 text-xs">
          <div className="rounded-lg border border-slate-200 bg-white px-3 py-1.5">
            <span className="text-slate-500">Below ROP:</span>{' '}
            <span className="font-bold text-rose-600">{belowRopCount}</span>
          </div>
          <div className="rounded-lg border border-slate-200 bg-white px-3 py-1.5">
            <span className="text-slate-500">Quarantined Units:</span>{' '}
            <span className="font-bold text-amber-700">{totalQuarantined}</span>
          </div>
          <button
            type="button"
            onClick={() => void loadParts()}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 font-medium text-slate-700 hover:bg-slate-50"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="relative flex-1 min-w-64 max-w-sm">
          <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-slate-400" />
          <input
            type="text"
            placeholder="Search SKU, part name, or supplier..."
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              if (focusPartId) onClearFocus();
            }}
            className="w-full rounded-lg border border-slate-200 bg-white py-2 pl-9 pr-3 text-sm text-slate-800 focus:border-slate-400 focus:outline-none"
          />
        </div>

        <div className="inline-flex rounded-lg border border-slate-200 bg-white p-0.5">
          {(['ALL', 'CRITICAL', 'STANDARD', 'LOW'] as const).map((lvl) => (
            <button
              key={lvl}
              type="button"
              onClick={() => setCriticality(lvl)}
              className={`rounded-md px-3 py-1.5 text-xs font-semibold transition ${
                criticality === lvl
                  ? 'bg-slate-900 text-white'
                  : 'text-slate-600 hover:bg-slate-100'
              }`}
            >
              {lvl}
            </button>
          ))}
        </div>
      </div>

      <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white shadow-sm">
        <table className="min-w-full divide-y divide-slate-200 text-left text-xs">
          <thead className="bg-slate-50 text-slate-600 font-semibold uppercase tracking-wider">
            <tr>
              <th className="px-4 py-3">Part</th>
              <th className="px-3 py-3 text-right">On-Hand</th>
              <th className="px-3 py-3 text-right">Quarantined</th>
              <th className="px-3 py-3 text-right">On-Order</th>
              <th className="px-3 py-3 text-right">Inv. Pos.</th>
              <th className="px-3 py-3 text-right">ROP / S</th>
              <th className="px-3 py-3 text-right">Demand Override</th>
              <th className="px-4 py-3">Primary &amp; Backup Suppliers</th>
              <th className="px-4 py-3 text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {filteredParts.map((part) => {
              const primary = part.suppliers.find((s) => s.supplier_rank === 1);
              const backup = part.suppliers.find((s) => s.supplier_rank === 2);
              const isBelowRop = part.inventory_position < part.rop;
              const isFocused = focusPartId === part.part_id;

              return (
                <tr
                  key={part.part_id}
                  className={
                    isFocused
                      ? 'bg-sky-50/80'
                      : isBelowRop
                        ? 'bg-rose-50/30 hover:bg-rose-50/60'
                        : 'hover:bg-slate-50'
                  }
                >
                  <td className="px-4 py-3 align-top">
                    <div className="flex items-center gap-2">
                      <Package className="h-3.5 w-3.5 text-slate-400 shrink-0" />
                      <span className="font-mono font-semibold text-slate-900">
                        {part.sku}
                      </span>
                      <span
                        className={`rounded-full border px-2 py-0.5 text-[10px] font-bold ${criticalityBadgeClass(
                          part.criticality,
                        )}`}
                      >
                        {part.criticality}
                      </span>
                    </div>
                    <div className="mt-0.5 text-slate-600 font-medium">{part.name}</div>
                  </td>

                  <td className="px-3 py-3 text-right align-top font-semibold tabular-nums text-slate-900">
                    {part.on_hand_stock}
                  </td>

                  <td className="px-3 py-3 text-right align-top tabular-nums">
                    {part.quarantined_stock > 0 ? (
                      <span className="inline-flex items-center gap-1 rounded bg-amber-100 px-1.5 py-0.5 font-semibold text-amber-800">
                        <AlertTriangle className="h-3 w-3" />
                        {part.quarantined_stock}
                      </span>
                    ) : (
                      <span className="text-slate-400">0</span>
                    )}
                  </td>

                  <td className="px-3 py-3 text-right align-top tabular-nums text-slate-700 font-medium">
                    {part.on_order_stock}
                  </td>

                  <td className="px-3 py-3 text-right align-top tabular-nums">
                    <span
                      className={`font-bold ${
                        isBelowRop ? 'text-rose-700' : 'text-slate-900'
                      }`}
                    >
                      {part.inventory_position}
                    </span>
                  </td>

                  <td className="px-3 py-3 text-right align-top tabular-nums text-slate-800">
                    <span className="font-semibold">{part.rop}</span>
                    <span className="text-slate-400"> / </span>
                    <span>{part.replenishment_point}</span>
                    <div className="text-[10px] text-slate-400">
                      {part.target_coverage_days}d cov
                    </div>
                  </td>

                  <td className="px-3 py-3 text-right align-top tabular-nums">
                    {part.manual_demand_override !== null ? (
                      <span className="rounded bg-indigo-100 px-1.5 py-0.5 font-semibold text-indigo-800">
                        {part.manual_demand_override}/d
                      </span>
                    ) : (
                      <span className="text-slate-400">Auto</span>
                    )}
                  </td>

                  <td className="px-4 py-3 align-top space-y-1">
                    {primary ? (
                      <div className="text-slate-800">
                        <span className="font-semibold">1° {primary.supplier_name}</span>{' '}
                        <span className="text-slate-500">
                          · {formatInr(primary.agreed_unit_price)} ·{' '}
                          {primary.effective_lead_time_days.toFixed(1)}d eff LT (q
                          {primary.quoted_lead_time_days}d) · MOQ {primary.moq}
                        </span>
                      </div>
                    ) : (
                      <div className="text-slate-400">No primary supplier</div>
                    )}
                    {backup && (
                      <div className="text-slate-500">
                        <span>2° {backup.supplier_name}</span> ·{' '}
                        {formatInr(backup.agreed_unit_price)} ·{' '}
                        {backup.quoted_lead_time_days}d LT · MOQ {backup.moq}
                      </div>
                    )}
                  </td>

                  <td className="px-4 py-3 text-right align-top">
                    <div className="inline-flex flex-wrap justify-end gap-1.5">
                      <button
                        type="button"
                        onClick={() => openModal({ kind: 'consume', part })}
                        className="inline-flex items-center gap-1 rounded-md bg-slate-900 px-2.5 py-1 text-xs font-semibold text-white hover:bg-slate-800"
                      >
                        <ArrowDownCircle className="h-3 w-3" />
                        Withdraw
                      </button>
                      <button
                        type="button"
                        onClick={() => openModal({ kind: 'rop', part })}
                        className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"
                        title="Edit ROP and S"
                      >
                        <Edit3 className="h-3 w-3" />
                        ROP/S
                      </button>
                      <button
                        type="button"
                        onClick={() => openModal({ kind: 'demand', part })}
                        className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"
                        title="Manual daily demand override"
                      >
                        <Sliders className="h-3 w-3" />
                        Demand
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {modal && (
        <div className="fixed inset-0 z-40 flex items-center justify-center bg-slate-900/40 p-4">
          <div className="w-full max-w-md rounded-xl border border-slate-200 bg-white p-5 shadow-xl">
            <div className="flex items-start justify-between gap-2">
              <div>
                <h2 className="text-base font-bold text-slate-900">
                  {modal.kind === 'consume'
                    ? `Withdraw Stock — ${modal.part.sku}`
                    : modal.kind === 'rop'
                      ? `Edit ROP / S — ${modal.part.sku}`
                      : `Demand Override — ${modal.part.sku}`}
                </h2>
                <p className="text-xs text-slate-500 mt-0.5">{modal.part.name}</p>
              </div>
              <button
                type="button"
                onClick={() => setModal(null)}
                className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            {modal.kind === 'consume' && (
              <form onSubmit={handleSubmitConsume} className="mt-4 space-y-3">
                <div className="rounded-lg bg-slate-50 p-3 text-xs text-slate-700 flex justify-between">
                  <span>Available On-Hand: <strong>{modal.part.on_hand_stock}</strong></span>
                  <span>Inv. Position: <strong>{modal.part.inventory_position}</strong></span>
                  <span>ROP: <strong>{modal.part.rop}</strong></span>
                </div>

                <div>
                  <label className="block text-xs font-semibold text-slate-700 mb-1">
                    Quantity to Withdraw
                  </label>
                  <input
                    type="number"
                    min={1}
                    max={modal.part.on_hand_stock}
                    value={withdrawQty}
                    onChange={(e) => setWithdrawQty(e.target.value)}
                    required
                    className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
                  />
                </div>

                <div>
                  <label className="block text-xs font-semibold text-slate-700 mb-1">
                    Note (optional)
                  </label>
                  <input
                    type="text"
                    placeholder="e.g. Line 2 assembly draw"
                    value={withdrawNote}
                    onChange={(e) => setWithdrawNote(e.target.value)}
                    className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
                  />
                </div>

                <div className="flex justify-end gap-2 pt-2">
                  <button
                    type="button"
                    onClick={() => setModal(null)}
                    className="rounded-lg border border-slate-200 px-3 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={submitting}
                    className="rounded-lg bg-slate-900 px-4 py-2 text-xs font-semibold text-white hover:bg-slate-800 disabled:opacity-50"
                  >
                    {submitting ? 'Withdrawing...' : 'Confirm Withdrawal'}
                  </button>
                </div>
              </form>
            )}

            {modal.kind === 'rop' && (
              <form onSubmit={handleSubmitRop} className="mt-4 space-y-3">
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-xs font-semibold text-slate-700 mb-1">
                      Reorder Point (ROP)
                    </label>
                    <input
                      type="number"
                      min={0}
                      value={ropInput}
                      onChange={(e) => setRopInput(e.target.value)}
                      required
                      className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-semibold text-slate-700 mb-1">
                      Order-Up-To (S)
                    </label>
                    <input
                      type="number"
                      min={1}
                      value={sInput}
                      onChange={(e) => setSInput(e.target.value)}
                      required
                      className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
                    />
                  </div>
                </div>

                <div className="flex justify-end gap-2 pt-2">
                  <button
                    type="button"
                    onClick={() => setModal(null)}
                    className="rounded-lg border border-slate-200 px-3 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={submitting}
                    className="rounded-lg bg-slate-900 px-4 py-2 text-xs font-semibold text-white hover:bg-slate-800 disabled:opacity-50"
                  >
                    {submitting ? 'Saving...' : 'Save ROP / S'}
                  </button>
                </div>
              </form>
            )}

            {modal.kind === 'demand' && (
              <form onSubmit={handleSubmitDemand} className="mt-4 space-y-3">
                <p className="text-xs text-slate-600">
                  Set an additive/override daily demand rate (units/day) for planning calculations, or leave blank to revert to EWMA forecast.
                </p>
                <div>
                  <label className="block text-xs font-semibold text-slate-700 mb-1">
                    Manual Daily Demand (units/day)
                  </label>
                  <input
                    type="number"
                    step="0.1"
                    min={0}
                    placeholder="Blank = auto EWMA"
                    value={demandInput}
                    onChange={(e) => setDemandInput(e.target.value)}
                    className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
                  />
                </div>

                <div className="flex justify-between gap-2 pt-2">
                  <button
                    type="button"
                    disabled={submitting}
                    onClick={() => setDemandInput('')}
                    className="rounded-lg border border-slate-200 px-3 py-2 text-xs font-medium text-slate-600 hover:bg-slate-50"
                  >
                    Clear Override
                  </button>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => setModal(null)}
                      className="rounded-lg border border-slate-200 px-3 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50"
                    >
                      Cancel
                    </button>
                    <button
                      type="submit"
                      disabled={submitting}
                      className="rounded-lg bg-slate-900 px-4 py-2 text-xs font-semibold text-white hover:bg-slate-800 disabled:opacity-50"
                    >
                      {submitting ? 'Saving...' : 'Save Override'}
                    </button>
                  </div>
                </div>
              </form>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
