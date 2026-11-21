import { AlertTriangle, Check, XCircle } from 'lucide-react';
import type { PODetail } from '../api/types';
import { formatSimDate, formatSimDateTime } from '../utils/format';

export type StepState = 'completed' | 'active' | 'blocked' | 'pending';

interface StepDescriptor {
  key: string;
  label: string;
  dateText: string;
  titleText?: string;
  state: StepState;
  isDanger?: boolean;
}

function buildSteps(po: PODetail): StepDescriptor[] {
  const latestGrn = po.goods_receipts[0] ?? null;
  const etaTarget = po.revised_eta ?? po.promised_eta;
  const etaPrefix = po.revised_eta ? 'Rev ETA' : 'ETA';
  const etaDisplay = etaTarget ? `${etaPrefix}: ${formatSimDate(etaTarget)}` : '—';

  const dispatchDateDisplay = po.actual_dispatch_date
    ? `Shipped: ${formatSimDate(po.actual_dispatch_date)}`
    : po.promised_dispatch_date
      ? `Promised: ${formatSimDate(po.promised_dispatch_date)}`
      : '—';

  const status = po.status;

  // Step 1: Created
  const stepCreated: StepDescriptor = {
    key: 'created',
    label: 'Created',
    dateText: formatSimDate(po.created_at),
    titleText: formatSimDateTime(po.created_at),
    state: 'completed',
  };

  // Step 2: PO Sent
  let stepSent: StepDescriptor = {
    key: 'po_sent',
    label: 'PO Sent',
    dateText: po.sent_at ? formatSimDate(po.sent_at) : '—',
    titleText: po.sent_at ? formatSimDateTime(po.sent_at) : undefined,
    state: po.sent_at ? 'completed' : 'pending',
  };

  // Step 3: Acknowledged
  let stepAck: StepDescriptor = {
    key: 'acknowledged',
    label: 'Acknowledged',
    dateText: po.acknowledged_at ? formatSimDate(po.acknowledged_at) : '—',
    titleText: po.acknowledged_at ? formatSimDateTime(po.acknowledged_at) : undefined,
    state: po.acknowledged_at ? 'completed' : 'pending',
  };

  // Step 4: Dispatched
  let stepDispatched: StepDescriptor = {
    key: 'dispatched',
    label: 'Dispatched',
    dateText: dispatchDateDisplay,
    state: po.actual_dispatch_date ? 'completed' : 'pending',
  };

  // Step 5: Delivered (GRN)
  let stepDelivered: StepDescriptor = {
    key: 'delivered',
    label: 'Delivered (GRN)',
    dateText: latestGrn ? formatSimDate(latestGrn.received_at) : etaDisplay,
    titleText: latestGrn ? formatSimDateTime(latestGrn.received_at) : undefined,
    state: latestGrn ? 'completed' : 'pending',
  };

  // Step 6: Closed
  let stepClosed: StepDescriptor = {
    key: 'closed',
    label: 'Closed',
    dateText: po.closed_at ? formatSimDate(po.closed_at) : '—',
    titleText: po.closed_at ? formatSimDateTime(po.closed_at) : undefined,
    state: po.closed_at ? 'completed' : 'pending',
  };

  switch (status) {
    case 'PENDING_DISPATCH_APPROVAL':
      stepSent = {
        ...stepSent,
        dateText: 'Draft pending',
        state: 'active',
      };
      break;

    case 'SENT_AWAITING_ACK':
      stepSent = { ...stepSent, state: 'completed' };
      stepAck = {
        ...stepAck,
        dateText: 'Awaiting ack',
        state: 'active',
      };
      break;

    case 'EXCEPTION_REVIEW':
      stepAck = {
        ...stepAck,
        dateText: 'Exception',
        state: 'blocked',
        isDanger: true,
      };
      break;

    case 'UNRESPONSIVE_ESCALATED':
      stepAck = {
        ...stepAck,
        dateText: 'Unresponsive',
        state: 'blocked',
        isDanger: true,
      };
      break;

    case 'ACKNOWLEDGED':
      stepAck = {
        ...stepAck,
        dateText: po.acknowledged_at ? formatSimDate(po.acknowledged_at) : 'Confirmed',
        state: 'completed',
      };
      if (po.actual_dispatch_date) {
        stepDispatched = { ...stepDispatched, state: 'completed' };
        stepDelivered = {
          ...stepDelivered,
          state: po.dispatch_details_status === 'TIMED_OUT' ? 'blocked' : 'active',
        };
      } else if (po.dispatch_details_status === 'TIMED_OUT') {
        stepDispatched = {
          ...stepDispatched,
          dateText: po.promised_dispatch_date
            ? `Promised: ${formatSimDate(po.promised_dispatch_date)}`
            : 'Details overdue',
          state: 'blocked',
        };
      } else {
        stepDispatched = {
          ...stepDispatched,
          dateText: po.promised_dispatch_date
            ? `Promised: ${formatSimDate(po.promised_dispatch_date)}`
            : po.dispatch_details_status === 'DEFERRED_2D'
              ? 'Waiting 2d'
              : 'Awaiting dispatch',
          state: 'active',
        };
      }
      break;

    case 'DELAYED_CRITICAL':
    case 'OVERDUE': {
      const isOverdue = status === 'OVERDUE';
      stepAck = {
        ...stepAck,
        dateText: po.acknowledged_at ? formatSimDate(po.acknowledged_at) : 'Confirmed',
        state: 'completed',
      };
      if (po.actual_dispatch_date) {
        stepDispatched = { ...stepDispatched, state: 'completed' };
        stepDelivered = {
          ...stepDelivered,
          dateText: `${isOverdue ? 'Overdue' : 'Delayed'} · ${etaDisplay}`,
          state: 'blocked',
          isDanger: isOverdue,
        };
      } else {
        stepDispatched = {
          ...stepDispatched,
          dateText: po.promised_dispatch_date
            ? `${isOverdue ? 'Overdue' : 'Delayed'} · ${formatSimDate(po.promised_dispatch_date)}`
            : isOverdue
              ? 'Overdue'
              : 'Delayed',
          state: 'blocked',
          isDanger: isOverdue,
        };
      }
      break;
    }

    case 'RECEIVED_PENDING_INVOICE':
      stepSent = { ...stepSent, state: 'completed' };
      stepAck = {
        ...stepAck,
        dateText: po.acknowledged_at ? formatSimDate(po.acknowledged_at) : 'Confirmed',
        state: 'completed',
      };
      stepDispatched = {
        ...stepDispatched,
        dateText:
          dispatchDateDisplay !== '—' ? dispatchDateDisplay : 'Dispatched',
        state: 'completed',
      };
      stepDelivered = {
        ...stepDelivered,
        dateText: latestGrn ? formatSimDate(latestGrn.received_at) : 'Received',
        state: 'completed',
      };
      stepClosed = {
        ...stepClosed,
        dateText: 'Pending invoice',
        state: 'active',
      };
      break;

    case 'QUARANTINED_INVOICE_MISMATCH':
      stepSent = { ...stepSent, state: 'completed' };
      stepAck = {
        ...stepAck,
        dateText: po.acknowledged_at ? formatSimDate(po.acknowledged_at) : 'Confirmed',
        state: 'completed',
      };
      stepDispatched = {
        ...stepDispatched,
        dateText:
          dispatchDateDisplay !== '—' ? dispatchDateDisplay : 'Dispatched',
        state: 'completed',
      };
      stepDelivered = {
        ...stepDelivered,
        dateText: latestGrn
          ? `Quarantined · ${formatSimDate(latestGrn.received_at)}`
          : 'Quarantined',
        state: 'blocked',
        isDanger: true,
      };
      stepClosed = {
        ...stepClosed,
        dateText: 'Invoice mismatch',
        state: 'blocked',
        isDanger: true,
      };
      break;

    case 'CLOSED':
      stepSent = { ...stepSent, state: 'completed' };
      stepAck = {
        ...stepAck,
        dateText: po.acknowledged_at ? formatSimDate(po.acknowledged_at) : 'Confirmed',
        state: 'completed',
      };
      stepDispatched = {
        ...stepDispatched,
        dateText:
          dispatchDateDisplay !== '—' ? dispatchDateDisplay : 'Dispatched',
        state: 'completed',
      };
      stepDelivered = {
        ...stepDelivered,
        dateText: latestGrn ? formatSimDate(latestGrn.received_at) : 'Delivered',
        state: 'completed',
      };
      stepClosed = {
        ...stepClosed,
        dateText: po.closed_at ? formatSimDate(po.closed_at) : 'Closed',
        state: 'completed',
      };
      break;

    case 'CANCELLED': {
      const steps = [stepCreated, stepSent, stepAck, stepDispatched, stepDelivered, stepClosed];
      const firstPendingIdx = steps.findIndex((s) => s.state === 'pending');
      const blockIdx = firstPendingIdx === -1 ? steps.length - 1 : firstPendingIdx;
      steps[blockIdx] = {
        ...steps[blockIdx],
        dateText: 'Cancelled',
        state: 'blocked',
        isDanger: true,
      };
      return steps;
    }

    default:
      break;
  }

  return [stepCreated, stepSent, stepAck, stepDispatched, stepDelivered, stepClosed];
}

