import React from 'react';
import { CodeBlock, CodeBlockCode, Text, TextContent, TextList, TextListItem } from '@patternfly/react-core';

/** Small markdown renderer for template guides (no HTML: everything becomes React text):
 * # headings, paragraphs, - / 1. lists (indented continuation lines), ``` code blocks,
 * `code`, **bold**, _italic_ / *italic*, [links](https://…) */

const INLINE = /(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^)\s]+\)|(?<![\w*])_[^_]+_(?!\w)|(?<![\w*])\*[^*\s][^*]*\*(?!\w))/g;

function inline(text: string, key: string): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  let last = 0;
  let i = 0;
  for (const m of Array.from(text.matchAll(INLINE))) {
    const start = m.index ?? 0;
    if (start > last) out.push(text.slice(last, start));
    const tok = m[0];
    const k = `${key}-${i++}`;
    if (tok.startsWith('`')) out.push(<code key={k}>{tok.slice(1, -1)}</code>);
    else if (tok.startsWith('**')) out.push(<strong key={k}>{inline(tok.slice(2, -2), k)}</strong>);
    else if (tok.startsWith('[')) {
      const [, label, href] = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(tok) || [];
      const safe = /^(https?:|\/|#)/.test(href || '');
      out.push(safe ? <a key={k} href={href} target={href.startsWith('http') ? '_blank' : undefined} rel="noreferrer">{label}</a> : label);
    } else out.push(<em key={k}>{inline(tok.slice(1, -1), k)}</em>);
    last = start + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

type Block =
  | { kind: 'h'; level: number; text: string }
  | { kind: 'p'; text: string }
  | { kind: 'code'; text: string }
  | { kind: 'list'; ordered: boolean; items: string[] };

function parse(md: string): Block[] {
  const lines = md.replace(/\r\n/g, '\n').split('\n');
  const blocks: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    if (line.trim().startsWith('```')) {
      const body: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith('```')) body.push(lines[i++]);
      i++;
      blocks.push({ kind: 'code', text: body.join('\n') });
      continue;
    }
    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    if (h) { blocks.push({ kind: 'h', level: h[1].length, text: h[2] }); i++; continue; }
    const li = /^\s*([-*]|\d+\.)\s+(.*)$/.exec(line);
    if (li) {
      const ordered = /\d/.test(li[1]);
      const items: string[] = [];
      while (i < lines.length) {
        const m = /^\s*([-*]|\d+\.)\s+(.*)$/.exec(lines[i]);
        if (m && /\d/.test(m[1]) === ordered) { items.push(m[2]); i++; continue; }
        if (items.length && /^\s{2,}\S/.test(lines[i]) && !m) { items[items.length - 1] += ` ${lines[i].trim()}`; i++; continue; }
        break;
      }
      blocks.push({ kind: 'list', ordered, items });
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,4})\s/.test(lines[i]) && !/^\s*([-*]|\d+\.)\s+/.test(lines[i])
      && !lines[i].trim().startsWith('```')) para.push(lines[i++].trim());
    blocks.push({ kind: 'p', text: para.join(' ') });
  }
  return blocks;
}

export const Markdown: React.FC<{ source: string; id?: string }> = ({ source, id }) => (
  <TextContent id={id}>
    {parse(source).map((b, n) => {
      const k = `b${n}`;
      if (b.kind === 'h') {
        const component = (['h1', 'h2', 'h3', 'h4'] as const)[b.level - 1];
        return <Text key={k} component={component}>{inline(b.text, k)}</Text>;
      }
      if (b.kind === 'code') return <CodeBlock key={k}><CodeBlockCode>{b.text}</CodeBlockCode></CodeBlock>;
      if (b.kind === 'list') {
        return (
          <TextList key={k} component={b.ordered ? 'ol' : 'ul'}>
            {b.items.map((it, j) => <TextListItem key={j}>{inline(it, `${k}-${j}`)}</TextListItem>)}
          </TextList>
        );
      }
      return <Text key={k} component="p">{inline(b.text, k)}</Text>;
    })}
  </TextContent>
);
