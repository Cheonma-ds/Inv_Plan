import type { ReactNode } from 'react';

interface CopilotMarkdownProps {
  content: string;
  className?: string;
}

type Block =
  | { kind: 'heading'; level: number; text: string }
  | { kind: 'list'; ordered: boolean; items: string[] }
  | { kind: 'table'; headers: string[]; rows: string[][] }
  | { kind: 'paragraph'; lines: string[] };

const HEADING_RE = /^(#{1,4})\s+(.+)$/;
const UL_RE = /^\s*(?:[-•*]|\u2022)\s+(.*)$/;
const OL_RE = /^\s*\d+[.)]\s+(.*)$/;
const TABLE_SEP_RE = /^\s*\|?(?:\s*:?-{2,}:?\s*\|)+\s*:?-{2,}:?\s*\|?\s*$/;

function stripStrayAsterisks(text: string): string {
  // Remove unmatched bold/italic markdown markers while keeping multiplication expressions like "2 * 3"
  return text
    .replace(/\*{2,}/g, '')
    .replace(/(^|\s)\*+(?=\S)/g, '$1')
    .replace(/(?<=\S)\*+(?=\s|$|[.,;:!?])/g, '');
}

function renderInline(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  // Match ***bold+italic***, **bold**, __bold__, `code`, or *italic*
  const tokenPattern =
    /(\*\*\*([^\n*]+?)\*\*\*|\*\*([^\n]+?)\*\*|__([^\n]+?)__|`([^`\n]+?)`|\*([^*\n]+?)\*)/g;
  let lastIndex = 0;
  let keySeq = 0;

  for (const match of text.matchAll(tokenPattern)) {
    const start = match.index ?? 0;
    if (start > lastIndex) {
      const plain = stripStrayAsterisks(text.slice(lastIndex, start));
      if (plain) nodes.push(plain);
    }

    if (match[2] !== undefined) {
      nodes.push(
        <strong key={`bi-${keySeq++}`} className="font-semibold italic text-[#1D1D1F]">
          {stripStrayAsterisks(match[2])}
        </strong>,
      );
    } else if (match[3] !== undefined || match[4] !== undefined) {
      const boldText = stripStrayAsterisks(match[3] ?? match[4] ?? '');
      nodes.push(
        <strong key={`b-${keySeq++}`} className="font-semibold text-[#1D1D1F]">
          {boldText}
        </strong>,
      );
    } else if (match[5] !== undefined) {
      nodes.push(
        <code
          key={`c-${keySeq++}`}
          className="rounded-md bg-black/[0.06] px-1.5 py-0.5 font-mono text-[11px] font-medium text-[#1D1D1F]"
        >
          {match[5]}
        </code>,
      );
    } else if (match[6] !== undefined) {
      nodes.push(
        <em key={`i-${keySeq++}`} className="italic text-[#1D1D1F]">
          {stripStrayAsterisks(match[6])}
        </em>,
      );
    }
    lastIndex = start + match[0].length;
  }

  if (lastIndex < text.length) {
    const tail = stripStrayAsterisks(text.slice(lastIndex));
    if (tail) nodes.push(tail);
  }

  return nodes.length > 0 ? nodes : [stripStrayAsterisks(text)];
}

function splitTableRow(line: string): string[] {
  const trimmed = line.trim().replace(/^\|/, '').replace(/\|$/, '');
  return trimmed.split('|').map((cell) => cell.trim());
}

function parseMarkdownBlocks(raw: string): Block[] {
  const lines = raw.replace(/\r\n/g, '\n').split('\n');
  const blocks: Block[] = [];
  let para: string[] = [];
  let listItems: string[] = [];
  let listOrdered = false;

  const flushPara = () => {
    if (para.length > 0) {
      blocks.push({ kind: 'paragraph', lines: para });
      para = [];
    }
  };

  const flushList = () => {
    if (listItems.length > 0) {
      blocks.push({ kind: 'list', ordered: listOrdered, items: listItems });
      listItems = [];
    }
  };

  let idx = 0;
  while (idx < lines.length) {
    const line = lines[idx];
    const trimmed = line.trim();

    if (!trimmed) {
      flushPara();
      flushList();
      idx += 1;
      continue;
    }

    // Markdown table detection
    if (
      line.includes('|') &&
      idx + 1 < lines.length &&
      TABLE_SEP_RE.test(lines[idx + 1])
    ) {
      flushPara();
      flushList();
      const headers = splitTableRow(line);
      const rows: string[][] = [];
      idx += 2;
      while (idx < lines.length && lines[idx].trim().includes('|')) {
        rows.push(splitTableRow(lines[idx]));
        idx += 1;
      }
      blocks.push({ kind: 'table', headers, rows });
      continue;
    }

    const headingMatch = trimmed.match(HEADING_RE);
    if (headingMatch) {
      flushPara();
      flushList();
      blocks.push({
        kind: 'heading',
        level: headingMatch[1].length,
        text: headingMatch[2].trim(),
      });
      idx += 1;
      continue;
    }

    const ulMatch = line.match(UL_RE);
    if (ulMatch) {
      flushPara();
      if (listItems.length > 0 && listOrdered) flushList();
      listOrdered = false;
      listItems.push(ulMatch[1].trim());
      idx += 1;
      continue;
    }

    const olMatch = line.match(OL_RE);
    if (olMatch) {
      flushPara();
      if (listItems.length > 0 && !listOrdered) flushList();
      listOrdered = true;
      listItems.push(olMatch[1].trim());
      idx += 1;
      continue;
    }

    flushList();
    para.push(line);
    idx += 1;
  }

  flushPara();
  flushList();
  return blocks;
}

export function CopilotMarkdown({ content, className = '' }: CopilotMarkdownProps) {
  const blocks = parseMarkdownBlocks(content);
  if (blocks.length === 0) return null;

  return (
    <div className={`space-y-2.5 text-[13px] leading-relaxed text-[#1D1D1F] ${className}`}>
      {blocks.map((block, bIdx) => {
        if (block.kind === 'heading') {
          const sizeCls =
            block.level <= 2 ? 'text-sm font-bold pt-1' : 'text-[13px] font-bold pt-0.5';
          return (
            <h4 key={`h-${bIdx}`} className={`${sizeCls} text-[#1D1D1F]`}>
              {renderInline(block.text)}
            </h4>
          );
        }

        if (block.kind === 'list') {
          const ListTag = block.ordered ? 'ol' : 'ul';
          return (
            <ListTag
              key={`l-${bIdx}`}
              className={`${
                block.ordered ? 'list-decimal' : 'list-disc'
              } pl-5 space-y-1 text-[#1D1D1F]`}
            >
              {block.items.map((item, iIdx) => (
                <li key={`li-${bIdx}-${iIdx}`}>{renderInline(item)}</li>
              ))}
            </ListTag>
          );
        }

        if (block.kind === 'table') {
          return (
            <div
              key={`t-${bIdx}`}
              className="overflow-x-auto rounded-xl border border-black/[0.08] bg-white"
            >
              <table className="w-full border-collapse text-left text-[11px] tabular-nums">
                <thead className="bg-[#FBFBFD] border-b border-black/[0.07]">
                  <tr>
                    {block.headers.map((h, hIdx) => (
                      <th key={`th-${hIdx}`} className="px-2.5 py-1.5 font-semibold text-[#6E6E73]">
                        {renderInline(h)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-black/[0.05]">
                  {block.rows.map((row, rIdx) => (
                    <tr key={`tr-${rIdx}`}>
                      {block.headers.map((_, cIdx) => (
                        <td key={`td-${rIdx}-${cIdx}`} className="px-2.5 py-1.5 text-[#1D1D1F]">
                          {renderInline(row[cIdx] ?? '')}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          );
        }

        return (
          <p key={`p-${bIdx}`}>
            {block.lines.map((line, lIdx) => (
              <span key={`ln-${bIdx}-${lIdx}`}>
                {lIdx > 0 && <br />}
                {renderInline(line)}
              </span>
            ))}
          </p>
        );
      })}
    </div>
  );
}
