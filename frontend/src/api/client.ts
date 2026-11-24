import type {
  AdvanceClockResponse,
  AgentTraceItem,
  AlertItem,
  ApproveDraftResponse,
  AutonomyPolicyItem,
  ClockResponse,
  ConsumeResponse,
  CycleAnalyticsResult,
  DispatchEditedPORequest,
  DispatchEditedPOResponse,
  EscalateProcurementResponse,
  EvalRunReport,
  EvalSuiteMetadata,
  MailroomSendResponse,
  ManualPOCreateResponse,
  ManualPODraftResponse,
  ObservabilityStatusResponse,
  PODetail,
  POListItem,
  POSuggestionResult,
  PartItem,
  PartSnapshot,
  PersonaKey,
  PlanningOverview,
  PlanningRecalcResponse,
  RecordGRNResponse,
  RejectDraftResponse,
  StoredEmail,
  SupplierItem,
  SupplierMemoryNote,
  SwitchSupplierResponse,
} from './types';

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    let message = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      if (typeof data?.detail === 'string') {
        message = data.detail;
      } else if (Array.isArray(data?.detail)) {
        message = data.detail.map((d: { msg?: string }) => d.msg ?? JSON.stringify(d)).join('; ');
      }
    } catch {
      const text = await res.text().catch(() => '');
      if (text) message = text;
    }
    throw new ApiError(res.status, message);
  }
  return res.json() as Promise<T>;
}

