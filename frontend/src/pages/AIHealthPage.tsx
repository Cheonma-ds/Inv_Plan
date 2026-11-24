import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Activity,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Coins,
  Cpu,
  FlaskConical,
  Play,
  RefreshCw,
  ShieldCheck,
  Wrench,
  XCircle,
} from 'lucide-react';
import { api } from '../api/client';
import type {
  AgentTraceItem,
  AutonomyPolicyItem,
  EvalRunReport,
  EvalSuiteMetadata,
  ObservabilityStatusResponse,
} from '../api/types';
import { DataTable, type ColumnDef } from '../components/DataTable';
import { formatSimDateTime } from '../utils/format';

interface AIHealthPageProps {
  refreshNonce: number;
  onMutated: () => void;
  onNotify: (kind: 'success' | 'error' | 'info', title: string, detail?: string) => void;
}

function formatInrMicro(value: number): string {
  return `₹${value.toFixed(4)}`;
}

function taskBadgeClass(taskType: string): string {
  switch (taskType) {
    case 'email_triage':
      return 'bg-[#0066CC]/10 text-[#0058B3] border-[#0066CC]/22';
    case 'invoice_extraction':
      return 'bg-[#5E5CE6]/10 text-[#4B48C4] border-[#5E5CE6]/22';
    case 'email_draft':
      return 'bg-[#B46900]/10 text-[#945600] border-[#B46900]/24';
    case 'copilot_query':
      return 'bg-[#0D9488]/10 text-[#0F766E] border-[#0D9488]/25';
    case 'stock_request_triage':
      return 'bg-[#1F7A4D]/10 text-[#1B6B43] border-[#1F7A4D]/22';
    case 'reminder_wakeup':
      return 'bg-[#6E6E73]/10 text-[#1D1D1F] border-black/[0.14]';
    default:
      return 'bg-black/[0.05] text-[#6E6E73] border-black/[0.10]';
  }
}

