import { useCallback, useEffect, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  Activity,
  AlertTriangle,
  CalendarClock,
  ExternalLink,
  Package,
  ShoppingCart,
  Truck,
  X,
} from 'lucide-react';
import { api } from '../api/client';
import type {
  CycleAnalyticsResult,
  CycleEventMarker,
  POSuggestionResult,
} from '../api/types';
import {
  criticalityBadgeClass,
  formatBurnRate,
  formatInr,
  formatSimDate,
  formatSimDateTime,
  stockoutUrgencyBadgeClass,
} from '../utils/format';

interface StockoutCycleDrawerProps {
  partId: number | null;
  activePoId?: number | null;
  activePoNumber?: string | null;
  refreshNonce: number;
  onClose: () => void;
  onNavigatePo: (poId: number) => void;
  onCreatePo?: (partId: number) => void;
}

const CHART_WIDTH = 760;
const CHART_HEIGHT = 276;
const PAD_LEFT = 46;
const PAD_RIGHT = 20;
const PAD_TOP = 22;
const PAD_BOTTOM = 42;
const PLOT_W = CHART_WIDTH - PAD_LEFT - PAD_RIGHT;
const PLOT_H = CHART_HEIGHT - PAD_TOP - PAD_BOTTOM;

function formatTrendLabel(trendPct: number): string {
  if (Math.abs(trendPct) < 0.5) return '0%';
  const rounded = Math.round(Math.abs(trendPct));
  return trendPct > 0 ? `↑ ${rounded}%` : `↓ ${rounded}%`;
}

function eventBadgeClass(eventType: string): string {
  switch (eventType) {
    case 'STOCKOUT':
      return 'bg-[#C9342D]/10 text-[#B82923] border-[#C9342D]/25';
    case 'LOW_STOCK':
      return 'bg-[#D96B00]/10 text-[#A84E00] border-[#D96B00]/25';
    case 'PO_PLACED':
      return 'bg-[#0066CC]/10 text-[#0058B3] border-[#0066CC]/22';
    case 'REPLENISHMENT':
      return 'bg-[#1F7A4D]/10 text-[#1B6B43] border-[#1F7A4D]/22';
    default:
      return 'bg-black/[0.05] text-[#6E6E73] border-black/[0.10]';
  }
}

