import { useState } from 'react';
import { createPortal } from 'react-dom';
import { BarChart3, Maximize2, Workflow, X } from 'lucide-react';
import type {
  CopilotChartSpec,
  CopilotDiagramSpec,
  CopilotVisual,
} from '../api/types';

const PALETTE = ['#0066CC', '#1F7A4D', '#D97706', '#5E5CE6', '#C9342D', '#0D9488'];

function formatVal(val: number, unit?: string): string {
  const num = Math.abs(val) >= 100 ? val.toFixed(0) : Number.isInteger(val) ? String(val) : val.toFixed(1);
  if (!unit) return num;
  if (unit === '₹' || unit === 'INR') return `₹${num}`;
  return `${num} ${unit}`;
}

function ChartSvg({ spec, compact = true }: { spec: CopilotChartSpec; compact?: boolean }) {
  const width = compact ? 420 : 760;
  const height = compact ? 220 : 340;
  const padLeft = 46;
  const padRight = 18;
  const padTop = 20;
  const padBottom = 42;
  const plotW = width - padLeft - padRight;
  const plotH = height - padTop - padBottom;

  const ctype = (spec.chart_type || 'bar').toLowerCase();
  const labels = spec.labels || [];
  const series = spec.series || [];
  const refs = spec.reference_lines || [];

  if (ctype === 'donut') {
    const values = series[0]?.values ?? [];
    const total = Math.max(
      1e-9,
      values.reduce((acc, v) => acc + Math.max(0, v), 0),
    );
    const cx = width * 0.34;
    const cy = height / 2;
    const r = Math.min(plotH, plotW) * 0.4;
    const ir = r * 0.56;
    let angle = -Math.PI / 2;

    const slices = values.map((rawV, idx) => {
      const v = Math.max(0, rawV);
      const frac = v / total;
      const sweep = frac * Math.PI * 2;
      const startA = angle;
      const endA = angle + sweep;
      angle = endA;

      const x1 = cx + r * Math.cos(startA);
      const y1 = cy + r * Math.sin(startA);
      const x2 = cx + r * Math.cos(endA);
      const y2 = cy + r * Math.sin(endA);
      const ix1 = cx + ir * Math.cos(endA);
      const iy1 = cy + ir * Math.sin(endA);
      const ix2 = cx + ir * Math.cos(startA);
      const iy2 = cy + ir * Math.sin(startA);
      const largeArc = sweep > Math.PI ? 1 : 0;
      const color = PALETTE[idx % PALETTE.length];
      const path =
        frac >= 0.999
          ? `M ${cx} ${cy - r} A ${r} ${r} 0 1 1 ${cx - 0.01} ${cy - r} Z`
          : `M ${x1} ${y1} A ${r} ${r} 0 ${largeArc} 1 ${x2} ${y2} L ${ix1} ${iy1} A ${ir} ${ir} 0 ${largeArc} 0 ${ix2} ${iy2} Z`;
      return { path, color, label: labels[idx] ?? `#${idx + 1}`, value: v, pct: frac * 100 };
    });

    return (
      <div className="flex flex-col sm:flex-row items-center gap-3">
        <svg viewBox={`0 0 ${width * 0.68} ${height}`} className="w-full max-w-[240px] h-auto shrink-0">
          {slices.map((s, i) => (
            <path key={i} d={s.path} fill={s.color} stroke="#FFFFFF" strokeWidth={1.5}>
              <title>{`${s.label}: ${formatVal(s.value, spec.unit)} (${s.pct.toFixed(1)}%)`}</title>
            </path>
          ))}
          <text x={cx * 0.98} y={cy - 2} textAnchor="middle" fontSize="11" fontWeight="700" fill="#1D1D1F">
            {formatVal(total, spec.unit)}
          </text>
          <text x={cx * 0.98} y={cy + 12} textAnchor="middle" fontSize="9" fill="#86868B">
            Total
          </text>
        </svg>
        <div className="flex-1 space-y-1 text-[11px] min-w-0 w-full">
          {slices.map((s, i) => (
            <div key={i} className="flex items-center justify-between gap-2 tabular-nums">
              <span className="inline-flex items-center gap-1.5 truncate text-[#1D1D1F]">
                <span
                  className="h-2.5 w-2.5 rounded-xs shrink-0"
                  style={{ backgroundColor: s.color }}
                />
                <span className="truncate">{s.label}</span>
              </span>
              <span className="font-semibold text-[#1D1D1F] shrink-0">
                {formatVal(s.value, spec.unit)} ({s.pct.toFixed(0)}%)
              </span>
            </div>
          ))}
        </div>
      </div>
    );
  }

  const allValues = [
    0,
    ...series.flatMap((s) => s.values),
    ...refs.map((r) => r.value),
  ];
  const rawMax = Math.max(...allValues, 1);
  const yMax = rawMax * 1.12;
  const yAt = (val: number) => padTop + plotH - (Math.max(0, val) / yMax) * plotH;

  const n = Math.max(1, labels.length);
  const labelStep = n > 12 ? Math.ceil(n / 6) : 1;

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-auto overflow-visible">
      {/* Gridlines */}
      {[0, 0.25, 0.5, 0.75, 1].map((t) => {
        const yVal = yMax * t;
        const y = yAt(yVal);
        return (
          <g key={t}>
            <line
              x1={padLeft}
              y1={y}
              x2={width - padRight}
              y2={y}
              stroke="#000000"
              strokeOpacity={t === 0 ? 0.14 : 0.06}
            />
            <text
              x={padLeft - 6}
              y={y + 3}
              textAnchor="end"
              fontSize="9.5"
              fill="#86868B"
            >
              {formatVal(yVal)}
            </text>
          </g>
        );
      })}

      {/* Reference threshold lines */}
      {refs.map((r, rIdx) => {
        const ry = yAt(r.value);
        const col = r.color || '#D96B00';
        return (
          <g key={`ref-${rIdx}`}>
            <line
              x1={padLeft}
              y1={ry}
              x2={width - padRight}
              y2={ry}
              stroke={col}
              strokeWidth={1.3}
              strokeDasharray="4 3"
            />
            <text
              x={width - padRight - 2}
              y={Math.max(padTop + 9, ry - 3)}
              textAnchor="end"
              fontSize="9.5"
              fontWeight="600"
              fill={col}
            >
              {r.label} ({formatVal(r.value, spec.unit)})
            </text>
          </g>
        );
      })}

      {/* Bars */}
      {(ctype === 'bar' || ctype === 'grouped_bar') &&
        labels.map((lbl, i) => {
          const groupW = plotW / n;
          const innerW = Math.min(44, groupW * 0.76);
          const sCount = Math.max(1, series.length);
          const barW = Math.max(4, innerW / sCount - 2);
          const groupLeft = padLeft + i * groupW + (groupW - innerW) / 2;

          return series.map((s, sIdx) => {
            const val = s.values[i] ?? 0;
            const y = yAt(val);
            const h = Math.max(1, padTop + plotH - y);
            const x = groupLeft + sIdx * (barW + 2);
            const col = s.color || PALETTE[sIdx % PALETTE.length];
            return (
              <rect
                key={`b-${i}-${sIdx}`}
                x={x}
                y={y}
                width={barW}
                height={h}
                rx={3}
                fill={col}
                fillOpacity={0.9}
              >
                <title>{`${lbl} · ${s.name}: ${formatVal(val, spec.unit)}`}</title>
              </rect>
            );
          });
        })}

      {/* Line / Area / Scatter */}
      {(ctype === 'line' || ctype === 'area' || ctype === 'scatter') &&
        series.map((s, sIdx) => {
          const col = s.color || PALETTE[sIdx % PALETTE.length];
          const xAt = (idx: number) =>
            n === 1 ? padLeft + plotW / 2 : padLeft + (idx / (n - 1)) * plotW;
          const pts = s.values.map((v, i) => ({ x: xAt(i), y: yAt(v), v, lbl: labels[i] }));
          const linePath = pts
            .map((p, i) => `${i === 0 ? 'M' : 'L'} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`)
            .join(' ');
          const areaPath =
            pts.length > 0
              ? `${linePath} L ${pts[pts.length - 1].x.toFixed(1)} ${(padTop + plotH).toFixed(1)} L ${pts[0].x.toFixed(1)} ${(padTop + plotH).toFixed(1)} Z`
              : '';
          return (
            <g key={`s-${sIdx}`}>
              {ctype === 'area' && pts.length > 1 && (
                <path d={areaPath} fill={col} fillOpacity={0.12} />
              )}
              {ctype !== 'scatter' && pts.length > 1 && (
                <path
                  d={linePath}
                  fill="none"
                  stroke={col}
                  strokeWidth={2}
                  strokeLinejoin="round"
                />
              )}
              {pts.map((p, i) => (
                <circle
                  key={`pt-${i}`}
                  cx={p.x}
                  cy={p.y}
                  r={ctype === 'scatter' ? 4.5 : n <= 20 ? 3.2 : 2.2}
                  fill={col}
                  stroke="#FFFFFF"
                  strokeWidth={1}
                >
                  <title>{`${p.lbl} · ${s.name}: ${formatVal(p.v, spec.unit)}`}</title>
                </circle>
              ))}
            </g>
          );
        })}

      {/* X-Axis Labels */}
      {labels.map((lbl, i) => {
        if (i % labelStep !== 0 && i !== n - 1) return null;
        const x =
          ctype === 'bar' || ctype === 'grouped_bar'
            ? padLeft + (i + 0.5) * (plotW / n)
            : n === 1
              ? padLeft + plotW / 2
              : padLeft + (i / (n - 1)) * plotW;
        const shortLbl = lbl.length > 12 ? `${lbl.slice(0, 11)}…` : lbl;
        return (
          <text
            key={`xl-${i}`}
            x={x}
            y={height - 14}
            textAnchor="middle"
            fontSize="9.5"
            fill="#6E6E73"
          >
            {shortLbl}
          </text>
        );
      })}
    </svg>
  );
}

