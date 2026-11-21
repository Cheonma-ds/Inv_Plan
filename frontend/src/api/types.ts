export type PersonaKey =
  | 'inventory_manager'
  | 'making'
  | 'procurement'
  | 'sup1'
  | 'sup2'
  | 'sup3'
  | 'lex';

export type NavTab =
  | 'alerts'
  | 'inventory'
  | 'pos'
  | 'mailroom'
  | 'planning'
  | 'ai_health';

export interface SLAEvent {
  po_id: number;
  po_number: string;
  event_type: string;
  summary: string;
  draft_comm_id: number | null;
  action_item_id: number | null;
}

export interface ClockResponse {
  sim_now: string;
}

export interface AdvanceClockResponse {
  sim_now: string;
  sla_events: SLAEvent[];
}

export interface SuggestedAction {
  action: string;
  label: string;
}

export interface AlertDraftPreview {
  id: number;
  status: string;
  category: string;
  to_personas: string[];
  cc_personas: string[];
  subject: string;
  body: string;
}

export interface AlertItem {
  id: number;
  type: string;
  severity: 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'INFO' | string;
  urgency_score: number;
  title: string;
  summary: string;
  po_id: number | null;
  part_id: number | null;
  assigned_team: string;
  status: 'OPEN' | 'RESOLVED' | 'DISMISSED' | string;
  draft_comm_id: number | null;
  draft_preview: AlertDraftPreview | null;
  suggested_actions: SuggestedAction[];
  cta_payload: Record<string, unknown>;
  created_at: string;
  resolved_at: string | null;
}

export interface PartSupplierTerm {
  supplier_id: number;
  supplier_name: string | null;
  supplier_persona: string | null;
  supplier_rank: number;
  agreed_unit_price: number;
  quoted_lead_time_days: number;
  effective_lead_time_days: number;
  moq: number;
}

export interface PartSnapshot {
  part_id: number;
  sku: string;
  name: string;
  criticality: 'CRITICAL' | 'STANDARD' | 'LOW' | string;
  on_hand_stock: number;
  quarantined_stock: number;
  on_order_stock: number;
  inventory_position: number;
  rop: number;
  replenishment_point: number;
  target_coverage_days: number;
  manual_demand_override: number | null;
}

export interface PartItem extends PartSnapshot {
  suppliers: PartSupplierTerm[];
}

export interface ConsumeResponse {
  consume: {
    snapshot: PartSnapshot;
    transaction_id: number;
    reorder_triggered: {
      part_id: number;
      sku: string;
      part_name: string;
      order_qty: number;
      agreed_unit_price: number;
      moq: number;
    } | null;
    accelerated_burn: {
      part_id: number;
      sku: string;
      open_po_ids: number[];
      earliest_eta: string;
      days_until_eta: number;
    } | null;
  };
  created_po: {
    id: number;
    po_number: string;
    qty: number;
    unit_price: number;
    status: string;
  } | null;
  draft_comm_id: number | null;
  alert_ids: number[];
}

export interface POListItem {
  id: number;
  po_number: string;
  part_id: number;
  part_sku: string | null;
  part_name: string | null;
  part_criticality: string | null;
  supplier_id: number;
  supplier_name: string | null;
  supplier_persona: string | null;
  qty: number;
  unit_price: number;
  total_amount: number;
  status: string;
  created_at: string;
  sent_at: string | null;
  acknowledged_at: string | null;
  promised_dispatch_date: string | null;
  actual_dispatch_date: string | null;
  carrier_name: string | null;
  tracking_number: string | null;
  dispatch_details_status:
    | 'NONE'
    | 'AWAITING_IMMEDIATE'
    | 'DEFERRED_2D'
    | 'COMPLETE'
    | 'TIMED_OUT'
    | string;
  dispatch_details_due_at: string | null;
  missing_dispatch_fields: string[];
  escalated_to_procurement_at: string | null;
  procurement_escalation_note: string;
  closed_at: string | null;
  promised_eta: string | null;
  revised_eta: string | null;
  follow_up_count: number;
  pre_eta_checked: boolean;
  notes: string | null;
}

export interface EscalateProcurementResponse {
  po: POListItem;
  comm_id: number;
  alert_id: number;
}

export interface GoodsReceiptItem {
  id: number;
  received_qty: number;
  received_at: string;
  condition: string;
  status: string;
}

export interface InvoiceItem {
  id: number;
  invoice_number: string;
  extracted_qty: number;
  extracted_unit_price: number;
  extracted_total: number;
  match_status: string;
  discrepancy_summary: string | null;
  version: number;
  pdf_path: string;
  received_at: string;
}

export interface POCommunicationItem {
  id: number;
  thread_id: string;
  direction: string;
  status: string;
  category: string;
  from_persona: string;
  to_personas: string[];
  cc_personas: string[];
  subject: string;
  body: string;
  attachment_path: string | null;
  ai_classification: string | null;
  ai_confidence: number | null;
  timestamp: string;
}

export interface PODetail extends POListItem {
  goods_receipts: GoodsReceiptItem[];
  invoices: InvoiceItem[];
  communications: POCommunicationItem[];
}

export interface RecordGRNResponse {
  grn_id: number;
  po_id: number;
  po_status: string;
  received_qty: number;
  grn_status: string;
  match_result: {
    match_status: string;
    discrepancy_summary?: string | null;
    stock_action?: string | null;
  } | null;
  alert_id: number | null;
  draft_comm_id: number | null;
}

export interface SwitchSupplierResponse {
  cancelled_po_id: number;
  new_po: POListItem;
  draft_comm_id: number | null;
  alert_id: number | null;
  quarantined_cleared_qty: number;
}

