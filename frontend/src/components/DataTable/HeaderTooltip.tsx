import { useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

export interface ActiveHeaderTooltip {
  title: string;
  description: string;
  anchorRect: DOMRect;
}

interface HeaderTooltipProps {
  tooltip: ActiveHeaderTooltip | null;
}

const VIEWPORT_PADDING = 12;
const GAP_PX = 8;

export function HeaderTooltip({ tooltip }: HeaderTooltipProps) {
  const tipRef = useRef<HTMLDivElement | null>(null);
  const [coords, setCoords] = useState<{ top: number; left: number } | null>(null);

  useLayoutEffect(() => {
    if (!tooltip) {
      setCoords(null);
      return;
    }

    const { anchorRect } = tooltip;
    const tipEl = tipRef.current;
    const tipWidth = tipEl ? tipEl.offsetWidth : 288;
    const tipHeight = tipEl ? tipEl.offsetHeight : 96;

    const centeredLeft = anchorRect.left + anchorRect.width / 2 - tipWidth / 2;
    const maxLeft = Math.max(
      VIEWPORT_PADDING,
      window.innerWidth - tipWidth - VIEWPORT_PADDING,
    );
    const clampedLeft = Math.min(Math.max(VIEWPORT_PADDING, centeredLeft), maxLeft);

    const belowTop = anchorRect.bottom + GAP_PX;
    const fitsBelow =
      belowTop + tipHeight <= window.innerHeight - VIEWPORT_PADDING;
    const top = fitsBelow
      ? belowTop
      : Math.max(VIEWPORT_PADDING, anchorRect.top - tipHeight - GAP_PX);

    setCoords({ top, left: clampedLeft });
  }, [tooltip]);

  if (!tooltip || typeof document === 'undefined') {
    return null;
  }

  return createPortal(
    <div
      ref={tipRef}
      role="tooltip"
      style={{
        position: 'fixed',
        top: coords ? `${coords.top}px` : `${tooltip.anchorRect.bottom + GAP_PX}px`,
        left: coords ? `${coords.left}px` : `${Math.max(VIEWPORT_PADDING, tooltip.anchorRect.left)}px`,
        opacity: coords ? 1 : 0,
      }}
      className="pointer-events-none z-50 w-max max-w-xs rounded-xl border border-black/[0.10] bg-white/95 px-3.5 py-2.5 text-left shadow-[0_12px_32px_rgba(0,0,0,0.12),0_2px_8px_rgba(0,0,0,0.06)] backdrop-blur-xl transition-opacity duration-150"
    >
      <div className="text-[11px] font-semibold text-[#1D1D1F] tracking-tight">
        {tooltip.title}
      </div>
      <p className="mt-1 text-[11px] leading-relaxed text-[#6E6E73] font-normal">
        {tooltip.description}
      </p>
    </div>,
    document.body,
  );
}