export function StockoutCycleDrawer({
  partId,
  activePoId,
  activePoNumber,
  refreshNonce,
  onClose,
  onNavigatePo,
  onCreatePo,
}: StockoutCycleDrawerProps) {
  const [data, setData] = useState<CycleAnalyticsResult | null>(null);
  const [poSuggestion, setPoSuggestion] = useState<POSuggestionResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadAnalytics = useCallback(async (id: number) => {
    setLoading(true);
    setError(null);
    try {
      const [analytics, suggestion] = await Promise.all([
        api.getPartCycleAnalytics(id),
        api.getPartPoSuggestion(id).catch(() => null),
      ]);
      setData(analytics);
      setPoSuggestion(suggestion ?? analytics.po_suggestion);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (partId === null) {
      setData(null);
      setPoSuggestion(null);
      setError(null);
      return;
    }
    void loadAnalytics(partId);
  }, [partId, refreshNonce, loadAnalytics]);

  useEffect(() => {
    if (partId === null) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [partId, onClose]);

  const chartModel = useMemo(() => {
    if (!data || data.history.length === 0) return null;

    const history = data.history;
    const projectedTail =
      data.projected.length > 0 &&
      data.projected[0].date === history[history.length - 1].date
        ? data.projected.slice(1)
        : data.projected;
    const totalPoints = history.length + projectedTail.length;
    const xDenom = Math.max(1, totalPoints - 1);
    const xAt = (idx: number) => PAD_LEFT + (idx / xDenom) * PLOT_W;

    const maxStockVal = Math.max(
      10,
      data.replenishment_point * 1.12,
      ...history.map((h) => h.closing_on_hand),
      ...data.projected.map((p) => p.projected_on_hand),
    );
    const yMax = Math.ceil(maxStockVal / 5) * 5;
    const yAt = (val: number) =>
      PAD_TOP + PLOT_H - (Math.max(0, Math.min(yMax, val)) / yMax) * PLOT_H;

    const maxCons = Math.max(1, ...history.map((h) => h.consumption));
    const maxBarH = 38;

    const historyPath = history
      .map((pt, i) => `${i === 0 ? 'M' : 'L'} ${xAt(i).toFixed(1)} ${yAt(pt.closing_on_hand).toFixed(1)}`)
      .join(' ');

    const todayIdx = history.length - 1;
    const todayX = xAt(todayIdx);
    const todayY = yAt(history[todayIdx].closing_on_hand);
    const areaPath = `${historyPath} L ${todayX.toFixed(1)} ${(PAD_TOP + PLOT_H).toFixed(1)} L ${xAt(0).toFixed(1)} ${(PAD_TOP + PLOT_H).toFixed(1)} Z`;

    const projectedPoints = [
      {
        x: todayX,
        y: todayY,
        date: history[todayIdx].date,
        val: history[todayIdx].closing_on_hand,
        note: null as string | null,
      },
      ...projectedTail.map((p, i) => ({
        x: xAt(history.length + i),
        y: yAt(p.projected_on_hand),
        date: p.date,
        val: p.projected_on_hand,
        note: p.event_note,
      })),
    ];
    const projectedPath =
      projectedPoints.length > 1
        ? projectedPoints
            .map((pt, i) => `${i === 0 ? 'M' : 'L'} ${pt.x.toFixed(1)} ${pt.y.toFixed(1)}`)
            .join(' ')
        : '';

    const indexByDate = new Map<string, number>();
    history.forEach((pt, idx) => indexByDate.set(pt.date, idx));

    const markers: Array<{
      event: CycleEventMarker;
      x: number;
      y: number;
    }> = [];
    for (const ev of data.events) {
      const idx = indexByDate.get(ev.date);
      if (idx === undefined) continue;
      const stockVal = ev.on_hand ?? history[idx].closing_on_hand;
      markers.push({
        event: ev,
        x: xAt(idx),
        y: yAt(stockVal),
      });
    }

    const startLabel = formatSimDate(history[0].date);
    const midIdx = Math.floor(history.length / 2);
    const midLabel = formatSimDate(history[midIdx].date);
    const todayLabel = formatSimDate(history[todayIdx].date);
    const endLabel =
      projectedTail.length > 0
        ? formatSimDate(projectedTail[projectedTail.length - 1].date)
        : null;

    return {
      history,
      projectedPoints,
      historyPath,
      areaPath,
      projectedPath,
      todayIdx,
      todayX,
      yMax,
      yRop: yAt(data.rop),
      yTarget: yAt(data.replenishment_point),
      xAt,
      yAt,
      maxCons,
      maxBarH,
      markers,
      startLabel,
      midLabel,
      todayLabel,
      endLabel,
    };
  }, [data]);

  if (partId === null || typeof document === 'undefined') {
    return null;
  }

  const suggestion = poSuggestion ?? data?.po_suggestion ?? null;
  const metrics = data?.metrics;
  const projStroke =
    metrics?.stockout_urgency === 'CRITICAL'
      ? '#C9342D'
      : metrics?.stockout_urgency === 'WARNING'
        ? '#B46900'
        : '#1F7A4D';

  return createPortal(
    <div
      onClick={onClose}
      className="anim-backdrop fixed inset-0 z-40 flex justify-end bg-black/28 backdrop-blur-xs"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={data ? `Stockout & Cycle Analytics — ${data.sku}` : 'Stockout & Cycle Analytics'}
        onClick={(e) => e.stopPropagation()}
        className="anim-drawer-slide h-full w-full max-w-3xl overflow-y-auto bg-white shadow-[var(--shadow-drawer)] border-l border-black/[0.08] flex flex-col"
      >
        {/* Sticky Header */}
        <div className="sticky top-0 z-10 flex items-center justify-between gap-3 border-b border-black/[0.08] bg-white/90 backdrop-blur-xl px-6 py-4">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <Package className="h-4 w-4 text-[#0066CC] shrink-0" />
              <h2 className="text-lg font-bold tracking-tight text-[#1D1D1F] font-mono">
                {data ? data.sku : `Part #${partId}`}
              </h2>
              {data && (
                <span
                  className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${criticalityBadgeClass(
                    data.criticality,
                  )}`}
                >
                  {data.criticality}
                </span>
              )}
              {metrics && (
                <span
                  className={`rounded-full border px-2.5 py-0.5 text-[11px] font-semibold tabular-nums ${stockoutUrgencyBadgeClass(
                    metrics.stockout_urgency,
                  )}`}
                >
                  {metrics.stockout_urgency} ·{' '}
                  {metrics.days_to_stockout !== null
                    ? `${metrics.days_to_stockout.toFixed(1)}d runway`
                    : 'No burn'}
                </span>
              )}
            </div>
            {data && (
              <p className="mt-0.5 text-xs text-[#6E6E73] truncate">{data.part_name}</p>
            )}
          </div>
          <button
            type="button"
            aria-label="Close stockout cycle drawer"
            onClick={onClose}
            className="apple-press rounded-full p-1.5 text-[#86868B] hover:bg-black/[0.05] hover:text-[#1D1D1F]"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {loading && !data ? (
          <div className="p-6 text-sm text-[#6E6E73]">Loading 90-day cycle analytics...</div>
        ) : error ? (
          <div className="p-6">
            <div className="rounded-xl border border-[#C9342D]/25 bg-[#C9342D]/[0.06] p-4 text-xs text-[#B82923]">
              Failed to load cycle analytics: {error}
            </div>
          </div>
        ) : data && metrics ? (
          <div className="p-6 space-y-5">
            {/* KPI Summary Row */}
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs tabular-nums">
              <div className="rounded-xl border border-black/[0.07] bg-[#FBFBFD] p-3">
                <div className="text-[#86868B]">On Hand / On Hold</div>
                <div className="mt-1 text-sm font-bold text-[#1D1D1F]">
                  {data.on_hand_stock}u{' '}
                  <span className="text-xs font-medium text-[#6E6E73]">
                    / {data.quarantined_stock}u hold
                  </span>
                </div>
                <div className="mt-0.5 text-[11px] text-[#6E6E73]">
                  On Order: {data.on_order_stock}u · IP: {data.inventory_position}u
                </div>
              </div>

              <div className="rounded-xl border border-black/[0.07] bg-[#FBFBFD] p-3">
                <div className="text-[#86868B]">Daily Burn (EWMA)</div>
                <div className="mt-1 flex items-baseline gap-1.5">
                  <span className="text-sm font-bold text-[#1D1D1F]">
                    {formatBurnRate(metrics.daily_burn_ewma)}
                  </span>
                  <span className="text-[11px] font-medium text-[#86868B]">
                    {formatTrendLabel(metrics.trend_pct)}
                  </span>
                </div>
                <div className="mt-0.5 text-[11px] text-[#6E6E73]">
                  α = {data.ewma_alpha.toFixed(2)} · σ_D = {metrics.daily_demand_std.toFixed(2)}
                </div>
              </div>

              <div className="rounded-xl border border-black/[0.07] bg-[#FBFBFD] p-3">
                <div className="text-[#86868B]">Predicted Stockout</div>
                <div className="mt-1 text-sm font-bold text-[#1D1D1F]">
                  {metrics.days_to_stockout !== null
                    ? `${metrics.days_to_stockout.toFixed(1)} days`
                    : '—'}
                </div>
                <div className="mt-0.5 text-[11px] text-[#6E6E73] truncate" title={formatSimDateTime(metrics.predicted_stockout_date)}>
                  {formatSimDateTime(metrics.predicted_stockout_date)}
                </div>
              </div>

              <div className="rounded-xl border border-black/[0.07] bg-[#FBFBFD] p-3">
                <div className="text-[#86868B]">Thresholds (ROP/T)</div>
                <div className="mt-1 text-sm font-bold text-[#1D1D1F]">
                  {data.rop} / {data.replenishment_point}
                </div>
                <div className="mt-0.5 text-[11px] text-[#6E6E73]">
                  SS: {metrics.safety_stock.toFixed(1)}u ({metrics.safety_days.toFixed(1)}d)
                </div>
              </div>
            </div>

            {/* EWMA α = 0.30 Methodology Note */}
            <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-[#0066CC]/18 bg-[#0066CC]/[0.04] px-3.5 py-2 text-[11px] text-[#1D1D1F]">
              <span className="flex items-center gap-1.5">
                <Activity className="h-3.5 w-3.5 text-[#0066CC] shrink-0" />
                <span>
                  <strong>Daily Burn uses EWMA (α = 0.30)</strong> over trailing 90 completed IST days. Same-day withdrawals update Stockout In immediately while EWMA settles at daily rollover.
                </span>
              </span>
              {metrics.is_intermittent && (
                <span className="rounded-full border border-[#B46900]/25 bg-[#B46900]/10 px-2 py-0.5 text-[10px] font-semibold text-[#945600]">
                  Intermittent Safety Floor Active
                </span>
              )}
            </div>

            {/* 90-Day Stock & Cycle SVG Chart */}
            <section className="rounded-2xl border border-black/[0.08] bg-white p-4 space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="text-xs font-bold uppercase tracking-wider text-[#1D1D1F]">
                  90-Day Stock Trajectory, Burn Bars &amp; Replenishment Cycles
                </h3>
                <span className="text-[11px] text-[#86868B] tabular-nums">
                  {data.events.length} cycle marker{data.events.length === 1 ? '' : 's'}
                </span>
              </div>

              {chartModel ? (
                <div className="w-full overflow-x-auto">
                  <svg
                    viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
                    className="w-full h-auto min-w-[540px]"
                    role="img"
                    aria-label={`90-day stockout and cycle chart for ${data.sku}`}
                  >
                    {/* Y-Axis Gridlines */}
                    {[0, 0.25, 0.5, 0.75, 1].map((frac) => {
                      const val = Math.round(chartModel.yMax * frac);
                      const y = chartModel.yAt(val);
                      return (
                        <g key={frac}>
                          <line
                            x1={PAD_LEFT}
                            y1={y}
                            x2={CHART_WIDTH - PAD_RIGHT}
                            y2={y}
                            stroke="rgba(0,0,0,0.06)"
                            strokeWidth={1}
                          />
                          <text
                            x={PAD_LEFT - 6}
                            y={y + 3}
                            textAnchor="end"
                            fontSize="10"
                            fill="#86868B"
                          >
                            {val}
                          </text>
                        </g>
                      );
                    })}

                    {/* Target (T) & ROP Dashed Lines */}
                    <line
                      x1={PAD_LEFT}
                      y1={chartModel.yTarget}
                      x2={CHART_WIDTH - PAD_RIGHT}
                      y2={chartModel.yTarget}
                      stroke="#5E5CE6"
                      strokeOpacity={0.65}
                      strokeWidth={1.25}
                      strokeDasharray="4 3"
                    />
                    <text
                      x={CHART_WIDTH - PAD_RIGHT - 2}
                      y={Math.max(PAD_TOP + 10, chartModel.yTarget - 4)}
                      textAnchor="end"
                      fontSize="10"
                      fontWeight="600"
                      fill="#4B48C4"
                    >
                      T = {data.replenishment_point}
                    </text>

                    <line
                      x1={PAD_LEFT}
                      y1={chartModel.yRop}
                      x2={CHART_WIDTH - PAD_RIGHT}
                      y2={chartModel.yRop}
                      stroke="#D96B00"
                      strokeOpacity={0.75}
                      strokeWidth={1.25}
                      strokeDasharray="4 3"
                    />
                    <text
                      x={CHART_WIDTH - PAD_RIGHT - 2}
                      y={Math.max(PAD_TOP + 10, chartModel.yRop - 4)}
                      textAnchor="end"
                      fontSize="10"
                      fontWeight="600"
                      fill="#A84E00"
                    >
                      ROP = {data.rop}
                    </text>

                    {/* Daily Consumption Bars along bottom */}
                    {chartModel.history.map((pt, idx) => {
                      if (pt.consumption <= 0) return null;
                      const barH = (pt.consumption / chartModel.maxCons) * chartModel.maxBarH;
                      const barW = Math.max(2, PLOT_W / (chartModel.history.length + 20) - 1);
                      const cx = chartModel.xAt(idx);
                      return (
                        <rect
                          key={`bar-${pt.date}`}
                          x={cx - barW / 2}
                          y={PAD_TOP + PLOT_H - barH}
                          width={barW}
                          height={barH}
                          rx={1}
                          fill="#86868B"
                          fillOpacity={0.22}
                        >
                          <title>
                            {formatSimDate(pt.date)}: Consumed {pt.consumption}u
                            {pt.receipt > 0 ? `, Received +${pt.receipt}u` : ''}
                            {` · Closing ${pt.closing_on_hand}u`}
                          </title>
                        </rect>
                      );
                    })}

                    {/* Historical Closing Stock Area + Line */}
                    <path d={chartModel.areaPath} fill="#0066CC" fillOpacity={0.08} />
                    <path
                      d={chartModel.historyPath}
                      fill="none"
                      stroke="#0066CC"
                      strokeWidth={2}
                      strokeLinejoin="round"
                    />

                    {/* Today Divider */}
                    <line
                      x1={chartModel.todayX}
                      y1={PAD_TOP}
                      x2={chartModel.todayX}
                      y2={PAD_TOP + PLOT_H}
                      stroke="#1D1D1F"
                      strokeOpacity={0.35}
                      strokeWidth={1}
                      strokeDasharray="3 3"
                    />
                    <text
                      x={chartModel.todayX}
                      y={PAD_TOP - 6}
                      textAnchor="middle"
                      fontSize="10"
                      fontWeight="700"
                      fill="#1D1D1F"
                    >
                      Today
                    </text>

                    {/* Projected Stockout Curve */}
                    {chartModel.projectedPath && (
                      <path
                        d={chartModel.projectedPath}
                        fill="none"
                        stroke={projStroke}
                        strokeWidth={2}
                        strokeDasharray="5 4"
                        strokeLinejoin="round"
                      />
                    )}

                    {/* Projected PO Arrival Markers */}
                    {chartModel.projectedPoints.map((pt, i) =>
                      pt.note ? (
                        <g key={`proj-note-${i}`}>
                          <circle
                            cx={pt.x}
                            cy={pt.y}
                            r={4.5}
                            fill="#1F7A4D"
                            stroke="#FFFFFF"
                            strokeWidth={1.5}
                          >
                            <title>{`${formatSimDate(pt.date)}: ${pt.note} (proj ${pt.val.toFixed(1)}u)`}</title>
                          </circle>
                        </g>
                      ) : null,
                    )}

                    {/* Historical Lifecycle Markers */}
                    {chartModel.markers.map((m, i) => {
                      const ev = m.event;
                      if (ev.event_type === 'PO_PLACED') {
                        const s = 4.5;
                        return (
                          <polygon
                            key={`mk-${i}`}
                            points={`${m.x},${m.y - s} ${m.x + s},${m.y} ${m.x},${m.y + s} ${m.x - s},${m.y}`}
                            fill="#0066CC"
                            stroke="#FFFFFF"
                            strokeWidth={1.2}
                          >
                            <title>{`${formatSimDate(ev.date)} — ${ev.label}`}</title>
                          </polygon>
                        );
                      }
                      const fill =
                        ev.event_type === 'STOCKOUT'
                          ? '#C9342D'
                          : ev.event_type === 'LOW_STOCK'
                            ? '#D96B00'
                            : '#1F7A4D';
                      return (
                        <circle
                          key={`mk-${i}`}
                          cx={m.x}
                          cy={m.y}
                          r={ev.event_type === 'REPLENISHMENT' ? 5 : 4}
                          fill={fill}
                          stroke="#FFFFFF"
                          strokeWidth={1.4}
                        >
                          <title>{`${formatSimDate(ev.date)} — ${ev.label}`}</title>
                        </circle>
                      );
                    })}

                    {/* X-Axis Labels */}
                    <text x={PAD_LEFT} y={CHART_HEIGHT - 12} fontSize="10" fill="#86868B" textAnchor="start">
                      {chartModel.startLabel}
                    </text>
                    <text
                      x={chartModel.xAt(Math.floor(chartModel.history.length / 2))}
                      y={CHART_HEIGHT - 12}
                      fontSize="10"
                      fill="#86868B"
                      textAnchor="middle"
                    >
                      {chartModel.midLabel}
                    </text>
                    <text
                      x={chartModel.todayX}
                      y={CHART_HEIGHT - 12}
                      fontSize="10"
                      fontWeight="600"
                      fill="#1D1D1F"
                      textAnchor="middle"
                    >
                      {chartModel.todayLabel}
                    </text>
                    {chartModel.endLabel && (
                      <text
                        x={CHART_WIDTH - PAD_RIGHT}
                        y={CHART_HEIGHT - 12}
                        fontSize="10"
                        fill="#86868B"
                        textAnchor="end"
                      >
                        {chartModel.endLabel}
                      </text>
                    )}
                  </svg>
                </div>
              ) : (
                <div className="py-8 text-center text-xs text-[#6E6E73]">
                  No historical stock trajectory available yet.
                </div>
              )}

              {/* Chart Legend */}
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 border-t border-black/[0.06] pt-2.5 text-[11px] text-[#6E6E73]">
                <span className="inline-flex items-center gap-1.5">
                  <span className="h-0.5 w-3.5 bg-[#0066CC] inline-block" /> Historical On Hand
                </span>
                <span className="inline-flex items-center gap-1.5">
                  <span
                    className="h-0.5 w-3.5 border-t-2 border-dashed inline-block"
                    style={{ borderColor: projStroke }}
                  />{' '}
                  Projected Runout
                </span>
                <span className="inline-flex items-center gap-1.5">
                  <span className="h-2.5 w-2.5 rounded-full bg-[#C9342D] inline-block" /> Stockout
                </span>
                <span className="inline-flex items-center gap-1.5">
                  <span className="h-2.5 w-2.5 rounded-full bg-[#D96B00] inline-block" /> Low Stock (&lt;ROP)
                </span>
                <span className="inline-flex items-center gap-1.5">
                  <span className="h-2.5 w-2.5 rotate-45 bg-[#0066CC] inline-block" /> PO Placed
                </span>
                <span className="inline-flex items-center gap-1.5">
                  <span className="h-2.5 w-2.5 rounded-full bg-[#1F7A4D] inline-block" /> Replenishment
                </span>
              </div>
            </section>

            {/* Primary Supplier Lead Time & Communication Buffer */}
            <section className="rounded-2xl border border-black/[0.08] bg-white p-4 space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="text-xs font-bold uppercase tracking-wider text-[#1D1D1F] flex items-center gap-1.5">
                  <Truck className="h-3.5 w-3.5 text-[#0066CC]" />
                  Primary Supplier Lead Time &amp; Communication Buffer
                </h3>
                <span className="text-xs font-semibold text-[#1D1D1F]">
                  {metrics.primary_supplier_name ?? 'No primary supplier'}
                </span>
              </div>

              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs tabular-nums">
                <div className="rounded-xl border border-black/[0.06] bg-[#FBFBFD] px-3 py-2">
                  <div className="text-[11px] text-[#86868B]">Effective Lead Time</div>
                  <div className="mt-0.5 font-bold text-[#1D1D1F]">
                    {metrics.primary_effective_lt.toFixed(1)}d
                  </div>
                  <div className="text-[10px] text-[#6E6E73]">
                    Quoted: {metrics.primary_quoted_lt}d · σ_L: {metrics.primary_lead_time_std.toFixed(1)}d
                  </div>
                </div>

                <div className="rounded-xl border border-black/[0.06] bg-[#FBFBFD] px-3 py-2">
                  <div className="text-[11px] text-[#86868B]">Comm Delay Buffer</div>
                  <div className="mt-0.5 font-bold text-[#1D1D1F]">
                    +{metrics.comm_buffer_days.toFixed(1)}d
                  </div>
                  <div className="text-[10px] text-[#6E6E73]">
                    Avg resp: {metrics.primary_avg_response_days.toFixed(1)}d ({Math.round(metrics.primary_response_rate * 100)}%)
                  </div>
                </div>

                <div className="rounded-xl border border-black/[0.06] bg-[#FBFBFD] px-3 py-2">
                  <div className="text-[11px] text-[#86868B]">Urgent / Warning Cutoff</div>
                  <div className="mt-0.5 font-bold text-[#1D1D1F]">
                    {metrics.urgent_threshold_days.toFixed(1)}d / {metrics.warning_threshold_days.toFixed(1)}d
                  </div>
                  <div className="text-[10px] text-[#6E6E73]">
                    L_eff + comm (+ safety)
                  </div>
                </div>

                <div className="rounded-xl border border-black/[0.06] bg-[#FBFBFD] px-3 py-2">
                  <div className="text-[11px] text-[#86868B]">Earliest Active PO ETA</div>
                  <div className="mt-0.5 font-semibold text-[#1D1D1F] truncate">
                    {metrics.earliest_po_eta ? formatSimDate(metrics.earliest_po_eta) : 'None open'}
                  </div>
                  <div className="text-[10px] text-[#6E6E73]">
                    {metrics.earliest_po_eta
                      ? formatSimDateTime(metrics.earliest_po_eta)
                      : 'No dispatched PO in flight'}
                  </div>
                </div>
              </div>
            </section>

            {/* Recommended PO Quantity Card */}
            {suggestion && (
              <section className="rounded-2xl border border-[#0066CC]/22 bg-[#0066CC]/[0.03] p-4 space-y-2.5">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <CalendarClock className="h-4 w-4 text-[#0066CC]" />
                    <h3 className="text-xs font-bold uppercase tracking-wider text-[#1D1D1F]">
                      Recommended Replenishment Order
                    </h3>
                  </div>
                  <div className="flex items-center gap-2">
                    {activePoId ? (
                      <button
                        type="button"
                        onClick={() => {
                          onClose();
                          onNavigatePo(activePoId);
                        }}
                        className="apple-press inline-flex items-center gap-1.5 rounded-xl bg-[#0066CC] px-3 py-1.5 text-xs font-semibold text-white hover:bg-[#0071E3]"
                      >
                        <ExternalLink className="h-3.5 w-3.5" />
                        View {activePoNumber ?? 'Active PO'}
                      </button>
                    ) : (
                      onCreatePo && (
                        <button
                          type="button"
                          onClick={() => {
                            onClose();
                            onCreatePo(data.part_id);
                          }}
                          className="apple-press inline-flex items-center gap-1.5 rounded-xl bg-[#0066CC] px-3 py-1.5 text-xs font-semibold text-white hover:bg-[#0071E3]"
                        >
                          <ShoppingCart className="h-3.5 w-3.5" />
                          Create PO ({suggestion.suggested_qty}u)
                        </button>
                      )
                    )}
                  </div>
                </div>

                <div className="flex flex-wrap items-baseline gap-x-5 gap-y-1 text-xs tabular-nums">
                  <span>
                    Suggested Qty:{' '}
                    <strong className="text-base text-[#0058B3]">
                      {suggestion.suggested_qty} units
                    </strong>
                  </span>
                  <span className="text-[#6E6E73]">
                    Supplier: <strong className="text-[#1D1D1F]">{suggestion.supplier_name}</strong>
                  </span>
                  <span className="text-[#6E6E73]">
                    MOQ: <strong className="text-[#1D1D1F]">{suggestion.moq}</strong>
                  </span>
                  <span className="text-[#6E6E73]">
                    Est. Total:{' '}
                    <strong className="text-[#1D1D1F]">
                      {formatInr(suggestion.suggested_qty * suggestion.unit_price)}
                    </strong>
                  </span>
                </div>
                <p className="text-xs leading-relaxed text-[#3A3A3C]">{suggestion.reasoning}</p>
              </section>
            )}

            {/* Lifecycle Events Timeline */}
            <section className="space-y-2">
              <h3 className="text-xs font-bold uppercase tracking-wider text-[#86868B] flex items-center gap-1.5">
                <AlertTriangle className="h-3.5 w-3.5" />
                90-Day Cycle Events ({data.events.length})
              </h3>
              {data.events.length === 0 ? (
                <p className="text-xs text-[#6E6E73]">No cycle events recorded in the trailing 90 days.</p>
              ) : (
                <div className="divide-y divide-black/[0.05] rounded-2xl border border-black/[0.08] bg-white max-h-56 overflow-y-auto">
                  {[...data.events].reverse().map((ev, idx) => (
                    <div
                      key={`${ev.date}-${ev.event_type}-${idx}`}
                      className="flex flex-wrap items-center justify-between gap-2 px-3.5 py-2.5 text-xs tabular-nums"
                    >
                      <div className="flex items-center gap-2 min-w-0">
                        <span
                          className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold shrink-0 ${eventBadgeClass(
                            ev.event_type,
                          )}`}
                        >
                          {ev.event_type}
                        </span>
                        <span className="text-[#1D1D1F] truncate">{ev.label}</span>
                      </div>
                      <div className="flex items-center gap-2 shrink-0 text-[11px] text-[#6E6E73]">
                        {ev.actual_lt_days !== null && (
                          <span className="rounded-full bg-[#1F7A4D]/10 px-2 py-0.5 font-semibold text-[#1B6B43]">
                            Actual LT: {ev.actual_lt_days.toFixed(1)}d
                          </span>
                        )}
                        <span>{formatSimDate(ev.date)}</span>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </section>
          </div>
        ) : null}
      </div>
    </div>,
    document.body,
  );
}
