import { useCallback, useEffect, useRef, useState } from 'react';
import {
  AlertTriangle,
  Bell,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  ClipboardList,
  MessageSquarePlus,
  Package,
  RefreshCw,
  RotateCcw,
  Send,
  Sparkles,
  Trash2,
  Wrench,
  X,
  XCircle,
} from 'lucide-react';
import { api } from '../api/client';
import type {
  CopilotMessageItem,
  CopilotSessionSummary,
} from '../api/types';
import { formatSimDateTime } from '../utils/format';
import { CopilotChartCard } from './CopilotChartCard';
import { CopilotMarkdown } from './CopilotMarkdown';

interface CopilotDrawerProps {
  open: boolean;
  refreshNonce: number;
  onClose: () => void;
  onMutated: () => void;
  onNotify: (kind: 'success' | 'error' | 'info', title: string, detail?: string) => void;
  onNavigatePart: (partId: number) => void;
  onNavigatePo: (poId: number) => void;
  onNavigateAlert: (alertId: number) => void;
}

const STARTER_PROMPTS = [
  {
    label: 'Stockout Urgency + Chart',
    prompt:
      'Which SKUs across inventory are currently in CRITICAL or WARNING stockout urgency? Show a bar chart comparing days to stockout against effective lead time + communication buffer.',
  },
  {
    label: 'ABC Spend Pareto + Stats',
    prompt:
      'Run an inventory ABC Pareto & spend statistical analysis across all 15 SKUs and render a chart of the top daily spend drivers.',
  },
  {
    label: 'Supplier Benchmark',
    prompt:
      'Compare Sup1, Sup2, and Sup3 on reliability score, average response delay, invoice accuracy, and memory notes, with a grouped comparison chart.',
  },
  {
    label: 'Blindspot & Open PO Audit',
    prompt:
      'Audit all unresolved dismissed alerts, draft POs in PENDING_DISPATCH_APPROVAL (why On Order shows 0), quarantined stock, and open PO bottlenecks.',
  },
  {
    label: 'Explain ROP/T & Burn Math',
    prompt:
      'Explain how ROP/T, Safety Stock Z-factors, 90-day EWMA Daily Burn (same-day withdrawal freeze vs rollover), and Stockout In urgency colors work in InvPlan.',
  },
];