export function AIHealthPage({ refreshNonce, onMutated, onNotify }: AIHealthPageProps) {
  const [agentFilter, setAgentFilter] = useState<string>('ALL');
  const [taskFilter, setTaskFilter] = useState<string>('ALL');
  const [evalMode, setEvalMode] = useState<'offline' | 'live'>('offline');

  const [statusInfo, setStatusInfo] = useState<ObservabilityStatusResponse | null>(null);
  const [traces, setTraces] = useState<AgentTraceItem[]>([]);
  const [policies, setPolicies] = useState<AutonomyPolicyItem[]>([]);
  const [evalSuite, setEvalSuite] = useState<EvalSuiteMetadata | null>(null);
  const [evalRuns, setEvalRuns] = useState<EvalRunReport[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);

  const [loading, setLoading] = useState(true);
  const [runningEvals, setRunningEvals] = useState(false);
  const [expandedTraceIds, setExpandedTraceIds] = useState<Record<number, boolean>>({});
  const [expandedEvalCases, setExpandedEvalCases] = useState<Record<string, boolean>>({});

  const evalAgent = useMemo(() => {
    if (agentFilter !== 'ALL') return agentFilter;
    return statusInfo?.registered_agents[0] ?? 'lex';
  }, [agentFilter, statusInfo]);

  const loadData = useCallback(async () => {
    setLoading(true);
    try {
      const agentParam = agentFilter === 'ALL' ? undefined : agentFilter;
      const taskParam = taskFilter === 'ALL' ? undefined : taskFilter;
      const [obsStatus, traceRows, policyRows] = await Promise.all([
        api.getObservabilityStatus(),
        api.listTraces({ agentName: agentParam, taskType: taskParam, limit: 100 }),
        api.listAutonomyPolicies(agentParam),
      ]);
      setStatusInfo(obsStatus);
      setTraces(traceRows);
      setPolicies(policyRows);

      const targetEvalAgent =
        agentFilter !== 'ALL' ? agentFilter : (obsStatus.registered_agents[0] ?? 'lex');
      if (obsStatus.registered_agents.includes(targetEvalAgent)) {
        const [suiteMeta, runs] = await Promise.all([
          api.getEvalSuite(targetEvalAgent),
          api.listEvalRuns(targetEvalAgent, 10),
        ]);
        setEvalSuite(suiteMeta);
        setEvalRuns(runs);
        setSelectedRunId((prev) =>
          prev && runs.some((r) => r.id === prev) ? prev : (runs[0]?.id ?? null),
        );
      } else {
        setEvalSuite(null);
        setEvalRuns([]);
        setSelectedRunId(null);
      }
    } catch (err) {
      onNotify(
        'error',
        'Failed to load AI observability data',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setLoading(false);
    }
  }, [agentFilter, taskFilter, onNotify]);

  useEffect(() => {
    void loadData();
  }, [loadData, refreshNonce]);

  const handleRunEvals = async () => {
    setRunningEvals(true);
    try {
      const report = await api.runEvalSuite(evalAgent, evalMode);
      onNotify(
        report.passed_cases === report.total_cases ? 'success' : 'info',
        `Eval run complete · ${report.passed_cases}/${report.total_cases} passed (${report.pass_rate}%)`,
        `Agent: ${report.agent_name} · Mode: ${report.mode} · Model: ${report.model_name}`,
      );
      setSelectedRunId(report.id);
      onMutated();
      await loadData();
    } catch (err) {
      onNotify(
        'error',
        'Eval run failed',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setRunningEvals(false);
    }
  };

  const toggleTrace = (id: number) => {
    setExpandedTraceIds((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  const toggleEvalCase = (id: string) => {
    setExpandedEvalCases((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  const agentOptions = useMemo(() => {
    const names = new Set<string>(statusInfo?.registered_agents ?? ['lex']);
    for (const p of policies) names.add(p.agent_name);
    for (const t of traces) names.add(t.agent_name);
    return Array.from(names).sort();
  }, [statusInfo, policies, traces]);

  const kpis = useMemo(() => {
    const totalCost = traces.reduce((acc, t) => acc + (t.cost_inr || 0), 0);
    const totalInTokens = traces.reduce((acc, t) => acc + (t.input_tokens || 0), 0);
    const totalOutTokens = traces.reduce((acc, t) => acc + (t.output_tokens || 0), 0);
    const avgLatency =
      traces.length > 0
        ? Math.round(traces.reduce((acc, t) => acc + (t.latency_ms || 0), 0) / traces.length)
        : 0;

    const totalAsIs = policies.reduce((acc, p) => acc + p.approved_as_is_count, 0);
    const totalEdited = policies.reduce((acc, p) => acc + p.approved_edited_count, 0);
    const totalRejected = policies.reduce((acc, p) => acc + p.rejected_count, 0);
    const totalDecisions = totalAsIs + totalEdited + totalRejected;
    const editFreeRate =
      totalDecisions > 0 ? Math.round((totalAsIs / totalDecisions) * 1000) / 10 : null;

    return {
      totalCost,
      totalInTokens,
      totalOutTokens,
      avgLatency,
      totalAsIs,
      totalEdited,
      totalRejected,
      totalDecisions,
      editFreeRate,
    };
  }, [traces, policies]);

  const activeRun = useMemo(() => {
    if (evalRuns.length === 0) return null;
    if (selectedRunId === null) return evalRuns[0];
    return evalRuns.find((r) => r.id === selectedRunId) ?? evalRuns[0];
  }, [evalRuns, selectedRunId]);

  const phoenixBadge = useMemo(() => {
    const mode = statusInfo?.phoenix.mode ?? 'disabled';
    if (mode === 'active') {
      return {
        label: 'Phoenix OTel: Active',
        cls: 'bg-[#1F7A4D]/10 text-[#1B6B43] border-[#1F7A4D]/22',
      };
    }
    if (mode === 'fallback_sqlite') {
      return {
        label: 'Phoenix: Fallback (SQLite)',
        cls: 'bg-[#B46900]/10 text-[#945600] border-[#B46900]/24',
      };
    }
    return {
      label: 'Phoenix: Off (SQLite Traces)',
      cls: 'bg-black/[0.05] text-[#6E6E73] border-black/[0.10]',
    };
  }, [statusInfo]);

  const policyColumns = useMemo<ColumnDef<AutonomyPolicyItem>[]>(
    () => [
      {
        key: 'category',
        header: 'Category',
        tooltip:
          'Outbound email workflow category governed by Lex draft-first policy (such as PO_DISPATCH, FOLLOW_UP, EXPEDITE, or INVOICE_DISPUTE).',
        defaultWidth: 260,
        minWidth: 165,
        cellClassName: 'font-semibold text-[#1D1D1F]',
        renderCell: (p) => p.action_category,
      },
      {
        key: 'mode',
        header: 'Policy Mode',
        tooltip:
          'Autonomy governance mode for this category. APPROVAL_REQUIRED holds every draft for Inventory Manager review; AUTO_SEND allows autonomous dispatch when enabled.',
        defaultWidth: 185,
        minWidth: 140,
        renderCell: (p) => (
          <span
            className={`inline-flex rounded-full border px-2.5 py-0.5 text-[10px] font-semibold ${
              p.mode === 'AUTO_SEND'
                ? 'bg-[#1F7A4D]/10 text-[#1B6B43] border-[#1F7A4D]/22'
                : 'bg-black/[0.04] text-[#6E6E73] border-black/[0.08]'
            }`}
          >
            {p.mode}
          </span>
        ),
      },
      {
        key: 'approved_as_is',
        header: 'Approved As-Is',
        tooltip:
          'Count of Lex drafts approved and sent by the Inventory Manager without any text modifications.',
        defaultWidth: 140,
        minWidth: 105,
        align: 'right',
        cellClassName: 'font-semibold tabular-nums text-[#1B6B43]',
        renderCell: (p) => p.approved_as_is_count,
      },
      {
        key: 'approved_edited',
        header: 'Approved Edited',
        tooltip:
          'Count of Lex drafts where the Inventory Manager edited the subject or body before approving dispatch.',
        defaultWidth: 150,
        minWidth: 110,
        align: 'right',
        cellClassName: 'font-semibold tabular-nums text-[#945600]',
        renderCell: (p) => p.approved_edited_count,
      },
      {
        key: 'rejected',
        header: 'Rejected',
        tooltip: 'Count of Lex drafts rejected/discarded by the Inventory Manager.',
        defaultWidth: 110,
        minWidth: 85,
        align: 'right',
        cellClassName: 'font-semibold tabular-nums text-[#B82923]',
        renderCell: (p) => p.rejected_count,
      },
      {
        key: 'edit_free_rate',
        header: 'Edit-Free %',
        tooltip:
          'Share of reviewed drafts approved without edits: Approved As-Is / (Approved As-Is + Approved Edited + Rejected).',
        defaultWidth: 125,
        minWidth: 95,
        align: 'right',
        cellClassName: 'font-bold tabular-nums text-[#1D1D1F]',
        renderCell: (p) =>
          p.edit_free_approval_rate !== null ? `${p.edit_free_approval_rate}%` : '—',
      },
      {
        key: 'trust_distribution',
        header: 'Trust Distribution',
        tooltip:
          'Visual proportion of review outcomes for this category: green = Approved As-Is, amber = Approved Edited, red = Rejected.',
        defaultWidth: 220,
        minWidth: 150,
        cellClassName: 'align-middle',
        renderCell: (p) => {
          const total = p.total_decisions;
          const asIsPct = total > 0 ? (p.approved_as_is_count / total) * 100 : 0;
          const editPct = total > 0 ? (p.approved_edited_count / total) * 100 : 0;
          const rejPct = total > 0 ? (p.rejected_count / total) * 100 : 0;
          return total === 0 ? (
            <div className="h-2 w-full rounded-full bg-black/[0.06]" />
          ) : (
            <div className="flex h-2 w-full overflow-hidden rounded-full bg-black/[0.06]">
              {asIsPct > 0 && (
                <div
                  className="bg-[#1F7A4D]"
                  style={{ width: `${asIsPct}%` }}
                  title={`As-is: ${p.approved_as_is_count}`}
                />
              )}
              {editPct > 0 && (
                <div
                  className="bg-[#D97706]"
                  style={{ width: `${editPct}%` }}
                  title={`Edited: ${p.approved_edited_count}`}
                />
              )}
              {rejPct > 0 && (
                <div
                  className="bg-[#C9342D]"
                  style={{ width: `${rejPct}%` }}
                  title={`Rejected: ${p.rejected_count}`}
                />
              )}
            </div>
          );
        },
      },
    ],
    [],
  );

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-xl font-bold tracking-tight text-[#1D1D1F] flex items-center gap-2">
            <Activity className="h-5 w-5 text-[#5E5CE6]" />
            AI Activity &amp; Health
          </h1>
          <p className="text-xs text-[#6E6E73] mt-0.5">
            Agent traces, Lex draft trust metrics, and declarative YAML evaluation scorecard
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <span
            className={`inline-flex items-center rounded-full border px-3 py-1 text-[11px] font-semibold ${phoenixBadge.cls}`}
          >
            {phoenixBadge.label}
          </span>

          <select
            aria-label="Filter by agent"
            value={agentFilter}
            onChange={(e) => setAgentFilter(e.target.value)}
            className="rounded-xl border border-black/[0.08] bg-white px-3 py-1.5 text-xs font-medium text-[#1D1D1F] shadow-2xs focus:border-[#0066CC] focus:outline-none"
          >
            <option value="ALL">All Agents</option>
            {agentOptions.map((name) => (
              <option key={name} value={name}>
                Agent: {name}
              </option>
            ))}
          </select>

          <button
            type="button"
            onClick={() => void loadData()}
            disabled={loading}
            className="apple-press inline-flex items-center gap-1.5 rounded-full border border-black/[0.08] bg-white px-3.5 py-1.5 text-xs font-medium text-[#1D1D1F] hover:bg-[#FBFBFD] shadow-2xs disabled:opacity-45"
          >
            <RefreshCw className={`h-3.5 w-3.5 text-[#6E6E73] ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>
      </div>

      {/* KPI Strip */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 tabular-nums">
        <div className="apple-card rounded-2xl p-4">
          <div className="flex items-center justify-between text-xs text-[#6E6E73]">
            <span>Agent Traces</span>
            <Cpu className="h-4 w-4 text-[#5E5CE6]" />
          </div>
          <div className="mt-1.5 text-2xl font-bold tracking-tight text-[#1D1D1F]">{traces.length}</div>
          <div className="mt-1 text-[11px] text-[#6E6E73]">
            Avg latency: <span className="font-semibold text-[#1D1D1F]">{kpis.avgLatency} ms</span>
          </div>
        </div>

        <div className="apple-card rounded-2xl p-4">
          <div className="flex items-center justify-between text-xs text-[#6E6E73]">
            <span>LLM Spend (INR)</span>
            <Coins className="h-4 w-4 text-[#B46900]" />
          </div>
          <div className="mt-1.5 text-2xl font-bold tracking-tight text-[#1D1D1F]">
            {formatInrMicro(kpis.totalCost)}
          </div>
          <div className="mt-1 text-[11px] text-[#6E6E73]">
            {kpis.totalInTokens.toLocaleString()} in · {kpis.totalOutTokens.toLocaleString()} out
            tokens
          </div>
        </div>

        <div className="apple-card rounded-2xl p-4">
          <div className="flex items-center justify-between text-xs text-[#6E6E73]">
            <span>Draft Edit-Free Rate</span>
            <ShieldCheck className="h-4 w-4 text-[#1B6B43]" />
          </div>
          <div className="mt-1.5 text-2xl font-bold tracking-tight text-[#1D1D1F]">
            {kpis.editFreeRate !== null ? `${kpis.editFreeRate}%` : '—'}
          </div>
          <div className="mt-1 text-[11px] text-[#6E6E73]">
            {kpis.totalDecisions} decision(s) ({kpis.totalAsIs} as-is · {kpis.totalEdited} edited ·{' '}
            {kpis.totalRejected} rejected)
          </div>
        </div>

        <div className="apple-card rounded-2xl p-4">
          <div className="flex items-center justify-between text-xs text-[#6E6E73]">
            <span>Latest Eval Pass Rate</span>
            <FlaskConical className="h-4 w-4 text-[#5E5CE6]" />
          </div>
          <div className="mt-1.5 text-2xl font-bold tracking-tight text-[#1D1D1F]">
            {activeRun ? `${activeRun.pass_rate}%` : 'Not Run'}
          </div>
          <div className="mt-1 text-[11px] text-[#6E6E73]">
            {activeRun
              ? `${activeRun.passed_cases}/${activeRun.total_cases} cases passed (${activeRun.mode})`
              : `${evalSuite?.total_cases ?? 0} YAML cases ready`}
          </div>
        </div>
      </div>

      {/* Section 1: Lex Draft Trust Metrics (Autonomy Policies) */}
      <section className="apple-card rounded-2xl overflow-hidden">
        <div className="px-5 py-3.5 border-b border-black/[0.06] flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="text-sm font-bold text-[#1D1D1F]">
              Lex Draft Trust Metrics (Autonomy Policies)
            </h2>
            <p className="text-xs text-[#6E6E73]">
              Tracks Inventory Manager draft reviews per email category (all MVP drafts require
              human approval)
            </p>
          </div>
          {kpis.totalDecisions > 0 && (
            <div className="flex items-center gap-3 text-[11px] font-semibold tabular-nums">
              <span className="text-[#1B6B43]">As-Is: {kpis.totalAsIs}</span>
              <span className="text-[#945600]">Edited: {kpis.totalEdited}</span>
              <span className="text-[#B82923]">Rejected: {kpis.totalRejected}</span>
            </div>
          )}
        </div>

        <DataTable
          embedded
          storageKey="invplan_col_widths_ai_health"
          ariaLabel="Autonomy Policies"
          columns={policyColumns}
          rows={policies}
          rowKey={(p) => p.id}
          loading={loading}
          maxHeight="380px"
          emptyMessage="No autonomy policy rows found."
          rowClassName={() => 'hover:bg-black/[0.02]'}
        />
      </section>

      {/* Section 2: 1-Click YAML Eval Runner & Scorecard */}
      <section className="apple-card rounded-2xl overflow-hidden">
        <div className="px-5 py-3.5 border-b border-black/[0.06] flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div>
            <h2 className="text-sm font-bold text-[#1D1D1F] flex items-center gap-2">
              <FlaskConical className="h-4 w-4 text-[#5E5CE6]" />
              YAML Eval Suite &amp; Scorecard
              {evalSuite && (
                <span className="rounded-full bg-black/[0.05] px-2.5 py-0.5 text-[11px] font-semibold text-[#6E6E73] tabular-nums">
                  {evalSuite.suite_name} · {evalSuite.total_cases} cases
                </span>
              )}
            </h2>
            {evalSuite?.description && (
              <p className="text-xs text-[#6E6E73] mt-0.5">{evalSuite.description}</p>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {evalRuns.length > 1 && (
              <select
                aria-label="Select eval run history"
                value={activeRun?.id ?? ''}
                onChange={(e) => setSelectedRunId(Number(e.target.value))}
                className="rounded-xl border border-black/[0.08] bg-white px-3 py-1.5 text-xs text-[#1D1D1F] shadow-2xs focus:border-[#5E5CE6] focus:outline-none"
              >
                {evalRuns.map((r) => (
                  <option key={r.id ?? r.timestamp} value={r.id ?? ''}>
                    Run #{r.id} · {r.passed_cases}/{r.total_cases} ({r.pass_rate}%) · {r.mode}
                  </option>
                ))}
              </select>
            )}

            <select
              aria-label="Eval execution mode"
              value={evalMode}
              onChange={(e) => setEvalMode(e.target.value as 'offline' | 'live')}
              className="rounded-xl border border-black/[0.08] bg-white px-3 py-1.5 text-xs font-medium text-[#1D1D1F] shadow-2xs focus:border-[#5E5CE6] focus:outline-none"
            >
              <option value="offline">Offline (Deterministic Guardrails)</option>
              <option value="live">Live Gemini (gemini-3.5-flash-lite)</option>
            </select>

            <button
              type="button"
              onClick={() => void handleRunEvals()}
              disabled={runningEvals}
              className="apple-press inline-flex items-center gap-1.5 rounded-full bg-[#5E5CE6] px-4 py-1.5 text-xs font-semibold text-white hover:bg-[#4B48C4] shadow-2xs disabled:opacity-45"
            >
              <Play className="h-3.5 w-3.5" />
              {runningEvals ? 'Running Evals…' : 'Run Evals'}
            </button>
          </div>
        </div>

        {activeRun ? (
          <div className="p-5 space-y-3">
            <div
              className={`flex flex-wrap items-center justify-between gap-2 rounded-xl border px-4 py-2.5 text-xs tabular-nums ${
                activeRun.passed_cases === activeRun.total_cases
                  ? 'border-[#1F7A4D]/22 bg-[#1F7A4D]/[0.07] text-[#1B6B43]'
                  : 'border-[#B46900]/24 bg-[#B46900]/[0.07] text-[#945600]'
              }`}
            >
              <div className="flex items-center gap-2 font-semibold">
                {activeRun.passed_cases === activeRun.total_cases ? (
                  <CheckCircle2 className="h-4 w-4 text-[#1B6B43]" />
                ) : (
                  <XCircle className="h-4 w-4 text-[#B82923]" />
                )}
                <span>
                  Score: {activeRun.passed_cases}/{activeRun.total_cases} passed (
                  {activeRun.pass_rate}%)
                </span>
              </div>
              <div className="text-[11px] opacity-85">
                Model: <span className="font-semibold">{activeRun.model_name}</span> · Mode:{' '}
                <span className="font-semibold">{activeRun.mode}</span> ·{' '}
                {formatSimDateTime(activeRun.timestamp)}
              </div>
            </div>

            <div className="divide-y divide-black/[0.06] rounded-xl border border-black/[0.08] overflow-hidden">
              {activeRun.results.map((c) => {
                const isOpen = !!expandedEvalCases[c.id];
                return (
                  <div key={c.id} className="bg-white">
                    <button
                      type="button"
                      onClick={() => toggleEvalCase(c.id)}
                      className="w-full px-4 py-2.5 flex items-start justify-between gap-3 text-left hover:bg-[#FBFBFD] transition-colors"
                    >
                      <div className="flex items-start gap-2.5 min-w-0">
                        <span
                          className={`mt-0.5 inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-bold ${
                            c.passed
                              ? 'bg-[#1F7A4D]/12 text-[#1B6B43]'
                              : 'bg-[#C9342D]/12 text-[#B82923]'
                          }`}
                        >
                          {c.passed ? 'PASS' : 'FAIL'}
                        </span>
                        <div className="min-w-0">
                          <div className="text-xs font-semibold text-[#1D1D1F] flex flex-wrap items-center gap-2">
                            <span>{c.name}</span>
                            <span className="font-mono text-[10px] text-[#86868B]">{c.id}</span>
                          </div>
                          <div
                            className={`text-[11px] mt-0.5 ${
                              c.passed ? 'text-[#6E6E73]' : 'text-[#B82923] font-medium'
                            }`}
                          >
                            {c.reason}
                          </div>
                        </div>
                      </div>

                      <div className="flex items-center gap-2 shrink-0 text-[11px] text-[#6E6E73] tabular-nums">
                        <span
                          className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${taskBadgeClass(
                            c.task,
                          )}`}
                        >
                          {c.task}
                        </span>
                        <span>{c.latency_ms} ms</span>
                        {isOpen ? (
                          <ChevronDown className="h-4 w-4 text-[#86868B]" />
                        ) : (
                          <ChevronRight className="h-4 w-4 text-[#86868B]" />
                        )}
                      </div>
                    </button>

                    {isOpen && (
                      <div className="anim-page-enter border-t border-black/[0.06] bg-[#FBFBFD] px-4 py-3 space-y-2 text-[11px]">
                        {c.assertions.length > 0 && (
                          <div className="space-y-1">
                            <div className="font-semibold text-[#1D1D1F]">Assertions</div>
                            {c.assertions.map((a, idx) => (
                              <div
                                key={`${c.id}-${a.field}-${idx}`}
                                className={`flex items-center justify-between gap-2 rounded-lg border px-2.5 py-1 ${
                                  a.passed
                                    ? 'border-[#1F7A4D]/22 bg-white text-[#1B6B43]'
                                    : 'border-[#C9342D]/22 bg-[#C9342D]/[0.05] text-[#B82923]'
                                }`}
                              >
                                <span className="font-mono">{a.message}</span>
                                <span className="font-bold">{a.passed ? 'OK' : 'FAIL'}</span>
                              </div>
                            ))}
                          </div>
                        )}
                        <div>
                          <div className="font-semibold text-[#1D1D1F] mb-1">Agent Output</div>
                          <pre className="overflow-x-auto rounded-xl border border-black/[0.08] bg-white p-2.5 text-[10px] text-[#1D1D1F]">
                            {JSON.stringify(c.output, null, 2)}
                          </pre>
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        ) : (
          <div className="p-5 space-y-2">
            <p className="text-xs text-[#6E6E73]">
              No eval run recorded yet. Click <span className="font-semibold text-[#1D1D1F]">Run Evals</span> to
              execute all {evalSuite?.total_cases ?? 0} YAML cases and persist a scorecard.
            </p>
            {evalSuite && (
              <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-2.5 pt-1">
                {evalSuite.cases.map((c) => (
                  <div
                    key={c.id}
                    className="rounded-xl border border-black/[0.07] bg-[#FBFBFD] px-3 py-2.5 text-xs"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-mono text-[10px] text-[#86868B]">{c.id}</span>
                      <span
                        className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${taskBadgeClass(
                          c.task,
                        )}`}
                      >
                        {c.task}
                      </span>
                    </div>
                    <div className="mt-1 font-medium text-[#1D1D1F]">{c.name}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </section>

      {/* Section 3: Recent Agent Traces Feed */}
      <section className="apple-card rounded-2xl overflow-hidden">
        <div className="px-5 py-3.5 border-b border-black/[0.06] flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="text-sm font-bold text-[#1D1D1F]">Recent Agent Activity Feed</h2>
            <p className="text-xs text-[#6E6E73]">
              Plain-English execution log with latency, token usage, INR cost, and raw
              prompt/output inspection
            </p>
          </div>

          <select
            aria-label="Filter traces by task type"
            value={taskFilter}
            onChange={(e) => setTaskFilter(e.target.value)}
            className="rounded-xl border border-black/[0.08] bg-white px-3 py-1.5 text-xs font-medium text-[#1D1D1F] shadow-2xs focus:border-[#0066CC] focus:outline-none"
          >
            <option value="ALL">All Task Types</option>
            <option value="copilot_query">copilot_query</option>
            <option value="email_triage">email_triage</option>
            <option value="invoice_extraction">invoice_extraction</option>
            <option value="email_draft">email_draft</option>
            <option value="stock_request_triage">stock_request_triage</option>
            <option value="reminder_wakeup">reminder_wakeup</option>
          </select>
        </div>

        <div className="divide-y divide-black/[0.05]">
          {traces.length === 0 ? (
            <div className="p-8 text-center text-xs text-[#6E6E73]">
              No agent traces recorded yet. Trigger an email triage, PDF invoice upload, PO draft,
              or click <span className="font-semibold text-[#1D1D1F]">Run Evals</span> above.
            </div>
          ) : (
            traces.map((t) => {
              const expanded = !!expandedTraceIds[t.id];
              const lowConf = t.confidence !== null && t.confidence < 0.85;
              return (
                <div key={t.id} className="p-4 hover:bg-black/[0.015] transition-colors">
                  <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
                    <div className="space-y-1 min-w-0">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="rounded-full bg-[#5E5CE6] px-2 py-0.5 text-[10px] font-bold uppercase text-white">
                          {t.agent_name}
                        </span>
                        <span
                          className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${taskBadgeClass(
                            t.task_type,
                          )}`}
                        >
                          {t.task_type}
                        </span>
                        {t.confidence !== null && (
                          <span
                            className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold tabular-nums ${
                              lowConf
                                ? 'bg-[#B46900]/10 text-[#945600] border-[#B46900]/24'
                                : 'bg-[#1F7A4D]/10 text-[#1B6B43] border-[#1F7A4D]/22'
                            }`}
                          >
                            Conf: {t.confidence.toFixed(2)}
                            {lowConf ? ' (<0.85 Escalated)' : ''}
                          </span>
                        )}
                        <span className="text-[11px] text-[#86868B] tabular-nums">
                          {formatSimDateTime(t.timestamp)}
                        </span>
                        {t.tool_calls.length > 0 && (
                          <span className="inline-flex items-center gap-1 rounded-full border border-[#0066CC]/24 bg-[#0066CC]/10 px-2 py-0.5 text-[10px] font-semibold text-[#0058B3] tabular-nums">
                            <Wrench className="h-2.5 w-2.5" />
                            {t.tool_calls.length} tool call{t.tool_calls.length === 1 ? '' : 's'}
                          </span>
                        )}
                      </div>

                      <div className="text-xs font-semibold text-[#1D1D1F]">
                        {t.trigger_summary}
                      </div>
                      {t.output_summary && (
                        <div className="text-xs text-[#6E6E73]">{t.output_summary}</div>
                      )}
                      {t.input_summary && (
                        <div className="text-[11px] text-[#86868B]">{t.input_summary}</div>
                      )}
                    </div>

                    <div className="flex flex-wrap items-center gap-3 shrink-0 text-[11px] text-[#6E6E73] tabular-nums">
                      <span>{t.latency_ms} ms</span>
                      <span>
                        {t.input_tokens}/{t.output_tokens} tok
                      </span>
                      <span className="font-semibold text-[#1D1D1F]">
                        {formatInrMicro(t.cost_inr)}
                      </span>
                      <button
                        type="button"
                        onClick={() => toggleTrace(t.id)}
                        className="apple-press inline-flex items-center gap-1 rounded-full border border-black/[0.10] bg-white px-2.5 py-1 text-[11px] font-semibold text-[#1D1D1F] hover:bg-[#FBFBFD]"
                      >
                        {expanded ? (
                          <>
                            <ChevronDown className="h-3 w-3" /> Hide Details
                          </>
                        ) : (
                          <>
                            <ChevronRight className="h-3 w-3" />{' '}
                            {t.tool_calls.length > 0 ? 'Tools & Raw' : 'Raw Prompt/Output'}
                          </>
                        )}
                      </button>
                    </div>
                  </div>

                  {expanded && (
                    <div className="anim-page-enter mt-3 space-y-3 text-[11px]">
                      {t.tool_calls.length > 0 && (
                        <div className="space-y-2">
                          <div className="font-semibold text-[#1D1D1F] flex items-center gap-1.5">
                            <Wrench className="h-3.5 w-3.5 text-[#0066CC]" />
                            Tool Execution Steps ({t.tool_calls.length})
                          </div>
                          <div className="space-y-2">
                            {t.tool_calls.map((tc, idx) => {
                              const toolName =
                                typeof tc.tool === 'string'
                                  ? tc.tool
                                  : typeof tc.tool_name === 'string'
                                    ? tc.tool_name
                                    : typeof tc.name === 'string'
                                      ? tc.name
                                      : 'unknown_tool';
                              const durationMs =
                                typeof tc.duration_ms === 'number' ? tc.duration_ms : null;
                              const errorText =
                                typeof tc.error === 'string' && tc.error.trim().length > 0
                                  ? tc.error
                                  : null;
                              const argsObj =
                                tc.args && typeof tc.args === 'object' ? tc.args : {};
                              const resultSummary =
                                typeof tc.result_summary === 'string'
                                  ? tc.result_summary
                                  : tc.result !== undefined
                                    ? JSON.stringify(tc.result)
                                    : '—';

                              return (
                                <div
                                  key={`${t.id}-tc-${idx}`}
                                  className={`rounded-xl border p-3 space-y-1.5 ${
                                    errorText
                                      ? 'border-[#C9342D]/25 bg-[#C9342D]/[0.04]'
                                      : 'border-black/[0.08] bg-[#FBFBFD]'
                                  }`}
                                >
                                  <div className="flex flex-wrap items-center justify-between gap-2">
                                    <div className="flex items-center gap-2">
                                      <span className="rounded-md bg-black/[0.06] px-1.5 py-0.5 text-[10px] font-bold text-[#6E6E73] tabular-nums">
                                        #{idx + 1}
                                      </span>
                                      <span className="font-mono font-bold text-[#1D1D1F]">
                                        {toolName}
                                      </span>
                                      {errorText && (
                                        <span className="rounded-full border border-[#C9342D]/30 bg-[#C9342D]/10 px-2 py-0.5 text-[10px] font-bold text-[#B82923]">
                                          ERROR
                                        </span>
                                      )}
                                    </div>
                                    {durationMs !== null && (
                                      <span className="text-[10px] font-medium text-[#6E6E73] tabular-nums">
                                        {durationMs} ms
                                      </span>
                                    )}
                                  </div>

                                  <div className="grid md:grid-cols-2 gap-2 text-[10px]">
                                    <div>
                                      <div className="font-semibold text-[#6E6E73] mb-0.5">
                                        Args
                                      </div>
                                      <pre className="overflow-x-auto rounded-lg border border-black/[0.06] bg-white px-2.5 py-1.5 text-[#1D1D1F] whitespace-pre-wrap break-words">
                                        {JSON.stringify(argsObj, null, 2)}
                                      </pre>
                                    </div>
                                    <div>
                                      <div className="font-semibold text-[#6E6E73] mb-0.5">
                                        {errorText ? 'Error' : 'Result Summary'}
                                      </div>
                                      <pre
                                        className={`overflow-x-auto rounded-lg border px-2.5 py-1.5 whitespace-pre-wrap break-words ${
                                          errorText
                                            ? 'border-[#C9342D]/20 bg-white text-[#B82923]'
                                            : 'border-black/[0.06] bg-white text-[#1D1D1F]'
                                        }`}
                                      >
                                        {errorText ?? resultSummary}
                                      </pre>
                                    </div>
                                  </div>
                                </div>
                              );
                            })}
                          </div>
                        </div>
                      )}

                      <div className="grid md:grid-cols-2 gap-3">
                        <div>
                          <div className="font-semibold text-[#1D1D1F] mb-1">Raw Prompt</div>
                          <pre className="max-h-64 overflow-auto rounded-xl border border-black/[0.08] bg-[#FBFBFD] p-3 text-[10px] text-[#1D1D1F] whitespace-pre-wrap">
                            {t.raw_prompt || '—'}
                          </pre>
                        </div>
                        <div>
                          <div className="font-semibold text-[#1D1D1F] mb-1">Raw Response</div>
                          <pre className="max-h-64 overflow-auto rounded-xl border border-black/[0.08] bg-[#FBFBFD] p-3 text-[10px] text-[#1D1D1F] whitespace-pre-wrap">
                            {t.raw_response || '—'}
                          </pre>
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              );
            })
          )}
        </div>
      </section>
    </div>
  );
}
