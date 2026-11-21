import { useCallback, useEffect, useState } from 'react';
import {
  Check,
  Inbox,
  Lock,
  Mail,
  Paperclip,
  PenSquare,
  RefreshCw,
  Reply,
  Send,
  Sparkles,
  X,
} from 'lucide-react';
import { api } from '../api/client';
import type { POListItem, PersonaKey, StoredEmail } from '../api/types';
import { formatSimDateTime, personaLabel } from '../utils/format';

interface MailroomPageProps {
  refreshNonce: number;
  onMutated: () => void;
  onNotify: (kind: 'success' | 'error' | 'info', title: string, detail?: string) => void;
}

const PERSONAS: PersonaKey[] = [
  'inventory_manager',
  'making',
  'procurement',
  'sup1',
  'sup2',
  'sup3',
  'lex',
];

type MailboxFolder = 'inbox' | 'sent' | 'drafts';

function emailAttachments(email: Pick<StoredEmail, 'attachment_path' | 'attachment_paths'>): string[] {
  if (Array.isArray(email.attachment_paths) && email.attachment_paths.length > 0) {
    return email.attachment_paths;
  }
  return email.attachment_path ? [email.attachment_path] : [];
}

function attachmentBasename(rawPath: string): string {
  const parts = rawPath.split('/');
  return parts[parts.length - 1] || rawPath;
}

function attachmentExtLabel(rawPath: string): string {
  const ext = rawPath.split('.').pop()?.toUpperCase();
  return ext || 'FILE';
}

