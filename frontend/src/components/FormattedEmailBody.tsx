import type { ReactNode } from 'react';

interface FormattedEmailBodyProps {
  body: string;
  className?: string;
}

type Block =
  | { kind: 'list'; items: string[] }
  | { kind: 'paragraph'; lines: string[] };

const BULLET_LINE_RE = /^\s*(?:[-•*]|\d+[.)])\s+(.*)$/;

function renderInlineBold(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const boldPattern = /\*\*(.+?)\*\*/g;
  let lastIndex = 0;
  let keySeq = 0;

  for (const match of text.matchAll(boldPattern)) {
    const start = match.index ?? 0;
    if (start > lastIndex) {
      const plain = text.slice(lastIndex, start).replace(/\*\*/g, '');
      if (plain) nodes.push(plain);
    }
    const boldContent = match[1].replace(/\*\*/g, '');
    if (boldContent) {
      nodes.push(
        <strong key={`b-${keySeq++}`} className="font-semibold text-[#1D1D1F]">
          {boldContent}
        </strong>,
      );
    }
    lastIndex = start + match[0].length;
  }

  if (lastIndex < text.length) {
    const tail = text.slice(lastIndex).replace(/\*\*/g, '');
    if (tail) nodes.push(tail);
  }

  return nodes.length > 0 ? nodes : [text.replace(/\*\*/g, '')];
}

function parseEmailBlocks(body: string): Block[] {
  const rawLines = body.replace(/\r\n/g, '\n').split('\n');
  const blocks: Block[] = [];
  let currentParagraph: string[] = [];
  let currentList: string[] = [];

  const flushParagraph = () => {
    if (currentParagraph.length > 0) {
      blocks.push({ kind: 'paragraph', lines: currentParagraph });
      currentParagraph = [];
    }
  };

  const flushList = () => {
    if (currentList.length > 0) {
      blocks.push({ kind: 'list', items: currentList });
      currentList = [];
    }
  };

  for (const line of rawLines) {
    const trimmed = line.trim();
    if (!trimmed) {
      flushParagraph();
      flushList();
      continue;
    }

    const bulletMatch = line.match(BULLET_LINE_RE);
    if (bulletMatch) {
      flushParagraph();
      currentList.push(bulletMatch[1].trim());
    } else {
      flushList();
      currentParagraph.push(line);
    }
  }

  flushParagraph();
  flushList();
  return blocks;
}

export function FormattedEmailBody({ body, className = '' }: FormattedEmailBodyProps) {
  const blocks = parseEmailBlocks(body);

  if (blocks.length === 0) {
    return null;
  }

  return (
    <div className={`space-y-2.5 leading-relaxed text-[#1D1D1F] ${className}`}>
      {blocks.map((block, bIdx) => {
        if (block.kind === 'list') {
          return (
            <ul key={`list-${bIdx}`} className="list-disc pl-5 space-y-1">
              {block.items.map((item, iIdx) => (
                <li key={`li-${bIdx}-${iIdx}`}>{renderInlineBold(item)}</li>
              ))}
            </ul>
          );
        }

        return (
          <p key={`p-${bIdx}`}>
            {block.lines.map((line, lIdx) => (
              <span key={`line-${bIdx}-${lIdx}`}>
                {lIdx > 0 && <br />}
                {renderInlineBold(line)}
              </span>
            ))}
          </p>
        );
      })}
    </div>
  );
}
