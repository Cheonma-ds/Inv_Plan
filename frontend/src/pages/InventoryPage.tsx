import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  Activity,
  AlertTriangle,
  Check,
  ExternalLink,
  Package,
  PackageMinus,
  RefreshCw,
  RotateCcw,
  Search,
  ShoppingCart,
  X,
} from 'lucide-react';
import { api } from '../api/client';
import type { PartItem } from '../api/types';
import { CreateEditPOModal } from '../components/CreateEditPOModal';
import { DataTable, type ColumnDef } from '../components/DataTable';
import { StockoutCycleDrawer } from '../components/StockoutCycleDrawer';
import {
  criticalityBadgeClass,
  formatBurnRate,
  formatInr,
  formatSimDateTime,
  severityBadgeClass,
  stockoutUrgencyBadgeClass,
} from '../utils/format';

interface InventoryPageProps {
  refreshNonce: number;
  focusPartId: number | null;
  onClearFocus: () => void;
  onMutated: () => void;
  onNotify: (kind: 'success' | 'error' | 'info', title: string, detail?: string) => void;
  onNavigatePo: (poId: number) => void;
}

type ModalState = { kind: 'withdraw'; part: PartItem } | null;

interface DismissedPopoverState {
  partId: number;
  anchorRect: DOMRect;
}

function formatTrendIndicator(trendPct: number | undefined): string {
  if (trendPct === undefined || Number.isNaN(trendPct) || Math.abs(trendPct) < 0.5) {
    return '0%';
  }
  const absRounded = Math.round(Math.abs(trendPct));
  return trendPct > 0 ? `↑ ${absRounded}%` : `↓ ${absRounded}%`;
}

function formatStockoutDays(
  onHand: number,
  daysToStockout: number | null | undefined,
): string {
  if (onHand <= 0) return '0d';
  if (daysToStockout === null || daysToStockout === undefined || Number.isNaN(daysToStockout)) {
    return '—';
  }
  if (daysToStockout < 10) return `${daysToStockout.toFixed(1)}d`;
  return `${Math.round(daysToStockout)}d`;
}

