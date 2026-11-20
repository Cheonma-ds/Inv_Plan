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
          if (item.draft_preview && !next[item.id]) {
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
      if (action === 'approve_draft' && item.draft_preview) {
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
      }

      const res = await api.executeAlertCta(item.id, action, params);
      const detail =
        typeof res.new_po_number === 'string'
          ? `Created backup PO ${res.new_po_number}`
          : typeof res.approval_outcome === 'string'
            ? `Draft ${res.approval_outcome}`
            : undefined;
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
          <h1 className="text-xl font-bold text-slate-900">Actionable Alerts</h1>
          <p className="text-sm text-slate-500">
            Urgency-ranked exceptions, Lex draft approvals, and 1-click operational CTAs.
          </p>
        </div>

        <div className="flex items-center gap-2">
          <div className="inline-flex rounded-lg border border-slate-200 bg-white p-0.5">
            {(['OPEN', 'RESOLVED', 'DISMISSED', 'ALL'] as const).map((tab) => (
              <button
                key={tab}
                type="button"
                onClick={() => setFilter(tab)}
                className={`rounded-md px-3 py-1.5 text-xs font-semibold transition ${
                  filter === tab
                    ? 'bg-slate-900 text-white'
                    : 'text-slate-600 hover:bg-slate-100'
                }`}
              >
                {tab}
              </button>
            ))}
          </div>

          <button
            type="button"
            onClick={() => void loadAlerts()}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>
      </div>

      {loading && alerts.length === 0 ? (
        <div className="rounded-xl border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">
          Loading alerts...
        </div>
      ) : alerts.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-300 bg-white p-10 text-center">
          <Check className="mx-auto h-8 w-8 text-emerald-500" />
          <h2 className="mt-2 text-sm font-semibold text-slate-900">
            No {filter.toLowerCase()} alerts
          </h2>
          <p className="mt-1 text-xs text-slate-500">
            Advance the simulated clock, withdraw stock, or trigger a supplier email to see alerts here.
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          {alerts.map((item) => {
            const payload = item.cta_payload ?? {};
            const isBusy = busyAlertId === item.id;
            const draft = item.draft_preview;
            const edit = draftEdits[item.id] ?? {
              subject: draft?.subject ?? '',
              body: draft?.body ?? '',
            };
            const draftModified =
              draft !== null &&
              (edit.subject.trim() !== draft.subject.trim() ||
                edit.body.trim() !== draft.body.trim());
            const hasAddNoteCta = item.suggested_actions.some((a) => a.action === 'add_note');

            const daysOfSupply = asNumber(payload.projected_days_of_supply);
            const stockoutDate = asString(payload.projected_stockout_date);
            const daysToNewEta = asNumber(payload.days_to_new_eta);
            const priceVarInr = asNumber(payload.price_variance_inr);
            const priceVarPct = asNumber(payload.price_variance_pct);
            const totalCostImpact = asNumber(payload.total_cost_impact_inr);
            const proposedPrice = asNumber(payload.proposed_unit_price);
            const proposedQty = asNumber(payload.proposed_qty);
            const proposedEta = asString(payload.proposed_eta_date);
            const backupName = asString(payload.backup_supplier_name);
            const backupPrice = asNumber(payload.backup_unit_price);
            const backupLt = asNumber(payload.backup_lead_time_days);
            const recRop = asNumber(payload.rop);
            const recS = asNumber(payload.replenishment_point);

            const hasImpactMetrics =
              daysOfSupply !== null ||
              priceVarInr !== null ||
              proposedPrice !== null ||
              proposedQty !== null ||
              proposedEta !== null ||
              backupName !== null ||
              recRop !== null;

            return (
              <article
                key={item.id}
                className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm transition hover:border-slate-300"
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="space-y-1.5">
                    <div className="flex flex-wrap items-center gap-2">
                      <span
                        className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-bold ${severityBadgeClass(
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
                      <span className="inline-flex items-center rounded-md bg-slate-900 px-2 py-0.5 text-xs font-semibold text-white tabular-nums">
                        Urgency {item.urgency_score}
                      </span>
                      <span className="rounded-md bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-700">
                        {item.type}
                      </span>
                      {item.status !== 'OPEN' && (
                        <span className="rounded-md bg-emerald-50 border border-emerald-200 px-2 py-0.5 text-xs font-semibold text-emerald-700">
                          {item.status}
                        </span>
                      )}
                    </div>

                    <h2 className="text-base font-semibold text-slate-900">{item.title}</h2>
                    <p className="text-sm text-slate-700 leading-relaxed">{item.summary}</p>
                  </div>

                  <div className="flex flex-col items-end gap-1.5 text-xs text-slate-500">
                    <span>{formatSimDateTime(item.created_at)}</span>
                    <div className="flex items-center gap-2">
                      {item.po_id && (
                        <button
                          type="button"
                          onClick={() => onNavigatePo(item.po_id!)}
                          className="inline-flex items-center gap-1 rounded border border-slate-200 bg-slate-50 px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-100"
                        >
                          PO #{item.po_id}
                          <ExternalLink className="h-3 w-3" />
                        </button>
                      )}
                      {item.part_id && (
                        <button
                          type="button"
                          onClick={() => onNavigatePart(item.part_id!)}
                          className="inline-flex items-center gap-1 rounded border border-slate-200 bg-slate-50 px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-100"
                        >
                          Part #{item.part_id}
                          <ExternalLink className="h-3 w-3" />
                        </button>
                      )}
                    </div>
                  </div>
                </div>

                {hasImpactMetrics && (
                  <div className="mt-3 flex flex-wrap gap-2 rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs">
                    {daysOfSupply !== null && (
                      <div className="rounded bg-white px-2.5 py-1 border border-slate-200">
                        <span className="text-slate-500">Projected Supply:</span>{' '}
                        <span className="font-semibold text-slate-900">
                          {daysOfSupply.toFixed(1)}d
                        </span>
                        {stockoutDate && (
                          <span className="text-rose-700 font-medium ml-1">
                            (Stockout {stockoutDate})
                          </span>
                        )}
                      </div>
                    )}
                    {daysToNewEta !== null && (
                      <div className="rounded bg-white px-2.5 py-1 border border-slate-200">
                        <span className="text-slate-500">Days to New ETA:</span>{' '}
                        <span className="font-semibold text-slate-900">
                          {daysToNewEta.toFixed(1)}d
                        </span>
                      </div>
                    )}
                    {proposedPrice !== null && (
                      <div className="rounded bg-white px-2.5 py-1 border border-slate-200">
                        <span className="text-slate-500">Proposed Price:</span>{' '}
                        <span className="font-semibold text-slate-900">
                          {formatInr(proposedPrice)}
                        </span>
                        {priceVarInr !== null && (
                          <span className="ml-1 font-medium text-amber-700">
                            ({priceVarInr >= 0 ? '+' : ''}
                            {formatInr(priceVarInr)}
                            {priceVarPct !== null ? ` / ${priceVarPct.toFixed(1)}%` : ''})
                          </span>
                        )}
                      </div>
                    )}
                    {totalCostImpact !== null && (
                      <div className="rounded bg-white px-2.5 py-1 border border-slate-200">
                        <span className="text-slate-500">Total Cost Impact:</span>{' '}
                        <span className="font-semibold text-rose-700">
                          {formatInr(totalCostImpact)}
                        </span>
                      </div>
                    )}
                    {proposedQty !== null && (
                      <div className="rounded bg-white px-2.5 py-1 border border-slate-200">
                        <span className="text-slate-500">Proposed Qty:</span>{' '}
                        <span className="font-semibold text-slate-900">{proposedQty}</span>
                      </div>
                    )}
                    {proposedEta && (
                      <div className="rounded bg-white px-2.5 py-1 border border-slate-200">
                        <span className="text-slate-500">Proposed ETA:</span>{' '}
                        <span className="font-semibold text-slate-900">{proposedEta}</span>
                      </div>
                    )}
                    {backupName && (
                      <div className="rounded bg-white px-2.5 py-1 border border-slate-200">
                        <span className="text-slate-500">Backup Option:</span>{' '}
                        <span className="font-semibold text-slate-900">{backupName}</span>
                        {backupPrice !== null && ` @ ${formatInr(backupPrice)}`}
                        {backupLt !== null && ` (${backupLt}d LT)`}
                      </div>
                    )}
                    {recRop !== null && recS !== null && (
                      <div className="rounded bg-white px-2.5 py-1 border border-slate-200">
                        <span className="text-slate-500">Recommended ROP / S:</span>{' '}
                        <span className="font-semibold text-indigo-700">
                          {recRop} / {recS}
                        </span>
                      </div>
                    )}
                  </div>
                )}

                {draft && (
                  <div className="mt-4 rounded-lg border border-indigo-200 bg-indigo-50/40 p-4 space-y-2.5">
                    <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
                      <div className="flex items-center gap-2">
                        <Sparkles className="h-3.5 w-3.5 text-indigo-600" />
                        <span className="font-semibold text-indigo-950">
                          Lex Draft Preview
                        </span>
                        <span className="rounded bg-indigo-100 px-2 py-0.5 font-medium text-indigo-800">
                          {draft.category}
                        </span>
                        <span className="rounded bg-white px-2 py-0.5 border border-indigo-200 text-slate-700">
                          {draft.status}
                        </span>
                        {draftModified && (
                          <span className="rounded bg-amber-100 px-2 py-0.5 font-semibold text-amber-800">
                            Edited
                          </span>
                        )}
                      </div>
                      <div className="flex items-center gap-1.5 text-slate-600">
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
                          className="w-full rounded-md border border-indigo-200 bg-white px-3 py-1.5 text-sm font-medium text-slate-900 focus:border-indigo-500 focus:outline-none"
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
                          className="w-full rounded-md border border-indigo-200 bg-white px-3 py-2 text-sm text-slate-800 focus:border-indigo-500 focus:outline-none"
                        />
                      </div>
                    ) : (
                      <div className="rounded-md border border-indigo-100 bg-white p-3 text-xs space-y-1">
                        <p className="font-semibold text-slate-900">{draft.subject}</p>
                        <p className="whitespace-pre-wrap text-slate-700">{draft.body}</p>
                      </div>
                    )}
                  </div>
                )}

                {item.status === 'OPEN' && (
                  <div className="mt-4 flex flex-wrap items-center justify-between gap-3 pt-3 border-t border-slate-100">
                    <div className="flex flex-wrap items-center gap-2 flex-1">
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
                          className="min-w-56 flex-1 max-w-sm rounded-lg border border-slate-200 px-3 py-1.5 text-xs text-slate-800 focus:border-slate-400 focus:outline-none"
                        />
                      )}

                      {item.suggested_actions.map((cta) => {
                        const isApprove =
                          cta.action === 'approve_draft' ||
                          cta.action === 'approve_rop' ||
                          cta.action === 'update_po';
                        const isRejectOrCancel =
                          cta.action === 'reject_draft' || cta.action === 'cancel_po';
                        const isSwitch = cta.action === 'switch_backup_supplier';

                        const btnClass = isApprove
                          ? 'bg-slate-900 text-white hover:bg-slate-800 border-slate-900'
                          : isSwitch
                            ? 'bg-amber-600 text-white hover:bg-amber-700 border-amber-600'
                            : isRejectOrCancel
                              ? 'bg-white text-rose-700 border-rose-200 hover:bg-rose-50'
                              : 'bg-white text-slate-800 border-slate-300 hover:bg-slate-50';

                        return (
                          <button
                            key={cta.action}
                            type="button"
                            disabled={isBusy}
                            onClick={() => void handleExecuteCta(item, cta.action, cta.label)}
                            className={`inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-semibold transition disabled:opacity-50 ${btnClass}`}
                          >
                            {cta.label}
                          </button>
                        );
                      })}
                    </div>

                    <button
                      type="button"
                      disabled={isBusy}
                      onClick={() => void handleDismiss(item)}
                      className="inline-flex items-center gap-1 rounded-lg border border-slate-200 px-2.5 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-100 disabled:opacity-50"
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
    </div>
  );
}
