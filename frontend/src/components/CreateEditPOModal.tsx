import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Calculator, RefreshCw, Sparkles, X } from 'lucide-react';
import { api } from '../api/client';
import type {
  PODetail,
  POListItem,
  POSuggestionResult,
  PartItem,
  PartSupplierTerm,
} from '../api/types';
import { formatBurnRate, formatInr, personaLabel } from '../utils/format';

export type CreateEditPOModalProps =
  | {
      mode: 'create';
      part: PartItem;
      onClose: () => void;
      onNotify: (kind: 'success' | 'error' | 'info', title: string, detail?: string) => void;
      onSuccess: (po: POListItem) => void | Promise<void>;
    }
  | {
      mode: 'edit';
      poId: number;
      initialPo?: PODetail | null;
      initialPart?: PartItem | null;
      initialDraftSubject?: string;
      initialDraftBody?: string;
      onClose: () => void;
      onNotify: (kind: 'success' | 'error' | 'info', title: string, detail?: string) => void;
      onSuccess: (po: POListItem) => void | Promise<void>;
    };

export function CreateEditPOModal(props: CreateEditPOModalProps) {
  const { mode, onClose, onNotify, onSuccess } = props;
  const editPoId = mode === 'edit' ? props.poId : null;
  const initialPo = mode === 'edit' ? props.initialPo ?? null : null;
  const initialPart = mode === 'create' ? props.part : props.initialPart ?? null;
  const initialDraftSubject = mode === 'edit' ? props.initialDraftSubject : undefined;
  const initialDraftBody = mode === 'edit' ? props.initialDraftBody : undefined;

  const [part, setPart] = useState<PartItem | null>(initialPart);
  const [poDetail, setPoDetail] = useState<PODetail | null>(initialPo);
  const [loadingContext, setLoadingContext] = useState(mode === 'edit');
  const [contextError, setContextError] = useState<string | null>(null);

  const [supplierId, setSupplierId] = useState<number | null>(null);
  const [qtyInput, setQtyInput] = useState('');
  const [unitPriceInput, setUnitPriceInput] = useState('');
  const [noteInput, setNoteInput] = useState('');

  const [suggestion, setSuggestion] = useState<POSuggestionResult | null>(null);
  const [suggestionLoading, setSuggestionLoading] = useState(false);

  const [draftGenerating, setDraftGenerating] = useState(false);
  const [draftReady, setDraftReady] = useState(false);
  const [subject, setSubject] = useState('');
  const [body, setBody] = useState('');
  const [draftBaseline, setDraftBaseline] = useState<{
    supplierId: number | null;
    qty: string;
    unitPrice: string;
  } | null>(null);

  const [submitting, setSubmitting] = useState(false);
  const suggestionSeq = useRef(0);

  const fetchSuggestion = useCallback(
    async (partId: number, targetSupplierId: number, applyToQty: boolean) => {
      const seq = ++suggestionSeq.current;
      setSuggestionLoading(true);
      try {
        const res = await api.getPartPoSuggestion(partId, targetSupplierId);
        if (seq !== suggestionSeq.current) return;
        setSuggestion(res);
        if (applyToQty && res.suggested_qty > 0) {
          setQtyInput(String(res.suggested_qty));
        }
      } catch (err) {
        if (seq !== suggestionSeq.current) return;
        setSuggestion(null);
        onNotify(
          'error',
          'Failed to load PO quantity suggestion',
          err instanceof Error ? err.message : String(err),
        );
      } finally {
        if (seq === suggestionSeq.current) {
          setSuggestionLoading(false);
        }
      }
    },
    [onNotify],
  );

  // Initialize context for create or edit mode
  useEffect(() => {
    let cancelled = false;

    if (mode === 'create' && initialPart) {
      const primary =
        initialPart.suppliers.find((s) => s.supplier_rank === 1) ??
        initialPart.suppliers[0] ??
        null;
      const defaultSupId = primary?.supplier_id ?? null;
      const fallbackQty = Math.max(
        primary?.moq ?? 1,
        initialPart.replenishment_point - initialPart.inventory_position,
      );

      setPart(initialPart);
      setPoDetail(null);
      setLoadingContext(false);
      setContextError(null);
      setSupplierId(defaultSupId);
      setQtyInput(String(fallbackQty > 0 ? fallbackQty : 1));
      setUnitPriceInput(primary ? String(primary.agreed_unit_price) : '');
      setNoteInput('');
      setDraftReady(false);
      setSubject('');
      setBody('');
      setDraftBaseline(null);

      if (defaultSupId !== null) {
        void fetchSuggestion(initialPart.part_id, defaultSupId, true);
      }
      return () => {
        cancelled = true;
      };
    }

    if (mode === 'edit' && editPoId !== null) {
      const loadEditContext = async () => {
        setLoadingContext(true);
        setContextError(null);
        try {
          const loadedPo = initialPo ?? (await api.getPoDetail(editPoId));
          if (cancelled) return;
          setPoDetail(loadedPo);

          let loadedPart = initialPart;
          if (!loadedPart || loadedPart.part_id !== loadedPo.part_id) {
            const allParts = await api.listParts();
            if (cancelled) return;
            loadedPart = allParts.find((p) => p.part_id === loadedPo.part_id) ?? null;
          }
          setPart(loadedPart);

          setSupplierId(loadedPo.supplier_id);
          const initQty = String(loadedPo.qty);
          const initPrice = String(loadedPo.unit_price);
          setQtyInput(initQty);
          setUnitPriceInput(initPrice);
          setNoteInput('');

          const pendingComm =
            [...loadedPo.communications]
              .reverse()
              .find((c) => c.status === 'DRAFT_PENDING_APPROVAL') ??
            [...loadedPo.communications]
              .reverse()
              .find(
                (c) =>
                  c.direction === 'OUTBOUND' &&
                  (c.category === 'PO_DISPATCH' || c.category === 'REVISED_PO'),
              ) ??
            null;

          const resolvedSubject = initialDraftSubject ?? pendingComm?.subject ?? '';
          const resolvedBody = initialDraftBody ?? pendingComm?.body ?? '';
          setSubject(resolvedSubject);
          setBody(resolvedBody);
          const hasDraft =
            resolvedSubject.trim().length > 0 && resolvedBody.trim().length > 0;
          setDraftReady(hasDraft);
          setDraftBaseline(
            hasDraft
              ? {
                  supplierId: loadedPo.supplier_id,
                  qty: initQty,
                  unitPrice: initPrice,
                }
              : null,
          );

          void fetchSuggestion(loadedPo.part_id, loadedPo.supplier_id, false);
        } catch (err) {
          if (cancelled) return;
          setContextError(err instanceof Error ? err.message : String(err));
        } finally {
          if (!cancelled) {
            setLoadingContext(false);
          }
        }
      };

      void loadEditContext();
    }

    return () => {
      cancelled = true;
    };
  }, [
    mode,
    editPoId,
    initialPo,
    initialPart,
    initialDraftSubject,
    initialDraftBody,
    fetchSuggestion,
  ]);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !submitting) {
        onClose();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [onClose, submitting]);

  const suppliers: PartSupplierTerm[] = useMemo(() => part?.suppliers ?? [], [part]);

  const selectedSupplier: PartSupplierTerm | null = useMemo(
    () => suppliers.find((s) => s.supplier_id === supplierId) ?? null,
    [suppliers, supplierId],
  );

  const parsedQty = Number.parseInt(qtyInput, 10);
  const parsedUnitPrice = Number.parseFloat(unitPriceInput);

  const effectiveUnitPrice = useMemo(() => {
    if (mode === 'edit') {
      return Number.isFinite(parsedUnitPrice) && parsedUnitPrice > 0
        ? parsedUnitPrice
        : (selectedSupplier?.agreed_unit_price ?? poDetail?.unit_price ?? 0);
    }
    return selectedSupplier?.agreed_unit_price ?? 0;
  }, [mode, parsedUnitPrice, selectedSupplier, poDetail]);

  const handleSelectSupplier = (sup: PartSupplierTerm) => {
    setSupplierId(sup.supplier_id);
    if (mode === 'create') {
      setUnitPriceInput(String(sup.agreed_unit_price));
      setDraftReady(false);
      setSubject('');
      setBody('');
      if (part) {
        void fetchSuggestion(part.part_id, sup.supplier_id, true);
      }
    } else {
      setUnitPriceInput(String(sup.agreed_unit_price));
      if (part) {
        void fetchSuggestion(part.part_id, sup.supplier_id, false);
      } else if (poDetail) {
        void fetchSuggestion(poDetail.part_id, sup.supplier_id, false);
      }
    }
  };

  const handleGenerateDraft = async () => {
    const targetPartId = part?.part_id ?? poDetail?.part_id ?? null;
    if (
      targetPartId === null ||
      supplierId === null ||
      !Number.isFinite(parsedQty) ||
      parsedQty <= 0
    ) {
      return;
    }
    setDraftGenerating(true);
    try {
      const draft = await api.generateManualPoDraft(targetPartId, supplierId, parsedQty);
      setSubject(draft.subject);
      setBody(draft.body);
      setDraftReady(true);
      setDraftBaseline({
        supplierId,
        qty: String(parsedQty),
        unitPrice: unitPriceInput,
      });
    } catch (err) {
      onNotify(
        'error',
        'Failed to generate PO draft',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setDraftGenerating(false);
    }
  };

  const canGenerateDraft =
    !loadingContext &&
    supplierId !== null &&
    Number.isFinite(parsedQty) &&
    parsedQty > 0 &&
    !draftGenerating &&
    !submitting;

  const canSubmit =
    !loadingContext &&
    !contextError &&
    supplierId !== null &&
    Number.isFinite(parsedQty) &&
    parsedQty > 0 &&
    !submitting &&
    (mode === 'create'
      ? part !== null &&
        draftReady &&
        subject.trim().length > 0 &&
        body.trim().length > 0
      : poDetail !== null &&
        Number.isFinite(parsedUnitPrice) &&
        parsedUnitPrice > 0);

  const editTermsChangedSinceDraft =
    mode === 'edit' &&
    draftBaseline !== null &&
    (draftBaseline.supplierId !== supplierId ||
      draftBaseline.qty !== qtyInput ||
      draftBaseline.unitPrice !== unitPriceInput);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit || supplierId === null) return;
    setSubmitting(true);
    try {
      if (mode === 'create' && part) {
        const res = await api.createManualPo({
          part_id: part.part_id,
          supplier_id: supplierId,
          qty: parsedQty,
          subject: subject.trim(),
          body: body.trim(),
        });
        onNotify(
          'success',
          `Created & dispatched ${res.po.po_number}`,
          `${parsedQty} units of ${part.sku} ordered from ${selectedSupplier?.supplier_name ?? 'supplier'}.`,
        );
        await onSuccess(res.po);
        onClose();
      } else if (mode === 'edit' && poDetail) {
        const res = await api.dispatchEditedPo(poDetail.id, {
          supplier_id: supplierId,
          qty: parsedQty,
          unit_price: parsedUnitPrice,
          subject: subject.trim() || undefined,
          body: body.trim() || undefined,
          note: noteInput.trim() || undefined,
        });
        onNotify(
          'success',
          `Dispatched ${res.po.po_number}`,
          `${res.po.qty} units @ ${formatInr(res.po.unit_price)} sent to ${res.po.supplier_name ?? selectedSupplier?.supplier_name ?? 'supplier'}.`,
        );
        await onSuccess(res.po);
        onClose();
      }
    } catch (err) {
      onNotify(
        'error',
        mode === 'create' ? 'Failed to create PO' : 'Failed to dispatch edited PO',
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setSubmitting(false);
    }
  };

  if (typeof document === 'undefined') return null;

  const titleSku = part?.sku ?? poDetail?.part_sku ?? (poDetail ? `Part #${poDetail.part_id}` : '');
  const titleName = part?.name ?? poDetail?.part_name ?? '';
  const modalTitle =
    mode === 'create'
      ? `Create PO — ${titleSku}`
      : `Edit & Dispatch ${poDetail?.po_number ?? `PO #${editPoId}`}${titleSku ? ` — ${titleSku}` : ''}`;

  return createPortal(
    <div
      onClick={() => {
        if (!submitting) onClose();
      }}
      className="anim-backdrop fixed inset-0 z-50 flex items-center justify-center bg-black/32 backdrop-blur-xs p-4"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={modalTitle}
        onClick={(e) => e.stopPropagation()}
        className="anim-modal-pop w-full max-w-2xl max-h-[90vh] overflow-y-auto rounded-2xl border border-black/[0.08] bg-white p-5 shadow-[0_18px_48px_rgba(0,0,0,0.14)]"
      >
        <div className="flex items-start justify-between gap-2">
          <div>
            <h2 className="text-base font-bold tracking-tight text-[#1D1D1F]">{modalTitle}</h2>
            {titleName && <p className="text-xs text-[#6E6E73] mt-0.5">{titleName}</p>}
          </div>
          <button
            type="button"
            aria-label="Close PO modal"
            disabled={submitting}
            onClick={onClose}
            className="apple-press rounded-full p-1.5 text-[#86868B] hover:bg-black/[0.05] hover:text-[#1D1D1F] disabled:opacity-45"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {loadingContext ? (
          <div className="py-10 text-center text-xs text-[#6E6E73]">
            Loading Purchase Order &amp; supplier recommendations...
          </div>
        ) : contextError ? (
          <div className="mt-4 rounded-xl border border-[#C9342D]/25 bg-[#C9342D]/[0.06] p-4 text-xs text-[#B82923]">
            {contextError}
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="mt-4 space-y-4">
            {/* Supplier Selector */}
            <div>
              <label className="block text-xs font-semibold text-[#1D1D1F] mb-1.5">
                Select Supplier (ranked by configured priority &amp; performance)
              </label>
              {suppliers.length === 0 ? (
                <div className="rounded-xl border border-black/[0.08] bg-[#FBFBFD] px-3 py-2 text-xs text-[#6E6E73]">
                  Supplier ID #{supplierId ?? poDetail?.supplier_id ?? '—'} (no supplier term catalog loaded)
                </div>
              ) : (
                <div className="grid grid-cols-1 gap-2">
                  {suppliers.map((sup) => {
                    const isSelected = sup.supplier_id === supplierId;
                    const score = sup.reliability_score;
                    const scoreColor =
                      score !== null && score !== undefined
                        ? score >= 80
                          ? 'text-[#1B6B43] bg-[#1B6B43]/10 border-[#1B6B43]/25'
                          : score >= 60
                            ? 'text-[#945600] bg-[#B46900]/10 border-[#B46900]/25'
                            : 'text-[#B82923] bg-[#C9342D]/10 border-[#C9342D]/25'
                        : 'text-[#6E6E73] bg-black/[0.04] border-black/[0.08]';
                    return (
                      <button
                        key={sup.supplier_id}
                        type="button"
                        onClick={() => handleSelectSupplier(sup)}
                        className={`text-left rounded-xl border p-3 transition-colors ${
                          isSelected
                            ? 'border-[#0066CC] bg-[#0066CC]/[0.05] ring-1 ring-[#0066CC]'
                            : 'border-black/[0.10] bg-[#FBFBFD] hover:border-black/[0.20]'
                        }`}
                      >
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <div className="flex items-center gap-2">
                            <span className="rounded-md bg-black/[0.06] px-1.5 py-0.5 text-[10px] font-bold text-[#1D1D1F]">
                              {sup.supplier_rank}°
                            </span>
                            <span className="text-sm font-semibold text-[#1D1D1F]">
                              {sup.supplier_persona
                                ? personaLabel(sup.supplier_persona)
                                : sup.supplier_name}
                            </span>
                          </div>
                          <div className="flex items-center gap-2 tabular-nums">
                            {score !== null && score !== undefined && (
                              <span
                                className={`rounded-full border px-2 py-0.5 text-[11px] font-semibold ${scoreColor}`}
                              >
                                Score {score.toFixed(1)}/100
                              </span>
                            )}
                            <span className="text-sm font-bold text-[#1D1D1F]">
                              {formatInr(sup.agreed_unit_price)}/unit
                            </span>
                          </div>
                        </div>
                        <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-[#6E6E73] tabular-nums">
                          <span>
                            Lead Time:{' '}
                            <strong className="text-[#1D1D1F]">
                              {sup.effective_lead_time_days.toFixed(1)}d eff
                            </strong>{' '}
                            (quoted {sup.quoted_lead_time_days}d)
                          </span>
                          {sup.estimated_arrival_date && (
                            <span>
                              Est. Arrival:{' '}
                              <strong className="text-[#1D1D1F]">
                                {sup.estimated_arrival_date}
                              </strong>
                            </span>
                          )}
                          <span>MOQ: {sup.moq}</span>
                          {sup.otd_rate !== null && sup.otd_rate !== undefined && (
                            <span>OTD: {Math.round(sup.otd_rate * 100)}%</span>
                          )}
                          {sup.response_rate !== null && sup.response_rate !== undefined && (
                            <span>Resp: {Math.round(sup.response_rate * 100)}%</span>
                          )}
                        </div>
                        {sup.latest_memory_note && (
                          <div className="mt-1 text-[11px] text-[#86868B] italic truncate">
                            Note: {sup.latest_memory_note}
                          </div>
                        )}
                      </button>
                    );
                  })}
                </div>
              )}
            </div>

            {/* Suggestion & Reasoning Card */}
            <div
              data-testid="po-suggestion-card"
              className="rounded-xl border border-[#0066CC]/22 bg-[#0066CC]/[0.03] p-3.5 space-y-2.5"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-1.5 text-xs font-bold text-[#0058B3]">
                  <Calculator className="h-3.5 w-3.5" />
                  <span>Suggestion &amp; Reasoning</span>
                  <span className="rounded-full bg-white border border-[#0066CC]/20 px-2 py-0.5 text-[10px] font-semibold text-[#6E6E73]">
                    EWMA α = 0.30
                  </span>
                </div>
                {suggestionLoading ? (
                  <span className="text-[11px] text-[#6E6E73]">Calculating...</span>
                ) : suggestion ? (
                  <div className="flex items-center gap-2 tabular-nums">
                    <span className="text-xs font-bold text-[#1D1D1F]">
                      Suggested: {suggestion.suggested_qty} units
                    </span>
                    {mode === 'edit' && parsedQty !== suggestion.suggested_qty && (
                      <button
                        type="button"
                        onClick={() => setQtyInput(String(suggestion.suggested_qty))}
                        className="apple-press rounded-lg border border-[#0066CC]/30 bg-white px-2.5 py-1 text-[11px] font-semibold text-[#0058B3] hover:bg-[#0066CC]/[0.08]"
                      >
                        Use Suggested Qty ({suggestion.suggested_qty})
                      </button>
                    )}
                  </div>
                ) : null}
              </div>

              {suggestion ? (
                <>
                  <div className="grid grid-cols-2 sm:grid-cols-3 gap-2 text-[11px] tabular-nums">
                    <div className="rounded-lg border border-black/[0.06] bg-white px-2.5 py-1.5">
                      <div className="text-[#86868B]">Inventory Position</div>
                      <div className="font-semibold text-[#1D1D1F]">
                        {suggestion.inventory_position} units
                      </div>
                    </div>
                    <div className="rounded-lg border border-black/[0.06] bg-white px-2.5 py-1.5">
                      <div className="text-[#86868B]">Daily Burn (EWMA α=0.30)</div>
                      <div className="font-semibold text-[#1D1D1F]">
                        {formatBurnRate(suggestion.daily_burn_ewma)}
                      </div>
                    </div>
                    <div className="rounded-lg border border-black/[0.06] bg-white px-2.5 py-1.5">
                      <div className="text-[#86868B]">Effective LT + Comm Buffer</div>
                      <div className="font-semibold text-[#1D1D1F]">
                        {suggestion.effective_lead_time_days.toFixed(1)}d +{' '}
                        {suggestion.comm_buffer_days.toFixed(1)}d
                      </div>
                    </div>
                    <div className="rounded-lg border border-black/[0.06] bg-white px-2.5 py-1.5">
                      <div className="text-[#86868B]">MOQ</div>
                      <div className="font-semibold text-[#1D1D1F]">{suggestion.moq} units</div>
                    </div>
                    <div className="rounded-lg border border-black/[0.06] bg-white px-2.5 py-1.5">
                      <div className="text-[#86868B]">Safety Stock</div>
                      <div className="font-semibold text-[#1D1D1F]">
                        {suggestion.safety_stock.toFixed(1)} units
                      </div>
                    </div>
                    <div className="rounded-lg border border-black/[0.06] bg-white px-2.5 py-1.5">
                      <div className="text-[#86868B]">Target Stock T (ROP)</div>
                      <div className="font-semibold text-[#1D1D1F]">
                        {suggestion.target_stock_t} ({suggestion.rop_for_supplier})
                      </div>
                    </div>
                  </div>
                  <p className="text-xs text-[#3A3A3C] leading-relaxed">{suggestion.reasoning}</p>
                </>
              ) : (
                !suggestionLoading && (
                  <p className="text-xs text-[#6E6E73]">
                    Select a supplier to calculate deterministic replenishment quantity.
                  </p>
                )
              )}
            </div>

            {/* Order Quantity / Unit Price / Total Value */}
            <div
              className={`grid grid-cols-1 ${
                mode === 'edit' ? 'sm:grid-cols-3' : 'sm:grid-cols-2'
              } gap-3 items-end`}
            >
              <div>
                <label className="block text-xs font-semibold text-[#1D1D1F] mb-1">
                  Order Quantity
                </label>
                <input
                  type="number"
                  min={1}
                  value={qtyInput}
                  onChange={(e) => {
                    setQtyInput(e.target.value);
                    if (mode === 'create') {
                      setDraftReady(false);
                    }
                  }}
                  required
                  className="w-full rounded-xl border border-black/[0.12] px-3 py-2 text-sm text-[#1D1D1F] tabular-nums focus:border-[#0066CC] focus:outline-none"
                />
              </div>

              {mode === 'edit' && (
                <div>
                  <label className="block text-xs font-semibold text-[#1D1D1F] mb-1">
                    Unit Price (INR)
                  </label>
                  <input
                    type="number"
                    min={0.01}
                    step="0.01"
                    value={unitPriceInput}
                    onChange={(e) => setUnitPriceInput(e.target.value)}
                    required
                    className="w-full rounded-xl border border-black/[0.12] px-3 py-2 text-sm text-[#1D1D1F] tabular-nums focus:border-[#0066CC] focus:outline-none"
                  />
                </div>
              )}

              <div className="rounded-xl border border-black/[0.08] bg-[#FBFBFD] px-3 py-2 flex items-center justify-between text-xs tabular-nums min-h-[38px]">
                <span className="text-[#6E6E73]">Total PO Value:</span>
                <span className="text-sm font-bold text-[#1D1D1F]">
                  {effectiveUnitPrice > 0 && Number.isFinite(parsedQty) && parsedQty > 0
                    ? formatInr(effectiveUnitPrice * parsedQty)
                    : '—'}
                </span>
              </div>
            </div>

            {mode === 'edit' && (
              <div>
                <label className="block text-xs font-semibold text-[#1D1D1F] mb-1">
                  Operational Note (optional)
                </label>
                <input
                  type="text"
                  placeholder="e.g., Adjusted qty after checking supplier lead time & MOQ"
                  value={noteInput}
                  onChange={(e) => setNoteInput(e.target.value)}
                  className="w-full rounded-xl border border-black/[0.12] px-3 py-2 text-xs text-[#1D1D1F] placeholder:text-[#86868B] focus:border-[#0066CC] focus:outline-none"
                />
              </div>
            )}

            {/* Generate / Regenerate Lex Draft */}
            <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-black/[0.06] bg-[#FBFBFD] px-3 py-2.5">
              <div className="text-xs text-[#6E6E73]">
                {editTermsChangedSinceDraft ? (
                  <span className="text-[#945600] font-medium">
                    Supplier, quantity, or price changed — regenerate Lex&apos;s draft or edit the email below before dispatching.
                  </span>
                ) : draftReady ? (
                  'Lex draft ready below. Review or edit before dispatching.'
                ) : mode === 'edit' ? (
                  'Edit email below, or click Regenerate Draft to build a fresh Lex PO email (leave blank to auto-draft on dispatch).'
                ) : (
                  'Set supplier & quantity, then ask Lex to draft the initial PO email.'
                )}
              </div>
              <button
                type="button"
                disabled={!canGenerateDraft}
                onClick={() => void handleGenerateDraft()}
                className="apple-press shrink-0 inline-flex items-center gap-1.5 rounded-xl bg-[#1D1D1F] px-3.5 py-2 text-xs font-semibold text-white hover:bg-black disabled:opacity-35 disabled:cursor-not-allowed"
              >
                {draftReady ? <RefreshCw className="h-3.5 w-3.5" /> : <Sparkles className="h-3.5 w-3.5" />}
                {draftGenerating
                  ? 'Generating...'
                  : draftReady
                    ? 'Regenerate Draft'
                    : 'Generate PO draft'}
              </button>
            </div>

            {(draftReady || mode === 'edit') && (
              <div className="space-y-2.5 rounded-xl border border-[#0066CC]/25 bg-[#0066CC]/[0.03] p-3">
                <div className="text-[11px] font-medium text-[#6E6E73]">
                  To:{' '}
                  <strong className="text-[#1D1D1F]">
                    {selectedSupplier?.supplier_persona
                      ? personaLabel(selectedSupplier.supplier_persona)
                      : selectedSupplier?.supplier_name ?? poDetail?.supplier_name ?? 'Supplier'}
                  </strong>{' '}
                  · CC: <strong className="text-[#1D1D1F]">Lex (AI Agent), Inventory Manager</strong>
                </div>
                <div>
                  <label className="block text-xs font-semibold text-[#1D1D1F] mb-1">
                    Subject {mode === 'edit' ? '(optional — auto-drafted if blank)' : ''}
                  </label>
                  <input
                    type="text"
                    value={subject}
                    onChange={(e) => setSubject(e.target.value)}
                    required={mode === 'create'}
                    placeholder={
                      mode === 'edit' ? 'Leave blank to auto-generate with Lex' : undefined
                    }
                    className="w-full rounded-xl border border-black/[0.12] bg-white px-3 py-1.5 text-sm text-[#1D1D1F] focus:border-[#0066CC] focus:outline-none"
                  />
                </div>
                <div>
                  <label className="block text-xs font-semibold text-[#1D1D1F] mb-1">
                    Email Body {mode === 'edit' ? '(Markdown · optional)' : '(Markdown)'}
                  </label>
                  <textarea
                    rows={6}
                    value={body}
                    onChange={(e) => setBody(e.target.value)}
                    required={mode === 'create'}
                    placeholder={
                      mode === 'edit' ? 'Leave blank to auto-generate with Lex' : undefined
                    }
                    className="w-full rounded-xl border border-black/[0.12] bg-white px-3 py-2 text-sm text-[#1D1D1F] focus:border-[#0066CC] focus:outline-none"
                  />
                </div>
              </div>
            )}

            <div className="flex justify-end gap-2 pt-1">
              <button
                type="button"
                disabled={submitting}
                onClick={onClose}
                className="apple-press rounded-xl border border-black/[0.10] px-3.5 py-2 text-xs font-medium text-[#1D1D1F] hover:bg-[#FBFBFD] disabled:opacity-45"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={!canSubmit}
                className="apple-press rounded-xl bg-[#0066CC] px-4 py-2 text-xs font-semibold text-white hover:bg-[#0071E3] disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {submitting
                  ? mode === 'create'
                    ? 'Creating PO...'
                    : 'Dispatching PO...'
                  : mode === 'create'
                    ? 'Create PO'
                    : 'Save & Dispatch PO'}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>,
    document.body,
  );
}