interface OrderTrackingStepperProps {
  po: PODetail;
}

export function OrderTrackingStepper({ po }: OrderTrackingStepperProps) {
  const steps = buildSteps(po);
  const isCancelled = po.status === 'CANCELLED';

  return (
    <div
      data-testid="order-tracking-stepper"
      className="rounded-2xl border border-black/[0.08] bg-[#FBFBFD] px-3.5 py-4 overflow-x-auto"
    >
      <div className="min-w-[540px] flex items-start">
        {steps.map((step, idx) => {
          const nextStep = steps[idx + 1];
          let connectorClass = 'bg-black/[0.10]';
          if (nextStep) {
            if (nextStep.state === 'blocked') {
              connectorClass = nextStep.isDanger
                ? 'bg-[#C9342D]/45'
                : 'bg-[#D96B00]/45';
            } else if (
              step.state === 'completed' &&
              (nextStep.state === 'completed' || nextStep.state === 'active')
            ) {
              connectorClass = 'bg-[#1F7A4D]/60';
            }
          }

          return (
            <div
              key={step.key}
              data-step-key={step.key}
              data-step-state={step.state}
              className="flex-1 min-w-0 flex flex-col items-center text-center relative"
            >
              {/* Connector segment to next node */}
              {idx < steps.length - 1 && (
                <div
                  aria-hidden="true"
                  className={`absolute top-3.5 left-1/2 w-full h-[2px] transition-colors duration-300 ${connectorClass}`}
                />
              )}

              {/* Step Node */}
              <div className="relative z-10 flex items-center justify-center">
                {step.state === 'completed' && (
                  <span className="flex h-7 w-7 items-center justify-center rounded-full bg-[#1F7A4D] text-white shadow-2xs transition-transform duration-200">
                    <Check className="h-3.5 w-3.5 stroke-[2.5]" />
                  </span>
                )}

                {step.state === 'active' && (
                  <span className="flex h-7 w-7 items-center justify-center rounded-full border-2 border-[#0066CC] bg-white text-[#0066CC] ring-4 ring-[#0066CC]/15 transition-all duration-200">
                    <span className="h-2.5 w-2.5 rounded-full bg-[#0066CC] animate-pulse" />
                  </span>
                )}

                {step.state === 'blocked' && (
                  <span
                    className={`flex h-7 w-7 items-center justify-center rounded-full border transition-all duration-200 ${
                      step.isDanger
                        ? 'border-[#C9342D]/45 bg-[#C9342D]/12 text-[#B82923] ring-4 ring-[#C9342D]/10'
                        : 'border-[#D96B00]/45 bg-[#D96B00]/12 text-[#A84E00] ring-4 ring-[#D96B00]/10'
                    }`}
                  >
                    {isCancelled ? (
                      <XCircle className="h-3.5 w-3.5" />
                    ) : (
                      <AlertTriangle className="h-3.5 w-3.5" />
                    )}
                  </span>
                )}

                {step.state === 'pending' && (
                  <span className="flex h-7 w-7 items-center justify-center rounded-full border border-black/[0.15] bg-white text-[11px] font-semibold text-[#86868B] tabular-nums">
                    {idx + 1}
                  </span>
                )}
              </div>

              {/* Label & Milestone Date */}
              <div className="mt-2 px-1 w-full">
                <div
                  className={`text-[11px] font-semibold leading-tight truncate ${
                    step.state === 'pending'
                      ? 'text-[#86868B]'
                      : step.state === 'blocked'
                        ? step.isDanger
                          ? 'text-[#B82923]'
                          : 'text-[#A84E00]'
                        : step.state === 'active'
                          ? 'text-[#0066CC]'
                          : 'text-[#1D1D1F]'
                  }`}
                >
                  {step.label}
                </div>
                <div
                  title={step.titleText}
                  className={`mt-0.5 text-[10px] leading-snug tabular-nums break-words ${
                    step.state === 'blocked'
                      ? step.isDanger
                        ? 'font-medium text-[#B82923]'
                        : 'font-medium text-[#945600]'
                      : step.state === 'active'
                        ? 'font-medium text-[#0058B3]'
                        : 'text-[#6E6E73]'
                  }`}
                >
                  {step.dateText}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
