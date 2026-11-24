import { useCallback, useEffect, useState } from 'react';
import {
  AlertTriangle,
  Check,
  ExternalLink,
  Mail,
  RefreshCw,
  ShieldAlert,
  Sparkles,
  X,
} from 'lucide-react';
import { api } from '../api/client';
import type { AlertItem } from '../api/types';
import { CreateEditPOModal } from '../components/CreateEditPOModal';
import { FormattedEmailBody } from '../components/FormattedEmailBody';
import {
  formatInr,
  formatSimDateTime,
  personaLabel,
  severityBadgeClass,
} from '../utils/format';

interface AlertsPageProps {
  refreshNonce: number;
  onMutated: () => void;
  onNotify: (kind: 'success' | 'error' | 'info', title: string, detail?: string) => void;
  onNavigatePo: (poId: number) => void;
  onNavigatePart: (partId: number) => void;
}

type StatusFilter = 'OPEN' | 'RESOLVED' | 'DISMISSED' | 'ALL';

interface DraftEdits {
  subject: string;
  body: string;
}

function asNumber(v: unknown): number | null {
  return typeof v === 'number' && !Number.isNaN(v) ? v : null;
}

function asString(v: unknown): string | null {
  return typeof v === 'string' && v.trim() ? v : null;
}

interface OpenPoSummary {
  po_id: number | null;
  po_number: string;
  status: string;
  qty: number;
  eta: string | null;
  supplier_name: string | null;
}

function asOpenPos(v: unknown): OpenPoSummary[] | null {
  if (!Array.isArray(v)) return null;
  return v
    .filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null)
    .map((item) => ({
      po_id: asNumber(item.po_id),
      po_number: asString(item.po_number) ?? 'PO',
      status: asString(item.status) ?? 'OPEN',
      qty: asNumber(item.qty) ?? 0,
      eta: asString(item.eta),
      supplier_name: asString(item.supplier_name),
    }));
}

function SupplierTermSuffix({
  unitPrice,
  effectiveLt,
  moq,
  reliability,
}: {
  unitPrice: number | null;
  effectiveLt: number | null;
  moq: number | null;
  reliability?: number | null;
}) {
  return (
    <>
      {unitPrice !== null && (
        <span className="text-[#6E6E73]"> @ {formatInr(unitPrice)}</span>
      )}
      {effectiveLt !== null && (
        <span className="text-[#6E6E73]"> · {effectiveLt.toFixed(1)}d LT</span>
      )}
      {moq !== null && <span className="text-[#6E6E73]"> · MOQ {moq}</span>}
      {reliability !== null && reliability !== undefined && (
        <span className="text-[#6E6E73]"> · Rel {Math.round(reliability * 100)}%</span>
      )}
    </>
  );
}

