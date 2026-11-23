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
      return 'bg-[#C9342D]/10 text-[#B82923] border-[#C9342D]/25';
    case 'HIGH':
      return 'bg-[#D96B00]/10 text-[#A84E00] border-[#D96B00]/25';
    case 'MEDIUM':
      return 'bg-[#B46900]/10 text-[#945600] border-[#B46900]/25';
    case 'INFO':
    default:
      return 'bg-[#0066CC]/10 text-[#0058B3] border-[#0066CC]/22';
  }
}

export function poStatusBadgeClass(status: string): string {
  switch (status) {
    case 'PENDING_DISPATCH_APPROVAL':
      return 'bg-[#B46900]/10 text-[#945600] border-[#B46900]/25';
    case 'SENT_AWAITING_ACK':
      return 'bg-[#0066CC]/10 text-[#0058B3] border-[#0066CC]/22';
    case 'ACKNOWLEDGED':
      return 'bg-[#5E5CE6]/10 text-[#4B48C4] border-[#5E5CE6]/22';
    case 'EXCEPTION_REVIEW':
    case 'UNRESPONSIVE_ESCALATED':
    case 'DELAYED_CRITICAL':
    case 'OVERDUE':
      return 'bg-[#D96B00]/10 text-[#A84E00] border-[#D96B00]/25';
    case 'QUARANTINED_INVOICE_MISMATCH':
      return 'bg-[#C9342D]/10 text-[#B82923] border-[#C9342D]/25';
    case 'RECEIVED_PENDING_INVOICE':
      return 'bg-[#0D8478]/10 text-[#0B6B61] border-[#0D8478]/22';
    case 'CLOSED':
      return 'bg-[#1F7A4D]/10 text-[#1B6B43] border-[#1F7A4D]/22';
    case 'CANCELLED':
      return 'bg-black/[0.06] text-[#6E6E73] border-black/[0.12]';
    default:
      return 'bg-black/[0.04] text-[#6E6E73] border-black/[0.08]';
  }
}

export function criticalityBadgeClass(criticality: string): string {
  switch (criticality) {
    case 'CRITICAL':
      return 'bg-[#C9342D]/10 text-[#B82923] border-[#C9342D]/22';
    case 'STANDARD':
      return 'bg-[#0066CC]/10 text-[#0058B3] border-[#0066CC]/22';
    case 'LOW':
    default:
      return 'bg-black/[0.05] text-[#6E6E73] border-black/[0.10]';
  }
}

export function personaLabel(persona: string): string {
  switch (persona) {
    case 'inventory_manager':
      return 'Inventory Manager';
    case 'making':
      return 'Making Team';
    case 'procurement':
      return 'Procurement';
    case 'sup1':
      return 'Apex Precision (Sup1)';
    case 'sup2':
      return 'Bharat Fasteners (Sup2)';
    case 'sup3':
      return 'Deccan Electro-Spares (Sup3)';
    case 'lex':
      return 'Lex (AI Agent)';
    default:
      return persona;
  }
}
