import React from 'react';

/**
 * 自给自足的轻量 Markdown 渲染组件（无第三方依赖、不用 dangerouslySetInnerHTML）。
 *
 * 为什么自己写：本仓库前端原本没有 markdown 库，各任务模块（PDF识别 / 会议纪要 /
 * 表格分析…）把含 Markdown 的自由文本（摘要、章节正文）当纯文本直接显示，导致表格被
 * 压成「| --- | --- |」一行、标题/列表也不成形，难读。这个组件统一渲染：
 *   标题(#~####) · GFM 表格 · 有序/无序列表 · 段落 · 行内 **粗** *斜* `码` [链接](http)。
 * 用 React 节点输出（天然防 XSS），样式走 Lumen-light 设计令牌，跨模块观感一致。
 */

const codeStyle: React.CSSProperties = {
  fontFamily: 'var(--font-mono)', fontSize: '0.92em',
  background: 'var(--surface-subtle)', padding: '1px 5px', borderRadius: 4,
};
const linkStyle: React.CSSProperties = { color: 'var(--brand-700)', textDecoration: 'none' };

// 行内：**粗** / *斜* / `码` / [文字](http链接)
function inline(text: string): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  const re = /(\*\*([^*]+)\*\*|\*([^*\n]+)\*|`([^`]+)`|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\))/;
  let rest = text;
  let k = 0;
  while (rest) {
    const m = re.exec(rest);
    if (!m) { out.push(rest); break; }
    if (m.index > 0) out.push(rest.slice(0, m.index));
    if (m[2] !== undefined) out.push(<strong key={k++}>{m[2]}</strong>);
    else if (m[3] !== undefined) out.push(<em key={k++}>{m[3]}</em>);
    else if (m[4] !== undefined) out.push(<code key={k++} style={codeStyle}>{m[4]}</code>);
    else if (m[5] !== undefined) out.push(<a key={k++} href={m[6]} target="_blank" rel="noreferrer" style={linkStyle}>{m[5]}</a>);
    rest = rest.slice(m.index + m[0].length);
  }
  return out;
}

const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$/;
const splitRow = (line: string): string[] => {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|')) s = s.slice(0, -1);
  return s.split('|').map(c => c.trim());
};
const isUl = (s: string) => /^\s*[-*•]\s+/.test(s);
const isOl = (s: string) => /^\s*\d+[.)]\s+/.test(s);

const thStyle: React.CSSProperties = {
  background: 'var(--surface-subtle)', textAlign: 'left', padding: '8px 12px',
  borderBottom: '1px solid var(--border-default)', fontWeight: 600, color: 'var(--text-secondary)',
};
const tdStyle: React.CSSProperties = {
  padding: '8px 12px', borderBottom: '1px solid var(--border-subtle)', color: 'var(--text-secondary)',
};

export const Markdown: React.FC<{ source?: string | null; style?: React.CSSProperties; className?: string }> = ({ source, style, className }) => {
  if (!source || !String(source).trim()) return null;
  const lines = String(source).replace(/\r\n/g, '\n').split('\n');
  const blocks: React.ReactNode[] = [];
  let i = 0; let key = 0;
  const n = lines.length;

  while (i < n) {
    const line = lines[i];
    const s = line.trim();
    if (!s) { i++; continue; }

    // 标题
    const hm = /^(#{1,6})\s+(.*)$/.exec(s);
    if (hm) {
      const lvl = Math.min(hm[1].length + 1, 6);
      const Tag = (`h${lvl}` as keyof JSX.IntrinsicElements);
      blocks.push(<Tag key={key++} style={{ margin: '14px 0 6px', lineHeight: 1.4 }}>{inline(hm[2].trim())}</Tag>);
      i++; continue;
    }
    // 表格
    if (s.includes('|') && i + 1 < n && TABLE_SEP.test(lines[i + 1])) {
      const header = splitRow(s);
      let j = i + 2; const rows: string[][] = [];
      while (j < n && lines[j].includes('|') && lines[j].trim()) { rows.push(splitRow(lines[j])); j++; }
      blocks.push(
        <div key={key++} style={{ overflowX: 'auto', margin: '8px 0' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
            <thead><tr>{header.map((c, ci) => <th key={ci} style={thStyle}>{inline(c)}</th>)}</tr></thead>
            <tbody>
              {rows.map((r, ri) => (
                <tr key={ri}>{r.map((c, ci) => <td key={ci} style={tdStyle}>{inline(c)}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
      );
      i = j; continue;
    }
    // 有序列表
    if (isOl(s)) {
      const items: React.ReactNode[] = [];
      while (i < n && isOl(lines[i])) { items.push(<li key={items.length}>{inline(lines[i].replace(/^\s*\d+[.)]\s+/, ''))}</li>); i++; }
      blocks.push(<ol key={key++} style={{ margin: '6px 0', paddingLeft: 22, lineHeight: 1.7 }}>{items}</ol>);
      continue;
    }
    // 无序列表
    if (isUl(s)) {
      const items: React.ReactNode[] = [];
      while (i < n && isUl(lines[i])) { items.push(<li key={items.length}>{inline(lines[i].trim().replace(/^[-*•]\s+/, ''))}</li>); i++; }
      blocks.push(<ul key={key++} style={{ margin: '6px 0', paddingLeft: 22, lineHeight: 1.7 }}>{items}</ul>);
      continue;
    }
    // 段落
    const para: string[] = [];
    while (i < n && lines[i].trim() && !/^#{1,6}\s/.test(lines[i].trim()) && !isUl(lines[i]) && !isOl(lines[i])
           && !(lines[i].includes('|') && i + 1 < n && TABLE_SEP.test(lines[i + 1]))) {
      para.push(lines[i].trim()); i++;
    }
    blocks.push(
      <p key={key++} style={{ margin: '6px 0', lineHeight: 1.7 }}>
        {para.map((l, li) => <React.Fragment key={li}>{li > 0 && <br />}{inline(l)}</React.Fragment>)}
      </p>
    );
  }

  return <div className={className} style={style}>{blocks}</div>;
};

export default Markdown;