export interface StoredEmail {
  id: number;
  po_id: number | null;
  thread_id: string;
  direction: string;
  status: string;
  category: string;
  from_persona: string;
  to_personas: string[];
  cc_personas: string[];
  subject: string;
  body: string;
  attachment_path: string | null;
  ai_classification: string | null;
  ai_extracted_json: Record<string, unknown> | null;
  ai_confidence: number | null;
  approval_outcome: string | null;
  timestamp: string;
}

export interface MailroomSendResponse {
  email: StoredEmail;
  triage: Record<string, unknown> | null;
}

export interface ApproveDraftResponse {
  email: StoredEmail;
  approval_outcome: string;
  policy_counts: Record<string, unknown>;
  po_transitioned_to: string | null;
}

export interface RejectDraftResponse {
  email: StoredEmail;
  approval_outcome: string;
  policy_counts: Record<string, unknown>;
}

export interface SupplierMemoryNote {
  id: number;
  note: string;
  source: string;
  created_at: string;
  updated_at: string;
}

export interface SupplierItem {
  id: number;
  name: string;
  persona_key: string;
  email: string;
  phone: string;
  reliability_score: number;
  otd_rate: number;
  invoice_accuracy_rate: number;
  response_rate: number;
  memory_notes: SupplierMemoryNote[];
}

export interface DemandForecastResult {
  part_id: number;
  sku: string;
  monthly_buckets: number[];
  ewma_monthly_demand: number;
  avg_daily_demand: number;
  daily_demand_std: number;
  is_intermittent: boolean;
  zero_month_count: number;
  rolling_peak_floor_monthly: number | null;
  manual_demand_override: number | null;
  effective_daily_demand: number;
}

export interface SupplierScoreResult {
  supplier_id: number;
  persona_key: string;
  name: string;
  closed_po_count: number;
  otd_rate: number;
  avg_days_late: number;
  otd_component: number;
  lead_time_std_dev: number;
  lead_time_consistency_component: number;
  invoice_accuracy_rate: number;
  invoice_accuracy_component: number;
  response_rate: number;
  responsiveness_component: number;
  reliability_score: number;
}

export interface EffectiveLeadTimeResult {
  term_id: number;
  part_id: number;
  supplier_id: number;
  supplier_rank: number;
  quoted_lead_time_days: number;
  closed_delivery_count: number;
  actual_mean_lead_time_days: number | null;
  effective_lead_time_days: number;
  lead_time_std_dev: number;
}

export interface ROPRecommendation {
  part_id: number;
  sku: string;
  part_name: string;
  criticality: string;
  primary_supplier_id: number | null;
  primary_supplier_name: string | null;
  quoted_lead_time_days: number;
  effective_lead_time_days: number;
  lead_time_std_dev: number;
  avg_daily_demand: number;
  effective_daily_demand: number;
  daily_demand_std: number;
  is_intermittent: boolean;
  manual_demand_override: number | null;
  z_factor: number;
  safety_stock: number;
  current_rop: number;
  recommended_rop: number;
  current_replenishment_point: number;
  recommended_replenishment_point: number;
  target_coverage_days: number;
  needs_update: boolean;
  reason: string;
  forecast: DemandForecastResult;
}

export interface PlanningOverview {
  calculated_at: string;
  suppliers_updated: number;
  terms_updated: number;
  parts_evaluated: number;
  recommendations_needing_update: number;
  supplier_scores: SupplierScoreResult[];
  lead_time_updates: EffectiveLeadTimeResult[];
  recommendations: ROPRecommendation[];
}

export interface PlanningRecalcResponse extends PlanningOverview {
  alerts_created: number;
  alerts_updated: number;
  alerts_resolved: number;
}

export interface AgentTraceItem {
  id: number;
  agent_name: string;
  task_type: string;
  trigger_summary: string;
  linked_entity_type: string | null;
  linked_entity_id: number | null;
  input_summary: string;
  output_summary: string;
  confidence: number | null;
  tool_calls: Array<Record<string, unknown>>;
  raw_prompt: string;
  raw_response: string;
  latency_ms: number;
  input_tokens: number;
  output_tokens: number;
  cost_inr: number;
  human_feedback: string | null;
  timestamp: string;
}

export interface AutonomyPolicyItem {
  id: number;
  agent_name: string;
  action_category: string;
  mode: string;
  approved_as_is_count: number;
  approved_edited_count: number;
  rejected_count: number;
  total_decisions: number;
  edit_free_approval_rate: number | null;
  updated_at: string;
}

export interface PhoenixStatus {
  enable_phoenix: boolean;
  phoenix_installed: boolean;
  phoenix_initialized: boolean;
  mode: 'active' | 'fallback_sqlite' | 'disabled' | string;
}

export interface ObservabilityStatusResponse {
  phoenix: PhoenixStatus;
  registered_agents: string[];
}

export interface EvalSuiteCasePreview {
  id: string;
  name: string;
  task: string;
  tags: string[];
}

export interface EvalSuiteMetadata {
  agent_name: string;
  suite_name: string;
  description: string;
  total_cases: number;
  cases: EvalSuiteCasePreview[];
}

export interface EvalAssertionResult {
  field: string;
  rule: string;
  expected: unknown;
  actual: unknown;
  passed: boolean;
  message: string;
}

export interface EvalCaseResult {
  id: string;
  name: string;
  task: string;
  passed: boolean;
  reason: string;
  latency_ms: number;
  assertions: EvalAssertionResult[];
  output: Record<string, unknown>;
  error: string | null;
}

export interface EvalRunReport {
  id: number | null;
  agent_name: string;
  suite_name: string;
  mode: 'offline' | 'live' | string;
  model_name: string;
  total_cases: number;
  passed_cases: number;
  pass_rate: number;
  results: EvalCaseResult[];
  timestamp: string;
}
