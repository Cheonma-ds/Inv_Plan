import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Activity,
  Award,
  Bell,
  ClipboardList,
  Mail,
  Package,
} from 'lucide-react';
import { api } from './api/client';
import type { NavTab } from './api/types';
import { ClockBar } from './components/ClockBar';
import { ToastContainer, type ToastMessage } from './components/ToastContainer';
import { AIHealthPlaceholderPage } from './pages/AIHealthPlaceholderPage';
import { AlertsPage } from './pages/AlertsPage';
import { InventoryPage } from './pages/InventoryPage';
import { MailroomPage } from './pages/MailroomPage';
import { POTrackerPage } from './pages/POTrackerPage';
import { SuppliersPlanningPage } from './pages/SuppliersPlanningPage';

const NAV_ITEMS: Array<{
  id: NavTab;
  label: string;
  icon: typeof Bell;
}> = [
  { id: 'alerts', label: 'Actionable Alerts', icon: Bell },
  { id: 'inventory', label: 'Inventory & Parts', icon: Package },
  { id: 'pos', label: 'PO Tracker', icon: ClipboardList },
  { id: 'mailroom', label: '5-Persona Mailroom', icon: Mail },
  { id: 'planning', label: 'Suppliers & Planning', icon: Award },
  { id: 'ai_health', label: 'AI Activity & Health', icon: Activity },
];

export default function App() {
  const [activeTab, setActiveTab] = useState<NavTab>('alerts');
  const [simNow, setSimNow] = useState<string | null>(null);
  const [openAlertsCount, setOpenAlertsCount] = useState(0);
  const [clockBusy, setClockBusy] = useState(false);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [focusPoId, setFocusPoId] = useState<number | null>(null);
  const [focusPartId, setFocusPartId] = useState<number | null>(null);

  const [toasts, setToasts] = useState<ToastMessage[]>([]);
  const toastSeq = useRef(1);

  const notify = useCallback(
    (kind: ToastMessage['kind'], title: string, detail?: string) => {
      const id = toastSeq.current++;
      setToasts((prev) => [...prev.slice(-4), { id, kind, title, detail }]);
      window.setTimeout(() => {
        setToasts((prev) => prev.filter((t) => t.id !== id));
      }, 6000);
    },
    [],
  );

  const dismissToast = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const refreshGlobalHeader = useCallback(async () => {
    try {
      const [clockRes, openAlerts] = await Promise.all([
        api.getClock(),
        api.listAlerts('OPEN'),
      ]);
      setSimNow(clockRes.sim_now);
      setOpenAlertsCount(openAlerts.length);
    } catch (err) {
      notify(
        'error',
        'Backend connection failed',
        err instanceof Error ? err.message : String(err),
      );
    }
  }, [notify]);

  useEffect(() => {
    void refreshGlobalHeader();
  }, [refreshGlobalHeader, refreshNonce]);

  const bumpRefresh = useCallback(() => {
    setRefreshNonce((n) => n + 1);
  }, []);

  const handleAdvanceClock = async (params: { days?: number; hours?: number }) => {
    setClockBusy(true);
    try {
      const res = await api.advanceClock(params);
      setSimNow(res.sim_now);
      if (res.sla_events.length > 0) {
        notify(
          'info',
          `Clock advanced · ${res.sla_events.length} SLA event(s) triggered`,
          res.sla_events.map((e) => `${e.po_number}: ${e.event_type}`).join(' | '),
        );
      } else {
        notify('success', 'Simulated clock advanced');
      }
      bumpRefresh();
    } catch (err) {
      notify(
        'error',
        'Failed to advance clock',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setClockBusy(false);
    }
  };

  const handleResetSeed = async () => {
    setClockBusy(true);
    try {
      await api.resetSeed();
      notify('success', 'Database reset to fresh seeded state');
      bumpRefresh();
    } catch (err) {
      notify(
        'error',
        'Reset seed failed',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setClockBusy(false);
    }
  };

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900 flex flex-col">
      <ClockBar
        simNow={simNow}
        openAlertsCount={openAlertsCount}
        busy={clockBusy}
        onAdvance={(p) => void handleAdvanceClock(p)}
        onResetSeed={() => void handleResetSeed()}
        onGoToAlerts={() => setActiveTab('alerts')}
      />

      <div className="flex flex-1 flex-col md:flex-row">
        <aside className="w-full md:w-60 shrink-0 border-b md:border-b-0 md:border-r border-slate-200 bg-white">
          <div className="px-4 py-4 border-b border-slate-100">
            <div className="text-base font-extrabold tracking-tight text-slate-900">
              InvPlan
            </div>
            <div className="text-[11px] text-slate-500">
              Demand-to-Delivery Operations
            </div>
          </div>

          <nav className="p-2.5 flex md:flex-col gap-1 overflow-x-auto">
            {NAV_ITEMS.map((item) => {
              const Icon = item.icon;
              const isActive = activeTab === item.id;
              return (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => setActiveTab(item.id)}
                  className={`flex items-center justify-between gap-2 rounded-lg px-3 py-2 text-xs font-semibold whitespace-nowrap transition ${
                    isActive
                      ? 'bg-slate-900 text-white'
                      : 'text-slate-700 hover:bg-slate-100'
                  }`}
                >
                  <span className="flex items-center gap-2">
                    <Icon className="h-4 w-4 shrink-0" />
                    {item.label}
                  </span>
                  {item.id === 'alerts' && openAlertsCount > 0 && (
                    <span
                      className={`rounded-full px-1.5 py-0.5 text-[10px] font-bold ${
                        isActive
                          ? 'bg-red-500 text-white'
                          : 'bg-red-100 text-red-700'
                      }`}
                    >
                      {openAlertsCount}
                    </span>
                  )}
                </button>
              );
            })}
          </nav>
        </aside>

        <main className="flex-1 p-5 md:p-6 max-w-7xl w-full mx-auto">
          {activeTab === 'alerts' && (
            <AlertsPage
              refreshNonce={refreshNonce}
              onMutated={bumpRefresh}
              onNotify={notify}
              onNavigatePo={(poId) => {
                setFocusPoId(poId);
                setActiveTab('pos');
              }}
              onNavigatePart={(partId) => {
                setFocusPartId(partId);
                setActiveTab('inventory');
              }}
            />
          )}

          {activeTab === 'inventory' && (
            <InventoryPage
              refreshNonce={refreshNonce}
              focusPartId={focusPartId}
              onClearFocus={() => setFocusPartId(null)}
              onMutated={bumpRefresh}
              onNotify={notify}
            />
          )}

          {activeTab === 'pos' && (
            <POTrackerPage
              refreshNonce={refreshNonce}
              focusPoId={focusPoId}
              onClearFocus={() => setFocusPoId(null)}
              onMutated={bumpRefresh}
              onNotify={notify}
            />
          )}

          {activeTab === 'mailroom' && (
            <MailroomPage
              refreshNonce={refreshNonce}
              onMutated={bumpRefresh}
              onNotify={notify}
            />
          )}

          {activeTab === 'planning' && (
            <SuppliersPlanningPage
              refreshNonce={refreshNonce}
              onMutated={bumpRefresh}
              onNotify={notify}
            />
          )}

          {activeTab === 'ai_health' && <AIHealthPlaceholderPage />}
        </main>
      </div>

      <ToastContainer toasts={toasts} onDismiss={dismissToast} />
    </div>
  );
}
