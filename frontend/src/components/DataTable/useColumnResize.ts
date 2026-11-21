import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { PointerEvent as ReactPointerEvent } from 'react';

export interface ResizableColumnSpec {
  key: string;
  defaultWidth: number;
  minWidth?: number;
}

const DEFAULT_MIN_WIDTH = 80;

function getMinWidth(col: ResizableColumnSpec): number {
  return Math.max(48, col.minWidth ?? DEFAULT_MIN_WIDTH);
}

function readStoredWidths(
  storageKey: string,
  columns: ResizableColumnSpec[],
): Record<string, number> {
  const defaults: Record<string, number> = {};
  for (const col of columns) {
    defaults[col.key] = Math.max(getMinWidth(col), col.defaultWidth);
  }

  if (typeof window === 'undefined') {
    return defaults;
  }

  try {
    const raw = window.localStorage.getItem(storageKey);
    if (!raw) return defaults;
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    if (!parsed || typeof parsed !== 'object') return defaults;

    const merged = { ...defaults };
    for (const col of columns) {
      const candidate = parsed[col.key];
      if (typeof candidate === 'number' && Number.isFinite(candidate)) {
        merged[col.key] = Math.max(getMinWidth(col), Math.round(candidate));
      }
    }
    return merged;
  } catch {
    return defaults;
  }
}

function persistWidths(storageKey: string, widths: Record<string, number>): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(storageKey, JSON.stringify(widths));
  } catch {
    // Ignore storage quota or private-browsing errors gracefully.
  }
}

export function useColumnResize(
  storageKey: string,
  columns: ResizableColumnSpec[],
) {
  const [widths, setWidths] = useState<Record<string, number>>(() =>
    readStoredWidths(storageKey, columns),
  );
  const [resizingKey, setResizingKey] = useState<string | null>(null);

  const widthsRef = useRef(widths);
  widthsRef.current = widths;

  const cleanupDragRef = useRef<(() => void) | null>(null);

  // Re-sync when storageKey or column definitions change.
  const colSignature = columns
    .map((c) => `${c.key}:${c.defaultWidth}:${getMinWidth(c)}`)
    .join('|');

  useEffect(() => {
    setWidths(readStoredWidths(storageKey, columns));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storageKey, colSignature]);

  useEffect(() => {
    return () => {
      cleanupDragRef.current?.();
    };
  }, []);

  const columnLookup = useMemo(() => {
    const map = new Map<string, ResizableColumnSpec>();
    for (const col of columns) {
      map.set(col.key, col);
    }
    return map;
  }, [columns]);

  const startResize = useCallback(
    (
      columnKey: string,
      event: ReactPointerEvent<HTMLElement>,
      renderedWidth?: number,
    ) => {
      const spec = columnLookup.get(columnKey);
      if (!spec) return;

      event.preventDefault();
      event.stopPropagation();

      cleanupDragRef.current?.();

      const minWidth = getMinWidth(spec);
      const baseWidth =
        renderedWidth && Number.isFinite(renderedWidth) && renderedWidth > 0
          ? renderedWidth
          : (widthsRef.current[columnKey] ?? spec.defaultWidth);
      const startWidth = Math.max(minWidth, Math.round(baseWidth));
      const startX = event.clientX;

      setResizingKey(columnKey);
      const prevCursor = document.body.style.cursor;
      const prevUserSelect = document.body.style.userSelect;
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';

      const handlePointerMove = (moveEvent: PointerEvent) => {
        const delta = moveEvent.clientX - startX;
        const nextWidth = Math.max(minWidth, Math.round(startWidth + delta));
        setWidths((prev) => {
          if (prev[columnKey] === nextWidth) return prev;
          const updated = { ...prev, [columnKey]: nextWidth };
          widthsRef.current = updated;
          return updated;
        });
      };

      const finish = () => {
        window.removeEventListener('pointermove', handlePointerMove);
        window.removeEventListener('pointerup', finish);
        window.removeEventListener('pointercancel', finish);
        document.body.style.cursor = prevCursor;
        document.body.style.userSelect = prevUserSelect;
        setResizingKey(null);
        persistWidths(storageKey, widthsRef.current);
        cleanupDragRef.current = null;
      };

      cleanupDragRef.current = () => {
        window.removeEventListener('pointermove', handlePointerMove);
        window.removeEventListener('pointerup', finish);
        window.removeEventListener('pointercancel', finish);
        document.body.style.cursor = prevCursor;
        document.body.style.userSelect = prevUserSelect;
      };

      window.addEventListener('pointermove', handlePointerMove);
      window.addEventListener('pointerup', finish);
      window.addEventListener('pointercancel', finish);
    },
    [columnLookup, storageKey],
  );

  const resetColumn = useCallback(
    (columnKey: string) => {
      const spec = columnLookup.get(columnKey);
      if (!spec) return;
      const defaultW = Math.max(getMinWidth(spec), spec.defaultWidth);
      setWidths((prev) => {
        const updated = { ...prev, [columnKey]: defaultW };
        widthsRef.current = updated;
        persistWidths(storageKey, updated);
        return updated;
      });
    },
    [columnLookup, storageKey],
  );

  const resetAllColumns = useCallback(() => {
    const defaults: Record<string, number> = {};
    for (const col of columns) {
      defaults[col.key] = Math.max(getMinWidth(col), col.defaultWidth);
    }
    widthsRef.current = defaults;
    setWidths(defaults);
    if (typeof window !== 'undefined') {
      try {
        window.localStorage.removeItem(storageKey);
      } catch {
        // Ignore storage errors
      }
    }
  }, [columns, storageKey]);

  const totalWidth = useMemo(
    () =>
      columns.reduce(
        (sum, col) => sum + (widths[col.key] ?? col.defaultWidth),
        0,
      ),
    [columns, widths],
  );

  const hasCustomWidths = useMemo(
    () =>
      columns.some(
        (col) =>
          (widths[col.key] ?? col.defaultWidth) !==
          Math.max(getMinWidth(col), col.defaultWidth),
      ),
    [columns, widths],
  );

  return {
    widths,
    totalWidth,
    resizingKey,
    hasCustomWidths,
    startResize,
    resetColumn,
    resetAllColumns,
  };
}
