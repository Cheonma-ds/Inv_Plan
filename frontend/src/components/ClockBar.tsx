import { Bell, CalendarClock, FastForward, RotateCcw } from 'lucide-react';
import { formatSimDateTime } from '../utils/format';

interface ClockBarProps {
  simNow: string | null;
  openAlertsCount: number;
  busy: boolean;
  onAdvance: (params: { days?: number; hours?: number }) => void;
  onResetSeed: () => void;
  onGoToAlerts: () => void;
}

export function ClockBar({
  simNow,
  openAlertsCount,
  busy,
  onAdvance,
  onResetSeed,
  onGoToAlerts,
}: ClockBarProps) {
  return (
    <header className="sticky top-0 z-30 border-b border-slate-200 bg-white/95 backdrop-blur">
      <div className="flex flex-wrap items-center justify-between gap-3 px-5 py-3">
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2 rounded-lg bg-slate-900 px-3 py-1.5 text-white">
            <CalendarClock className="h-4 w-4 text-sky-400" />
            <span className="text-xs uppercase tracking-wider text-slate-400 font-medium">
              Sim Clock
            </span>
            <span className="text-sm font-semibold tabular-nums">
              {formatSimDateTime(simNow)}
            </span>
          </div>

          <div className="flex items-center gap-1.5">
            <button
              type="button"
              disabled={busy}
              onClick={() => onAdvance({ hours: 6 })}
              className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-slate-50 px-2.5 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-100 disabled:opacity-50"
              title="Advance simulated clock by 6 hours"
            >
              <FastForward className="h-3 w-3" />
              +6h
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => onAdvance({ days: 1 })}
              className="inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-semibold text-slate-800 hover:bg-slate-50 disabled:opacity-50"
            >
              +1d
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => onAdvance({ days: 3 })}
              className="inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-semibold text-slate-800 hover:bg-slate-50 disabled:opacity-50"
            >
              +3d
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => onAdvance({ days: 7 })}
              className="inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-semibold text-slate-800 hover:bg-slate-50 disabled:opacity-50"
            >
              +7d
            </button>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={onGoToAlerts}
            className="inline-flex items-center gap-2 rounded-lg border border-slate-200 bg-slate-50 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-100"
          >
            <Bell className="h-3.5 w-3.5 text-slate-600" />
            <span>Open Alerts</span>
            <span
              className={`inline-flex min-w-5 items-center justify-center rounded-full px-1.5 py-0.5 text-xs font-bold ${
                openAlertsCount > 0
                  ? 'bg-red-600 text-white'
                  : 'bg-slate-200 text-slate-700'
              }`}
            >
              {openAlertsCount}
            </span>
          </button>

          <button
            type="button"
            disabled={busy}
            onClick={onResetSeed}
            className="inline-flex items-center gap-1.5 rounded-lg border border-rose-200 bg-rose-50 px-3 py-1.5 text-xs font-semibold text-rose-700 hover:bg-rose-100 disabled:opacity-50"
          >
            <RotateCcw className="h-3.5 w-3.5" />
            Reset Seed
          </button>
        </div>
      </div>
    </header>
  );
}
