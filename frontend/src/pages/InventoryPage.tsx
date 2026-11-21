import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  Edit3,
  Package,
  RefreshCw,
  Search,
  Sliders,
  X,
} from 'lucide-react';
import { api } from '../api/client';
import type { PartItem } from '../api/types';
import { DataTable, type ColumnDef } from '../components/DataTable';
import { criticalityBadgeClass, formatInr } from '../utils/format';

interface InventoryPageProps {
  refreshNonce: number;
  focusPartId: number | null;
  onClearFocus: () => void;
  onMutated: () => void;
  onNotify: (kind: 'success' | 'error' | 'info', title: string, detail?: string) => void;
}

type ModalState =
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

  const openModal = useCallback((next: ModalState) => {
    setModal(next);
    if (!next) return;
    if (next.kind === 'rop') {
      setRopInput(String(next.part.rop));
      setSInput(String(next.part.replenishment_point));
    } else if (next.kind === 'demand') {
      setDemandInput(
        next.part.manual_demand_override !== null
          ? String(next.part.manual_demand_override)
          : '',
      );
    }
  }, []);

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

  const columns = useMemo<ColumnDef<PartItem>[]>(
    () => [
      {
        key: 'part',
        header: 'Part',
        tooltip:
          'Unique SKU identifier, part name, and operational criticality tier. CRITICAL: essential parts with 98% target service level (Z = 2.05) and 24h supplier ack SLA. STANDARD: core production items with 95% service level (Z = 1.65) and 48h SLA. LOW: routine/non-urgent items with 90% service level (Z = 1.28).',
        defaultWidth: 250,
        minWidth: 180,
        renderCell: (part) => (
          <>
            <div className="flex items-center gap-2 flex-wrap">
              <Package className="h-3.5 w-3.5 text-[#86868B] shrink-0" />
              <span className="font-mono font-semibold text-[#1D1D1F]">{part.sku}</span>
              <span
                className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${criticalityBadgeClass(
                  part.criticality,
                )}`}
              >
                {part.criticality}
              </span>
            </div>
            <div className="mt-0.5 text-[#6E6E73] font-medium">{part.name}</div>
          </>
        ),
      },
      {
        key: 'on_hand',
        header: 'On-Hand',
        tooltip:
          'Usable physical stock currently available in the warehouse. Excludes quarantined units awaiting invoice discrepancy resolution.',
        defaultWidth: 105,
        minWidth: 85,
        align: 'right',
        cellClassName: 'font-semibold tabular-nums text-[#1D1D1F]',
        renderCell: (part) => part.on_hand_stock,
      },
      {
        key: 'quarantined',
        header: 'Quarantined',
        tooltip:
          'Units physically received via GRN but locked out of usable On-Hand stock due to a 3-way invoice mismatch. Released once a corrected PDF invoice matches.',
        defaultWidth: 120,
        minWidth: 95,
        align: 'right',
        cellClassName: 'tabular-nums',
        renderCell: (part) =>
          part.quarantined_stock > 0 ? (
            <span className="inline-flex items-center gap-1 rounded-full bg-[#B46900]/10 border border-[#B46900]/24 px-2 py-0.5 font-semibold text-[#945600]">
              <AlertTriangle className="h-3 w-3" />
              {part.quarantined_stock}
            </span>
          ) : (
            <span className="text-[#86868B]">0</span>
          ),
      },
      {
        key: 'on_order',
        header: 'On-Order',
        tooltip:
          'Open replenishment quantity across active Purchase Orders (including drafts awaiting dispatch approval, net of quarantined units) to prevent duplicate POs.',
        defaultWidth: 105,
        minWidth: 85,
        align: 'right',
        cellClassName: 'tabular-nums text-[#6E6E73] font-medium',
        renderCell: (part) => part.on_order_stock,
      },
      {
        key: 'inventory_position',
        header: 'Inventory Position',
        tooltip:
          'Total pipeline stock (On-Hand + On-Order + Quarantined) evaluated against ROP before triggering a new PO so in-flight or disputed shipments never spawn duplicate POs.',
        defaultWidth: 150,
        minWidth: 115,
        align: 'right',
        cellClassName: 'tabular-nums',
        renderCell: (part) => {
          const isBelowRop = part.inventory_position < part.rop;
          return (
            <span className={`font-bold ${isBelowRop ? 'text-[#B82923]' : 'text-[#1D1D1F]'}`}>
              {part.inventory_position}
            </span>
          );
        },
      },
      {
        key: 'rop_s',
        header: 'Reorder Point / Target',
        tooltip:
          'Reorder Point (ROP) trigger threshold and Order-Up-To target level (S) with target coverage days. When Inventory Position < ROP, an order of (S - Inventory Position) rounded up to MOQ is triggered.',
        defaultWidth: 175,
        minWidth: 135,
        align: 'right',
        cellClassName: 'tabular-nums text-[#1D1D1F]',
        renderCell: (part) => (
          <>
            <span className="font-semibold">{part.rop}</span>
            <span className="text-[#86868B]"> / </span>
            <span>{part.replenishment_point}</span>
            <div className="text-[10px] text-[#86868B]">{part.target_coverage_days}d cov</div>
          </>
        ),
      },
      {
        key: 'demand_override',
        header: 'Demand Override',
        tooltip:
          'Manual daily demand rate (units/day) overriding the automatic 12-month EWMA demand forecast. Shows Auto when statistical EWMA forecasting is active.',
        defaultWidth: 145,
        minWidth: 115,
        align: 'right',
        cellClassName: 'tabular-nums',
        renderCell: (part) =>
          part.manual_demand_override !== null ? (
            <span className="rounded-full bg-[#0066CC]/10 border border-[#0066CC]/22 px-2 py-0.5 font-semibold text-[#0058B3]">
              {part.manual_demand_override}/d
            </span>
          ) : (
            <span className="text-[#86868B]">Auto</span>
          ),
      },
      {
        key: 'suppliers',
        header: 'Primary & Backup Suppliers',
        tooltip:
          'Ranked Primary (1°) and Backup (2°) suppliers for this SKU, displaying agreed unit price, observed effective lead time vs. quoted lead time, and Minimum Order Quantity (MOQ).',
        defaultWidth: 360,
        minWidth: 220,
        cellClassName: 'space-y-1 tabular-nums',
        renderCell: (part) => {
          const primary = part.suppliers.find((s) => s.supplier_rank === 1);
          const backup = part.suppliers.find((s) => s.supplier_rank === 2);
          return (
            <>
              {primary ? (
                <div className="text-[#1D1D1F]">
                  <span className="font-semibold">1° {primary.supplier_name}</span>{' '}
                  <span className="text-[#6E6E73]">
                    · {formatInr(primary.agreed_unit_price)} ·{' '}
                    {primary.effective_lead_time_days.toFixed(1)}d eff LT (q
                    {primary.quoted_lead_time_days}d) · MOQ {primary.moq}
                  </span>
                </div>
              ) : (
                <div className="text-[#86868B]">No primary supplier</div>
              )}
              {backup && (
                <div className="text-[#6E6E73]">
                  <span>2° {backup.supplier_name}</span> · {formatInr(backup.agreed_unit_price)} ·{' '}
                  {backup.quoted_lead_time_days}d LT · MOQ {backup.moq}
                </div>
              )}
            </>
          );
        },
      },
      {
        key: 'actions',
        header: 'Actions',
        tooltip:
          'SKU planning controls: adjust Reorder Point / Order-Up-To (ROP/S) parameters or set a manual daily demand override. Stock withdrawals are requested by Making Team via Mailroom and approved in Alerts.',
        defaultWidth: 185,
        minWidth: 165,
        align: 'right',
        renderCell: (part) => (
          <div className="inline-flex flex-wrap justify-end gap-1.5">
            <button
              type="button"
              onClick={() => openModal({ kind: 'rop', part })}
              className="apple-press inline-flex items-center gap-1 rounded-lg border border-black/[0.10] bg-white px-2.5 py-1 text-xs font-medium text-[#1D1D1F] hover:bg-[#FBFBFD]"
              title="Edit ROP and S"
            >
              <Edit3 className="h-3 w-3 text-[#6E6E73]" />
              ROP/S
            </button>
            <button
              type="button"
              onClick={() => openModal({ kind: 'demand', part })}
              className="apple-press inline-flex items-center gap-1 rounded-lg border border-black/[0.10] bg-white px-2.5 py-1 text-xs font-medium text-[#1D1D1F] hover:bg-[#FBFBFD]"
              title="Manual daily demand override"
            >
              <Sliders className="h-3 w-3 text-[#6E6E73]" />
              Demand
            </button>
          </div>
        ),
      },
    ],
    [openModal],
  );

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold tracking-tight text-[#1D1D1F]">Inventory &amp; Parts</h1>
          <p className="text-sm text-[#6E6E73]">
            15-SKU live stock ledger, inventory position (On-Hand + On-Order + Quarantined), ROP/S, and supplier terms. Withdrawals flow via Making Team Mailroom requests &amp; Manager alert approvals.
          </p>
        </div>

        <div className="flex items-center gap-2 text-xs tabular-nums">
          <div className="rounded-full border border-black/[0.08] bg-white px-3.5 py-1.5 shadow-2xs">
            <span className="text-[#6E6E73]">Below ROP:</span>{' '}
            <span className="font-bold text-[#B82923]">{belowRopCount}</span>
          </div>
          <div className="rounded-full border border-black/[0.08] bg-white px-3.5 py-1.5 shadow-2xs">
            <span className="text-[#6E6E73]">Quarantined Units:</span>{' '}
            <span className="font-bold text-[#945600]">{totalQuarantined}</span>
          </div>
          <button
            type="button"
            onClick={() => void loadParts()}
            className="apple-press inline-flex items-center gap-1.5 rounded-full border border-black/[0.08] bg-white px-3.5 py-1.5 font-medium text-[#1D1D1F] hover:bg-[#FBFBFD] shadow-2xs"
          >
            <RefreshCw className={`h-3.5 w-3.5 text-[#6E6E73] ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="relative flex-1 min-w-64 max-w-sm">
          <Search className="pointer-events-none absolute left-3.5 top-2.5 h-4 w-4 text-[#86868B]" />
          <input
            type="text"
            placeholder="Search SKU, part name, or supplier..."
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              if (focusPartId) onClearFocus();
            }}
            className="w-full rounded-xl border border-black/[0.08] bg-white py-2 pl-9 pr-3 text-sm text-[#1D1D1F] placeholder:text-[#86868B] shadow-2xs focus:border-[#0066CC] focus:outline-none"
          />
        </div>

        <div className="inline-flex rounded-full border border-black/[0.08] bg-white/90 p-1 shadow-2xs">
          {(['ALL', 'CRITICAL', 'STANDARD', 'LOW'] as const).map((lvl) => (
            <button
              key={lvl}
              type="button"
              onClick={() => setCriticality(lvl)}
              className={`apple-press rounded-full px-3.5 py-1 text-xs font-semibold ${
                criticality === lvl
                  ? 'bg-[#0066CC] text-white shadow-2xs'
                  : 'text-[#6E6E73] hover:bg-black/[0.04] hover:text-[#1D1D1F]'
              }`}
            >
              {lvl}
            </button>
          ))}
        </div>
      </div>

      <DataTable
        storageKey="invplan_col_widths_inventory"
        ariaLabel="Inventory and Parts Ledger"
        columns={columns}
        rows={filteredParts}
        rowKey={(part) => part.part_id}
        loading={loading}
        maxHeight="calc(100vh - 280px)"
        emptyMessage="No parts match the current filter."
        rowClassName={(part) => {
          const isBelowRop = part.inventory_position < part.rop;
          const isFocused = focusPartId === part.part_id;
          if (isFocused) return 'bg-[#0066CC]/[0.07]';
          if (isBelowRop) return 'bg-[#C9342D]/[0.03] hover:bg-[#C9342D]/[0.06]';
          return 'hover:bg-black/[0.02]';
        }}
      />

      {modal && (
        <div className="anim-backdrop fixed inset-0 z-40 flex items-center justify-center bg-black/30 backdrop-blur-xs p-4">
          <div className="anim-modal-pop w-full max-w-md rounded-2xl border border-black/[0.08] bg-white p-5 shadow-[0_18px_48px_rgba(0,0,0,0.14)]">
            <div className="flex items-start justify-between gap-2">
              <div>
                <h2 className="text-base font-bold tracking-tight text-[#1D1D1F]">
                  {modal.kind === 'rop'
                    ? `Edit ROP / S — ${modal.part.sku}`
                    : `Demand Override — ${modal.part.sku}`}
                </h2>
                <p className="text-xs text-[#6E6E73] mt-0.5">{modal.part.name}</p>
              </div>
              <button
                type="button"
                onClick={() => setModal(null)}
                className="apple-press rounded-full p-1.5 text-[#86868B] hover:bg-black/[0.05] hover:text-[#1D1D1F]"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            {modal.kind === 'rop' && (
              <form onSubmit={handleSubmitRop} className="mt-4 space-y-3">
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-xs font-semibold text-[#1D1D1F] mb-1">
                      Reorder Point (ROP)
                    </label>
                    <input
                      type="number"
                      min={0}
                      value={ropInput}
                      onChange={(e) => setRopInput(e.target.value)}
                      required
                      className="w-full rounded-xl border border-black/[0.12] px-3 py-2 text-sm text-[#1D1D1F] tabular-nums focus:border-[#0066CC] focus:outline-none"
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-semibold text-[#1D1D1F] mb-1">
                      Order-Up-To (S)
                    </label>
                    <input
                      type="number"
                      min={1}
                      value={sInput}
                      onChange={(e) => setSInput(e.target.value)}
                      required
                      className="w-full rounded-xl border border-black/[0.12] px-3 py-2 text-sm text-[#1D1D1F] tabular-nums focus:border-[#0066CC] focus:outline-none"
                    />
                  </div>
                </div>

                <div className="flex justify-end gap-2 pt-2">
                  <button
                    type="button"
                    onClick={() => setModal(null)}
                    className="apple-press rounded-xl border border-black/[0.10] px-3.5 py-2 text-xs font-medium text-[#1D1D1F] hover:bg-[#FBFBFD]"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={submitting}
                    className="apple-press rounded-xl bg-[#0066CC] px-4 py-2 text-xs font-semibold text-white hover:bg-[#0071E3] disabled:opacity-45"
                  >
                    {submitting ? 'Saving...' : 'Save ROP / S'}
                  </button>
                </div>
              </form>
            )}

            {modal.kind === 'demand' && (
              <form onSubmit={handleSubmitDemand} className="mt-4 space-y-3">
                <p className="text-xs text-[#6E6E73]">
                  Set an additive/override daily demand rate (units/day) for planning calculations, or leave blank to revert to EWMA forecast.
                </p>
                <div>
                  <label className="block text-xs font-semibold text-[#1D1D1F] mb-1">
                    Manual Daily Demand (units/day)
                  </label>
                  <input
                    type="number"
                    step="0.1"
                    min={0}
                    placeholder="Blank = auto EWMA"
                    value={demandInput}
                    onChange={(e) => setDemandInput(e.target.value)}
                    className="w-full rounded-xl border border-black/[0.12] px-3 py-2 text-sm text-[#1D1D1F] tabular-nums focus:border-[#0066CC] focus:outline-none"
                  />
                </div>

                <div className="flex justify-between gap-2 pt-2">
                  <button
                    type="button"
                    disabled={submitting}
                    onClick={() => setDemandInput('')}
                    className="apple-press rounded-xl border border-black/[0.10] px-3 py-2 text-xs font-medium text-[#6E6E73] hover:bg-[#FBFBFD]"
                  >
                    Clear Override
                  </button>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => setModal(null)}
                      className="apple-press rounded-xl border border-black/[0.10] px-3.5 py-2 text-xs font-medium text-[#1D1D1F] hover:bg-[#FBFBFD]"
                    >
                      Cancel
                    </button>
                    <button
                      type="submit"
                      disabled={submitting}
                      className="apple-press rounded-xl bg-[#0066CC] px-4 py-2 text-xs font-semibold text-white hover:bg-[#0071E3] disabled:opacity-45"
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