export function CopilotDrawer({
  open,
  refreshNonce,
  onClose,
  onMutated,
  onNotify,
  onNavigatePart,
  onNavigatePo,
  onNavigateAlert,
}: CopilotDrawerProps) {
  const [sessions, setSessions] = useState<CopilotSessionSummary[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<number | null>(null);
  const [messages, setMessages] = useState<CopilotMessageItem[]>([]);
  const [loadingSession, setLoadingSession] = useState(false);
  const [sending, setSending] = useState(false);
  const [input, setInput] = useState('');

  const [busyActionMsgId, setBusyActionMsgId] = useState<number | null>(null);
  const [revisingMsgId, setRevisingMsgId] = useState<number | null>(null);
  const [revisionInput, setRevisionInput] = useState('');
  const [expandedTools, setExpandedTools] = useState<Record<number, boolean>>({});

  const bottomRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  const loadSessions = useCallback(async () => {
    try {
      const list = await api.listCopilotSessions();
      setSessions(list);
      setActiveSessionId((prev) => {
        if (prev !== null && list.some((s) => s.id === prev)) return prev;
        return list[0]?.id ?? null;
      });
    } catch {
      // Non-blocking on background refresh
    }
  }, []);

  const loadActiveMessages = useCallback(async (sessionId: number | null) => {
    if (sessionId === null) {
      setMessages([]);
      return;
    }
    setLoadingSession(true);
    try {
      const detail = await api.getCopilotSession(sessionId);
      setMessages(detail.messages);
    } catch {
      setMessages([]);
      setActiveSessionId(null);
    } finally {
      setLoadingSession(false);
    }
  }, []);

  useEffect(() => {
    if (open) {
      void loadSessions();
    }
  }, [open, refreshNonce, loadSessions]);

  useEffect(() => {
    if (open) {
      void loadActiveMessages(activeSessionId);
    }
  }, [open, activeSessionId, refreshNonce, loadActiveMessages]);

  useEffect(() => {
    if (open) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [open, messages.length, sending]);

  useEffect(() => {
    if (open) {
      window.setTimeout(() => inputRef.current?.focus(), 60);
    }
  }, [open]);

  const handleNewChat = () => {
    setActiveSessionId(null);
    setMessages([]);
    setRevisingMsgId(null);
    setRevisionInput('');
    window.setTimeout(() => inputRef.current?.focus(), 40);
  };

  const handleDeleteSession = async () => {
    if (activeSessionId === null) return;
    try {
      await api.deleteCopilotSession(activeSessionId);
      const remaining = sessions.filter((s) => s.id !== activeSessionId);
      setSessions(remaining);
      setActiveSessionId(remaining[0]?.id ?? null);
      if (remaining.length === 0) setMessages([]);
      onNotify('info', 'Deleted Copilot chat');
    } catch (err) {
      onNotify(
        'error',
        'Failed to delete chat',
        err instanceof Error ? err.message : String(err),
      );
    }
  };

  const sendQuery = async (questionText: string, reviseMessageId?: number | null) => {
    const clean = questionText.trim();
    if (!clean || sending) return;

    setSending(true);
    if (!reviseMessageId) {
      setInput('');
    }
    try {
      const res = await api.queryCopilot({
        question: clean,
        sessionId: activeSessionId,
        reviseMessageId: reviseMessageId ?? null,
      });
      setActiveSessionId(res.session.id);
      await loadSessions();
      const detail = await api.getCopilotSession(res.session.id);
      setMessages(detail.messages);
      if (reviseMessageId) {
        setRevisingMsgId(null);
        setRevisionInput('');
      }
    } catch (err) {
      onNotify(
        'error',
        'Copilot query failed',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setSending(false);
    }
  };

  const handleConfirmActions = async (messageId: number) => {
    setBusyActionMsgId(messageId);
    try {
      const res = await api.confirmCopilotActions(messageId);
      setMessages((prev) => prev.map((m) => (m.id === messageId ? res.message : m)));
      onNotify(
        'success',
        `Executed ${res.execution_results.length} Copilot action(s)`,
        res.execution_results.map((r) => r.summary).join(' | '),
      );
      onMutated();
    } catch (err) {
      onNotify(
        'error',
        'Action execution failed',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setBusyActionMsgId(null);
    }
  };

  const handleDeclineActions = async (messageId: number) => {
    setBusyActionMsgId(messageId);
    try {
      const res = await api.declineCopilotActions(messageId);
      setMessages((prev) => prev.map((m) => (m.id === messageId ? res.message : m)));
      onNotify('info', 'Proposed actions cancelled');
    } catch (err) {
      onNotify(
        'error',
        'Failed to cancel actions',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setBusyActionMsgId(null);
    }
  };

  if (!open) return null;

  return (
    <aside
      aria-label="Lex Manager Copilot"
      data-testid="copilot-drawer"
      className="fixed top-[53px] right-0 bottom-0 z-35 w-full sm:w-[460px] flex flex-col border-l border-black/[0.09] bg-[#FBFBFD]/96 backdrop-blur-xl shadow-[-12px_0_32px_rgba(0,0,0,0.08)]"
    >
      {/* Header */}
      <div className="flex items-center justify-between gap-2 border-b border-black/[0.08] bg-white/90 px-4 py-3">
        <div className="flex items-center gap-2 min-w-0">
          <div className="flex h-7 w-7 items-center justify-center rounded-xl bg-[#5E5CE6] text-white shadow-2xs shrink-0">
            <Sparkles className="h-3.5 w-3.5" />
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-1.5">
              <h2 className="text-sm font-bold tracking-tight text-[#1D1D1F]">
                Lex Copilot
              </h2>
              <span className="rounded-md border border-black/[0.10] bg-black/[0.04] px-1.5 py-0.5 font-mono text-[10px] font-semibold text-[#6E6E73]">
                Ctrl+L
              </span>
            </div>
            <p className="text-[11px] text-[#6E6E73] truncate">
              Analytics, charts &amp; human-approved actions
            </p>
          </div>
        </div>

        <div className="flex items-center gap-1 shrink-0">
          <button
            type="button"
            onClick={handleNewChat}
            title="New Copilot Chat"
            className="apple-press inline-flex items-center gap-1 rounded-lg border border-black/[0.09] bg-white px-2.5 py-1 text-[11px] font-semibold text-[#1D1D1F] hover:bg-black/[0.03]"
          >
            <MessageSquarePlus className="h-3.5 w-3.5 text-[#0066CC]" />
            New
          </button>
          {activeSessionId !== null && (
            <button
              type="button"
              onClick={() => void handleDeleteSession()}
              title="Delete current chat"
              aria-label="Delete current chat"
              className="apple-press rounded-lg p-1.5 text-[#86868B] hover:bg-[#C9342D]/10 hover:text-[#B82923]"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          )}
          <button
            type="button"
            onClick={onClose}
            aria-label="Close Copilot"
            className="apple-press rounded-full p-1.5 text-[#86868B] hover:bg-black/[0.06] hover:text-[#1D1D1F]"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      </div>

      {/* Session Switcher */}
      {sessions.length > 0 && (
        <div className="border-b border-black/[0.06] bg-white/70 px-4 py-2 flex items-center gap-2">
          <span className="text-[11px] font-semibold text-[#86868B] shrink-0">Session:</span>
          <select
            aria-label="Select Copilot session"
            value={activeSessionId ?? ''}
            onChange={(e) => {
              const val = e.target.value;
              setActiveSessionId(val ? Number(val) : null);
            }}
            className="flex-1 min-w-0 rounded-lg border border-black/[0.08] bg-white px-2.5 py-1 text-xs text-[#1D1D1F] focus:border-[#0066CC] focus:outline-none"
          >
            {activeSessionId === null && <option value="">Unsaved New Chat</option>}
            {sessions.map((s) => (
              <option key={s.id} value={s.id}>
                {s.title} ({s.message_count})
              </option>
            ))}
          </select>
        </div>
      )}

      {/* Messages Timeline */}
      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4">
        {loadingSession && messages.length === 0 ? (
          <div className="py-8 text-center text-xs text-[#6E6E73]">Loading conversation...</div>
        ) : messages.length === 0 ? (
          <div className="space-y-4 py-2">
            <div className="rounded-2xl border border-black/[0.07] bg-white p-4 space-y-1.5 shadow-2xs">
              <div className="text-xs font-bold text-[#1D1D1F]">
                Ask Lex anything about live inventory, POs, or suppliers
              </div>
              <p className="text-xs text-[#6E6E73] leading-relaxed">
                Lex runs read-only database lookups, Python statistical analysis, and interactive
                charts. When you ask Lex to take an action, it summarizes the plan here for your
                approval first.
              </p>
            </div>

            <div className="space-y-2">
              <div className="text-[11px] font-bold uppercase tracking-wider text-[#86868B]">
                Quick-Start Prompts
              </div>
              {STARTER_PROMPTS.map((sp) => (
                <button
                  key={sp.label}
                  type="button"
                  disabled={sending}
                  onClick={() => void sendQuery(sp.prompt)}
                  className="apple-press w-full text-left rounded-xl border border-black/[0.08] bg-white px-3.5 py-2.5 hover:border-[#0066CC]/35 hover:bg-[#0066CC]/[0.03] transition-colors space-y-0.5 disabled:opacity-50"
                >
                  <div className="text-xs font-semibold text-[#0058B3]">{sp.label}</div>
                  <div className="text-[11px] text-[#6E6E73] line-clamp-2">{sp.prompt}</div>
                </button>
              ))}
            </div>
          </div>
        ) : (
          messages.map((msg) => {
            if (msg.role === 'user') {
              return (
                <div key={msg.id} className="flex justify-end">
                  <div className="max-w-[88%] rounded-2xl rounded-br-sm bg-[#0066CC] px-3.5 py-2.5 text-[13px] text-white shadow-2xs space-y-1">
                    <div className="whitespace-pre-wrap leading-relaxed">{msg.content}</div>
                    <div className="text-[10px] text-white/75 text-right tabular-nums">
                      {formatSimDateTime(msg.created_at)}
                    </div>
                  </div>
                </div>
              );
            }

            const toolsOpen = !!expandedTools[msg.id];
            const isActionBusy = busyActionMsgId === msg.id;
            const isRevising = revisingMsgId === msg.id;

            return (
              <div key={msg.id} className="space-y-2">
                <div className="rounded-2xl rounded-bl-sm border border-black/[0.08] bg-white p-3.5 shadow-2xs space-y-3">
                  {/* Degraded Notice */}
                  {msg.degraded && (
                    <div className="rounded-xl border border-[#B46900]/30 bg-[#B46900]/[0.07] px-3 py-2 text-xs text-[#945600] flex items-start gap-2">
                      <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
                      <div>
                        <div className="font-bold">
                          Offline Degraded Mode ({msg.error_code ?? 'LLM_UNAVAILABLE'})
                        </div>
                        <div className="text-[11px] mt-0.5 opacity-90">
                          No inventory or PO state was modified.
                        </div>
                      </div>
                    </div>
                  )}

                  {/* Clean Rich Markdown Answer (Zero Asterisks) */}
                  <CopilotMarkdown content={msg.content} />

                  {/* Cited Metrics */}
                  {msg.cited_metrics.length > 0 && (
                    <div className="flex flex-wrap gap-1.5 pt-0.5">
                      {msg.cited_metrics.map((m, idx) => (
                        <span
                          key={`${m.name}-${idx}`}
                          className="inline-flex items-center gap-1 rounded-lg border border-black/[0.08] bg-[#FBFBFD] px-2 py-1 text-[11px] tabular-nums"
                        >
                          <span className="text-[#6E6E73]">{m.name}:</span>
                          <span className="font-bold text-[#1D1D1F]">{m.value}</span>
                        </span>
                      ))}
                    </div>
                  )}

                  {/* Charts & Diagrams */}
                  {msg.visuals.length > 0 && (
                    <div className="space-y-2.5">
                      {msg.visuals.map((vis, vIdx) => (
                        <CopilotChartCard key={`${msg.id}-vis-${vIdx}`} visual={vis} />
                      ))}
                    </div>
                  )}

                  {/* In-Chat Proposed Actions Card (Confirm / Revise / Cancel) */}
                  {msg.proposed_actions.length > 0 && (
                    <div
                      data-testid={`proposed-actions-${msg.id}`}
                      className="rounded-xl border border-[#0066CC]/25 bg-[#0066CC]/[0.03] p-3 space-y-2.5"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-xs font-bold text-[#0058B3]">
                          Proposed Actions ({msg.proposed_actions.length})
                        </span>
                        {msg.action_status && (
                          <span
                            className={`rounded-full border px-2 py-0.5 text-[10px] font-bold ${
                              msg.action_status === 'EXECUTED'
                                ? 'border-[#1F7A4D]/25 bg-[#1F7A4D]/12 text-[#1B6B43]'
                                : msg.action_status === 'DECLINED'
                                  ? 'border-black/[0.12] bg-black/[0.05] text-[#6E6E73]'
                                  : msg.action_status === 'REVISED'
                                    ? 'border-[#5E5CE6]/25 bg-[#5E5CE6]/10 text-[#4B48C4]'
                                    : 'border-[#B46900]/30 bg-[#B46900]/10 text-[#945600]'
                            }`}
                          >
                            {msg.action_status === 'PENDING_CONFIRMATION'
                              ? 'Awaiting Approval'
                              : msg.action_status}
                          </span>
                        )}
                      </div>

                      <div className="space-y-2">
                        {msg.proposed_actions.map((act, aIdx) => (
                          <div
                            key={aIdx}
                            className="rounded-lg border border-black/[0.07] bg-white p-2.5 text-xs space-y-1.5"
                          >
                            <div className="flex items-center justify-between gap-2">
                              <span className="font-bold text-[#1D1D1F]">{act.title}</span>
                              <span className="font-mono text-[10px] text-[#6E6E73]">
                                {act.action_type}
                              </span>
                            </div>
                            <p className="text-[11px] text-[#3A3A3C] leading-relaxed">
                              {act.summary}
                            </p>
                            {act.params.length > 0 && (
                              <div className="flex flex-wrap gap-1 pt-0.5">
                                {act.params.map((p, pIdx) => (
                                  <span
                                    key={pIdx}
                                    className="rounded-md bg-[#F5F5F7] px-1.5 py-0.5 font-mono text-[10px] text-[#1D1D1F]"
                                  >
                                    {p.key}={p.value}
                                  </span>
                                ))}
                              </div>
                            )}
                          </div>
                        ))}
                      </div>

                      {msg.action_status === 'PENDING_CONFIRMATION' && (
                        <div className="space-y-2 pt-0.5">
                          <div className="flex flex-wrap items-center gap-1.5">
                            <button
                              type="button"
                              disabled={isActionBusy || sending}
                              onClick={() => void handleConfirmActions(msg.id)}
                              className="apple-press inline-flex items-center gap-1 rounded-lg bg-[#1F7A4D] px-3 py-1.5 text-[11px] font-semibold text-white hover:bg-[#1B6B43] disabled:opacity-45"
                            >
                              <CheckCircle2 className="h-3.5 w-3.5" />
                              {isActionBusy ? 'Executing...' : 'Confirm & Execute'}
                            </button>
                            <button
                              type="button"
                              disabled={isActionBusy || sending}
                              onClick={() => {
                                setRevisingMsgId(isRevising ? null : msg.id);
                                setRevisionInput('');
                              }}
                              className="apple-press inline-flex items-center gap-1 rounded-lg border border-[#0066CC]/30 bg-white px-2.5 py-1.5 text-[11px] font-semibold text-[#0058B3] hover:bg-[#0066CC]/[0.05] disabled:opacity-45"
                            >
                              <RotateCcw className="h-3 w-3" />
                              Revise / Clarify Intent
                            </button>
                            <button
                              type="button"
                              disabled={isActionBusy || sending}
                              onClick={() => void handleDeclineActions(msg.id)}
                              className="apple-press inline-flex items-center gap-1 rounded-lg border border-black/[0.12] bg-white px-2.5 py-1.5 text-[11px] font-medium text-[#6E6E73] hover:bg-black/[0.04] disabled:opacity-45"
                            >
                              <XCircle className="h-3 w-3" />
                              Cancel
                            </button>
                          </div>

                          {isRevising && (
                            <div className="rounded-lg border border-[#0066CC]/25 bg-white p-2.5 space-y-2">
                              <div className="text-[11px] font-semibold text-[#1D1D1F]">
                                Clarify your original intent or what Lex should change:
                              </div>
                              <textarea
                                rows={2}
                                value={revisionInput}
                                onChange={(e) => setRevisionInput(e.target.value)}
                                placeholder="e.g., Use Backup supplier Sup1 for 15 units instead, or don't cancel the PO..."
                                className="w-full rounded-lg border border-black/[0.12] px-2.5 py-1.5 text-xs text-[#1D1D1F] focus:border-[#0066CC] focus:outline-none"
                              />
                              <div className="flex justify-end gap-1.5">
                                <button
                                  type="button"
                                  onClick={() => {
                                    setRevisingMsgId(null);
                                    setRevisionInput('');
                                  }}
                                  className="rounded-lg px-2.5 py-1 text-[11px] text-[#6E6E73] hover:bg-black/[0.04]"
                                >
                                  Close
                                </button>
                                <button
                                  type="button"
                                  disabled={sending || !revisionInput.trim()}
                                  onClick={() => void sendQuery(revisionInput, msg.id)}
                                  className="apple-press inline-flex items-center gap-1 rounded-lg bg-[#0066CC] px-3 py-1 text-[11px] font-semibold text-white hover:bg-[#0071E3] disabled:opacity-45"
                                >
                                  <Send className="h-3 w-3" />
                                  Re-send with Intent
                                </button>
                              </div>
                            </div>
                          )}
                        </div>
                      )}

                      {msg.action_status === 'EXECUTED' &&
                        msg.action_execution_results.length > 0 && (
                          <div className="space-y-1.5 border-t border-[#1F7A4D]/20 pt-2">
                            {msg.action_execution_results.map((r, rIdx) => (
                              <div
                                key={rIdx}
                                className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-[#1B6B43]"
                              >
                                <span className="inline-flex items-center gap-1 font-medium">
                                  <Check className="h-3 w-3 shrink-0" />
                                  {r.summary}
                                </span>
                                <div className="flex items-center gap-1">
                                  {r.po_id && (
                                    <button
                                      type="button"
                                      onClick={() => onNavigatePo(r.po_id!)}
                                      className="rounded-md bg-[#1F7A4D]/12 px-1.5 py-0.5 text-[10px] font-bold hover:bg-[#1F7A4D]/20"
                                    >
                                      Open {r.po_number ?? `PO #${r.po_id}`}
                                    </button>
                                  )}
                                  {r.part_id && !r.po_id && (
                                    <button
                                      type="button"
                                      onClick={() => onNavigatePart(r.part_id!)}
                                      className="rounded-md bg-[#1F7A4D]/12 px-1.5 py-0.5 text-[10px] font-bold hover:bg-[#1F7A4D]/20"
                                    >
                                      View Part
                                    </button>
                                  )}
                                </div>
                              </div>
                            ))}
                          </div>
                        )}
                    </div>
                  )}

                  {/* Clickable Deep-Link Chips (Keep Copilot Open!) */}
                  {(msg.resolved_parts.length > 0 ||
                    msg.resolved_pos.length > 0 ||
                    msg.resolved_alerts.length > 0) && (
                    <div className="flex flex-wrap items-center gap-1.5 pt-1 border-t border-black/[0.06]">
                      <span className="text-[10px] font-bold uppercase tracking-wider text-[#86868B] mr-0.5">
                        Jump to:
                      </span>
                      {msg.resolved_parts.map((p) => (
                        <button
                          key={`part-${p.part_id}`}
                          type="button"
                          onClick={() => onNavigatePart(p.part_id)}
                          className="apple-press inline-flex items-center gap-1 rounded-full border border-[#0066CC]/25 bg-[#0066CC]/[0.07] px-2.5 py-0.5 text-[11px] font-semibold text-[#0058B3] hover:bg-[#0066CC]/[0.14]"
                        >
                          <Package className="h-3 w-3" />
                          {p.sku}
                        </button>
                      ))}
                      {msg.resolved_pos.map((po) => (
                        <button
                          key={`po-${po.po_id}`}
                          type="button"
                          onClick={() => onNavigatePo(po.po_id)}
                          className="apple-press inline-flex items-center gap-1 rounded-full border border-[#5E5CE6]/25 bg-[#5E5CE6]/[0.07] px-2.5 py-0.5 text-[11px] font-semibold text-[#4B48C4] hover:bg-[#5E5CE6]/[0.14]"
                        >
                          <ClipboardList className="h-3 w-3" />
                          {po.po_number}
                        </button>
                      ))}
                      {msg.resolved_alerts.map((al) => (
                        <button
                          key={`al-${al.alert_id}`}
                          type="button"
                          onClick={() => onNavigateAlert(al.alert_id)}
                          className="apple-press inline-flex items-center gap-1 rounded-full border border-[#B46900]/28 bg-[#B46900]/[0.07] px-2.5 py-0.5 text-[11px] font-semibold text-[#945600] hover:bg-[#B46900]/[0.14]"
                        >
                          <Bell className="h-3 w-3" />
                          Alert #{al.alert_id}
                        </button>
                      ))}
                    </div>
                  )}

                  {/* Recommended Actions & Caveats */}
                  {msg.recommended_actions.length > 0 && (
                    <div className="rounded-xl bg-[#FBFBFD] border border-black/[0.06] px-3 py-2 space-y-1">
                      <div className="text-[10px] font-bold uppercase tracking-wider text-[#6E6E73]">
                        Recommended Next Steps
                      </div>
                      <ul className="list-disc pl-4 space-y-0.5 text-[11px] text-[#1D1D1F]">
                        {msg.recommended_actions.map((ra, idx) => (
                          <li key={idx}>{ra}</li>
                        ))}
                      </ul>
                    </div>
                  )}

                  {msg.cannot_verify_or_missing_tools.length > 0 && (
                    <div className="rounded-xl bg-[#C9342D]/[0.04] border border-[#C9342D]/20 px-3 py-2 space-y-1 text-[11px] text-[#B82923]">
                      <div className="font-bold">Could not verify / not allowed:</div>
                      <ul className="list-disc pl-4 space-y-0.5">
                        {msg.cannot_verify_or_missing_tools.map((c, idx) => (
                          <li key={idx}>{c}</li>
                        ))}
                      </ul>
                    </div>
                  )}

                  {/* Footer: Tool Badges + Confidence + Time */}
                  <div className="flex flex-wrap items-center justify-between gap-2 pt-1 border-t border-black/[0.05] text-[10px] text-[#86868B] tabular-nums">
                    <div className="flex items-center gap-1.5">
                      {msg.tool_calls.length > 0 && (
                        <button
                          type="button"
                          onClick={() =>
                            setExpandedTools((prev) => ({ ...prev, [msg.id]: !prev[msg.id] }))
                          }
                          className="apple-press inline-flex items-center gap-1 rounded-full border border-black/[0.09] bg-[#FBFBFD] px-2 py-0.5 font-semibold text-[#1D1D1F] hover:bg-black/[0.04]"
                        >
                          <Wrench className="h-2.5 w-2.5 text-[#0066CC]" />
                          Tools ({msg.tool_calls.length})
                          {toolsOpen ? (
                            <ChevronDown className="h-2.5 w-2.5" />
                          ) : (
                            <ChevronRight className="h-2.5 w-2.5" />
                          )}
                        </button>
                      )}
                      {msg.confidence !== null && (
                        <span className="rounded-full bg-black/[0.04] px-2 py-0.5 font-medium text-[#6E6E73]">
                          Conf {msg.confidence.toFixed(2)}
                        </span>
                      )}
                    </div>
                    <span>{formatSimDateTime(msg.created_at)}</span>
                  </div>

                  {toolsOpen && msg.tool_calls.length > 0 && (
                    <div className="space-y-1.5 pt-1">
                      {msg.tool_calls.map((tc, tIdx) => {
                        const toolName = String(tc.tool ?? tc.name ?? 'tool');
                        const dur =
                          typeof tc.duration_ms === 'number' ? `${tc.duration_ms}ms` : '';
                        const summary =
                          typeof tc.result_summary === 'string' ? tc.result_summary : '';
                        return (
                          <div
                            key={tIdx}
                            className="rounded-lg border border-black/[0.07] bg-[#FBFBFD] px-2.5 py-1.5 text-[10px] space-y-0.5"
                          >
                            <div className="flex items-center justify-between gap-2">
                              <span className="font-mono font-bold text-[#1D1D1F]">
                                #{tIdx + 1} {toolName}
                              </span>
                              <span className="text-[#86868B] tabular-nums">{dur}</span>
                            </div>
                            {summary && (
                              <div className="text-[#6E6E73] break-all line-clamp-2">
                                {summary}
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              </div>
            );
          })
        )}

        {sending && (
          <div className="flex items-center gap-2 rounded-2xl border border-black/[0.08] bg-white px-3.5 py-2.5 text-xs text-[#6E6E73] shadow-2xs">
            <RefreshCw className="h-3.5 w-3.5 animate-spin text-[#5E5CE6]" />
            <span>Lex is inspecting live DB, stats, and charts...</span>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Composer */}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void sendQuery(input);
        }}
        className="border-t border-black/[0.08] bg-white p-3 space-y-2"
      >
        <div className="flex items-end gap-2">
          <textarea
            ref={inputRef}
            rows={2}
            value={input}
            disabled={sending}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                void sendQuery(input);
              }
            }}
            placeholder="Ask Lex to analyze, chart, or propose an action... (Enter to send)"
            className="flex-1 resize-none rounded-xl border border-black/[0.12] bg-[#FBFBFD] px-3 py-2 text-xs text-[#1D1D1F] placeholder:text-[#86868B] focus:border-[#0066CC] focus:bg-white focus:outline-none disabled:opacity-60"
          />
          <button
            type="submit"
            disabled={sending || !input.trim()}
            aria-label="Send Copilot message"
            className="apple-press inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-[#0066CC] text-white hover:bg-[#0071E3] disabled:opacity-40"
          >
            <Send className="h-4 w-4" />
          </button>
        </div>
      </form>
    </aside>
  );
}
