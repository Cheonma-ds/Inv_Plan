import { Bell, CalendarClock, FastForward, RotateCcw, Sparkles } from 'lucide-react';
import { formatSimDateTime } from '../utils/format';

interface ClockBarProps {
  simNow: string | null;
  openAlertsCount: number;
  alertsActive?: boolean;
  copilotOpen?: boolean;
  busy: boolean;
  onAdvance: (params: { days?: number; hours?: number }) => void;
  onResetSeed: () => void;
  onGoToAlerts: () => void;
  onToggleCopilot: () => void;
}

export function ClockBar({
  simNow,
  openAlertsCount,
  alertsActive = false,
  copilotOpen = false,
  busy,
  onAdvance,
  onResetSeed,
  onGoToAlerts,
  onToggleCopilot,
}: ClockBarProps) {
  return (
    <header className="sticky top-0 z-30 border-b border-black/[0.08] bg-white/82 backdrop-blur-xl">
      <div className="flex flex-wrap items-center justify-between gap-3 px-5 md:px-7 py-2.5">
        <div className="flex flex-wrap items-center gap-2.5">
          <div className="flex items-center gap-2 rounded-full bg-[#1D1D1F] px-3.5 py-1.5 text-white shadow-xs">
            <CalendarClock className="h-3.5 w-3.5 text-[#5AC8FA]" />
            <span className="text-[11px] uppercase tracking-wider text-[#A1A1A6] font-medium">
              Sim Clock
            </span>
            <span className="text-xs font-semibold tabular-nums">
              {formatSimDateTime(simNow)}
            </span>
          </div>

          <div className="inline-flex items-center rounded-full border border-black/[0.08] bg-[#FBFBFD] p-0.5 shadow-2xs">
            <button
              type="button"
              disabled={busy}
              onClick={() => onAdvance({ hours: 6 })}
              className="apple-press inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-medium text-[#1D1D1F] hover:bg-black/[0.05] disabled:opacity-45"
              title="Advance simulated clock by 6 hours"
            >
              <FastForward className="h-3 w-3 text-[#0066CC]" />
              +6h
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => onAdvance({ days: 1 })}
              className="apple-press inline-flex items-center rounded-full px-2.5 py-1 text-xs font-semibold text-[#1D1D1F] hover:bg-black/[0.05] disabled:opacity-45"
            >
              +1d
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => onAdvance({ days: 3 })}
              className="apple-press inline-flex items-center rounded-full px-2.5 py-1 text-xs font-semibold text-[#1D1D1F] hover:bg-black/[0.05] disabled:opacity-45"
            >
              +3d
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => onAdvance({ days: 7 })}
              className="apple-press inline-flex items-center rounded-full px-2.5 py-1 text-xs font-semibold text-[#1D1D1F] hover:bg-black/[0.05] disabled:opacity-45"
            >
              +7d
            </button>
          </div>
        </div>

        <div className="flex items-center gap-2.5">
          <button
            type="button"
            onClick={onToggleCopilot}
            aria-pressed={copilotOpen}
            title="Toggle Lex Manager Copilot (Ctrl+L)"
            className={`apple-press inline-flex items-center gap-1.5 rounded-full border px-3.5 py-1.5 text-xs shadow-2xs ${
              copilotOpen
                ? 'border-[#5E5CE6] bg-[#5E5CE6] font-semibold text-white shadow-[0_1px_4px_rgba(94,92,230,0.32)]'
                : 'border-[#5E5CE6]/28 bg-[#5E5CE6]/[0.07] font-semibold text-[#4B48C4] hover:bg-[#5E5CE6]/[0.14]'
            }`}
          >
            <Sparkles className={`h-3.5 w-3.5 ${copilotOpen ? 'text-white' : 'text-[#5E5CE6]'}`} />
            <span>Ask Lex</span>
            <kbd
              className={`hidden sm:inline-block rounded px-1.5 py-0.5 font-mono text-[10px] font-semibold ${
                copilotOpen
                  ? 'bg-white/20 text-white'
                  : 'bg-[#5E5CE6]/12 text-[#4B48C4]'
              }`}
            >
              Ctrl+L
            </kbd>
          </button>

          <button
            type="button"
            onClick={onGoToAlerts}
            aria-current={alertsActive ? 'page' : undefined}
            className={`apple-press inline-flex items-center gap-2 rounded-full border px-3.5 py-1.5 text-xs shadow-2xs ${
              alertsActive
                ? 'border-[#0066CC] bg-[#0066CC] font-semibold text-white shadow-[0_1px_4px_rgba(0,102,204,0.3)]'
                : openAlertsCount > 0
                  ? 'border-[#C9342D]/35 bg-[#C9342D]/[0.07] font-semibold text-[#B82923] hover:bg-[#C9342D]/[0.12]'
                  : 'border-black/[0.08] bg-white/90 font-medium text-[#1D1D1F] hover:bg-white'
            }`}
          >
            <Bell
              className={`h-3.5 w-3.5 ${
                alertsActive
                  ? 'text-white'
                  : openAlertsCount > 0
                    ? 'text-[#C9342D]'
                    : 'text-[#6E6E73]'
              }`}
            />
            <span>Open Alerts</span>
            <span
              className={`inline-flex min-w-5 items-center justify-center rounded-full px-1.5 py-0.5 text-[11px] font-bold tabular-nums ${
                alertsActive
                  ? openAlertsCount > 0
                    ? 'bg-white text-[#B82923]'
                    : 'bg-white/20 text-white'
                  : openAlertsCount > 0
                    ? 'bg-[#C9342D] text-white'
                    : 'bg-black/[0.06] text-[#6E6E73]'
              }`}
            >
              {openAlertsCount}
            </span>
          </button>

          <button
            type="button"
            disabled={busy}
            onClick={onResetSeed}
            className="apple-press inline-flex items-center gap-1.5 rounded-full border border-[#C9342D]/20 bg-[#C9342D]/[0.07] px-3.5 py-1.5 text-xs font-semibold text-[#B82923] hover:bg-[#C9342D]/[0.12] disabled:opacity-45"
          >
            <RotateCcw className="h-3.5 w-3.5" />
            Reset Seed
          </button>
        </div>
      </div>
    </header>
  );
}
