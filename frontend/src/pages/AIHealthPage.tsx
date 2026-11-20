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
      return 'bg-sky-100 text-sky-800 border-sky-200';
    case 'invoice_extraction':
      return 'bg-violet-100 text-violet-800 border-violet-200';
    case 'email_draft':
      return 'bg-amber-100 text-amber-800 border-amber-200';
    default:
      return 'bg-slate-100 text-slate-700 border-slate-200';
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
        cls: 'bg-emerald-50 text-emerald-700 border-emerald-200',
      };
    }
    if (mode === 'fallback_sqlite') {
      return {
        label: 'Phoenix: Fallback (SQLite)',
        cls: 'bg-amber-50 text-amber-800 border-amber-200',
      };
    }
    return {
      label: 'Phoenix: Off (SQLite Traces)',
      cls: 'bg-slate-100 text-slate-600 border-slate-200',
    };
  }, [statusInfo]);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-xl font-bold text-slate-900 flex items-center gap-2">
            <Activity className="h-5 w-5 text-indigo-600" />
            AI Activity &amp; Health
          </h1>
          <p className="text-xs text-slate-500 mt-0.5">
            Agent traces, Lex draft trust metrics, and declarative YAML evaluation scorecard
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <span
            className={`inline-flex items-center rounded-full border px-2.5 py-1 text-[11px] font-semibold ${phoenixBadge.cls}`}
          >
            {phoenixBadge.label}
          </span>

          <select
            aria-label="Filter by agent"
            value={agentFilter}
            onChange={(e) => setAgentFilter(e.target.value)}
            className="rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-medium text-slate-700"
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
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-50"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>
      </div>

      {/* KPI Strip */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <div className="rounded-xl border border-slate-200 bg-white p-4">
          <div className="flex items-center justify-between text-xs text-slate-500">
            <span>Agent Traces</span>
            <Cpu className="h-4 w-4 text-indigo-500" />
          </div>
          <div className="mt-1.5 text-2xl font-bold text-slate-900">{traces.length}</div>
          <div className="mt-1 text-[11px] text-slate-500">
            Avg latency: <span className="font-semibold text-slate-700">{kpis.avgLatency} ms</span>
          </div>
        </div>

        <div className="rounded-xl border border-slate-200 bg-white p-4">
          <div className="flex items-center justify-between text-xs text-slate-500">
            <span>LLM Spend (INR)</span>
            <Coins className="h-4 w-4 text-amber-500" />
          </div>
          <div className="mt-1.5 text-2xl font-bold text-slate-900">
            {formatInrMicro(kpis.totalCost)}
          </div>
          <div className="mt-1 text-[11px] text-slate-500">
            {kpis.totalInTokens.toLocaleString()} in · {kpis.totalOutTokens.toLocaleString()} out
            tokens
          </div>
        </div>

        <div className="rounded-xl border border-slate-200 bg-white p-4">
          <div className="flex items-center justify-between text-xs text-slate-500">
            <span>Draft Edit-Free Rate</span>
            <ShieldCheck className="h-4 w-4 text-emerald-600" />
          </div>
          <div className="mt-1.5 text-2xl font-bold text-slate-900">
            {kpis.editFreeRate !== null ? `${kpis.editFreeRate}%` : '—'}
          </div>
          <div className="mt-1 text-[11px] text-slate-500">
            {kpis.totalDecisions} decision(s) ({kpis.totalAsIs} as-is · {kpis.totalEdited} edited ·{' '}
            {kpis.totalRejected} rejected)
          </div>
        </div>

        <div className="rounded-xl border border-slate-200 bg-white p-4">
          <div className="flex items-center justify-between text-xs text-slate-500">
            <span>Latest Eval Pass Rate</span>
            <FlaskConical className="h-4 w-4 text-violet-600" />
          </div>
          <div className="mt-1.5 text-2xl font-bold text-slate-900">
            {activeRun ? `${activeRun.pass_rate}%` : 'Not Run'}
          </div>
          <div className="mt-1 text-[11px] text-slate-500">
            {activeRun
              ? `${activeRun.passed_cases}/${activeRun.total_cases} cases passed (${activeRun.mode})`
              : `${evalSuite?.total_cases ?? 0} YAML cases ready`}
          </div>
        </div>
      </div>

      {/* Section 1: Lex Draft Trust Metrics (Autonomy Policies) */}
      <section className="rounded-xl border border-slate-200 bg-white overflow-hidden">
        <div className="px-4 py-3 border-b border-slate-100 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="text-sm font-bold text-slate-900">
              Lex Draft Trust Metrics (Autonomy Policies)
            </h2>
            <p className="text-xs text-slate-500">
              Tracks Inventory Manager draft reviews per email category (all MVP drafts require
              human approval)
            </p>
          </div>
          {kpis.totalDecisions > 0 && (
            <div className="flex items-center gap-3 text-[11px] font-semibold">
              <span className="text-emerald-700">As-Is: {kpis.totalAsIs}</span>
              <span className="text-amber-700">Edited: {kpis.totalEdited}</span>
              <span className="text-red-700">Rejected: {kpis.totalRejected}</span>
            </div>
          )}
        </div>

        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-slate-200 text-xs">
            <thead className="bg-slate-50 text-slate-600 font-semibold">
              <tr>
                <th className="px-4 py-2.5 text-left">Category</th>
                <th className="px-4 py-2.5 text-left">Policy Mode</th>
                <th className="px-4 py-2.5 text-right">Approved As-Is</th>
                <th className="px-4 py-2.5 text-right">Approved Edited</th>
                <th className="px-4 py-2.5 text-right">Rejected</th>
                <th className="px-4 py-2.5 text-right">Edit-Free %</th>
                <th className="px-4 py-2.5 text-left w-44">Trust Distribution</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {policies.length === 0 ? (
                <tr>
                  <td colSpan={7} className="px-4 py-6 text-center text-slate-500">
                    No autonomy policy rows found.
                  </td>
                </tr>
              ) : (
                policies.map((p) => {
                  const total = p.total_decisions;
                  const asIsPct = total > 0 ? (p.approved_as_is_count / total) * 100 : 0;
                  const editPct = total > 0 ? (p.approved_edited_count / total) * 100 : 0;
                  const rejPct = total > 0 ? (p.rejected_count / total) * 100 : 0;
                  return (
                    <tr key={p.id} className="hover:bg-slate-50">
                      <td className="px-4 py-2.5 font-semibold text-slate-900">
                        {p.action_category}
                      </td>
                      <td className="px-4 py-2.5">
                        <span
                          className={`inline-flex rounded-full border px-2 py-0.5 text-[10px] font-semibold ${
                            p.mode === 'AUTO_SEND'
                              ? 'bg-emerald-100 text-emerald-800 border-emerald-200'
                              : 'bg-slate-100 text-slate-700 border-slate-200'
                          }`}
                        >
                          {p.mode}
                        </span>
                      </td>
                      <td className="px-4 py-2.5 text-right font-semibold text-emerald-700">
                        {p.approved_as_is_count}
                      </td>
                      <td className="px-4 py-2.5 text-right font-semibold text-amber-700">
                        {p.approved_edited_count}
                      </td>
                      <td className="px-4 py-2.5 text-right font-semibold text-red-700">
                        {p.rejected_count}
                      </td>
                      <td className="px-4 py-2.5 text-right font-bold text-slate-900">
                        {p.edit_free_approval_rate !== null
                          ? `${p.edit_free_approval_rate}%`
                          : '—'}
                      </td>
                      <td className="px-4 py-2.5">
                        {total === 0 ? (
                          <div className="h-2 w-full rounded-full bg-slate-100" />
                        ) : (
                          <div className="flex h-2 w-full overflow-hidden rounded-full bg-slate-100">
                            {asIsPct > 0 && (
                              <div
                                className="bg-emerald-500"
                                style={{ width: `${asIsPct}%` }}
                                title={`As-is: ${p.approved_as_is_count}`}
                              />
                            )}
                            {editPct > 0 && (
                              <div
                                className="bg-amber-400"
                                style={{ width: `${editPct}%` }}
                                title={`Edited: ${p.approved_edited_count}`}
                              />
                            )}
                            {rejPct > 0 && (
                              <div
                                className="bg-red-500"
                                style={{ width: `${rejPct}%` }}
                                title={`Rejected: ${p.rejected_count}`}
                              />
                            )}
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </section>

      {/* Section 2: 1-Click YAML Eval Runner & Scorecard */}
      <section className="rounded-xl border border-slate-200 bg-white overflow-hidden">
        <div className="px-4 py-3 border-b border-slate-100 flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div>
            <h2 className="text-sm font-bold text-slate-900 flex items-center gap-2">
              <FlaskConical className="h-4 w-4 text-violet-600" />
              YAML Eval Suite &amp; Scorecard
              {evalSuite && (
                <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-semibold text-slate-600">
                  {evalSuite.suite_name} · {evalSuite.total_cases} cases
                </span>
              )}
            </h2>
            {evalSuite?.description && (
              <p className="text-xs text-slate-500 mt-0.5">{evalSuite.description}</p>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {evalRuns.length > 1 && (
              <select
                aria-label="Select eval run history"
                value={activeRun?.id ?? ''}
                onChange={(e) => setSelectedRunId(Number(e.target.value))}
                className="rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-xs text-slate-700"
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
              className="rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-medium text-slate-700"
            >
              <option value="offline">Offline (Deterministic Guardrails)</option>
              <option value="live">Live Gemini (gemini-3.5-flash-lite)</option>
            </select>

            <button
              type="button"
              onClick={() => void handleRunEvals()}
              disabled={runningEvals}
              className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-indigo-700 disabled:opacity-50"
            >
              <Play className="h-3.5 w-3.5" />
              {runningEvals ? 'Running Evals…' : 'Run Evals'}
            </button>
          </div>
        </div>

        {activeRun ? (
          <div className="p-4 space-y-3">
            <div
              className={`flex flex-wrap items-center justify-between gap-2 rounded-lg border px-3.5 py-2.5 text-xs ${
                activeRun.passed_cases === activeRun.total_cases
                  ? 'border-emerald-200 bg-emerald-50 text-emerald-900'
                  : 'border-amber-200 bg-amber-50 text-amber-900'
              }`}
            >
              <div className="flex items-center gap-2 font-semibold">
                {activeRun.passed_cases === activeRun.total_cases ? (
                  <CheckCircle2 className="h-4 w-4 text-emerald-600" />
                ) : (
                  <XCircle className="h-4 w-4 text-red-600" />
                )}
                <span>
                  Score: {activeRun.passed_cases}/{activeRun.total_cases} passed (
                  {activeRun.pass_rate}%)
                </span>
              </div>
              <div className="text-[11px] opacity-80">
                Model: <span className="font-semibold">{activeRun.model_name}</span> · Mode:{' '}
                <span className="font-semibold">{activeRun.mode}</span> ·{' '}
                {formatSimDateTime(activeRun.timestamp)}
              </div>
            </div>

            <div className="divide-y divide-slate-200 rounded-lg border border-slate-200">
              {activeRun.results.map((c) => {
                const isOpen = !!expandedEvalCases[c.id];
                return (
                  <div key={c.id} className="bg-white">
                    <button
                      type="button"
                      onClick={() => toggleEvalCase(c.id)}
                      className="w-full px-3.5 py-2.5 flex items-start justify-between gap-3 text-left hover:bg-slate-50"
                    >
                      <div className="flex items-start gap-2.5 min-w-0">
                        <span
                          className={`mt-0.5 inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-bold ${
                            c.passed
                              ? 'bg-emerald-100 text-emerald-800'
                              : 'bg-red-100 text-red-800'
                          }`}
                        >
                          {c.passed ? 'PASS' : 'FAIL'}
                        </span>
                        <div className="min-w-0">
                          <div className="text-xs font-semibold text-slate-900 flex flex-wrap items-center gap-2">
                            <span>{c.name}</span>
                            <span className="font-mono text-[10px] text-slate-400">{c.id}</span>
                          </div>
                          <div
                            className={`text-[11px] mt-0.5 ${
                              c.passed ? 'text-slate-600' : 'text-red-700 font-medium'
                            }`}
                          >
                            {c.reason}
                          </div>
                        </div>
                      </div>

                      <div className="flex items-center gap-2 shrink-0 text-[11px] text-slate-500">
                        <span
                          className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${taskBadgeClass(
                            c.task,
                          )}`}
                        >
                          {c.task}
                        </span>
                        <span>{c.latency_ms} ms</span>
                        {isOpen ? (
                          <ChevronDown className="h-4 w-4 text-slate-400" />
                        ) : (
                          <ChevronRight className="h-4 w-4 text-slate-400" />
                        )}
                      </div>
                    </button>

                    {isOpen && (
                      <div className="border-t border-slate-100 bg-slate-50 px-4 py-3 space-y-2 text-[11px]">
                        {c.assertions.length > 0 && (
                          <div className="space-y-1">
                            <div className="font-semibold text-slate-700">Assertions</div>
                            {c.assertions.map((a, idx) => (
                              <div
                                key={`${c.id}-${a.field}-${idx}`}
                                className={`flex items-center justify-between gap-2 rounded border px-2.5 py-1 ${
                                  a.passed
                                    ? 'border-emerald-200 bg-white text-emerald-800'
                                    : 'border-red-200 bg-red-50 text-red-800'
                                }`}
                              >
                                <span className="font-mono">{a.message}</span>
                                <span className="font-bold">{a.passed ? 'OK' : 'FAIL'}</span>
                              </div>
                            ))}
                          </div>
                        )}
                        <div>
                          <div className="font-semibold text-slate-700 mb-1">Agent Output</div>
                          <pre className="overflow-x-auto rounded border border-slate-200 bg-white p-2 text-[10px] text-slate-700">
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
          <div className="p-4 space-y-2">
            <p className="text-xs text-slate-600">
              No eval run recorded yet. Click <span className="font-semibold">Run Evals</span> to
              execute all {evalSuite?.total_cases ?? 0} YAML cases and persist a scorecard.
            </p>
            {evalSuite && (
              <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-2 pt-1">
                {evalSuite.cases.map((c) => (
                  <div
                    key={c.id}
                    className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-mono text-[10px] text-slate-500">{c.id}</span>
                      <span
                        className={`rounded-full border px-1.5 py-0.5 text-[10px] font-semibold ${taskBadgeClass(
                          c.task,
                        )}`}
                      >
                        {c.task}
                      </span>
                    </div>
                    <div className="mt-1 font-medium text-slate-800">{c.name}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </section>

      {/* Section 3: Recent Agent Traces Feed */}
      <section className="rounded-xl border border-slate-200 bg-white overflow-hidden">
        <div className="px-4 py-3 border-b border-slate-100 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="text-sm font-bold text-slate-900">Recent Agent Activity Feed</h2>
            <p className="text-xs text-slate-500">
              Plain-English execution log with latency, token usage, INR cost, and raw
              prompt/output inspection
            </p>
          </div>

          <select
            aria-label="Filter traces by task type"
            value={taskFilter}
            onChange={(e) => setTaskFilter(e.target.value)}
            className="rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-medium text-slate-700"
          >
            <option value="ALL">All Task Types</option>
            <option value="email_triage">email_triage</option>
            <option value="invoice_extraction">invoice_extraction</option>
            <option value="email_draft">email_draft</option>
          </select>
        </div>

        <div className="divide-y divide-slate-200">
          {traces.length === 0 ? (
            <div className="p-8 text-center text-xs text-slate-500">
              No agent traces recorded yet. Trigger an email triage, PDF invoice upload, PO draft,
              or click <span className="font-semibold">Run Evals</span> above.
            </div>
          ) : (
            traces.map((t) => {
              const expanded = !!expandedTraceIds[t.id];
              const lowConf = t.confidence !== null && t.confidence < 0.85;
              return (
                <div key={t.id} className="p-4 hover:bg-slate-50/70 transition">
                  <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
                    <div className="space-y-1 min-w-0">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="rounded bg-slate-900 px-1.5 py-0.5 text-[10px] font-bold uppercase text-white">
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
                            className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${
                              lowConf
                                ? 'bg-amber-100 text-amber-800 border-amber-200'
                                : 'bg-emerald-50 text-emerald-700 border-emerald-200'
                            }`}
                          >
                            Conf: {t.confidence.toFixed(2)}
                            {lowConf ? ' (<0.85 Escalated)' : ''}
                          </span>
                        )}
                        <span className="text-[11px] text-slate-400">
                          {formatSimDateTime(t.timestamp)}
                        </span>
                      </div>

                      <div className="text-xs font-semibold text-slate-900">
                        {t.trigger_summary}
                      </div>
                      {t.output_summary && (
                        <div className="text-xs text-slate-700">{t.output_summary}</div>
                      )}
                      {t.input_summary && (
                        <div className="text-[11px] text-slate-500">{t.input_summary}</div>
                      )}
                    </div>

                    <div className="flex flex-wrap items-center gap-3 shrink-0 text-[11px] text-slate-600">
                      <span>{t.latency_ms} ms</span>
                      <span>
                        {t.input_tokens}/{t.output_tokens} tok
                      </span>
                      <span className="font-semibold text-slate-900">
                        {formatInrMicro(t.cost_inr)}
                      </span>
                      <button
                        type="button"
                        onClick={() => toggleTrace(t.id)}
                        className="inline-flex items-center gap-1 rounded border border-slate-300 bg-white px-2 py-1 text-[11px] font-semibold text-slate-700 hover:bg-slate-100"
                      >
                        {expanded ? (
                          <>
                            <ChevronDown className="h-3 w-3" /> Hide Raw
                          </>
                        ) : (
                          <>
                            <ChevronRight className="h-3 w-3" /> Raw Prompt / Output
                          </>
                        )}
                      </button>
                    </div>
                  </div>

                  {expanded && (
                    <div className="mt-3 grid md:grid-cols-2 gap-3 text-[11px]">
                      <div>
                        <div className="font-semibold text-slate-700 mb-1">Raw Prompt</div>
                        <pre className="max-h-64 overflow-auto rounded-lg border border-slate-200 bg-slate-50 p-2.5 text-[10px] text-slate-700 whitespace-pre-wrap">
                          {t.raw_prompt || '—'}
                        </pre>
                      </div>
                      <div>
                        <div className="font-semibold text-slate-700 mb-1">Raw Response</div>
                        <pre className="max-h-64 overflow-auto rounded-lg border border-slate-200 bg-slate-50 p-2.5 text-[10px] text-slate-700 whitespace-pre-wrap">
                          {t.raw_response || '—'}
                        </pre>
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