export function MailroomPage({
  refreshNonce,
  onMutated,
  onNotify,
}: MailroomPageProps) {
  const [persona, setPersona] = useState<PersonaKey>('inventory_manager');
  const [folder, setFolder] = useState<MailboxFolder>('inbox');
  const [emails, setEmails] = useState<StoredEmail[]>([]);
  const [selectedEmail, setSelectedEmail] = useState<StoredEmail | null>(null);
  const [threadEmails, setThreadEmails] = useState<StoredEmail[]>([]);
  const [loading, setLoading] = useState(true);
  const [pos, setPos] = useState<POListItem[]>([]);

  // Draft editing state
  const [draftSubject, setDraftSubject] = useState('');
  const [draftBody, setDraftBody] = useState('');
  const [busyDraftId, setBusyDraftId] = useState<number | null>(null);

  // Compose modal state
  const [composeOpen, setComposeOpen] = useState(false);
  const [toPersonas, setToPersonas] = useState<PersonaKey[]>(['sup1']);
  const [ccPersonas, setCcPersonas] = useState<PersonaKey[]>([]);
  const [composeSubject, setComposeSubject] = useState('');
  const [composeBody, setComposeBody] = useState('');
  const [composePoId, setComposePoId] = useState<string>('');
  const [composeThreadId, setComposeThreadId] = useState<string>('');
  const [composeFiles, setComposeFiles] = useState<File[]>([]);
  const [sending, setSending] = useState(false);

  const loadFolder = useCallback(async () => {
    setLoading(true);
    try {
      const [list, poList] = await Promise.all([
        folder === 'inbox'
          ? api.listInbox(persona)
          : folder === 'sent'
            ? api.listSent(persona)
            : api.listDrafts(persona),
        api.listPos(),
      ]);
      setEmails(list);
      setPos(poList);
      setSelectedEmail((prev) => {
        if (!prev) return list[0] ?? null;
        return list.find((e) => e.id === prev.id) ?? list[0] ?? null;
      });
    } catch (err) {
      onNotify(
        'error',
        'Failed to load mailbox',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setLoading(false);
    }
  }, [persona, folder, onNotify]);

  useEffect(() => {
    void loadFolder();
  }, [loadFolder, refreshNonce]);

  useEffect(() => {
    if (!selectedEmail) {
      setThreadEmails([]);
      return;
    }
    setDraftSubject(selectedEmail.subject);
    setDraftBody(selectedEmail.body);

    let cancelled = false;
    api
      .getThread(selectedEmail.thread_id)
      .then((t) => {
        if (!cancelled) setThreadEmails(t);
      })
      .catch(() => {
        if (!cancelled) setThreadEmails([selectedEmail]);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedEmail]);

  const makingRequiresLexCc =
    persona === 'making' && toPersonas.includes('inventory_manager');

  const openCompose = (replyTo?: StoredEmail) => {
    if (persona === 'lex') return;
    if (replyTo) {
      let defaultRecipient: PersonaKey =
        replyTo.from_persona !== persona
          ? (replyTo.from_persona as PersonaKey)
          : ((replyTo.to_personas[0] as PersonaKey) ?? 'inventory_manager');
      if (
        persona === 'making' &&
        replyTo.from_persona === 'lex' &&
        replyTo.cc_personas.includes('inventory_manager')
      ) {
        defaultRecipient = 'inventory_manager';
      }
      const nextTo: PersonaKey[] = [defaultRecipient];
      const mustLockLex =
        persona === 'making' && nextTo.includes('inventory_manager');
      setToPersonas(nextTo);
      setCcPersonas(
        mustLockLex ||
          (defaultRecipient !== 'lex' &&
            persona !== 'inventory_manager' &&
            persona !== 'procurement')
          ? ['lex']
          : [],
      );
      setComposeSubject(
        replyTo.subject.startsWith('Re:') ? replyTo.subject : `Re: ${replyTo.subject}`,
      );
      setComposeBody('');
      setComposePoId(replyTo.po_id ? String(replyTo.po_id) : '');
      setComposeThreadId(replyTo.thread_id);
    } else {
      setToPersonas(persona === 'inventory_manager' ? ['sup1'] : ['inventory_manager']);
      setCcPersonas(
        persona === 'inventory_manager' || persona === 'procurement' ? [] : ['lex'],
      );
      setComposeSubject('');
      setComposeBody('');
      setComposePoId('');
      setComposeThreadId('');
    }
    setComposeFiles([]);
    setComposeOpen(true);
  };

  const toggleRecipient = (list: PersonaKey[], setList: (v: PersonaKey[]) => void, p: PersonaKey) => {
    if (list.includes(p)) {
      setList(list.filter((item) => item !== p));
    } else {
      setList([...list, p]);
    }
  };

  const handleToggleTo = (p: PersonaKey) => {
    const nextTo = toPersonas.includes(p)
      ? toPersonas.filter((item) => item !== p)
      : [...toPersonas, p];
    setToPersonas(nextTo);
    if (
      persona === 'making' &&
      nextTo.includes('inventory_manager') &&
      !ccPersonas.includes('lex')
    ) {
      setCcPersonas((prev) => [...prev, 'lex']);
    }
  };

  const handleToggleCc = (p: PersonaKey) => {
    if (makingRequiresLexCc && p === 'lex') return;
    toggleRecipient(ccPersonas, setCcPersonas, p);
  };

  const handleSendEmail = async (e: React.FormEvent) => {
    e.preventDefault();
    if (persona === 'lex') {
      onNotify('error', 'Lex is read-only', 'Manual send from Lex is forbidden.');
      return;
    }
    if (toPersonas.length === 0) {
      onNotify('error', 'Recipient required', 'Select at least one To persona.');
      return;
    }
    const effectiveCc =
      makingRequiresLexCc && !ccPersonas.includes('lex')
        ? [...ccPersonas, 'lex']
        : ccPersonas;
    setSending(true);
    try {
      const fd = new FormData();
      fd.append('from_persona', persona);
      fd.append('to_personas', toPersonas.join(','));
      fd.append('cc_personas', effectiveCc.join(','));
      fd.append('subject', composeSubject.trim());
      fd.append('body', composeBody.trim());
      if (composePoId) fd.append('po_id', composePoId);
      if (composeThreadId) fd.append('thread_id', composeThreadId);
      fd.append('category', 'GENERAL');
      for (const file of composeFiles) {
        fd.append('attachments', file);
      }

      const res = await api.sendEmail(fd);
      const triageInfo = res.triage
        ? `Lex triage completed${
            typeof res.triage.matched_po_id === 'number'
              ? ` (PO #${res.triage.matched_po_id})`
              : Array.isArray(res.triage.stock_request_ids) &&
                  res.triage.stock_request_ids.length > 0
                ? ` (${res.triage.stock_request_ids.length} stock request alert${res.triage.stock_request_ids.length > 1 ? 's' : ''} created)`
                : ''
          }`
        : undefined;
      onNotify('success', `Email sent from ${personaLabel(persona)}`, triageInfo);
      setComposeOpen(false);
      onMutated();
      await loadFolder();
    } catch (err) {
      onNotify(
        'error',
        'Failed to send email',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setSending(false);
    }
  };

  const handleApproveDraft = async (comm: StoredEmail) => {
    setBusyDraftId(comm.id);
    try {
      const editedSubj =
        draftSubject.trim() !== comm.subject.trim() ? draftSubject.trim() : null;
      const editedBdy =
        draftBody.trim() !== comm.body.trim() ? draftBody.trim() : null;
      const res = await api.approveDraft(comm.id, editedSubj, editedBdy);
      onNotify(
        'success',
        `Draft approved (${res.approval_outcome})`,
        res.po_transitioned_to
          ? `PO transitioned to ${res.po_transitioned_to}`
          : undefined,
      );
      onMutated();
      await loadFolder();
    } catch (err) {
      onNotify(
        'error',
        'Draft approval failed',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setBusyDraftId(null);
    }
  };

  const handleRejectDraft = async (comm: StoredEmail) => {
    setBusyDraftId(comm.id);
    try {
      await api.rejectDraft(comm.id);
      onNotify('info', 'Draft rejected', comm.subject);
      onMutated();
      await loadFolder();
    } catch (err) {
      onNotify(
        'error',
        'Draft rejection failed',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setBusyDraftId(null);
    }
  };

  const isLex = persona === 'lex';

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold tracking-tight text-[#1D1D1F]">Mailroom</h1>
          <p className="text-sm text-[#6E6E73]">
            Switch personas to coordinate across internal teams &amp; suppliers, submit Making stock requests, attach PDF invoices, and approve Lex drafts.
          </p>
        </div>

        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={isLex}
            onClick={() => openCompose()}
            className="apple-press inline-flex items-center gap-1.5 rounded-full bg-[#0066CC] px-4 py-2 text-xs font-semibold text-white hover:bg-[#0071E3] shadow-2xs disabled:cursor-not-allowed disabled:opacity-40"
          >
            <PenSquare className="h-3.5 w-3.5" />
            Compose
          </button>
          <button
            type="button"
            onClick={() => void loadFolder()}
            className="apple-press inline-flex items-center gap-1.5 rounded-full border border-black/[0.08] bg-white px-3.5 py-2 text-xs font-medium text-[#1D1D1F] hover:bg-[#FBFBFD] shadow-2xs"
          >
            <RefreshCw className={`h-3.5 w-3.5 text-[#6E6E73] ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>
      </div>

      {/* Persona Switcher */}
      <div className="apple-card flex flex-wrap items-center justify-between gap-3 rounded-2xl p-2.5">
        <div className="flex flex-wrap items-center gap-1.5">
          {PERSONAS.map((p) => {
            const active = persona === p;
            return (
              <button
                key={p}
                type="button"
                onClick={() => {
                  setPersona(p);
                  setSelectedEmail(null);
                }}
                className={`apple-press inline-flex items-center gap-1.5 rounded-full px-3.5 py-1.5 text-xs font-semibold ${
                  active
                    ? p === 'lex'
                      ? 'bg-[#5E5CE6] text-white shadow-2xs'
                      : 'bg-[#0066CC] text-white shadow-2xs'
                    : 'bg-[#FBFBFD] text-[#6E6E73] border border-black/[0.06] hover:bg-black/[0.04] hover:text-[#1D1D1F]'
                }`}
              >
                {p === 'lex' && <Lock className="h-3 w-3" />}
                {personaLabel(p)}
              </button>
            );
          })}
        </div>

        <div className="inline-flex rounded-full border border-black/[0.08] bg-[#F5F5F7] p-0.5">
          {(['inbox', 'sent', 'drafts'] as const).map((f) => (
            <button
              key={f}
              type="button"
              onClick={() => {
                setFolder(f);
                setSelectedEmail(null);
              }}
              className={`apple-press rounded-full px-3.5 py-1 text-xs font-semibold capitalize ${
                folder === f
                  ? 'bg-white text-[#1D1D1F] shadow-2xs'
                  : 'text-[#6E6E73] hover:text-[#1D1D1F]'
              }`}
            >
              {f}
            </button>
          ))}
        </div>
      </div>

      {isLex && (
        <div className="flex items-center gap-2 rounded-xl border border-[#B46900]/24 bg-[#B46900]/[0.08] px-4 py-2.5 text-xs font-medium text-[#945600]">
          <Lock className="h-4 w-4 shrink-0 text-[#945600]" />
          Lex inbox is read-only. Manual send from Lex is disabled; switch to Inventory Manager, Making Team, Procurement, or a supplier to send messages.
        </div>
      )}

      {/* Split Mail List & Thread Pane */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-4 min-h-[540px]">
        {/* Email List */}
        <div className="apple-card lg:col-span-5 rounded-2xl overflow-hidden flex flex-col">
          <div className="border-b border-black/[0.06] bg-[#FBFBFD] px-4 py-2.5 text-xs font-bold uppercase tracking-wider text-[#86868B] flex items-center justify-between">
            <span className="flex items-center gap-1.5">
              <Inbox className="h-3.5 w-3.5" />
              {personaLabel(persona)} · {folder}
            </span>
            <span className="tabular-nums">{emails.length}</span>
          </div>

          <div className="divide-y divide-black/[0.05] overflow-y-auto max-h-[600px]">
            {emails.length === 0 ? (
              <div className="p-8 text-center text-xs text-[#6E6E73]">
                No emails in {folder}.
              </div>
            ) : (
              emails.map((email) => {
                const isSelected = selectedEmail?.id === email.id;
                return (
                  <button
                    key={email.id}
                    type="button"
                    onClick={() => setSelectedEmail(email)}
                    className={`w-full text-left px-4 py-3 transition-colors ${
                      isSelected ? 'bg-[#0066CC]/[0.07]' : 'hover:bg-black/[0.02]'
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2 text-xs">
                      <span className="font-semibold text-[#1D1D1F] truncate">
                        {folder === 'sent' || folder === 'drafts'
                          ? `To: ${email.to_personas.map(personaLabel).join(', ')}`
                          : personaLabel(email.from_persona)}
                      </span>
                      <span className="text-[11px] text-[#86868B] shrink-0 tabular-nums">
                        {formatSimDateTime(email.timestamp)}
                      </span>
                    </div>
                    <div className="mt-1 text-xs font-semibold text-[#1D1D1F] truncate">
                      {email.subject}
                    </div>
                    <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                      {email.ai_classification && (
                        <span className="inline-flex items-center gap-1 rounded-full bg-[#5E5CE6]/10 border border-[#5E5CE6]/22 px-2 py-0.5 text-[10px] font-semibold text-[#4B48C4]">
                          <Sparkles className="h-2.5 w-2.5" />
                          {email.ai_classification}
                          {email.ai_confidence !== null &&
                            ` (${Math.round(email.ai_confidence * 100)}%)`}
                        </span>
                      )}
                      {email.status === 'DRAFT_PENDING_APPROVAL' && (
                        <span className="rounded-full bg-[#B46900]/10 border border-[#B46900]/24 px-2 py-0.5 text-[10px] font-semibold text-[#945600]">
                          PENDING APPROVAL
                        </span>
                      )}
                      {emailAttachments(email).map((attPath) => (
                        <span
                          key={attPath}
                          className="inline-flex items-center gap-0.5 rounded-full bg-black/[0.05] px-2 py-0.5 text-[10px] font-medium text-[#6E6E73]"
                        >
                          <Paperclip className="h-2.5 w-2.5" />
                          {attachmentExtLabel(attPath)}
                        </span>
                      ))}
                      {email.po_id && (
                        <span className="rounded-full bg-black/[0.05] px-2 py-0.5 text-[10px] font-medium text-[#6E6E73] tabular-nums">
                          PO #{email.po_id}
                        </span>
                      )}
                    </div>
                  </button>
                );
              })
            )}
          </div>
        </div>

        {/* Reading & Thread Pane */}
        <div className="apple-card lg:col-span-7 rounded-2xl p-5 flex flex-col space-y-4">
          {!selectedEmail ? (
            <div className="flex-1 flex flex-col items-center justify-center text-[#86868B] py-16">
              <Mail className="h-8 w-8 mb-2" />
              <p className="text-sm">Select a message to read its thread</p>
            </div>
          ) : (
            <>
              <div className="flex flex-wrap items-start justify-between gap-3 border-b border-black/[0.06] pb-4">
                <div>
                  <h2 className="text-base font-bold tracking-tight text-[#1D1D1F]">
                    {selectedEmail.subject}
                  </h2>
                  <div className="mt-1 text-xs text-[#6E6E73] space-y-0.5">
                    <div>
                      <strong className="text-[#1D1D1F]">From:</strong> {personaLabel(selectedEmail.from_persona)}
                    </div>
                    <div>
                      <strong className="text-[#1D1D1F]">To:</strong>{' '}
                      {selectedEmail.to_personas.map(personaLabel).join(', ')}
                      {selectedEmail.cc_personas.length > 0 && (
                        <>
                          {' · '}
                          <strong className="text-[#1D1D1F]">CC:</strong>{' '}
                          {selectedEmail.cc_personas.map(personaLabel).join(', ')}
                        </>
                      )}
                    </div>
                    <div className="text-[#86868B] tabular-nums">
                      Thread: {selectedEmail.thread_id}
                      {selectedEmail.po_id ? ` · Linked PO #${selectedEmail.po_id}` : ''}
                    </div>
                  </div>
                </div>

                {!isLex && (
                  <button
                    type="button"
                    onClick={() => openCompose(selectedEmail)}
                    className="apple-press inline-flex items-center gap-1.5 rounded-full border border-black/[0.10] bg-white px-3.5 py-1.5 text-xs font-semibold text-[#1D1D1F] hover:bg-[#FBFBFD] shadow-2xs"
                  >
                    <Reply className="h-3.5 w-3.5 text-[#6E6E73]" />
                    Reply
                  </button>
                )}
              </div>

              {/* Draft approval controls if selected message is pending approval */}
              {selectedEmail.status === 'DRAFT_PENDING_APPROVAL' && (
                <div className="rounded-xl border border-[#5E5CE6]/22 bg-[#5E5CE6]/[0.04] p-4 space-y-3">
                  <div className="flex items-center justify-between text-xs">
                    <span className="font-bold text-[#1D1D1F] flex items-center gap-1.5">
                      <Sparkles className="h-3.5 w-3.5 text-[#5E5CE6]" />
                      Lex Draft Pending Manager Approval ({selectedEmail.category})
                    </span>
                  </div>
                  <input
                    type="text"
                    value={draftSubject}
                    onChange={(e) => setDraftSubject(e.target.value)}
                    className="w-full rounded-xl border border-[#5E5CE6]/25 bg-white px-3 py-1.5 text-xs font-semibold text-[#1D1D1F] focus:border-[#5E5CE6] focus:outline-none"
                  />
                  <textarea
                    rows={5}
                    value={draftBody}
                    onChange={(e) => setDraftBody(e.target.value)}
                    className="w-full rounded-xl border border-[#5E5CE6]/25 bg-white px-3 py-2 text-xs text-[#1D1D1F] focus:border-[#5E5CE6] focus:outline-none"
                  />
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      disabled={busyDraftId === selectedEmail.id}
                      onClick={() => void handleApproveDraft(selectedEmail)}
                      className="apple-press inline-flex items-center gap-1.5 rounded-xl bg-[#0066CC] px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-[#0071E3] disabled:opacity-45"
                    >
                      <Check className="h-3.5 w-3.5" />
                      Approve &amp; Send
                    </button>
                    <button
                      type="button"
                      disabled={busyDraftId === selectedEmail.id}
                      onClick={() => void handleRejectDraft(selectedEmail)}
                      className="apple-press inline-flex items-center gap-1.5 rounded-xl border border-[#C9342D]/25 bg-white px-3 py-1.5 text-xs font-semibold text-[#B82923] hover:bg-[#C9342D]/[0.07] disabled:opacity-45"
                    >
                      <X className="h-3.5 w-3.5" />
                      Reject
                    </button>
                  </div>
                </div>
              )}

              {/* Thread messages */}
              <div className="space-y-3 overflow-y-auto max-h-[460px] pr-1">
                {(threadEmails.length > 0 ? threadEmails : [selectedEmail]).map((msg) => (
                  <div
                    key={msg.id}
                    className={`rounded-xl border p-3.5 text-xs space-y-2 ${
                      msg.id === selectedEmail.id
                        ? 'border-[#0066CC]/22 bg-[#0066CC]/[0.03]'
                        : 'border-black/[0.08] bg-white'
                    }`}
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="font-semibold text-[#1D1D1F]">
                        {personaLabel(msg.from_persona)} →{' '}
                        {msg.to_personas.map(personaLabel).join(', ')}
                      </span>
                      <div className="flex flex-wrap items-center gap-1.5">
                        {msg.ai_classification && (
                          <span className="rounded-full bg-[#5E5CE6]/10 border border-[#5E5CE6]/22 px-2 py-0.5 text-[11px] font-semibold text-[#4B48C4]">
                            Lex: {msg.ai_classification}
                            {msg.ai_confidence !== null &&
                              ` (${Math.round(msg.ai_confidence * 100)}%)`}
                          </span>
                        )}
                        <span className="rounded-full bg-black/[0.05] px-2 py-0.5 text-[11px] font-medium text-[#6E6E73]">
                          {msg.status}
                        </span>
                        <span className="text-[11px] text-[#86868B] tabular-nums">
                          {formatSimDateTime(msg.timestamp)}
                        </span>
                      </div>
                    </div>

                    <p className="whitespace-pre-wrap text-[#1D1D1F] leading-relaxed">
                      {msg.body}
                    </p>

                    {emailAttachments(msg).length > 0 && (
                      <div className="flex flex-wrap gap-1.5">
                        {emailAttachments(msg).map((attPath) => (
                          <div
                            key={attPath}
                            className="inline-flex items-center gap-1.5 rounded-lg border border-black/[0.08] bg-[#FBFBFD] px-2.5 py-1 text-[11px] font-medium text-[#6E6E73]"
                          >
                            <Paperclip className="h-3 w-3" />
                            <span>{attachmentBasename(attPath)}</span>
                          </div>
                        ))}
                      </div>
                    )}

                    {msg.ai_extracted_json &&
                      Object.keys(msg.ai_extracted_json).length > 0 && (
                        <details className="rounded-xl border border-[#5E5CE6]/18 bg-[#5E5CE6]/[0.04] p-2.5">
                          <summary className="cursor-pointer font-semibold text-[#4B48C4]">
                            Lex Triage Extraction
                          </summary>
                          <pre className="mt-1.5 overflow-x-auto text-[11px] text-[#1D1D1F]">
                            {JSON.stringify(msg.ai_extracted_json, null, 2)}
                          </pre>
                        </details>
                      )}
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      </div>

      {/* Compose / Reply Modal */}
      {composeOpen && (
        <div className="anim-backdrop fixed inset-0 z-40 flex items-center justify-center bg-black/30 backdrop-blur-xs p-4">
          <div className="anim-modal-pop w-full max-w-xl rounded-2xl border border-black/[0.08] bg-white p-5 shadow-[0_18px_48px_rgba(0,0,0,0.14)]">
            <div className="flex items-center justify-between border-b border-black/[0.06] pb-3">
              <h2 className="text-base font-bold tracking-tight text-[#1D1D1F]">
                New Email from {personaLabel(persona)}
              </h2>
              <button
                type="button"
                onClick={() => setComposeOpen(false)}
                className="apple-press rounded-full p-1.5 text-[#86868B] hover:bg-black/[0.05] hover:text-[#1D1D1F]"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <form onSubmit={handleSendEmail} className="mt-4 space-y-3 text-xs">
              <div>
                <label className="block font-semibold text-[#1D1D1F] mb-1">To</label>
                <div className="flex flex-wrap gap-1.5">
                  {PERSONAS.filter((p) => p !== persona).map((p) => {
                    const checked = toPersonas.includes(p);
                    return (
                      <button
                        key={p}
                        type="button"
                        onClick={() => handleToggleTo(p)}
                        className={`apple-press rounded-full border px-3 py-1 font-semibold ${
                          checked
                            ? 'border-[#0066CC] bg-[#0066CC] text-white'
                            : 'border-black/[0.08] bg-[#FBFBFD] text-[#6E6E73] hover:bg-black/[0.04] hover:text-[#1D1D1F]'
                        }`}
                      >
                        {personaLabel(p)}
                      </button>
                    );
                  })}
                </div>
              </div>

              <div>
                <label className="block font-semibold text-[#1D1D1F] mb-1">
                  CC {makingRequiresLexCc ? '(Lex required)' : '(optional)'}
                </label>
                <div className="flex flex-wrap gap-1.5">
                  {PERSONAS.filter((p) => p !== persona).map((p) => {
                    const lockedLex = makingRequiresLexCc && p === 'lex';
                    const checked = lockedLex || ccPersonas.includes(p);
                    return (
                      <button
                        key={p}
                        type="button"
                        disabled={lockedLex}
                        onClick={() => handleToggleCc(p)}
                        title={
                          lockedLex
                            ? 'Lex is automatically CC’d and locked when Making Team emails Inventory Manager.'
                            : undefined
                        }
                        className={`apple-press inline-flex items-center gap-1 rounded-full border px-3 py-1 font-medium ${
                          checked
                            ? p === 'lex'
                              ? 'border-[#5E5CE6] bg-[#5E5CE6] text-white'
                              : 'border-[#0066CC] bg-[#0066CC] text-white'
                            : 'border-black/[0.08] bg-white text-[#6E6E73] hover:bg-[#FBFBFD] hover:text-[#1D1D1F]'
                        } ${lockedLex ? 'cursor-not-allowed opacity-90' : ''}`}
                      >
                        {lockedLex && <Lock className="h-3 w-3" />}
                        {personaLabel(p)}
                      </button>
                    );
                  })}
                </div>
                {makingRequiresLexCc && (
                  <p className="mt-1.5 flex items-center gap-1 text-[11px] font-medium text-[#4B48C4]">
                    <Sparkles className="h-3 w-3 shrink-0" />
                    Lex is automatically CC&apos;d and locked so stock requests are analyzed against inventory and active POs.
                  </p>
                )}
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label className="block font-semibold text-[#1D1D1F] mb-1">
                    Linked PO (optional)
                  </label>
                  <select
                    value={composePoId}
                    onChange={(e) => setComposePoId(e.target.value)}
                    className="w-full rounded-xl border border-black/[0.12] px-2.5 py-1.5 text-xs text-[#1D1D1F] focus:border-[#0066CC] focus:outline-none"
                  >
                    <option value="">Auto-detect / None</option>
                    {pos.map((po) => (
                      <option key={po.id} value={String(po.id)}>
                        {po.po_number} · {po.part_sku} ({po.status})
                      </option>
                    ))}
                  </select>
                </div>

                <div>
                  <label className="block font-semibold text-[#1D1D1F] mb-1">
                    Attachments (.pdf, .csv, .jpg, .png)
                  </label>
                  <input
                    type="file"
                    multiple
                    accept=".pdf,.csv,.jpg,.jpeg,.png,application/pdf,text/csv,image/jpeg,image/png"
                    onChange={(e) => {
                      const picked = Array.from(e.target.files ?? []);
                      if (picked.length > 0) {
                        setComposeFiles((prev) => {
                          const existing = new Set(prev.map((f) => `${f.name}:${f.size}`));
                          const next = [...prev];
                          for (const f of picked) {
                            const key = `${f.name}:${f.size}`;
                            if (!existing.has(key)) {
                              next.push(f);
                              existing.add(key);
                            }
                          }
                          return next;
                        });
                      }
                      e.target.value = '';
                    }}
                    className="w-full text-xs text-[#6E6E73] file:mr-2 file:rounded-full file:border-0 file:bg-black/[0.06] file:px-3 file:py-1 file:text-xs file:font-semibold file:text-[#1D1D1F] hover:file:bg-black/[0.10]"
                  />
                  {composeFiles.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {composeFiles.map((file, idx) => (
                        <span
                          key={`${file.name}-${idx}`}
                          className="inline-flex items-center gap-1 rounded-full border border-black/[0.08] bg-[#FBFBFD] px-2.5 py-0.5 text-[11px] font-medium text-[#1D1D1F]"
                        >
                          <Paperclip className="h-3 w-3 text-[#6E6E73]" />
                          <span className="max-w-[160px] truncate">{file.name}</span>
                          <button
                            type="button"
                            aria-label={`Remove ${file.name}`}
                            onClick={() =>
                              setComposeFiles((prev) => prev.filter((_, i) => i !== idx))
                            }
                            className="rounded-full p-0.5 text-[#86868B] hover:bg-black/[0.06] hover:text-[#1D1D1F]"
                          >
                            <X className="h-3 w-3" />
                          </button>
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              </div>

              <div>
                <label className="block font-semibold text-[#1D1D1F] mb-1">Subject</label>
                <input
                  type="text"
                  required
                  value={composeSubject}
                  onChange={(e) => setComposeSubject(e.target.value)}
                  placeholder="Subject line..."
                  className="w-full rounded-xl border border-black/[0.12] px-3 py-2 text-sm text-[#1D1D1F] focus:border-[#0066CC] focus:outline-none"
                />
              </div>

              <div>
                <label className="block font-semibold text-[#1D1D1F] mb-1">Body</label>
                <textarea
                  rows={5}
                  required
                  value={composeBody}
                  onChange={(e) => setComposeBody(e.target.value)}
                  placeholder="Write message..."
                  className="w-full rounded-xl border border-black/[0.12] px-3 py-2 text-sm text-[#1D1D1F] focus:border-[#0066CC] focus:outline-none"
                />
              </div>

              <div className="flex justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={() => setComposeOpen(false)}
                  className="apple-press rounded-xl border border-black/[0.10] px-3.5 py-2 text-xs font-medium text-[#1D1D1F] hover:bg-[#FBFBFD]"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={sending}
                  className="apple-press inline-flex items-center gap-1.5 rounded-xl bg-[#0066CC] px-4 py-2 text-xs font-semibold text-white hover:bg-[#0071E3] disabled:opacity-45"
                >
                  <Send className="h-3.5 w-3.5" />
                  {sending ? 'Sending...' : 'Send Email'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
