import { useCallback, useEffect, useState } from 'react';
import {
  Award,
  Calculator,
  Check,
  Edit2,
  Plus,
  RefreshCw,
  Trash2,
  TrendingUp,
} from 'lucide-react';
import { api } from '../api/client';
import type {
  PlanningOverview,
  ROPRecommendation,
  SupplierItem,
} from '../api/types';
import {
  criticalityBadgeClass,
  formatPct,
  formatSimDateTime,
} from '../utils/format';

interface SuppliersPlanningPageProps {
  refreshNonce: number;
  onMutated: () => void;
  onNotify: (kind: 'success' | 'error' | 'info', title: string, detail?: string) => void;
}

export function SuppliersPlanningPage({
  refreshNonce,
  onMutated,
  onNotify,
}: SuppliersPlanningPageProps) {
  const [suppliers, setSuppliers] = useState<SupplierItem[]>([]);
  const [overview, setOverview] = useState<PlanningOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [recalculating, setRecalculating] = useState(false);
  const [applyingPartId, setApplyingPartId] = useState<number | null>(null);
  const [showOnlyUpdates, setShowOnlyUpdates] = useState(false);

  // Memory note inputs
  const [newNotes, setNewNotes] = useState<Record<number, string>>({});
  const [editingNote, setEditingNote] = useState<{
    supplierId: number;
    noteId: number;
    text: string;
  } | null>(null);
  const [busySupplierId, setBusySupplierId] = useState<number | null>(null);

  const loadData = useCallback(async () => {
    setLoading(true);
    try {
      const [supData, planData] = await Promise.all([
        api.listSuppliers(),
        api.getPlanningOverview(),
      ]);
      setSuppliers(supData);
      setOverview(planData);
    } catch (err) {
      onNotify(
        'error',
        'Failed to load suppliers & planning',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setLoading(false);
    }
  }, [onNotify]);

  useEffect(() => {
    void loadData();
  }, [loadData, refreshNonce]);

  const handleRecalculate = async () => {
    setRecalculating(true);
    try {
      const res = await api.recalculatePlanning();
      setOverview(res);
      const refreshedSuppliers = await api.listSuppliers();
      setSuppliers(refreshedSuppliers);
      onNotify(
        'success',
        'Planning recalculated',
        `${res.recommendations_needing_update} ROP/S update(s) flagged · Alerts: +${res.alerts_created} created, ${res.alerts_updated} updated, ${res.alerts_resolved} resolved.`,
      );
      onMutated();
    } catch (err) {
      onNotify(
        'error',
        'Planning recalculation failed',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setRecalculating(false);
    }
  };

  const handleApplyRecommendation = async (rec: ROPRecommendation) => {
    setApplyingPartId(rec.part_id);
    try {
      await api.applyPlanningRecommendation(
        rec.part_id,
        rec.recommended_rop,
        rec.recommended_replenishment_point,
      );
      onNotify(
        'success',
        `Applied ROP/S for ${rec.sku}`,
        `ROP=${rec.recommended_rop}, S=${rec.recommended_replenishment_point}`,
      );
      onMutated();
      await loadData();
    } catch (err) {
      onNotify(
        'error',
        `Failed to apply ROP/S for ${rec.sku}`,
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setApplyingPartId(null);
    }
  };

  const handleAddNote = async (supplierId: number) => {
    const text = (newNotes[supplierId] ?? '').trim();
    if (!text) return;
    setBusySupplierId(supplierId);
    try {
      await api.addSupplierMemory(supplierId, text);
      setNewNotes((prev) => ({ ...prev, [supplierId]: '' }));
      onNotify('success', 'Supplier memory note added');
      await loadData();
    } catch (err) {
      onNotify(
        'error',
        'Failed to add memory note',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setBusySupplierId(null);
    }
  };

  const handleSaveEditNote = async () => {
    if (!editingNote || !editingNote.text.trim()) return;
    setBusySupplierId(editingNote.supplierId);
    try {
      await api.updateSupplierMemory(
        editingNote.supplierId,
        editingNote.noteId,
        editingNote.text.trim(),
      );
      setEditingNote(null);
      onNotify('success', 'Supplier memory note updated');
      await loadData();
    } catch (err) {
      onNotify(
        'error',
        'Failed to update memory note',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setBusySupplierId(null);
    }
  };

  const handleDeleteNote = async (supplierId: number, noteId: number) => {
    setBusySupplierId(supplierId);
    try {
      await api.deleteSupplierMemory(supplierId, noteId);
      onNotify('info', 'Supplier memory note deleted');
      await loadData();
    } catch (err) {
      onNotify(
        'error',
        'Failed to delete memory note',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setBusySupplierId(null);
    }
  };

  const recommendations = (overview?.recommendations ?? []).filter((r) =>
    showOnlyUpdates ? r.needs_update : true,
  );

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold text-slate-900">
            Suppliers &amp; Planning Intelligence
          </h1>
          <p className="text-sm text-slate-500">
            Rolling 12-month supplier reliability scorecards, memory notes, and deterministic EWMA + Z-factor ROP/S recommendations.
          </p>
        </div>

        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={recalculating}
            onClick={() => void handleRecalculate()}
            className="inline-flex items-center gap-1.5 rounded-lg bg-slate-900 px-3.5 py-2 text-xs font-semibold text-white hover:bg-slate-800 disabled:opacity-50"
          >
            <Calculator className={`h-3.5 w-3.5 ${recalculating ? 'animate-spin' : ''}`} />
            {recalculating ? 'Recalculating...' : 'Recalculate Planning'}
          </button>
          <button
            type="button"
            onClick={() => void loadData()}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>
      </div>

      {/* Supplier Scorecards */}
      <section className="space-y-3">
        <h2 className="text-sm font-bold uppercase tracking-wider text-slate-600 flex items-center gap-1.5">
          <Award className="h-4 w-4 text-amber-600" />
          Supplier Scorecards &amp; Memory
        </h2>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          {suppliers.map((sup) => {
            const scoreDetail = overview?.supplier_scores.find(
              (s) => s.supplier_id === sup.id,
            );
            const terms = (overview?.lead_time_updates ?? []).filter(
              (t) => t.supplier_id === sup.id && t.supplier_rank === 1,
            );
            const avgQuoted =
              terms.length > 0
                ? terms.reduce((a, b) => a + b.quoted_lead_time_days, 0) / terms.length
                : null;
            const avgEffective =
              terms.length > 0
                ? terms.reduce((a, b) => a + b.effective_lead_time_days, 0) / terms.length
                : null;
            const maxLt = Math.max(avgQuoted ?? 1, avgEffective ?? 1, 1);

            const scoreColor =
              sup.reliability_score >= 85
                ? 'text-emerald-700 bg-emerald-50 border-emerald-200'
                : sup.reliability_score >= 65
                  ? 'text-amber-700 bg-amber-50 border-amber-200'
                  : 'text-rose-700 bg-rose-50 border-rose-200';

            return (
              <div
                key={sup.id}
                className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm flex flex-col justify-between space-y-4"
              >
                <div className="space-y-3">
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <span className="text-[11px] font-mono uppercase text-slate-400">
                        {sup.persona_key}
                      </span>
                      <h3 className="text-base font-bold text-slate-900">{sup.name}</h3>
                      <p className="text-xs text-slate-500">
                        {sup.email} · {sup.phone}
                      </p>
                    </div>

                    <div className={`rounded-xl border px-3 py-1.5 text-center ${scoreColor}`}>
                      <div className="text-lg font-extrabold tabular-nums">
                        {sup.reliability_score.toFixed(1)}
                      </div>
                      <div className="text-[10px] font-semibold uppercase">Score</div>
                    </div>
                  </div>

                  {/* Core metrics */}
                  <div className="grid grid-cols-3 gap-2 text-center text-xs">
                    <div className="rounded-lg bg-slate-50 p-2 border border-slate-100">
                      <div className="text-slate-500 text-[11px]">OTD %</div>
                      <div className="font-bold text-slate-900">
                        {formatPct(sup.otd_rate)}
                      </div>
                    </div>
                    <div className="rounded-lg bg-slate-50 p-2 border border-slate-100">
                      <div className="text-slate-500 text-[11px]">Invoice Acc.</div>
                      <div className="font-bold text-slate-900">
                        {formatPct(sup.invoice_accuracy_rate)}
                      </div>
                    </div>
                    <div className="rounded-lg bg-slate-50 p-2 border border-slate-100">
                      <div className="text-slate-500 text-[11px]">Response</div>
                      <div className="font-bold text-slate-900">
                        {formatPct(sup.response_rate)}
                      </div>
                    </div>
                  </div>

                  {/* Quoted vs Actual/Effective Lead Time Bar */}
                  {avgQuoted !== null && avgEffective !== null && (
                    <div className="rounded-lg border border-slate-100 bg-slate-50 p-3 space-y-1.5 text-xs">
                      <div className="flex justify-between font-semibold text-slate-700">
                        <span>Primary SKU Lead Time (Avg)</span>
                        {scoreDetail && (
                          <span className="text-slate-500 font-normal">
                            {scoreDetail.closed_po_count} closed POs ·{' '}
                            {scoreDetail.avg_days_late.toFixed(1)}d avg late
                          </span>
                        )}
                      </div>
                      <div className="space-y-1">
                        <div className="flex items-center gap-2">
                          <span className="w-16 text-[11px] text-slate-500">Quoted</span>
                          <div className="flex-1 h-2 rounded-full bg-slate-200 overflow-hidden">
                            <div
                              className="h-full bg-sky-500 rounded-full"
                              style={{ width: `${Math.min(100, (avgQuoted / maxLt) * 100)}%` }}
                            />
                          </div>
                          <span className="w-12 text-right font-semibold tabular-nums">
                            {avgQuoted.toFixed(1)}d
                          </span>
                        </div>
                        <div className="flex items-center gap-2">
                          <span className="w-16 text-[11px] text-slate-500">Effective</span>
                          <div className="flex-1 h-2 rounded-full bg-slate-200 overflow-hidden">
                            <div
                              className={`h-full rounded-full ${
                                avgEffective > avgQuoted + 1
                                  ? 'bg-amber-500'
                                  : 'bg-emerald-500'
                              }`}
                              style={{
                                width: `${Math.min(100, (avgEffective / maxLt) * 100)}%`,
                              }}
                            />
                          </div>
                          <span className="w-12 text-right font-semibold tabular-nums">
                            {avgEffective.toFixed(1)}d
                          </span>
                        </div>
                      </div>
                    </div>
                  )}

                  {/* Score components */}
                  {scoreDetail && (
                    <div className="flex flex-wrap gap-1.5 text-[11px] text-slate-600">
                      <span className="rounded bg-slate-100 px-2 py-0.5">
                        OTD {scoreDetail.otd_component.toFixed(1)}/40
                      </span>
                      <span className="rounded bg-slate-100 px-2 py-0.5">
                        LT Cons. {scoreDetail.lead_time_consistency_component.toFixed(1)}/25
                      </span>
                      <span className="rounded bg-slate-100 px-2 py-0.5">
                        Inv. {scoreDetail.invoice_accuracy_component.toFixed(1)}/20
                      </span>
                      <span className="rounded bg-slate-100 px-2 py-0.5">
                        Resp. {scoreDetail.responsiveness_component.toFixed(1)}/15
                      </span>
                    </div>
                  )}
                </div>

                {/* Supplier Memory Notes */}
                <div className="border-t border-slate-200 pt-3 space-y-2">
                  <div className="text-xs font-bold text-slate-700">
                    Supplier Memory ({sup.memory_notes.length})
                  </div>

                  <div className="space-y-1.5 max-h-40 overflow-y-auto">
                    {sup.memory_notes.length === 0 ? (
                      <p className="text-xs text-slate-400">No memory notes yet.</p>
                    ) : (
                      sup.memory_notes.map((n) => {
                        const isEditing =
                          editingNote?.supplierId === sup.id &&
                          editingNote.noteId === n.id;

                        return (
                          <div
                            key={n.id}
                            className="rounded-lg border border-slate-200 bg-slate-50 p-2 text-xs"
                          >
                            {isEditing ? (
                              <div className="flex gap-1.5">
                                <input
                                  type="text"
                                  value={editingNote.text}
                                  onChange={(e) =>
                                    setEditingNote({
                                      ...editingNote,
                                      text: e.target.value,
                                    })
                                  }
                                  className="flex-1 rounded border border-slate-300 bg-white px-2 py-1 text-xs"
                                />
                                <button
                                  type="button"
                                  onClick={() => void handleSaveEditNote()}
                                  className="rounded bg-slate-900 px-2 py-1 text-white"
                                >
                                  <Check className="h-3 w-3" />
                                </button>
                              </div>
                            ) : (
                              <div className="flex items-start justify-between gap-2">
                                <div className="space-y-0.5">
                                  <p className="text-slate-800">{n.note}</p>
                                  <span className="inline-block rounded bg-white px-1.5 py-0.5 text-[10px] font-medium text-slate-500 border border-slate-200">
                                    {n.source}
                                  </span>
                                </div>
                                <div className="flex items-center gap-1 shrink-0">
                                  <button
                                    type="button"
                                    disabled={busySupplierId === sup.id}
                                    onClick={() =>
                                      setEditingNote({
                                        supplierId: sup.id,
                                        noteId: n.id,
                                        text: n.note,
                                      })
                                    }
                                    className="rounded p-1 text-slate-400 hover:bg-white hover:text-slate-700"
                                    title="Edit note"
                                  >
                                    <Edit2 className="h-3 w-3" />
                                  </button>
                                  <button
                                    type="button"
                                    disabled={busySupplierId === sup.id}
                                    onClick={() => void handleDeleteNote(sup.id, n.id)}
                                    className="rounded p-1 text-slate-400 hover:bg-white hover:text-rose-600"
                                    title="Delete note"
                                  >
                                    <Trash2 className="h-3 w-3" />
                                  </button>
                                </div>
                              </div>
                            )}
                          </div>
                        );
                      })
                    )}
                  </div>

                  <div className="flex gap-1.5">
                    <input
                      type="text"
                      placeholder="Add supplier memory note..."
                      value={newNotes[sup.id] ?? ''}
                      onChange={(e) =>
                        setNewNotes((prev) => ({ ...prev, [sup.id]: e.target.value }))
                      }
                      className="flex-1 rounded-lg border border-slate-200 px-2.5 py-1.5 text-xs focus:border-slate-400 focus:outline-none"
                    />
                    <button
                      type="button"
                      disabled={busySupplierId === sup.id || !(newNotes[sup.id] ?? '').trim()}
                      onClick={() => void handleAddNote(sup.id)}
                      className="inline-flex items-center gap-1 rounded-lg bg-slate-900 px-2.5 py-1.5 text-xs font-semibold text-white hover:bg-slate-800 disabled:opacity-40"
                    >
                      <Plus className="h-3.5 w-3.5" />
                      Add
                    </button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </section>

      {/* Dynamic ROP / S Recommendations */}
      <section className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="text-sm font-bold uppercase tracking-wider text-slate-600 flex items-center gap-1.5">
              <TrendingUp className="h-4 w-4 text-indigo-600" />
              Dynamic ROP &amp; Order-Up-To (S) Recommendations
            </h2>
            {overview && (
              <p className="text-xs text-slate-500">
                Evaluated {overview.parts_evaluated} parts at{' '}
                {formatSimDateTime(overview.calculated_at)} ·{' '}
                <strong>{overview.recommendations_needing_update}</strong> need update.
              </p>
            )}
          </div>

          <div className="inline-flex rounded-lg border border-slate-200 bg-white p-0.5 text-xs">
            <button
              type="button"
              onClick={() => setShowOnlyUpdates(false)}
              className={`rounded-md px-3 py-1 font-semibold ${
                !showOnlyUpdates ? 'bg-slate-900 text-white' : 'text-slate-600'
              }`}
            >
              All Parts ({overview?.recommendations.length ?? 0})
            </button>
            <button
              type="button"
              onClick={() => setShowOnlyUpdates(true)}
              className={`rounded-md px-3 py-1 font-semibold ${
                showOnlyUpdates ? 'bg-slate-900 text-white' : 'text-slate-600'
              }`}
            >
              Needs Update ({overview?.recommendations_needing_update ?? 0})
            </button>
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {recommendations.map((rec) => {
            const buckets = rec.forecast.monthly_buckets;
            const maxBucket = Math.max(...buckets, 1);

            return (
              <div
                key={rec.part_id}
                className={`rounded-xl border bg-white p-4 shadow-sm space-y-3 ${
                  rec.needs_update ? 'border-indigo-300' : 'border-slate-200'
                }`}
              >
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-sm font-bold text-slate-900">
                        {rec.sku}
                      </span>
                      <span
                        className={`rounded-full border px-2 py-0.5 text-[10px] font-bold ${criticalityBadgeClass(
                          rec.criticality,
                        )}`}
                      >
                        {rec.criticality}
                      </span>
                      {rec.is_intermittent && (
                        <span className="rounded bg-purple-100 px-2 py-0.5 text-[10px] font-bold text-purple-800">
                          Intermittent
                        </span>
                      )}
                      {rec.needs_update && (
                        <span className="rounded bg-indigo-100 px-2 py-0.5 text-[10px] font-bold text-indigo-800">
                          Update Suggested
                        </span>
                      )}
                    </div>
                    <div className="text-xs font-medium text-slate-600 mt-0.5">
                      {rec.part_name} · {rec.primary_supplier_name ?? 'No primary'}
                    </div>
                  </div>

                  {rec.needs_update && (
                    <button
                      type="button"
                      disabled={applyingPartId === rec.part_id}
                      onClick={() => void handleApplyRecommendation(rec)}
                      className="inline-flex items-center gap-1 rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-indigo-700 disabled:opacity-50"
                    >
                      <Check className="h-3.5 w-3.5" />
                      Apply ROP/S
                    </button>
                  )}
                </div>

                {/* Current vs Recommended */}
                <div className="grid grid-cols-2 gap-2 rounded-lg bg-slate-50 p-3 text-xs border border-slate-100">
                  <div>
                    <span className="text-slate-500">Current ROP / S:</span>{' '}
                    <span className="font-bold text-slate-800">
                      {rec.current_rop} / {rec.current_replenishment_point}
                    </span>
                  </div>
                  <div>
                    <span className="text-slate-500">Recommended ROP / S:</span>{' '}
                    <span
                      className={`font-bold ${
                        rec.needs_update ? 'text-indigo-700' : 'text-emerald-700'
                      }`}
                    >
                      {rec.recommended_rop} / {rec.recommended_replenishment_point}
                    </span>
                  </div>
                </div>

                <p className="text-xs text-slate-600">{rec.reason}</p>

                {/* Math Breakdown + 12-month sparkline bars */}
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-[11px] text-slate-600">
                  <div className="rounded border border-slate-100 px-2 py-1">
                    <div className="text-slate-400">Eff. Demand</div>
                    <div className="font-semibold text-slate-900">
                      {rec.effective_daily_demand.toFixed(2)}/d
                    </div>
                  </div>
                  <div className="rounded border border-slate-100 px-2 py-1">
                    <div className="text-slate-400">Eff. Lead Time</div>
                    <div className="font-semibold text-slate-900">
                      {rec.effective_lead_time_days.toFixed(1)}d (q{rec.quoted_lead_time_days}d)
                    </div>
                  </div>
                  <div className="rounded border border-slate-100 px-2 py-1">
                    <div className="text-slate-400">Safety Stock</div>
                    <div className="font-semibold text-slate-900">
                      {rec.safety_stock.toFixed(1)} (Z={rec.z_factor})
                    </div>
                  </div>
                  <div className="rounded border border-slate-100 px-2 py-1">
                    <div className="text-slate-400">LT / Demand σ</div>
                    <div className="font-semibold text-slate-900">
                      {rec.lead_time_std_dev.toFixed(2)}d / {rec.daily_demand_std.toFixed(2)}
                    </div>
                  </div>
                </div>

                <div>
                  <div className="text-[10px] font-semibold uppercase text-slate-400 mb-1">
                    12-Month Consumption Buckets (Oldest → Latest)
                  </div>
                  <div className="flex items-end gap-1 h-10">
                    {buckets.map((val, idx) => (
                      <div
                        key={idx}
                        title={`Month -${12 - idx}: ${val} units`}
                        className="flex-1 rounded-t bg-slate-300 hover:bg-indigo-500 transition"
                        style={{
                          height: `${Math.max(8, Math.round((val / maxBucket) * 100))}%`,
                        }}
                      />
                    ))}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </section>
    </div>
  );
}
