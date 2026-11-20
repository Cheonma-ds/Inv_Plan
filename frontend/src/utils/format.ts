export function formatSimDateTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString('en-US', {
    timeZone: 'UTC',
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }) + ' UTC';
}

export function formatSimDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString('en-US', {
    timeZone: 'UTC',
    year: 'numeric',
    month: 'short',
    day: '2-digit',
  });
}

export function formatInr(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return new Intl.NumberFormat('en-IN', {
    style: 'currency',
    currency: 'INR',
    maximumFractionDigits: 2,
  }).format(value);
}

export function formatPct(ratio: number | null | undefined): string {
  if (ratio === null || ratio === undefined || Number.isNaN(ratio)) return '—';
  return `${Math.round(ratio * 100)}%`;
}

export function severityBadgeClass(severity: string): string {
  switch (severity) {
    case 'CRITICAL':
      return 'bg-red-100 text-red-800 border-red-200';
    case 'HIGH':
      return 'bg-orange-100 text-orange-800 border-orange-200';
    case 'MEDIUM':
      return 'bg-amber-100 text-amber-800 border-amber-200';
    case 'INFO':
    default:
      return 'bg-sky-100 text-sky-800 border-sky-200';
  }
}

export function poStatusBadgeClass(status: string): string {
  switch (status) {
    case 'PENDING_DISPATCH_APPROVAL':
      return 'bg-amber-100 text-amber-800 border-amber-200';
    case 'SENT_AWAITING_ACK':
      return 'bg-sky-100 text-sky-800 border-sky-200';
    case 'ACKNOWLEDGED':
      return 'bg-indigo-100 text-indigo-800 border-indigo-200';
    case 'EXCEPTION_REVIEW':
    case 'UNRESPONSIVE_ESCALATED':
    case 'DELAYED_CRITICAL':
    case 'OVERDUE':
      return 'bg-orange-100 text-orange-800 border-orange-200';
    case 'QUARANTINED_INVOICE_MISMATCH':
      return 'bg-red-100 text-red-800 border-red-200';
    case 'RECEIVED_PENDING_INVOICE':
      return 'bg-teal-100 text-teal-800 border-teal-200';
    case 'CLOSED':
      return 'bg-emerald-100 text-emerald-800 border-emerald-200';
    case 'CANCELLED':
      return 'bg-slate-200 text-slate-600 border-slate-300';
    default:
      return 'bg-slate-100 text-slate-700 border-slate-200';
  }
}

export function criticalityBadgeClass(criticality: string): string {
  switch (criticality) {
    case 'CRITICAL':
      return 'bg-rose-100 text-rose-800 border-rose-200';
    case 'STANDARD':
      return 'bg-blue-100 text-blue-800 border-blue-200';
    case 'LOW':
    default:
      return 'bg-slate-100 text-slate-700 border-slate-200';
  }
}

export function personaLabel(persona: string): string {
  switch (persona) {
    case 'inventory_manager':
      return 'Inventory Manager';
    case 'sup1':
      return 'Sup1 (Apex Precision)';
    case 'sup2':
      return 'Sup2 (Bharat Fasteners)';
    case 'sup3':
      return 'Sup3 (Deccan Valves)';
    case 'lex':
      return 'Lex (AI Agent)';
    default:
      return persona;
  }
}