function toneClasses(tone?: string): string {
  switch ((tone || 'neutral').toLowerCase()) {
    case 'info':
      return 'border-[#0066CC]/28 bg-[#0066CC]/[0.07] text-[#0058B3]';
    case 'success':
      return 'border-[#1F7A4D]/28 bg-[#1F7A4D]/[0.07] text-[#1B6B43]';
    case 'warning':
      return 'border-[#B46900]/30 bg-[#B46900]/[0.08] text-[#945600]';
    case 'danger':
      return 'border-[#C9342D]/30 bg-[#C9342D]/[0.08] text-[#B82923]';
    default:
      return 'border-black/[0.10] bg-[#FBFBFD] text-[#1D1D1F]';
  }
}

function DiagramView({ spec }: { spec: CopilotDiagramSpec }) {
  const nodeMap = new Map(spec.nodes.map((n) => [n.id, n]));
  return (
    <div className="space-y-2.5">
      <div className="flex flex-wrap items-stretch gap-2">
        {spec.nodes.map((node, idx) => (
          <div key={node.id} className="flex items-center gap-1.5">
            <div
              className={`rounded-xl border px-3 py-2 text-xs min-w-[115px] max-w-[190px] ${toneClasses(
                node.tone,
              )}`}
            >
              <div className="font-semibold leading-snug">{node.label}</div>
              {node.detail && (
                <div className="mt-0.5 text-[10px] opacity-80 leading-snug">{node.detail}</div>
              )}
            </div>
            {spec.edges.length === 0 && idx < spec.nodes.length - 1 && (
              <span className="text-xs text-[#86868B] font-bold">→</span>
            )}
          </div>
        ))}
      </div>
      {spec.edges.length > 0 && (
        <div className="rounded-xl border border-black/[0.06] bg-[#FBFBFD] px-3 py-2 space-y-1 text-[11px]">
          {spec.edges.map((edge, idx) => {
            const srcLabel = nodeMap.get(edge.source)?.label ?? edge.source;
            const dstLabel = nodeMap.get(edge.target)?.label ?? edge.target;
            return (
              <div key={idx} className="flex flex-wrap items-center gap-1.5 text-[#1D1D1F]">
                <span className="font-semibold">{srcLabel}</span>
                <span className="text-[#86868B]">→</span>
                {edge.label && (
                  <span className="rounded-full bg-black/[0.06] px-2 py-0.5 text-[10px] font-medium text-[#6E6E73]">
                    {edge.label}
                  </span>
                )}
                {edge.label && <span className="text-[#86868B]">→</span>}
                <span className="font-semibold">{dstLabel}</span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export function CopilotChartCard({ visual }: { visual: CopilotVisual }) {
  const [expanded, setExpanded] = useState(false);

  const isChart = visual.kind === 'chart';
  const title = visual.spec.title;
  const subtitle = visual.spec.subtitle;

  return (
    <>
      <div className="rounded-2xl border border-black/[0.08] bg-white p-3.5 space-y-2.5 shadow-2xs">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="flex items-center gap-1.5 text-xs font-bold text-[#1D1D1F]">
              {isChart ? (
                <BarChart3 className="h-3.5 w-3.5 text-[#0066CC] shrink-0" />
              ) : (
                <Workflow className="h-3.5 w-3.5 text-[#5E5CE6] shrink-0" />
              )}
              <span className="truncate">{title}</span>
            </div>
            {subtitle && <p className="text-[11px] text-[#6E6E73] mt-0.5">{subtitle}</p>}
          </div>
          {isChart && (
            <button
              type="button"
              onClick={() => setExpanded(true)}
              aria-label={`Expand ${title}`}
              className="apple-press rounded-lg border border-black/[0.08] p-1 text-[#6E6E73] hover:bg-black/[0.04] hover:text-[#1D1D1F]"
            >
              <Maximize2 className="h-3 w-3" />
            </button>
          )}
        </div>

        {isChart ? (
          <>
            <ChartSvg spec={visual.spec} compact />
            {visual.spec.series.length > 1 && visual.spec.chart_type !== 'donut' && (
              <div className="flex flex-wrap items-center gap-3 pt-1 border-t border-black/[0.05] text-[10px] text-[#6E6E73]">
                {visual.spec.series.map((s, idx) => (
                  <span key={s.name} className="inline-flex items-center gap-1.5">
                    <span
                      className="h-2 w-2 rounded-full"
                      style={{ backgroundColor: s.color || PALETTE[idx % PALETTE.length] }}
                    />
                    {s.name}
                  </span>
                ))}
              </div>
            )}
          </>
        ) : (
          <DiagramView spec={visual.spec} />
        )}
      </div>

      {expanded &&
        isChart &&
        typeof document !== 'undefined' &&
        createPortal(
          <div
            onClick={() => setExpanded(false)}
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/35 backdrop-blur-xs p-4"
          >
            <div
              onClick={(e) => e.stopPropagation()}
              className="w-full max-w-4xl rounded-2xl border border-black/[0.08] bg-white p-6 shadow-2xl space-y-4"
            >
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h3 className="text-base font-bold text-[#1D1D1F]">{title}</h3>
                  {subtitle && <p className="text-xs text-[#6E6E73] mt-0.5">{subtitle}</p>}
                </div>
                <button
                  type="button"
                  onClick={() => setExpanded(false)}
                  className="apple-press rounded-full p-1.5 text-[#86868B] hover:bg-black/[0.05] hover:text-[#1D1D1F]"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
              <ChartSvg spec={visual.spec} compact={false} />
            </div>
          </div>,
          document.body,
        )}
    </>
  );
}