export function AlertsPage({
  refreshNonce,
  onMutated,
  onNotify,
  onNavigatePo,
  onNavigatePart,
}: AlertsPageProps) {
  const [filter, setFilter] = useState<StatusFilter>('OPEN');
  const [alerts, setAlerts] = useState<AlertItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [busyAlertId, setBusyAlertId] = useState<number | null>(null);
  const [draftEdits, setDraftEdits] = useState<Record<number, DraftEdits>>({});
  const [noteInputs, setNoteInputs] = useState<Record<number, string>>({});
  const [goodQtyInputs, setGoodQtyInputs] = useState<Record<number, string>>({});
  const [editPoModal, setEditPoModal] = useState<{
    poId: number;
    subject?: string;
    body?: string;
  } | null>(null);

  const loadAlerts = useCallback(async () => {
    setLoading(true);
    try {
      let data: AlertItem[];
      if (filter === 'ALL') {
        const [openList, resolvedList, dismissedList] = await Promise.all([
          api.listAlerts('OPEN'),
          api.listAlerts('RESOLVED'),
          api.listAlerts('DISMISSED'),
        ]);
        data = [...openList, ...resolvedList, ...dismissedList].sort(
          (a, b) => b.urgency_score - a.urgency_score || b.id - a.id,
        );
      } else {
        data = await api.listAlerts(filter);
      }
      setAlerts(data);
      setDraftEdits((prev) => {
        const next = { ...prev };
        for (const item of data) {
          if (item.status !== 'DISMISSED' && item.draft_preview && !next[item.id]) {
            next[item.id] = {
              subject: item.draft_preview.subject,
              body: item.draft_preview.body,
            };
          }
        }
        return next;
      });
    } catch (err) {
      onNotify(
        'error',
        'Failed to load alerts',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setLoading(false);
    }
  }, [filter, onNotify]);

  useEffect(() => {
    void loadAlerts();
  }, [loadAlerts, refreshNonce]);

  const handleExecuteCta = async (item: AlertItem, action: string, label: string) => {
    setBusyAlertId(item.id);
    try {
      const params: Record<string, unknown> = {};
      if (
        (action === 'approve_draft' || action === 'ask_making_minimum') &&
        item.draft_preview
      ) {
        const edit = draftEdits[item.id];
        if (edit) {
          if (edit.subject.trim() !== item.draft_preview.subject.trim()) {
            params.edited_subject = edit.subject;
          }
          if (edit.body.trim() !== item.draft_preview.body.trim()) {
            params.edited_body = edit.body;
          }
        }
      } else if (action === 'add_note') {
        params.note =
          (noteInputs[item.id] ?? '').trim() ||
          `Reviewed alert #${item.id} (${label}) via Action Center.`;
      } else if (action === 'release_damaged_goods') {
        const rawGood = (goodQtyInputs[item.id] ?? '').trim();
        if (rawGood !== '') {
          const parsed = Number.parseInt(rawGood, 10);
          if (!Number.isFinite(parsed) || parsed < 0) {
            onNotify('error', 'Invalid usable qty', 'Enter a non-negative integer.');
            setBusyAlertId(null);
            return;
          }
          params.good_qty = parsed;
        }
        if ((noteInputs[item.id] ?? '').trim()) {
          params.note = noteInputs[item.id].trim();
        }
      }

      const res = await api.executeAlertCta(item.id, action, params);
      let detail: string | undefined;
      if (typeof res.new_po_number === 'string') {
        detail = `Created backup PO ${res.new_po_number}`;
      } else if (typeof res.approved_qty === 'number') {
        const stockAfter =
          typeof res.on_hand_after === 'number'
            ? ` (on-hand now ${res.on_hand_after})`
            : '';
        const poNote =
          typeof res.created_po_number === 'string'
            ? ` · Triggered ${res.created_po_number}`
            : '';
        detail = `Issued ${res.approved_qty} units${stockAfter}${poNote}`;
      } else if (typeof res.approval_outcome === 'string') {
        detail = `Draft ${res.approval_outcome}`;
      }
      onNotify('success', `Executed: ${label}`, detail);
      onMutated();
      await loadAlerts();
    } catch (err) {
      onNotify(
        'error',
        `Action failed: ${label}`,
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setBusyAlertId(null);
    }
  };

  const handleDismiss = async (item: AlertItem) => {
    setBusyAlertId(item.id);
    try {
      await api.dismissAlert(item.id);
      onNotify('info', 'Alert dismissed', item.title);
      onMutated();
      await loadAlerts();
    } catch (err) {
      onNotify(
        'error',
        'Failed to dismiss alert',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setBusyAlertId(null);
    }
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold tracking-tight text-[#1D1D1F]">Actionable Alerts</h1>
          <p className="text-sm text-[#6E6E73]">
            Urgency-ranked exceptions, Lex draft approvals, and 1-click operational CTAs.
          </p>
        </div>

        <div className="flex items-center gap-2.5">
          <div className="inline-flex rounded-full border border-black/[0.08] bg-white/90 p-1 shadow-2xs">
            {(['OPEN', 'RESOLVED', 'DISMISSED', 'ALL'] as const).map((tab) => (
              <button
                key={tab}
                type="button"
                onClick={() => setFilter(tab)}
                className={`apple-press rounded-full px-3.5 py-1 text-xs font-semibold ${
                  filter === tab
                    ? 'bg-[#0066CC] text-white shadow-2xs'
                    : 'text-[#6E6E73] hover:bg-black/[0.04] hover:text-[#1D1D1F]'
                }`}
              >
                {tab}
              </button>
            ))}
          </div>

          <button
            type="button"
            onClick={() => void loadAlerts()}
            className="apple-press inline-flex items-center gap-1.5 rounded-full border border-black/[0.08] bg-white px-3.5 py-1.5 text-xs font-medium text-[#1D1D1F] hover:bg-[#FBFBFD] shadow-2xs"
          >
            <RefreshCw className={`h-3.5 w-3.5 text-[#6E6E73] ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>
      </div>

      {loading && alerts.length === 0 ? (
        <div className="apple-card rounded-2xl p-8 text-center text-sm text-[#6E6E73]">
          Loading alerts...
        </div>
      ) : alerts.length === 0 ? (
        <div className="apple-card rounded-2xl border-dashed p-10 text-center">
          <Check className="mx-auto h-8 w-8 text-[#1B6B43]" />
          <h2 className="mt-2 text-sm font-semibold text-[#1D1D1F]">
            No {filter.toLowerCase()} alerts
          </h2>
          <p className="mt-1 text-xs text-[#6E6E73]">
            Advance the simulated clock, send a Making Team stock request, or trigger a supplier email to see alerts here.
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          {alerts.map((item) => {
            const payload = item.cta_payload ?? {};
            const isBusy = busyAlertId === item.id;
            const draft = item.status === 'DISMISSED' ? null : item.draft_preview;
            const edit = draftEdits[item.id] ?? {
              subject: draft?.subject ?? '',
              body: draft?.body ?? '',
            };
            const draftModified =
              draft !== null &&
              (edit.subject.trim() !== draft.subject.trim() ||
                edit.body.trim() !== draft.body.trim());
            const hasAddNoteCta = item.suggested_actions.some((a) => a.action === 'add_note');
            const hasReleaseDamagedCta = item.suggested_actions.some(
              (a) => a.action === 'release_damaged_goods',
            );

            const daysOfSupply = asNumber(payload.projected_days_of_supply);
            const stockoutDate = asString(payload.projected_stockout_date);
            const daysToNewEta = asNumber(payload.days_to_new_eta);
            const priceVarInr = asNumber(payload.price_variance_inr);
            const priceVarPct = asNumber(payload.price_variance_pct);
            const priceOnlyImpact = asNumber(payload.price_only_impact_inr);
            const totalCostImpact = asNumber(payload.total_cost_impact_inr);
            const proposedPrice = asNumber(payload.proposed_unit_price);
            const proposedCurrency = asString(payload.proposed_currency);
            const poCurrency = asString(payload.po_currency) ?? 'INR';
            const currencyMismatch = payload.currency_mismatch === true;
            const proposedQty = asNumber(payload.proposed_qty);
            const proposedEta = asString(payload.proposed_eta_date);
            const backupName = asString(payload.backup_supplier_name);
            const backupPrice = asNumber(payload.backup_unit_price);
            const backupLt =
              asNumber(payload.backup_lead_time_days) ??
              asNumber(payload.backup_effective_lt);
            const recRop = asNumber(payload.rop);
            const recS = asNumber(payload.replenishment_point);
            const heldQty = asNumber(payload.held_qty);
            const invoiceAmount = asNumber(payload.invoice_amount);
            const creditNoteAmount = asNumber(payload.credit_note_amount);
            const netAmount = asNumber(payload.net_amount);
            const packingListQty = asNumber(payload.packing_list_qty);

            // Making-team stock request metrics
            const stockRequestId = asNumber(payload.stock_request_id);
            const isStockRequestAlert =
              stockRequestId !== null ||
              item.type === 'STOCK_WITHDRAWAL_REQUEST' ||
              item.type === 'STOCK_SHORTAGE';
            const reqSku = asString(payload.sku);
            const reqQty = asNumber(payload.requested_qty);
            const origReqQty = asNumber(payload.original_requested_qty);
            const onHandStock = asNumber(payload.on_hand_stock);
            const quarantinedStock = asNumber(payload.quarantined_stock);
            const inventoryPosition = asNumber(payload.inventory_position);
            const shortfallQty = asNumber(payload.shortfall_qty);
            const projectedOnHand = asNumber(payload.projected_on_hand);
            const projectedIp = asNumber(payload.projected_ip);
            const willTriggerPo = payload.will_trigger_po === true;
            const projectedOrderQty = asNumber(payload.projected_order_qty);
            const primarySupName = asString(payload.primary_supplier_name);
            const primaryUnitPrice = asNumber(payload.primary_unit_price);
            const primaryEffLt = asNumber(payload.primary_effective_lt);
            const primaryMoq = asNumber(payload.primary_moq);
            const primaryReliability = asNumber(payload.primary_reliability_score);
            const openPos = asOpenPos(payload.open_pos);

            const hasStockRequestMetrics =
              isStockRequestAlert &&
              (reqQty !== null ||
                onHandStock !== null ||
                shortfallQty !== null ||
                openPos !== null ||
                projectedOrderQty !== null);

            const hasImpactMetrics =
              hasStockRequestMetrics ||
              daysOfSupply !== null ||
              priceVarInr !== null ||
              proposedPrice !== null ||
              proposedQty !== null ||
              proposedEta !== null ||
              backupName !== null ||
              heldQty !== null ||
              netAmount !== null ||
              packingListQty !== null ||
              (!isStockRequestAlert && recRop !== null);

            return (
              <article
                key={item.id}
                className="apple-card rounded-2xl p-5 transition-all duration-200 hover:border-black/[0.14] hover:shadow-[0_4px_18px_rgba(0,0,0,0.05)]"
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="space-y-1.5">
                    <div className="flex flex-wrap items-center gap-2">
                      <span
                        className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-semibold ${severityBadgeClass(
                          item.severity,
                        )}`}
                      >
                        {item.severity === 'CRITICAL' ? (
                          <ShieldAlert className="h-3 w-3" />
                        ) : (
                          <AlertTriangle className="h-3 w-3" />
                        )}
                        {item.severity}
                      </span>
                      <span className="inline-flex items-center rounded-full bg-[#1D1D1F] px-2.5 py-0.5 text-[11px] font-semibold text-white tabular-nums">
                        Urgency {item.urgency_score}
                      </span>
                      <span className="rounded-full bg-black/[0.05] px-2.5 py-0.5 text-[11px] font-medium text-[#6E6E73]">
                        {item.type}
                      </span>
                      {item.status !== 'OPEN' && (
                        <span className="rounded-full bg-[#1F7A4D]/10 border border-[#1F7A4D]/22 px-2.5 py-0.5 text-[11px] font-semibold text-[#1B6B43]">
                          {item.status}
                        </span>
                      )}
                    </div>

                    <h2 className="text-base font-semibold tracking-tight text-[#1D1D1F]">
                      {item.title}
                    </h2>
                    <p className="text-sm text-[#6E6E73] leading-relaxed">{item.summary}</p>
                  </div>

                  <div className="flex flex-col items-end gap-1.5 text-xs text-[#86868B]">
                    <span className="tabular-nums">{formatSimDateTime(item.created_at)}</span>
                    <div className="flex items-center gap-2">
                      {item.po_id && (
                        <button
                          type="button"
                          onClick={() => onNavigatePo(item.po_id!)}
                          className="apple-press inline-flex items-center gap-1 rounded-full border border-black/[0.08] bg-[#FBFBFD] px-2.5 py-1 text-xs font-medium text-[#1D1D1F] hover:bg-black/[0.04]"
                        >
                          PO #{item.po_id}
                          <ExternalLink className="h-3 w-3 text-[#6E6E73]" />
                        </button>
                      )}
                      {item.part_id && (
                        <button
                          type="button"
                          onClick={() => onNavigatePart(item.part_id!)}
                          className="apple-press inline-flex items-center gap-1 rounded-full border border-black/[0.08] bg-[#FBFBFD] px-2.5 py-1 text-xs font-medium text-[#1D1D1F] hover:bg-black/[0.04]"
                        >
                          Part #{item.part_id}
                          <ExternalLink className="h-3 w-3 text-[#6E6E73]" />
                        </button>
                      )}
                    </div>
                  </div>
                </div>

                {hasImpactMetrics && (
                  <div className="mt-3.5 flex flex-wrap gap-2 rounded-xl border border-black/[0.06] bg-[#FBFBFD] p-3 text-xs tabular-nums">
                    {hasStockRequestMetrics && (
                      <>
                        {reqQty !== null && (
                          <div className="rounded-lg bg-white px-2.5 py-1.5 border border-black/[0.07]">
                            <span className="text-[#6E6E73]">
                              {reqSku ? `Requested (${reqSku}):` : 'Requested Qty:'}
                            </span>{' '}
                            <span className="font-semibold text-[#1D1D1F]">{reqQty} units</span>
                            {origReqQty !== null && origReqQty !== reqQty && (
                              <span className="ml-1 text-[#6E6E73]">(orig. {origReqQty})</span>
                            )}
                          </div>
                        )}
                        {onHandStock !== null && (
                          <div className="rounded-lg bg-white px-2.5 py-1.5 border border-black/[0.07]">
                            <span className="text-[#6E6E73]">On Hand:</span>{' '}
                            <span
                              className={`font-semibold ${
                                onHandStock === 0 ? 'text-[#B82923]' : 'text-[#1D1D1F]'
                              }`}
                            >
                              {onHandStock} units
                            </span>
                            {quarantinedStock !== null && quarantinedStock > 0 && (
                              <span className="ml-1.5 font-medium text-[#945600]">
                                ({quarantinedStock} quarantined)
                              </span>
                            )}
                          </div>
                        )}
                        {shortfallQty !== null && shortfallQty > 0 && (
                          <div className="rounded-lg bg-[#C9342D]/[0.06] px-2.5 py-1.5 border border-[#C9342D]/20">
                            <span className="text-[#B82923]">Shortfall:</span>{' '}
                            <span className="font-bold text-[#B82923]">{shortfallQty} units</span>
                          </div>
                        )}
                        {shortfallQty === 0 &&
                          projectedOnHand !== null &&
                          projectedIp !== null &&
                          recRop !== null && (
                            <div className="rounded-lg bg-white px-2.5 py-1.5 border border-black/[0.07]">
                              <span className="text-[#6E6E73]">Post-Issue:</span>{' '}
                              <span className="font-semibold text-[#1D1D1F]">
                                {projectedOnHand} on-hand · IP {projectedIp}
                              </span>{' '}
                              <span
                                className={
                                  willTriggerPo
                                    ? 'font-semibold text-[#945600]'
                                    : 'text-[#1B6B43] font-medium'
                                }
                              >
                                ({willTriggerPo ? `< ROP ${recRop}` : `>= ROP ${recRop}`})
                              </span>
                            </div>
                          )}
                        {(shortfallQty === null || shortfallQty > 0) &&
                          inventoryPosition !== null &&
                          recRop !== null &&
                          recS !== null && (
                            <div className="rounded-lg bg-white px-2.5 py-1.5 border border-black/[0.07]">
                              <span className="text-[#6E6E73]">Inv. Position / ROP/T:</span>{' '}
                              <span className="font-semibold text-[#1D1D1F]">
                                {inventoryPosition}
                              </span>{' '}
                              <span className="text-[#6E6E73]">
                                (ROP {recRop}/T {recS})
                              </span>
                            </div>
                          )}
                        {willTriggerPo && projectedOrderQty !== null && (
                          <div className="rounded-lg bg-[#0066CC]/[0.06] px-2.5 py-1.5 border border-[#0066CC]/20">
                            <span className="text-[#0058B3] font-medium">Projected PO:</span>{' '}
                            <span className="font-semibold text-[#1D1D1F]">
                              {projectedOrderQty} units
                            </span>
                            {primarySupName && (
                              <span className="text-[#1D1D1F]"> via {primarySupName}</span>
                            )}
                            <SupplierTermSuffix
                              unitPrice={primaryUnitPrice}
                              effectiveLt={primaryEffLt}
                              moq={primaryMoq}
                              reliability={primaryReliability}
                            />
                          </div>
                        )}
                        {!willTriggerPo &&
                          shortfallQty !== null &&
                          shortfallQty > 0 &&
                          primarySupName && (
                            <div className="rounded-lg bg-white px-2.5 py-1.5 border border-black/[0.07]">
                              <span className="text-[#6E6E73]">Primary Supplier:</span>{' '}
                              <span className="font-semibold text-[#1D1D1F]">{primarySupName}</span>
                              <SupplierTermSuffix
                                unitPrice={primaryUnitPrice}
                                effectiveLt={primaryEffLt}
                                moq={primaryMoq}
                              />
                            </div>
                          )}
                        {openPos !== null && (
                          <div className="rounded-lg bg-white px-2.5 py-1.5 border border-black/[0.07] flex flex-wrap items-center gap-1.5">
                            <span className="text-[#6E6E73]">Active POs:</span>
                            {openPos.length === 0 ? (
                              <span className="font-medium text-[#86868B]">None</span>
                            ) : (
                              openPos.map((po, idx) =>
                                po.po_id !== null ? (
                                  <button
                                    key={`${po.po_number}-${idx}`}
                                    type="button"
                                    onClick={() => onNavigatePo(po.po_id!)}
                                    className="apple-press inline-flex items-center gap-1 rounded-md border border-black/[0.08] bg-[#FBFBFD] px-2 py-0.5 font-semibold text-[#0066CC] hover:bg-[#0066CC]/[0.06]"
                                  >
                                    {po.po_number} · {po.qty}u · {po.status} · ETA{' '}
                                    {po.eta ?? 'TBD'}
                                  </button>
                                ) : (
                                  <span
                                    key={`${po.po_number}-${idx}`}
                                    className="rounded-md border border-black/[0.08] bg-[#FBFBFD] px-2 py-0.5 font-semibold text-[#1D1D1F]"
                                  >
                                    {po.po_number} · {po.qty}u · {po.status} · ETA{' '}
                                    {po.eta ?? 'TBD'}
                                  </span>
                                ),
                              )
                            )}
                          </div>
                        )}
                      </>
                    )}
                    {daysOfSupply !== null && (
                      <div className="rounded-lg bg-white px-2.5 py-1.5 border border-black/[0.07]">
                        <span className="text-[#6E6E73]">Projected Supply:</span>{' '}
                        <span className="font-semibold text-[#1D1D1F]">
                          {daysOfSupply.toFixed(1)}d
                        </span>
                        {stockoutDate && (
                          <span className="text-[#B82923] font-medium ml-1">
                            (Stockout {stockoutDate})
                          </span>
                        )}
                      </div>
                    )}
                    {daysToNewEta !== null && (
                      <div className="rounded-lg bg-white px-2.5 py-1.5 border border-black/[0.07]">
                        <span className="text-[#6E6E73]">Days to New ETA:</span>{' '}
                        <span className="font-semibold text-[#1D1D1F]">
                          {daysToNewEta.toFixed(1)}d
                        </span>
                      </div>
                    )}
                    {proposedPrice !== null && (
                      <div className="rounded-lg bg-white px-2.5 py-1.5 border border-black/[0.07]">
                        <span className="text-[#6E6E73]">
                          {currencyMismatch ? 'Foreign Quote:' : 'Proposed Price:'}
                        </span>{' '}
                        <span className="font-semibold text-[#1D1D1F]">
                          {currencyMismatch && proposedCurrency
                            ? `${proposedPrice.toFixed(2)} ${proposedCurrency} (vs PO ${poCurrency})`
                            : formatInr(proposedPrice)}
                        </span>
                        {!currencyMismatch && priceVarInr !== null && (
                          <span className="ml-1 font-medium text-[#945600]">
                            ({priceVarInr >= 0 ? '+' : ''}
                            {formatInr(priceVarInr)}
                            {priceVarPct !== null ? `/${priceVarPct.toFixed(1)}%` : ''})
                          </span>
                        )}
                      </div>
                    )}
                    {!currencyMismatch &&
                      priceOnlyImpact !== null &&
                      totalCostImpact !== null &&
                      Math.abs(priceOnlyImpact - totalCostImpact) > 0.01 && (
                        <div className="rounded-lg bg-white px-2.5 py-1.5 border border-black/[0.07]">
                          <span className="text-[#6E6E73]">Price-Only Impact:</span>{' '}
                          <span className="font-semibold text-[#945600]">
                            {priceOnlyImpact >= 0 ? '+' : ''}
                            {formatInr(priceOnlyImpact)}
                          </span>
                        </div>
                      )}
                    {!currencyMismatch && totalCostImpact !== null && (
                      <div className="rounded-lg bg-white px-2.5 py-1.5 border border-black/[0.07]">
                        <span className="text-[#6E6E73]">Total Spend Impact:</span>{' '}
                        <span className="font-semibold text-[#B82923]">
                          {totalCostImpact >= 0 ? '+' : ''}
                          {formatInr(totalCostImpact)}
                        </span>
                      </div>
                    )}
                    {heldQty !== null && (
                      <div className="rounded-lg bg-[#C9342D]/[0.06] px-2.5 py-1.5 border border-[#C9342D]/20">
                        <span className="text-[#B82923]">Quarantined (Damaged Hold):</span>{' '}
                        <span className="font-bold text-[#B82923]">{heldQty} units</span>
                      </div>
                    )}
                    {invoiceAmount !== null && creditNoteAmount !== null && netAmount !== null && (
                      <div className="rounded-lg bg-white px-2.5 py-1.5 border border-black/[0.07]">
                        <span className="text-[#6E6E73]">Net Settlement:</span>{' '}
                        <span className="font-semibold text-[#1D1D1F]">
                          {formatInr(invoiceAmount)} {creditNoteAmount < 0 ? '-' : '+'}{' '}
                          {formatInr(Math.abs(creditNoteAmount))} = {formatInr(netAmount)}
                        </span>
                      </div>
                    )}
                    {packingListQty !== null && (
                      <div className="rounded-lg bg-white px-2.5 py-1.5 border border-black/[0.07]">
                        <span className="text-[#6E6E73]">Packing List Qty:</span>{' '}
                        <span className="font-semibold text-[#945600]">{packingListQty}</span>
                      </div>
                    )}
                    {proposedQty !== null && (
                      <div className="rounded-lg bg-white px-2.5 py-1.5 border border-black/[0.07]">
                        <span className="text-[#6E6E73]">Proposed Qty:</span>{' '}
                        <span className="font-semibold text-[#1D1D1F]">{proposedQty}</span>
                      </div>
                    )}
                    {proposedEta && (
                      <div className="rounded-lg bg-white px-2.5 py-1.5 border border-black/[0.07]">
                        <span className="text-[#6E6E73]">Proposed ETA:</span>{' '}
                        <span className="font-semibold text-[#1D1D1F]">{proposedEta}</span>
                      </div>
                    )}
                    {backupName && (
                      <div className="rounded-lg bg-white px-2.5 py-1.5 border border-black/[0.07]">
                        <span className="text-[#6E6E73]">Backup Option:</span>{' '}
                        <span className="font-semibold text-[#1D1D1F]">{backupName}</span>
                        {backupPrice !== null && ` @ ${formatInr(backupPrice)}`}
                        {backupLt !== null && ` (${backupLt}d LT)`}
                      </div>
                    )}
                    {!isStockRequestAlert && recRop !== null && recS !== null && (
                      <div className="rounded-lg bg-white px-2.5 py-1.5 border border-black/[0.07]">
                        <span className="text-[#6E6E73]">Recommended ROP/T:</span>{' '}
                        <span className="font-semibold text-[#0066CC]">
                          {recRop}/{recS}
                        </span>
                      </div>
                    )}
                  </div>
                )}

                {draft && (
                  <div className="mt-4 rounded-xl border border-[#5E5CE6]/22 bg-[#5E5CE6]/[0.04] p-4 space-y-2.5">
                    <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
                      <div className="flex items-center gap-2">
                        <Sparkles className="h-3.5 w-3.5 text-[#5E5CE6]" />
                        <span className="font-semibold text-[#1D1D1F]">
                          Lex Draft Preview
                        </span>
                        <span className="rounded-full bg-[#5E5CE6]/12 px-2.5 py-0.5 text-[11px] font-semibold text-[#4B48C4]">
                          {draft.category}
                        </span>
                        <span className="rounded-full bg-white px-2.5 py-0.5 border border-[#5E5CE6]/20 text-[11px] text-[#6E6E73]">
                          {draft.status}
                        </span>
                        {draftModified && (
                          <span className="rounded-full bg-[#B46900]/12 px-2.5 py-0.5 text-[11px] font-semibold text-[#945600]">
                            Edited
                          </span>
                        )}
                      </div>
                      <div className="flex items-center gap-1.5 text-[#6E6E73]">
                        <Mail className="h-3.5 w-3.5" />
                        <span>
                          To: {draft.to_personas.map(personaLabel).join(', ')}
                          {draft.cc_personas.length > 0
                            ? ` · CC: ${draft.cc_personas.map(personaLabel).join(', ')}`
                            : ''}
                        </span>
                      </div>
                    </div>

                    {item.status === 'OPEN' && draft.status === 'DRAFT_PENDING_APPROVAL' ? (
                      <div className="space-y-2">
                        <input
                          type="text"
                          value={edit.subject}
                          onChange={(e) =>
                            setDraftEdits((prev) => ({
                              ...prev,
                              [item.id]: { ...edit, subject: e.target.value },
                            }))
                          }
                          aria-label="Draft subject"
                          className="w-full rounded-xl border border-[#5E5CE6]/25 bg-white px-3 py-1.5 text-sm font-medium text-[#1D1D1F] focus:border-[#5E5CE6] focus:outline-none"
                        />
                        <textarea
                          rows={4}
                          value={edit.body}
                          onChange={(e) =>
                            setDraftEdits((prev) => ({
                              ...prev,
                              [item.id]: { ...edit, body: e.target.value },
                            }))
                          }
                          aria-label="Draft body"
                          className="w-full rounded-xl border border-[#5E5CE6]/25 bg-white px-3 py-2 text-sm text-[#1D1D1F] focus:border-[#5E5CE6] focus:outline-none"
                        />
                      </div>
                    ) : (
                      <div className="rounded-xl border border-[#5E5CE6]/15 bg-white p-3 text-xs space-y-1.5">
                        <p className="font-semibold text-[#1D1D1F]">{draft.subject}</p>
                        <FormattedEmailBody body={draft.body} className="text-[#3A3A3C]" />
                      </div>
                    )}
                  </div>
                )}

                {item.status === 'OPEN' && (
                  <div className="mt-4 flex flex-wrap items-center justify-between gap-3 pt-3 border-t border-black/[0.06]">
                    <div className="flex flex-wrap items-center gap-2 flex-1">
                      {hasReleaseDamagedCta && (
                        <input
                          type="number"
                          min={0}
                          max={heldQty ?? undefined}
                          placeholder={`Usable qty (default ${heldQty ?? 0})`}
                          value={goodQtyInputs[item.id] ?? ''}
                          onChange={(e) =>
                            setGoodQtyInputs((prev) => ({
                              ...prev,
                              [item.id]: e.target.value,
                            }))
                          }
                          aria-label="Usable quantity to release"
                          className="w-40 rounded-xl border border-black/[0.10] bg-[#FBFBFD] px-3 py-1.5 text-xs text-[#1D1D1F] tabular-nums focus:border-[#0066CC] focus:bg-white focus:outline-none"
                        />
                      )}

                      {hasAddNoteCta && (
                        <input
                          type="text"
                          placeholder="Optional decision note..."
                          value={noteInputs[item.id] ?? ''}
                          onChange={(e) =>
                            setNoteInputs((prev) => ({
                              ...prev,
                              [item.id]: e.target.value,
                            }))
                          }
                          className="min-w-56 flex-1 max-w-sm rounded-xl border border-black/[0.10] bg-[#FBFBFD] px-3 py-1.5 text-xs text-[#1D1D1F] focus:border-[#0066CC] focus:bg-white focus:outline-none"
                        />
                      )}

                      {item.suggested_actions
                        .filter(
                          (cta) =>
                            !(item.type === 'PO_DISPATCH_APPROVAL' && cta.action === 'reject_draft'),
                        )
                        .map((cta) => {
                        const isApprove =
                          cta.action === 'approve_draft' ||
                          cta.action === 'approve_rop' ||
                          cta.action === 'update_po' ||
                          cta.action === 'approve_stock_withdrawal' ||
                          cta.action === 'ask_making_minimum' ||
                          cta.action === 'release_damaged_goods' ||
                          cta.action === 'approve_net_settlement' ||
                          cta.action === 'request_inr_quote';
                        const isRejectOrCancel =
                          cta.action === 'reject_draft' ||
                          cta.action === 'cancel_po' ||
                          cta.action === 'reject_stock_request';
                        const isSwitchOrPartial =
                          cta.action === 'switch_backup_supplier' ||
                          cta.action === 'issue_partial_stock' ||
                          cta.action === 'draft_damage_claim' ||
                          cta.action === 'escalate_procurement';
                        const isEditPo = cta.action === 'edit_po';
                        const draftUnavailable =
                          cta.action === 'ask_making_minimum' &&
                          draft !== null &&
                          draft.status !== 'DRAFT_PENDING_APPROVAL';

                        const btnClass = isApprove
                          ? 'bg-[#0066CC] text-white hover:bg-[#0071E3] border-[#0066CC] shadow-2xs'
                          : isSwitchOrPartial
                            ? 'bg-[#B46900] text-white hover:bg-[#945600] border-[#B46900]'
                            : isRejectOrCancel
                              ? 'bg-white text-[#B82923] border-[#C9342D]/25 hover:bg-[#C9342D]/[0.07]'
                              : isEditPo
                                ? 'bg-white text-[#0058B3] border-[#0066CC]/30 hover:bg-[#0066CC]/[0.07]'
                                : 'bg-white text-[#1D1D1F] border-black/[0.10] hover:bg-[#FBFBFD]';

                        const buttonLabel = draftUnavailable
                          ? draft.status === 'SENT'
                            ? 'Min-Qty Inquiry Sent'
                            : 'Draft Rejected'
                          : cta.label;

                        const handleClick = () => {
                          if (isEditPo) {
                            const targetPoId = asNumber(payload.po_id) ?? item.po_id;
                            if (targetPoId === null) {
                              onNotify(
                                'error',
                                'Missing PO reference',
                                `Alert #${item.id} has no linked PO to edit.`,
                              );
                              return;
                            }
                            setEditPoModal({
                              poId: targetPoId,
                              subject: draft?.status === 'DRAFT_PENDING_APPROVAL' ? edit.subject : undefined,
                              body: draft?.status === 'DRAFT_PENDING_APPROVAL' ? edit.body : undefined,
                            });
                            return;
                          }
                          void handleExecuteCta(item, cta.action, cta.label);
                        };

                        return (
                          <button
                            key={cta.action}
                            type="button"
                            disabled={isBusy || draftUnavailable}
                            onClick={handleClick}
                            className={`apple-press inline-flex items-center gap-1.5 rounded-xl border px-3.5 py-1.5 text-xs font-semibold disabled:opacity-45 ${btnClass}`}
                          >
                            {buttonLabel}
                          </button>
                        );
                      })}
                    </div>

                    <button
                      type="button"
                      disabled={isBusy}
                      onClick={() => void handleDismiss(item)}
                      className="apple-press inline-flex items-center gap-1 rounded-xl border border-black/[0.08] px-3 py-1.5 text-xs font-medium text-[#6E6E73] hover:bg-black/[0.04] hover:text-[#1D1D1F] disabled:opacity-45"
                    >
                      <X className="h-3.5 w-3.5" />
                      Dismiss
                    </button>
                  </div>
                )}
              </article>
            );
          })}
        </div>
      )}

      {editPoModal && (
        <CreateEditPOModal
          mode="edit"
          poId={editPoModal.poId}
          initialDraftSubject={editPoModal.subject}
          initialDraftBody={editPoModal.body}
          onClose={() => setEditPoModal(null)}
          onNotify={onNotify}
          onSuccess={async () => {
            onMutated();
            await loadAlerts();
          }}
        />
      )}
    </div>
  );
}
