import { useCallback, useEffect, useRef, useState } from 'react';
import { HeaderTooltip, type ActiveHeaderTooltip } from './HeaderTooltip';
import type { ColumnAlignment, DataTableProps } from './types';
import { useColumnResize } from './useColumnResize';

function alignClass(align: ColumnAlignment | undefined): string {
  switch (align) {
    case 'right':
      return 'text-right';
    case 'center':
      return 'text-center';
    default:
      return 'text-left';
  }
}

export function DataTable<T>({
  storageKey,
  columns,
  rows,
  rowKey,
  maxHeight = 'calc(100vh - 260px)',
  emptyMessage = 'No records found.',
  loading = false,
  onRowClick,
  rowClassName,
  embedded = false,
  className = '',
  ariaLabel,
}: DataTableProps<T>) {
  const {
    widths,
    totalWidth,
    resizingKey,
    hasCustomWidths,
    startResize,
    resetColumn,
    resetAllColumns,
  } = useColumnResize(storageKey, columns);

  const [activeTooltip, setActiveTooltip] = useState<ActiveHeaderTooltip | null>(null);
  const thRefs = useRef<Record<string, HTMLTableCellElement | null>>({});

  // Dismiss tooltip whenever resizing starts or scroll container scrolls.
  useEffect(() => {
    if (resizingKey) {
      setActiveTooltip(null);
    }
  }, [resizingKey]);

  const showTooltipFor = useCallback(
    (colKey: string, header: string, description?: string) => {
      if (!description || resizingKey) return;
      const el = thRefs.current[colKey];
      if (!el) return;
      setActiveTooltip({
        title: header,
        description,
        anchorRect: el.getBoundingClientRect(),
      });
    },
    [resizingKey],
  );

  const hideTooltip = useCallback(() => {
    setActiveTooltip(null);
  }, []);

  const wrapperClass = embedded
    ? `overflow-hidden ${className}`
    : `apple-card overflow-hidden rounded-2xl ${className}`;

  return (
    <div className={wrapperClass}>
      <div
        className="overflow-auto overscroll-contain"
        style={{ maxHeight }}
        onScroll={hideTooltip}
      >
        <table
          aria-label={ariaLabel}
          className="border-separate border-spacing-0 table-fixed text-left text-xs"
          style={{ width: `${totalWidth}px`, minWidth: '100%' }}
        >
          <colgroup>
            {columns.map((col) => (
              <col
                key={col.key}
                style={{ width: `${widths[col.key] ?? col.defaultWidth}px` }}
              />
            ))}
          </colgroup>

          <thead>
            <tr>
              {columns.map((col) => {
                const isResizing = resizingKey === col.key;
                const align = alignClass(col.align);
                return (
                  <th
                    key={col.key}
                    ref={(el) => {
                      thRefs.current[col.key] = el;
                    }}
                    scope="col"
                    tabIndex={col.tooltip ? 0 : undefined}
                    onMouseEnter={() => showTooltipFor(col.key, col.header, col.tooltip)}
                    onMouseLeave={hideTooltip}
                    onFocus={() => showTooltipFor(col.key, col.header, col.tooltip)}
                    onBlur={hideTooltip}
                    className={`group/th sticky top-0 z-10 select-none border-b border-black/[0.08] bg-[#FBFBFD]/95 backdrop-blur-md px-3.5 py-3 text-xs font-semibold tracking-normal text-[#6E6E73] whitespace-nowrap focus:outline-none focus-visible:text-[#1D1D1F] ${align} ${col.tooltip ? 'cursor-help' : ''} ${col.headerClassName ?? ''}`}
                  >
                    <span className="inline-block max-w-full truncate align-middle">
                      {col.header}
                    </span>

                    {/* Invisible-until-hovered column resize divider */}
                    <div
                      role="separator"
                      aria-orientation="vertical"
                      aria-label={`Resize ${col.header} column`}
                      onPointerDown={(e) => {
                        hideTooltip();
                        const renderedW = thRefs.current[col.key]?.getBoundingClientRect().width;
                        startResize(col.key, e, renderedW);
                      }}
                      onDoubleClick={(e) => {
                        e.stopPropagation();
                        hideTooltip();
                        resetColumn(col.key);
                      }}
                      onClick={(e) => e.stopPropagation()}
                      onMouseEnter={(e) => {
                        e.stopPropagation();
                        hideTooltip();
                      }}
                      className="group/resizer absolute right-0 top-1 bottom-1 z-20 flex w-2.5 cursor-col-resize touch-none select-none items-center justify-center"
                    >
                      <span
                        className={`h-4/5 w-[2px] rounded-full transition-opacity duration-150 ${
                          isResizing
                            ? 'bg-[#0066CC] opacity-100'
                            : 'bg-black/20 opacity-0 group-hover/th:opacity-60 group-hover/resizer:bg-[#0066CC] group-hover/resizer:opacity-100'
                        }`}
                      />
                    </div>
                  </th>
                );
              })}
            </tr>
          </thead>

          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td
                  colSpan={columns.length}
                  className="px-4 py-8 text-center text-xs text-[#6E6E73]"
                >
                  {loading ? 'Loading...' : emptyMessage}
                </td>
              </tr>
            ) : (
              rows.map((row, rowIndex) => {
                const isLast = rowIndex === rows.length - 1;
                const customRowCls = rowClassName ? rowClassName(row, rowIndex) : '';
                const clickableCls = onRowClick ? 'cursor-pointer' : '';
                return (
                  <tr
                    key={rowKey(row, rowIndex)}
                    onClick={onRowClick ? () => onRowClick(row) : undefined}
                    className={`transition-colors ${clickableCls} ${customRowCls}`}
                  >
                    {columns.map((col) => {
                      const align = alignClass(col.align);
                      const cellCls =
                        typeof col.cellClassName === 'function'
                          ? col.cellClassName(row, rowIndex)
                          : (col.cellClassName ?? '');
                      return (
                        <td
                          key={col.key}
                          className={`overflow-hidden break-words px-3.5 py-3 align-top ${
                            isLast ? '' : 'border-b border-black/[0.05]'
                          } ${align} ${cellCls}`}
                        >
                          {col.renderCell(row, rowIndex)}
                        </td>
                      );
                    })}
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {hasCustomWidths && (
        <div className="flex items-center justify-end border-t border-black/[0.05] bg-[#FBFBFD] px-3.5 py-1.5">
          <button
            type="button"
            onClick={resetAllColumns}
            className="text-[11px] font-medium text-[#0066CC] hover:text-[#0071E3] hover:underline"
          >
            Reset column widths
          </button>
        </div>
      )}

      <HeaderTooltip tooltip={activeTooltip} />
    </div>
  );
}