function jsonPost<T>(url: string, body?: unknown): Promise<T> {
  return requestJson<T>(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
}

function jsonPatch<T>(url: string, body: unknown): Promise<T> {
  return requestJson<T>(url, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export const api = {
  // Clock & Admin
  getClock: () => requestJson<ClockResponse>('/api/clock'),
  advanceClock: (params: { days?: number; hours?: number }) =>
    jsonPost<AdvanceClockResponse>('/api/clock/advance', {
      days: params.days ?? 0,
      hours: params.hours ?? 0,
    }),
  jumpToPoEta: (poId: number, offsetDays = 0) =>
    jsonPost<AdvanceClockResponse>(`/api/clock/jump-eta/${poId}`, {
      offset_days: offsetDays,
    }),
  resetSeed: () => jsonPost<{ status: string; summary: Record<string, unknown> }>('/api/admin/reset-seed'),

  // Alerts
  listAlerts: (status?: 'OPEN' | 'RESOLVED' | 'DISMISSED') =>
    requestJson<AlertItem[]>(
      status ? `/api/alerts?status=${encodeURIComponent(status)}` : '/api/alerts',
    ),
  executeAlertCta: (
    actionItemId: number,
    action: string,
    params: Record<string, unknown> = {},
  ) =>
    jsonPost<Record<string, unknown>>(`/api/alerts/${actionItemId}/execute-cta`, {
      action,
      params,
    }),
  dismissAlert: (actionItemId: number) =>
    jsonPost<{ id: number; status: string }>(`/api/alerts/${actionItemId}/dismiss`),
  reopenAlert: (actionItemId: number) =>
    jsonPost<{ id: number; status: string }>(`/api/alerts/${actionItemId}/reopen`),
  acknowledgeDismissedAlert: (actionItemId: number) =>
    jsonPost<{ id: number; status: string; dismiss_resolved_at: string | null }>(
      `/api/alerts/${actionItemId}/acknowledge-dismissed`,
    ),

  // Parts / Inventory
  listParts: () => requestJson<PartItem[]>('/api/parts'),
  getPartCycleAnalytics: (partId: number) =>
    requestJson<CycleAnalyticsResult>(`/api/parts/${partId}/cycle-analytics`),
  getPartPoSuggestion: (partId: number, supplierId?: number) =>
    requestJson<POSuggestionResult>(
      supplierId !== undefined
        ? `/api/parts/${partId}/po-suggestion?supplier_id=${encodeURIComponent(String(supplierId))}`
        : `/api/parts/${partId}/po-suggestion`,
    ),
  consumeStock: (partId: number, qty: number, note: string) =>
    jsonPost<ConsumeResponse>(`/api/parts/${partId}/consume`, { qty, note }),
  updatePartRop: (partId: number, rop: number, replenishmentPoint: number) =>
    jsonPatch<{ part_id: number; sku: string; rop: number; replenishment_point: number }>(
      `/api/parts/${partId}/rop`,
      { rop, replenishment_point: replenishmentPoint },
    ),
  updateDemandOverride: (partId: number, manualDemandOverride: number | null) =>
    jsonPatch<PartSnapshot>(`/api/parts/${partId}/demand-override`, {
      manual_demand_override: manualDemandOverride,
    }),

  // Purchase Orders
  listPos: (status?: string) =>
    requestJson<POListItem[]>(
      status ? `/api/pos?status=${encodeURIComponent(status)}` : '/api/pos',
    ),
  getPoDetail: (poId: number) => requestJson<PODetail>(`/api/pos/${poId}`),
  recordGrn: (poId: number, receivedQty: number, condition = 'GOOD') =>
    jsonPost<RecordGRNResponse>(`/api/pos/${poId}/grn`, {
      received_qty: receivedQty,
      condition,
    }),
  updatePo: (
    poId: number,
    payload: {
      qty?: number;
      unit_price?: number;
      revised_eta?: string;
      note?: string;
    },
  ) => jsonPatch<POListItem>(`/api/pos/${poId}`, payload),
  switchPoSupplier: (poId: number, backupSupplierId?: number) =>
    jsonPost<SwitchSupplierResponse>(`/api/pos/${poId}/switch-supplier`, {
      backup_supplier_id: backupSupplierId ?? null,
    }),
  escalatePoToProcurement: (poId: number, reason = '') =>
    jsonPost<EscalateProcurementResponse>(`/api/pos/${poId}/escalate-procurement`, {
      reason,
    }),
  generateManualPoDraft: (partId: number, supplierId: number, qty: number) =>
    jsonPost<ManualPODraftResponse>('/api/pos/draft-manual', {
      part_id: partId,
      supplier_id: supplierId,
      qty,
    }),
  createManualPo: (payload: {
    part_id: number;
    supplier_id: number;
    qty: number;
    subject: string;
    body: string;
  }) => jsonPost<ManualPOCreateResponse>('/api/pos/manual', payload),
  dispatchEditedPo: (poId: number, payload: DispatchEditedPORequest) =>
    jsonPost<DispatchEditedPOResponse>(`/api/pos/${poId}/dispatch-edited`, payload),

  // Mailroom
  listInbox: (persona: PersonaKey) =>
    requestJson<StoredEmail[]>(`/api/mailroom/${persona}/inbox`),
  listSent: (persona: PersonaKey) =>
    requestJson<StoredEmail[]>(`/api/mailroom/${persona}/sent`),
  listDrafts: (persona: PersonaKey) =>
    requestJson<StoredEmail[]>(`/api/mailroom/${persona}/drafts`),
  getThread: (threadId: string) =>
    requestJson<StoredEmail[]>(`/api/mailroom/threads/${encodeURIComponent(threadId)}`),
  sendEmail: (formData: FormData) =>
    requestJson<MailroomSendResponse>('/api/mailroom/send', {
      method: 'POST',
      body: formData,
    }),
  approveDraft: (
    commId: number,
    editedSubject?: string | null,
    editedBody?: string | null,
  ) =>
    jsonPost<ApproveDraftResponse>(`/api/mailroom/drafts/${commId}/approve`, {
      edited_subject: editedSubject ?? null,
      edited_body: editedBody ?? null,
    }),
  rejectDraft: (commId: number) =>
    jsonPost<RejectDraftResponse>(`/api/mailroom/drafts/${commId}/reject`),

  // Suppliers & Planning
  listSuppliers: () => requestJson<SupplierItem[]>('/api/suppliers'),
  addSupplierMemory: (supplierId: number, note: string) =>
    jsonPost<SupplierMemoryNote & { supplier_id: number }>(
      `/api/suppliers/${supplierId}/memory`,
      { note },
    ),
  updateSupplierMemory: (supplierId: number, noteId: number, note: string) =>
    jsonPatch<SupplierMemoryNote & { supplier_id: number }>(
      `/api/suppliers/${supplierId}/memory/${noteId}`,
      { note },
    ),
  deleteSupplierMemory: (supplierId: number, noteId: number) =>
    requestJson<{ deleted: boolean; id: number }>(
      `/api/suppliers/${supplierId}/memory/${noteId}`,
      { method: 'DELETE' },
    ),
  getPlanningOverview: () => requestJson<PlanningOverview>('/api/planning/overview'),
  recalculatePlanning: () =>
    jsonPost<PlanningRecalcResponse>('/api/planning/recalculate'),
  applyPlanningRecommendation: (
    partId: number,
    rop?: number,
    replenishmentPoint?: number,
  ) =>
    jsonPost<Record<string, unknown>>(
      `/api/planning/parts/${partId}/apply-recommendation`,
      rop !== undefined || replenishmentPoint !== undefined
        ? { rop: rop ?? null, replenishment_point: replenishmentPoint ?? null }
        : {},
    ),

  // Observability & Evals
  getObservabilityStatus: () =>
    requestJson<ObservabilityStatusResponse>('/api/observability/status'),
  listTraces: (params?: { agentName?: string; taskType?: string; limit?: number }) => {
    const qs = new URLSearchParams();
    if (params?.agentName) qs.set('agent_name', params.agentName);
    if (params?.taskType) qs.set('task_type', params.taskType);
    if (params?.limit) qs.set('limit', String(params.limit));
    const q = qs.toString();
    return requestJson<AgentTraceItem[]>(
      q ? `/api/observability/traces?${q}` : '/api/observability/traces',
    );
  },
  listAutonomyPolicies: (agentName?: string) =>
    requestJson<AutonomyPolicyItem[]>(
      agentName
        ? `/api/observability/autonomy?agent_name=${encodeURIComponent(agentName)}`
        : '/api/observability/autonomy',
    ),
  getEvalSuite: (agentName: string) =>
    requestJson<EvalSuiteMetadata>(`/api/evals/${encodeURIComponent(agentName)}/suite`),
  listEvalRuns: (agentName: string, limit = 10) =>
    requestJson<EvalRunReport[]>(
      `/api/evals/${encodeURIComponent(agentName)}/run?limit=${limit}`,
    ),
  runEvalSuite: (agentName: string, mode: 'offline' | 'live' = 'offline') =>
    jsonPost<EvalRunReport>(`/api/evals/${encodeURIComponent(agentName)}/run`, { mode }),
};
