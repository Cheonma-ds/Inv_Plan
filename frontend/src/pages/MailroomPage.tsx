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
  'sup1',
  'sup2',
  'sup3',
  'lex',
];

type MailboxFolder = 'inbox' | 'sent' | 'drafts';

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
  const [composeFile, setComposeFile] = useState<File | null>(null);
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

  const openCompose = (replyTo?: StoredEmail) => {
    if (persona === 'lex') return;
    if (replyTo) {
      const defaultRecipient: PersonaKey =
        replyTo.from_persona !== persona
          ? (replyTo.from_persona as PersonaKey)
          : ((replyTo.to_personas[0] as PersonaKey) ?? 'inventory_manager');
      setToPersonas([defaultRecipient]);
      setCcPersonas(
        defaultRecipient !== 'lex' && persona !== 'inventory_manager' ? ['lex'] : [],
      );
      setComposeSubject(
        replyTo.subject.startsWith('Re:') ? replyTo.subject : `Re: ${replyTo.subject}`,
      );
      setComposeBody('');
      setComposePoId(replyTo.po_id ? String(replyTo.po_id) : '');
      setComposeThreadId(replyTo.thread_id);
    } else {
      setToPersonas(persona === 'inventory_manager' ? ['sup1'] : ['inventory_manager']);
      setCcPersonas(persona === 'inventory_manager' ? [] : ['lex']);
      setComposeSubject('');
      setComposeBody('');
      setComposePoId('');
      setComposeThreadId('');
    }
    setComposeFile(null);
    setComposeOpen(true);
  };

  const toggleRecipient = (list: PersonaKey[], setList: (v: PersonaKey[]) => void, p: PersonaKey) => {
    if (list.includes(p)) {
      setList(list.filter((item) => item !== p));
    } else {
      setList([...list, p]);
    }
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
    setSending(true);
    try {
      const fd = new FormData();
      fd.append('from_persona', persona);
      fd.append('to_personas', toPersonas.join(','));
      fd.append('cc_personas', ccPersonas.join(','));
      fd.append('subject', composeSubject.trim());
      fd.append('body', composeBody.trim());
      if (composePoId) fd.append('po_id', composePoId);
      if (composeThreadId) fd.append('thread_id', composeThreadId);
      fd.append('category', 'GENERAL');
      if (composeFile) {
        fd.append('attachment', composeFile);
      }

      const res = await api.sendEmail(fd);
      const triageInfo = res.triage
        ? `Lex triage completed${
            typeof res.triage.matched_po_id === 'number'
              ? ` (PO #${res.triage.matched_po_id})`
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
          <h1 className="text-xl font-bold text-slate-900">5-Persona Mailroom</h1>
          <p className="text-sm text-slate-500">
            Switch personas to simulate supplier correspondence, attach PDF invoices, and approve Lex email drafts.
          </p>
        </div>

        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={isLex}
            onClick={() => openCompose()}
            className="inline-flex items-center gap-1.5 rounded-lg bg-slate-900 px-3.5 py-2 text-xs font-semibold text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <PenSquare className="h-3.5 w-3.5" />
            Compose
          </button>
          <button
            type="button"
            onClick={() => void loadFolder()}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>
      </div>

      {/* Persona Switcher */}
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white p-2.5">
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
                className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-semibold transition ${
                  active
                    ? 'bg-slate-900 text-white'
                    : 'bg-slate-50 text-slate-700 hover:bg-slate-100'
                }`}
              >
                {p === 'lex' && <Lock className="h-3 w-3" />}
                {personaLabel(p)}
              </button>
            );
          })}
        </div>

        <div className="inline-flex rounded-lg border border-slate-200 bg-slate-50 p-0.5">
          {(['inbox', 'sent', 'drafts'] as const).map((f) => (
            <button
              key={f}
              type="button"
              onClick={() => {
                setFolder(f);
                setSelectedEmail(null);
              }}
              className={`rounded-md px-3 py-1 text-xs font-semibold capitalize transition ${
                folder === f
                  ? 'bg-white text-slate-900 shadow-sm'
                  : 'text-slate-600 hover:text-slate-900'
              }`}
            >
              {f}
            </button>
          ))}
        </div>
      </div>

      {isLex && (
        <div className="flex items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-4 py-2.5 text-xs font-medium text-amber-900">
          <Lock className="h-4 w-4 shrink-0 text-amber-700" />
          Lex inbox is read-only. Manual send from Lex is disabled; switch to Inventory Manager or a supplier to send messages.
        </div>
      )}

      {/* Split Mail List & Thread Pane */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-4 min-h-[540px]">
        {/* Email List */}
        <div className="lg:col-span-5 rounded-xl border border-slate-200 bg-white overflow-hidden flex flex-col">
          <div className="border-b border-slate-200 bg-slate-50 px-4 py-2.5 text-xs font-bold uppercase tracking-wider text-slate-500 flex items-center justify-between">
            <span className="flex items-center gap-1.5">
              <Inbox className="h-3.5 w-3.5" />
              {personaLabel(persona)} · {folder}
            </span>
            <span>{emails.length}</span>
          </div>

          <div className="divide-y divide-slate-100 overflow-y-auto max-h-[600px]">
            {emails.length === 0 ? (
              <div className="p-8 text-center text-xs text-slate-500">
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
                    className={`w-full text-left px-4 py-3 transition ${
                      isSelected ? 'bg-sky-50' : 'hover:bg-slate-50'
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2 text-xs">
                      <span className="font-semibold text-slate-900 truncate">
                        {folder === 'sent' || folder === 'drafts'
                          ? `To: ${email.to_personas.map(personaLabel).join(', ')}`
                          : personaLabel(email.from_persona)}
                      </span>
                      <span className="text-[11px] text-slate-400 shrink-0">
                        {formatSimDateTime(email.timestamp)}
                      </span>
                    </div>
                    <div className="mt-1 text-xs font-semibold text-slate-800 truncate">
                      {email.subject}
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-1.5">
                      {email.ai_classification && (
                        <span className="inline-flex items-center gap-1 rounded bg-indigo-100 px-1.5 py-0.5 text-[10px] font-bold text-indigo-800">
                          <Sparkles className="h-2.5 w-2.5" />
                          {email.ai_classification}
                          {email.ai_confidence !== null &&
                            ` (${Math.round(email.ai_confidence * 100)}%)`}
                        </span>
                      )}
                      {email.status === 'DRAFT_PENDING_APPROVAL' && (
                        <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-bold text-amber-800">
                          PENDING APPROVAL
                        </span>
                      )}
                      {email.attachment_path && (
                        <span className="inline-flex items-center gap-0.5 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-700">
                          <Paperclip className="h-2.5 w-2.5" />
                          PDF
                        </span>
                      )}
                      {email.po_id && (
                        <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-600">
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
        <div className="lg:col-span-7 rounded-xl border border-slate-200 bg-white p-5 flex flex-col space-y-4">
          {!selectedEmail ? (
            <div className="flex-1 flex flex-col items-center justify-center text-slate-400 py-16">
              <Mail className="h-8 w-8 mb-2" />
              <p className="text-sm">Select a message to read its thread</p>
            </div>
          ) : (
            <>
              <div className="flex flex-wrap items-start justify-between gap-3 border-b border-slate-200 pb-4">
                <div>
                  <h2 className="text-base font-bold text-slate-900">
                    {selectedEmail.subject}
                  </h2>
                  <div className="mt-1 text-xs text-slate-600 space-y-0.5">
                    <div>
                      <strong>From:</strong> {personaLabel(selectedEmail.from_persona)}
                    </div>
                    <div>
                      <strong>To:</strong>{' '}
                      {selectedEmail.to_personas.map(personaLabel).join(', ')}
                      {selectedEmail.cc_personas.length > 0 && (
                        <>
                          {' · '}
                          <strong>CC:</strong>{' '}
                          {selectedEmail.cc_personas.map(personaLabel).join(', ')}
                        </>
                      )}
                    </div>
                    <div className="text-slate-400">
                      Thread: {selectedEmail.thread_id}
                      {selectedEmail.po_id ? ` · Linked PO #${selectedEmail.po_id}` : ''}
                    </div>
                  </div>
                </div>

                {!isLex && (
                  <button
                    type="button"
                    onClick={() => openCompose(selectedEmail)}
                    className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-semibold text-slate-800 hover:bg-slate-50"
                  >
                    <Reply className="h-3.5 w-3.5" />
                    Reply
                  </button>
                )}
              </div>

              {/* Draft approval controls if selected message is pending approval */}
              {selectedEmail.status === 'DRAFT_PENDING_APPROVAL' && (
                <div className="rounded-xl border border-indigo-200 bg-indigo-50/50 p-4 space-y-3">
                  <div className="flex items-center justify-between text-xs">
                    <span className="font-bold text-indigo-950 flex items-center gap-1.5">
                      <Sparkles className="h-3.5 w-3.5 text-indigo-600" />
                      Lex Draft Pending Manager Approval ({selectedEmail.category})
                    </span>
                  </div>
                  <input
                    type="text"
                    value={draftSubject}
                    onChange={(e) => setDraftSubject(e.target.value)}
                    className="w-full rounded-lg border border-indigo-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-900 focus:border-indigo-500 focus:outline-none"
                  />
                  <textarea
                    rows={5}
                    value={draftBody}
                    onChange={(e) => setDraftBody(e.target.value)}
                    className="w-full rounded-lg border border-indigo-200 bg-white px-3 py-2 text-xs text-slate-800 focus:border-indigo-500 focus:outline-none"
                  />
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      disabled={busyDraftId === selectedEmail.id}
                      onClick={() => void handleApproveDraft(selectedEmail)}
                      className="inline-flex items-center gap-1.5 rounded-lg bg-slate-900 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-slate-800 disabled:opacity-50"
                    >
                      <Check className="h-3.5 w-3.5" />
                      Approve &amp; Send
                    </button>
                    <button
                      type="button"
                      disabled={busyDraftId === selectedEmail.id}
                      onClick={() => void handleRejectDraft(selectedEmail)}
                      className="inline-flex items-center gap-1.5 rounded-lg border border-rose-200 bg-white px-3 py-1.5 text-xs font-semibold text-rose-700 hover:bg-rose-50 disabled:opacity-50"
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
                    className={`rounded-lg border p-3.5 text-xs space-y-2 ${
                      msg.id === selectedEmail.id
                        ? 'border-sky-200 bg-sky-50/30'
                        : 'border-slate-200 bg-white'
                    }`}
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="font-semibold text-slate-900">
                        {personaLabel(msg.from_persona)} →{' '}
                        {msg.to_personas.map(personaLabel).join(', ')}
                      </span>
                      <div className="flex flex-wrap items-center gap-1.5">
                        {msg.ai_classification && (
                          <span className="rounded bg-indigo-100 px-2 py-0.5 text-[11px] font-semibold text-indigo-800">
                            Lex: {msg.ai_classification}
                            {msg.ai_confidence !== null &&
                              ` (${Math.round(msg.ai_confidence * 100)}%)`}
                          </span>
                        )}
                        <span className="rounded bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-700">
                          {msg.status}
                        </span>
                        <span className="text-[11px] text-slate-400">
                          {formatSimDateTime(msg.timestamp)}
                        </span>
                      </div>
                    </div>

                    <p className="whitespace-pre-wrap text-slate-800 leading-relaxed">
                      {msg.body}
                    </p>

                    {msg.attachment_path && (
                      <div className="inline-flex items-center gap-1.5 rounded border border-slate-200 bg-slate-50 px-2.5 py-1 text-[11px] font-medium text-slate-700">
                        <Paperclip className="h-3 w-3" />
                        {msg.attachment_path}
                      </div>
                    )}

                    {msg.ai_extracted_json &&
                      Object.keys(msg.ai_extracted_json).length > 0 && (
                        <details className="rounded border border-indigo-100 bg-indigo-50/40 p-2">
                          <summary className="cursor-pointer font-semibold text-indigo-900">
                            Lex Triage Extraction
                          </summary>
                          <pre className="mt-1.5 overflow-x-auto text-[11px] text-slate-700">
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
        <div className="fixed inset-0 z-40 flex items-center justify-center bg-slate-900/40 p-4">
          <div className="w-full max-w-xl rounded-xl border border-slate-200 bg-white p-5 shadow-xl">
            <div className="flex items-center justify-between border-b border-slate-200 pb-3">
              <h2 className="text-base font-bold text-slate-900">
                New Email from {personaLabel(persona)}
              </h2>
              <button
                type="button"
                onClick={() => setComposeOpen(false)}
                className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <form onSubmit={handleSendEmail} className="mt-4 space-y-3 text-xs">
              <div>
                <label className="block font-semibold text-slate-700 mb-1">To</label>
                <div className="flex flex-wrap gap-1.5">
                  {PERSONAS.filter((p) => p !== persona).map((p) => {
                    const checked = toPersonas.includes(p);
                    return (
                      <button
                        key={p}
                        type="button"
                        onClick={() => toggleRecipient(toPersonas, setToPersonas, p)}
                        className={`rounded-full border px-2.5 py-1 font-semibold transition ${
                          checked
                            ? 'border-slate-900 bg-slate-900 text-white'
                            : 'border-slate-200 bg-slate-50 text-slate-700 hover:bg-slate-100'
                        }`}
                      >
                        {personaLabel(p)}
                      </button>
                    );
                  })}
                </div>
              </div>

              <div>
                <label className="block font-semibold text-slate-700 mb-1">
                  CC (optional)
                </label>
                <div className="flex flex-wrap gap-1.5">
                  {PERSONAS.filter((p) => p !== persona).map((p) => {
                    const checked = ccPersonas.includes(p);
                    return (
                      <button
                        key={p}
                        type="button"
                        onClick={() => toggleRecipient(ccPersonas, setCcPersonas, p)}
                        className={`rounded-full border px-2.5 py-1 font-medium transition ${
                          checked
                            ? 'border-indigo-700 bg-indigo-700 text-white'
                            : 'border-slate-200 bg-white text-slate-600 hover:bg-slate-50'
                        }`}
                      >
                        {personaLabel(p)}
                      </button>
                    );
                  })}
                </div>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label className="block font-semibold text-slate-700 mb-1">
                    Linked PO (optional)
                  </label>
                  <select
                    value={composePoId}
                    onChange={(e) => setComposePoId(e.target.value)}
                    className="w-full rounded-lg border border-slate-300 px-2.5 py-1.5 text-xs focus:border-slate-500 focus:outline-none"
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
                  <label className="block font-semibold text-slate-700 mb-1">
                    PDF Attachment (optional)
                  </label>
                  <input
                    type="file"
                    accept=".pdf,application/pdf"
                    onChange={(e) => setComposeFile(e.target.files?.[0] ?? null)}
                    className="w-full text-xs text-slate-600 file:mr-2 file:rounded-md file:border-0 file:bg-slate-100 file:px-2.5 file:py-1 file:text-xs file:font-semibold file:text-slate-700 hover:file:bg-slate-200"
                  />
                </div>
              </div>

              <div>
                <label className="block font-semibold text-slate-700 mb-1">Subject</label>
                <input
                  type="text"
                  required
                  value={composeSubject}
                  onChange={(e) => setComposeSubject(e.target.value)}
                  placeholder="Subject line..."
                  className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
                />
              </div>

              <div>
                <label className="block font-semibold text-slate-700 mb-1">Body</label>
                <textarea
                  rows={5}
                  required
                  value={composeBody}
                  onChange={(e) => setComposeBody(e.target.value)}
                  placeholder="Write message..."
                  className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
                />
              </div>

              <div className="flex justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={() => setComposeOpen(false)}
                  className="rounded-lg border border-slate-200 px-3 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={sending}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-slate-900 px-4 py-2 text-xs font-semibold text-white hover:bg-slate-800 disabled:opacity-50"
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