export function InventoryPage({
  refreshNonce,
  focusPartId,
  onClearFocus,
  onMutated,
  onNotify,
  onNavigatePo,
}: InventoryPageProps) {
  const [parts, setParts] = useState<PartItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [criticality, setCriticality] = useState<string>('ALL');
  const [modal, setModal] = useState<ModalState>(null);
  const [submitting, setSubmitting] = useState(false);

  // Stockout & Cycle drawer state
  const [cyclePartId, setCyclePartId] = useState<number | null>(null);

  // Unresolved dismissed alerts popover state
  const [dismissedPopover, setDismissedPopover] = useState<DismissedPopoverState | null>(null);
  const [busyDismissedId, setBusyDismissedId] = useState<number | null>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);
  const [popoverCoords, setPopoverCoords] = useState<{ top: number; left: number } | null>(null);

  // Withdraw form fields
  const [withdrawQtyInput, setWithdrawQtyInput] = useState('1');
  const [withdrawNoteInput, setWithdrawNoteInput] = useState('');

  // Create PO modal target
  const [createPoPart, setCreatePoPart] = useState<PartItem | null>(null);

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

  const activePopoverPart = useMemo(() => {
    if (!dismissedPopover) return null;
    const found = parts.find((p) => p.part_id === dismissedPopover.partId);
    if (!found || found.unresolved_dismissed_alerts.length === 0) return null;
    return found;
  }, [dismissedPopover, parts]);

  useEffect(() => {
    if (dismissedPopover && !activePopoverPart) {
      setDismissedPopover(null);
    }
  }, [dismissedPopover, activePopoverPart]);

  useLayoutEffect(() => {
    if (!dismissedPopover || !activePopoverPart) {
      setPopoverCoords(null);
      return;
    }
    const { anchorRect } = dismissedPopover;
    const el = popoverRef.current;
    const width = el ? el.offsetWidth : 340;
    const height = el ? el.offsetHeight : 220;
    const pad = 12;

    const left = Math.min(
      Math.max(pad, anchorRect.left),
      Math.max(pad, window.innerWidth - width - pad),
    );
    const fitsBelow = anchorRect.bottom + 8 + height <= window.innerHeight - pad;
    const top = fitsBelow
      ? anchorRect.bottom + 8
      : Math.max(pad, anchorRect.top - height - 8);
    setPopoverCoords({ top, left });
  }, [dismissedPopover, activePopoverPart]);

  useEffect(() => {
    if (!dismissedPopover) return;
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setDismissedPopover(null);
    };
    const handleClickOutside = (e: MouseEvent) => {
      if (popoverRef.current && !popoverRef.current.contains(e.target as Node)) {
        setDismissedPopover(null);
      }
    };
    window.addEventListener('keydown', handleKey);
    window.addEventListener('mousedown', handleClickOutside);
    return () => {
      window.removeEventListener('keydown', handleKey);
      window.removeEventListener('mousedown', handleClickOutside);
    };
  }, [dismissedPopover]);

  const openModal = useCallback((next: ModalState) => {
    setModal(next);
    if (!next) return;
    if (next.kind === 'withdraw') {
      setWithdrawQtyInput(next.part.on_hand_stock > 0 ? '1' : '0');
      setWithdrawNoteInput('');
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

  const handleSubmitWithdraw = async (e: React.FormEvent) => {
    e.preventDefault();
    if (modal?.kind !== 'withdraw') return;
    const qty = Number.parseInt(withdrawQtyInput, 10);
    if (!Number.isFinite(qty) || qty <= 0) {
      onNotify('error', 'Invalid withdraw quantity', 'Enter a positive integer.');
      return;
    }
    setSubmitting(true);
    try {
      const res = await api.consumeStock(modal.part.part_id, qty, withdrawNoteInput.trim());
      const poMsg = res.created_po
        ? ` Auto-triggered ${res.created_po.po_number} (${res.created_po.qty} units) for approval.`
        : '';
      onNotify(
        'success',
        `Withdrew ${qty} units of ${modal.part.sku}`,
        `Remaining on hand: ${res.consume.snapshot.on_hand_stock}.${poMsg}`,
      );
      setModal(null);
      onMutated();
      await loadParts();
    } catch (err) {
      onNotify(
        'error',
        'Withdrawal failed',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setSubmitting(false);
    }
  };

  const handleReopenDismissedAlert = async (alertId: number) => {
    setBusyDismissedId(alertId);
    try {
      await api.reopenAlert(alertId);
      onNotify('success', `Reopened alert #${alertId}`, 'Alert restored to Open Alerts.');
      onMutated();
      await loadParts();
    } catch (err) {
      onNotify(
        'error',
        'Failed to reopen alert',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setBusyDismissedId(null);
    }
  };

  const handleAcknowledgeDismissedAlert = async (alertId: number) => {
    setBusyDismissedId(alertId);
    try {
      await api.acknowledgeDismissedAlert(alertId);
      onNotify('info', `Acknowledged alert #${alertId}`, 'Cleared inventory warning badge.');
      onMutated();
      await loadParts();
    } catch (err) {
      onNotify(
        'error',
        'Failed to acknowledge dismissed alert',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setBusyDismissedId(null);
    }
  };

  const totalOnHold = parts.reduce((acc, p) => acc + p.quarantined_stock, 0);
  const belowRopCount = parts.filter((p) => p.inventory_position < p.rop).length;
  const cyclePart = useMemo(
    () => (cyclePartId !== null ? parts.find((p) => p.part_id === cyclePartId) ?? null : null),
    [parts, cyclePartId],
  );

  const columns = useMemo<ColumnDef<PartItem>[]>(
    () => [
      {
        key: 'part',
        header: 'Part',
        tooltip:
          'Unique SKU identifier, part name, operational criticality tier, and warning indicator for unresolved dismissed alerts.',
        defaultWidth: 255,
        minWidth: 190,
        renderCell: (part) => {
          const dismissedCount = part.unresolved_dismissed_alerts?.length ?? 0;
          return (
            <>
              <div className="flex items-center gap-1.5 flex-wrap">
                <Package className="h-3.5 w-3.5 text-[#86868B] shrink-0" />
                <span className="font-mono font-semibold text-[#1D1D1F]">{part.sku}</span>
                <span
                  className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${criticalityBadgeClass(
                    part.criticality,
                  )}`}
                >
                  {part.criticality}
                </span>
                {dismissedCount > 0 && (
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      const rect = e.currentTarget.getBoundingClientRect();
                      setDismissedPopover((prev) =>
                        prev?.partId === part.part_id
                          ? null
                          : { partId: part.part_id, anchorRect: rect },
                      );
                    }}
                    title={`${dismissedCount} unresolved dismissed alert(s) — click to reopen or acknowledge`}
                    aria-label={`${dismissedCount} unresolved dismissed alert(s) for ${part.sku}`}
                    className="apple-press inline-flex items-center gap-1 rounded-full border border-[#B46900]/35 bg-[#B46900]/12 px-1.5 py-0.5 text-[10px] font-bold text-[#945600] hover:bg-[#B46900]/20"
                  >
                    <AlertTriangle className="h-3 w-3 shrink-0" />
                    <span>{dismissedCount}</span>
                  </button>
                )}
              </div>
              <div className="mt-0.5 text-[#6E6E73] font-medium">{part.name}</div>
            </>
          );
        },
      },
      {
        key: 'on_hand',
        header: 'On Hand',
        tooltip:
          'Usable physical stock currently available in the warehouse. Excludes On Hold units awaiting invoice discrepancy resolution.',
        defaultWidth: 95,
        minWidth: 80,
        align: 'right',
        cellClassName: 'font-semibold tabular-nums text-[#1D1D1F]',
        renderCell: (part) => part.on_hand_stock,
      },
      {
        key: 'on_hold',
        header: 'On Hold',
        tooltip:
          'Units physically received via GRN but locked in quarantine due to a 3-way invoice mismatch or damage hold.',
        defaultWidth: 95,
        minWidth: 80,
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
        header: 'On Order',
        tooltip:
          'Remaining quantity across dispatched+ active Purchase Orders (PENDING_DISPATCH_APPROVAL drafts contribute 0 until sent; net of GRNs received).',
        defaultWidth: 105,
        minWidth: 85,
        align: 'right',
        cellClassName: 'tabular-nums text-[#6E6E73] font-medium',
        renderCell: (part) => (
          <div>
            <div>{part.on_order_stock}</div>
            {part.pending_dispatch_qty > 0 && (
              <div className="text-[10px] text-[#86868B]" title="Pending dispatch approval (not counted in On Order yet)">
                Draft {part.pending_dispatch_qty}u
              </div>
            )}
          </div>
        ),
      },
      {
        key: 'inventory_position',
        header: 'Inventory Position',
        tooltip:
          'Total pipeline stock (On Hand + On Order + On Hold) evaluated against ROP before triggering replenishment.',
        defaultWidth: 138,
        minWidth: 110,
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
        key: 'daily_burn',
        header: 'Daily Burn',
        tooltip:
          'Trailing 90-day IST completed-day EWMA burn rate (α = 0.30) with 7-day neutral trend indicator.',
        defaultWidth: 120,
        minWidth: 100,
        align: 'right',
        cellClassName: 'tabular-nums',
        renderCell: (part) => (
          <div>
            <div className="font-semibold text-[#1D1D1F]">
              {formatBurnRate(part.daily_burn_ewma)}
            </div>
            <div className="text-[10px] font-medium text-[#86868B]">
              {formatTrendIndicator(part.trend_pct)}
            </div>
          </div>
        ),
      },
      {
        key: 'stockout_in',
        header: 'Stockout In',
        tooltip:
          'Estimated runway until stockout vs. primary supplier effective lead time + communication buffer. Click to inspect 90-day stockout & replenishment cycle chart.',
        defaultWidth: 118,
        minWidth: 96,
        align: 'right',
        cellClassName: 'tabular-nums',
        renderCell: (part) => (
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              setCyclePartId(part.part_id);
            }}
            title="Inspect 90-day stockout & replenishment cycle chart"
            className={`apple-press inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-semibold cursor-pointer ${stockoutUrgencyBadgeClass(
              part.stockout_urgency,
            )}`}
          >
            <Activity className="h-3 w-3 shrink-0" />
            <span>{formatStockoutDays(part.on_hand_stock, part.days_to_stockout)}</span>
          </button>
        ),
      },
      {
        key: 'rop_t',
        header: 'ROP/T',
        tooltip:
          'Dynamic Reorder Point (ROP) and Order-Up-To Target level (T) derived from daily EWMA (α = 0.30), demand variance, and effective lead time.',
        defaultWidth: 110,
        minWidth: 90,
        align: 'right',
        cellClassName: 'tabular-nums text-[#1D1D1F]',
        renderCell: (part) => (
          <span>
            <span className="font-semibold">{part.rop}</span>
            <span className="text-[#86868B]">/</span>
            <span>{part.replenishment_point}</span>
          </span>
        ),
      },
      {
        key: 'suppliers',
        header: 'Primary & Backup Suppliers',
        tooltip:
          'Ranked Primary (1°) and Backup (2°) suppliers displaying unit price, effective lead time vs. quoted lead time, and MOQ.',
        defaultWidth: 340,
        minWidth: 215,
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
          'Withdraw stock, view an active Purchase Order in PO Tracker, or create a manual Purchase Order when no active PO is open.',
        defaultWidth: 205,
        minWidth: 180,
        align: 'right',
        renderCell: (part) => (
          <div className="inline-flex flex-wrap justify-end gap-1.5">
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                openModal({ kind: 'withdraw', part });
              }}
              disabled={part.on_hand_stock <= 0}
              className="apple-press inline-flex items-center gap-1 rounded-lg border border-black/[0.10] bg-white px-2.5 py-1 text-xs font-medium text-[#1D1D1F] hover:bg-[#FBFBFD] disabled:opacity-40"
              title="Withdraw stock"
            >
              <PackageMinus className="h-3 w-3 text-[#6E6E73]" />
              Withdraw
            </button>
            {part.active_po_id !== null ? (
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  if (part.active_po_id !== null) {
                    onNavigatePo(part.active_po_id);
                  }
                }}
                className="apple-press inline-flex items-center gap-1 rounded-lg border border-[#5E5CE6]/30 bg-[#5E5CE6]/[0.07] px-2.5 py-1 text-xs font-semibold text-[#4B48C4] hover:bg-[#5E5CE6]/[0.14]"
                title={`View ${part.active_po_number ?? 'active PO'} (${part.active_po_status ?? 'OPEN'}) in PO Tracker`}
              >
                <ExternalLink className="h-3 w-3" />
                View PO
              </button>
            ) : (
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  setCreatePoPart(part);
                }}
                disabled={part.suppliers.length === 0}
                className="apple-press inline-flex items-center gap-1 rounded-lg border border-[#0066CC]/30 bg-[#0066CC]/[0.06] px-2.5 py-1 text-xs font-semibold text-[#0058B3] hover:bg-[#0066CC]/[0.12] disabled:opacity-40"
                title="Create manual Purchase Order"
              >
                <ShoppingCart className="h-3 w-3" />
                Create PO
              </button>
            )}
          </div>
        ),
      },
    ],
    [openModal, onNavigatePo],
  );

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold tracking-tight text-[#1D1D1F]">Inventory &amp; Parts</h1>
          <p className="text-sm text-[#6E6E73]">
            15-SKU live stock ledger with On Hand, On Hold, On Order, EWMA Daily Burn (α = 0.30), supplier-aware Stockout In runway, and dynamic ROP/T.
          </p>
        </div>

        <div className="flex items-center gap-2 text-xs tabular-nums">
          <div className="rounded-full border border-black/[0.08] bg-white px-3.5 py-1.5 shadow-2xs">
            <span className="text-[#6E6E73]">Below ROP:</span>{' '}
            <span className="font-bold text-[#B82923]">{belowRopCount}</span>
          </div>
          <div className="rounded-full border border-black/[0.08] bg-white px-3.5 py-1.5 shadow-2xs">
            <span className="text-[#6E6E73]">On Hold Units:</span>{' '}
            <span className="font-bold text-[#945600]">{totalOnHold}</span>
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
        onRowClick={(part) => setCyclePartId(part.part_id)}
        rowClassName={(part) => {
          const isBelowRop = part.inventory_position < part.rop;
          const isFocused = focusPartId === part.part_id;
          if (isFocused) return 'bg-[#0066CC]/[0.07]';
          if (isBelowRop) return 'bg-[#C9342D]/[0.03] hover:bg-[#C9342D]/[0.06]';
          return 'hover:bg-black/[0.02]';
        }}
      />

      {/* Interactive Popover for Unresolved Dismissed Alerts */}
      {dismissedPopover &&
        activePopoverPart &&
        typeof document !== 'undefined' &&
        createPortal(
          <div
            ref={popoverRef}
            role="dialog"
            aria-label={`Unresolved dismissed alerts for ${activePopoverPart.sku}`}
            style={{
              position: 'fixed',
              top: popoverCoords
                ? `${popoverCoords.top}px`
                : `${dismissedPopover.anchorRect.bottom + 8}px`,
              left: popoverCoords
                ? `${popoverCoords.left}px`
                : `${Math.max(12, dismissedPopover.anchorRect.left)}px`,
              opacity: popoverCoords ? 1 : 0,
            }}
            className="z-50 w-[360px] max-w-[calc(100vw-24px)] rounded-2xl border border-black/[0.12] bg-white/96 p-4 shadow-[0_14px_38px_rgba(0,0,0,0.16)] backdrop-blur-xl space-y-3"
          >
            <div className="flex items-center justify-between gap-2 border-b border-black/[0.06] pb-2">
              <div className="flex items-center gap-1.5 text-xs font-bold text-[#945600]">
                <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                <span>
                  Unresolved Dismissed Alert
                  {activePopoverPart.unresolved_dismissed_alerts.length > 1 ? 's' : ''} ·{' '}
                  <span className="font-mono">{activePopoverPart.sku}</span>
                </span>
              </div>
              <button
                type="button"
                aria-label="Close dismissed alerts popover"
                onClick={() => setDismissedPopover(null)}
                className="apple-press rounded-full p-1 text-[#86868B] hover:bg-black/[0.05] hover:text-[#1D1D1F]"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>

            <div className="max-h-72 space-y-2.5 overflow-y-auto pr-0.5">
              {activePopoverPart.unresolved_dismissed_alerts.map((alert) => {
                const isBusy = busyDismissedId === alert.id;
                return (
                  <div
                    key={alert.id}
                    className="rounded-xl border border-black/[0.08] bg-[#FBFBFD] p-3 text-xs space-y-2"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span
                        className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${severityBadgeClass(
                          alert.severity,
                        )}`}
                      >
                        {alert.severity}
                      </span>
                      <span className="text-[10px] text-[#86868B] tabular-nums">
                        {formatSimDateTime(alert.created_at)}
                      </span>
                    </div>
                    <div className="font-semibold text-[#1D1D1F]">{alert.title}</div>
                    <p className="text-[11px] leading-relaxed text-[#6E6E73]">{alert.summary}</p>
                    <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
                      <button
                        type="button"
                        disabled={isBusy}
                        onClick={() => void handleReopenDismissedAlert(alert.id)}
                        className="apple-press inline-flex items-center gap-1 rounded-lg bg-[#0066CC] px-2.5 py-1 text-[11px] font-semibold text-white hover:bg-[#0071E3] disabled:opacity-45"
                      >
                        <RotateCcw className="h-3 w-3" />
                        Reopen Alert
                      </button>
                      <button
                        type="button"
                        disabled={isBusy}
                        onClick={() => void handleAcknowledgeDismissedAlert(alert.id)}
                        className="apple-press inline-flex items-center gap-1 rounded-lg border border-black/[0.12] bg-white px-2.5 py-1 text-[11px] font-medium text-[#1D1D1F] hover:bg-black/[0.03] disabled:opacity-45"
                      >
                        <Check className="h-3 w-3 text-[#1B6B43]" />
                        Acknowledge
                      </button>
                      {alert.po_id !== null && (
                        <button
                          type="button"
                          onClick={() => {
                            setDismissedPopover(null);
                            if (alert.po_id !== null) onNavigatePo(alert.po_id);
                          }}
                          className="apple-press inline-flex items-center gap-1 rounded-lg border border-[#5E5CE6]/28 bg-[#5E5CE6]/[0.07] px-2 py-1 text-[11px] font-semibold text-[#4B48C4] hover:bg-[#5E5CE6]/[0.14]"
                        >
                          <ExternalLink className="h-3 w-3" />
                          View PO
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>,
          document.body,
        )}

      {/* 90-Day Stockout & Cycle Analytics Drawer */}
      <StockoutCycleDrawer
        partId={cyclePartId}
        activePoId={cyclePart?.active_po_id}
        activePoNumber={cyclePart?.active_po_number}
        refreshNonce={refreshNonce}
        onClose={() => setCyclePartId(null)}
        onNavigatePo={onNavigatePo}
        onCreatePo={(id) => {
          const target = parts.find((p) => p.part_id === id);
          if (target && target.active_po_id === null) {
            setCreatePoPart(target);
          }
        }}
      />

      {createPoPart && (
        <CreateEditPOModal
          mode="create"
          part={createPoPart}
          onClose={() => setCreatePoPart(null)}
          onNotify={onNotify}
          onSuccess={async () => {
            onMutated();
            await loadParts();
          }}
        />
      )}

      {modal && (
        <div className="anim-backdrop fixed inset-0 z-40 flex items-center justify-center bg-black/30 backdrop-blur-xs p-4">
          <div className="anim-modal-pop w-full max-w-md rounded-2xl border border-black/[0.08] bg-white p-5 shadow-[0_18px_48px_rgba(0,0,0,0.14)]">
            <div className="flex items-start justify-between gap-2">
              <div>
                <h2 className="text-base font-bold tracking-tight text-[#1D1D1F]">
                  {`Withdraw Stock — ${modal.part.sku}`}
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

            {modal.kind === 'withdraw' && (
              <form onSubmit={handleSubmitWithdraw} className="mt-4 space-y-3">
                <div className="rounded-xl bg-[#FBFBFD] border border-black/[0.06] px-3 py-2 text-xs text-[#6E6E73] flex items-center justify-between">
                  <span>Available On Hand Stock:</span>
                  <span className="font-bold text-[#1D1D1F] tabular-nums">
                    {modal.part.on_hand_stock} units
                  </span>
                </div>
                <div>
                  <label className="block text-xs font-semibold text-[#1D1D1F] mb-1">
                    Withdraw Quantity
                  </label>
                  <input
                    type="number"
                    min={1}
                    max={Math.max(1, modal.part.on_hand_stock)}
                    value={withdrawQtyInput}
                    onChange={(e) => setWithdrawQtyInput(e.target.value)}
                    required
                    className="w-full rounded-xl border border-black/[0.12] px-3 py-2 text-sm text-[#1D1D1F] tabular-nums focus:border-[#0066CC] focus:outline-none"
                  />
                </div>
                <div>
                  <label className="block text-xs font-semibold text-[#1D1D1F] mb-1">
                    Note (optional)
                  </label>
                  <input
                    type="text"
                    placeholder="e.g., Line 1 assembly pull"
                    value={withdrawNoteInput}
                    onChange={(e) => setWithdrawNoteInput(e.target.value)}
                    className="w-full rounded-xl border border-black/[0.12] px-3 py-2 text-sm text-[#1D1D1F] focus:border-[#0066CC] focus:outline-none"
                  />
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
                    disabled={submitting || modal.part.on_hand_stock <= 0}
                    className="apple-press rounded-xl bg-[#0066CC] px-4 py-2 text-xs font-semibold text-white hover:bg-[#0071E3] disabled:opacity-45"
                  >
                    {submitting ? 'Withdrawing...' : 'Withdraw'}
                  </button>
                </div>
              </form>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
