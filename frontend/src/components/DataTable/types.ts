import type { ReactNode } from 'react';

export type ColumnAlignment = 'left' | 'right' | 'center';

export interface ColumnDef<T> {
  key: string;
  /** Title Case column header text (never rendered uppercase). */
  header: string;
  /** Descriptive explanation shown via floating portal tooltip on header hover/focus. */
  tooltip?: string;
  /** Initial width in pixels (also used when double-clicking a divider to reset). */
  defaultWidth: number;
  /** Minimum width in pixels during drag resizing (defaults to 80px). */
  minWidth?: number;
  align?: ColumnAlignment;
  renderCell: (row: T, rowIndex: number) => ReactNode;
  headerClassName?: string;
  cellClassName?: string | ((row: T, rowIndex: number) => string);
}

export interface DataTableProps<T> {
  storageKey: string;
  columns: ColumnDef<T>[];
  rows: T[];
  rowKey: (row: T, rowIndex: number) => string | number;
  /** CSS max-height for the dual-axis scroll viewport (e.g. 'calc(100vh - 280px)'). */
  maxHeight?: string;
  emptyMessage?: ReactNode;
  loading?: boolean;
  onRowClick?: (row: T) => void;
  rowClassName?: (row: T, rowIndex: number) => string;
  /** Omit outer card border/shadow when embedded inside an existing card section. */
  embedded?: boolean;
  className?: string;
  ariaLabel?: string;
}
