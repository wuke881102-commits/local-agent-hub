import React, { useEffect, useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import useSWR from 'swr';
import { Icon } from '../components/icons';
import { api, fetcher, errMsg, Asset } from '../api';
import { useToast } from '../components/Toast';

const PAGE_SIZE = 50;

const TYPE_TABS = [
  { id: '',             label: '全部' },
  { id: 'doc',          label: '文档' },
  { id: 'meeting_notes', label: '会议纪要' },
  { id: 'wiki',         label: '知识库' },
  { id: 'base',         label: '多维表格' },
  { id: 'sheet',        label: '电子表格' },
  { id: 'slides',       label: '幻灯片' },
  { id: 'file',         label: '文件' },
  { id: 'pdf',          label: 'PDF' },
];

// 标签 → 底层 asset_type（用于按全量 stats 统计每个标签的条数）。
// 'pdf' 是 file/shortcut 中 .pdf 的子集；'meeting_notes' 是 妙记 + AI 智能纪要/文字记录 docx
// 的跨类型子集——两者后端 stats 都已单列计数（不计入 total）。
const TAB_RAW_TYPES: Record<string, string[]> = {
  doc: ['docx', 'doc'],
  meeting_notes: ['meeting_notes'],
  wiki: ['wiki'],
  base: ['bitable'],
  sheet: ['sheet'],
  slides: ['slides'],
  file: ['file', 'shortcut'],
  pdf: ['pdf'],
};

const AssetsPage: React.FC = () => {
  const nav = useNavigate();
  const toast = useToast();
  const [type, setType] = useState('');
  const [q, setQ] = useState('');
  const [page, setPage] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);

  // ── 来自"文档地图"分面的下钻过滤（写在 URL 上，可分享/可后退）──
  const [searchParams, setSearchParams] = useSearchParams();
  const fOrigin = searchParams.get('origin') || '';
  const fRecency = searchParams.get('recency') || '';
  const fOwnerId = searchParams.get('owner_id') || '';
  const fOwnerName = searchParams.get('owner_name') || '';
  const fSpace = searchParams.get('space') || '';
  const fTypeExact = searchParams.get('type_exact') || '';
  const fTypeLabel = searchParams.get('type_label') || '';
  const fCreatedYear = searchParams.get('created_year') || '';
  const fCategory = searchParams.get('category') || '';
  const hasFilter = !!(fOrigin || fRecency || fOwnerId || fSpace || fTypeExact || fCreatedYear || fCategory);

  // 筛选下拉选项（AI 分类 / 空间 / 负责人，含计数）
  const { data: filterOpts } = useSWR<{ categories: any[]; spaces: any[]; owners: any[] }>('/api/assets/filters', fetcher);

  // 更新单个筛选参数（写回 URL，可分享 / 可后退）
  function setParam(key: string, val: string) {
    const next = new URLSearchParams(searchParams);
    if (val) next.set(key, val); else next.delete(key);
    if (key === 'owner_id') next.delete('owner_name');
    setSearchParams(next);
  }

  // 仅来自"文档地图"下钻、没有专门下拉的维度，用可移除 chip 展示
  const drillChips = ([
    fOrigin && { keys: ['origin'], kind: '来源', value: fOrigin },
    fRecency && { keys: ['recency'], kind: '活跃度', value: fRecency },
    fTypeExact && { keys: ['type_exact', 'type_label'], kind: '类型', value: fTypeLabel || fTypeExact },
    fCreatedYear && { keys: ['created_year'], kind: '创建年份', value: fCreatedYear === '时间未知' ? '时间未知' : `${fCreatedYear} 年` },
  ].filter(Boolean) as { keys: string[]; kind: string; value: string }[]);

  // 筛选条件变化时回到第 1 页（不含 limit/offset，避免翻页也重置）
  const filterKey = [type, q, fCategory, fSpace, fOwnerId, fOrigin, fRecency, fTypeExact, fCreatedYear].join('|');
  useEffect(() => { setPage(0); }, [filterKey]);

  const query = (() => {
    const p = new URLSearchParams();
    if (type) p.set('type', type);
    if (q) p.set('q', q);
    if (fOrigin) p.set('origin', fOrigin);
    if (fRecency) p.set('recency', fRecency);
    if (fOwnerId) p.set('owner_id', fOwnerId);
    if (fSpace) p.set('space', fSpace);
    if (fTypeExact) p.set('type_exact', fTypeExact);
    if (fCreatedYear) p.set('created_year', fCreatedYear);
    if (fCategory) p.set('category', fCategory);
    p.set('limit', String(PAGE_SIZE));
    p.set('offset', String(page * PAGE_SIZE));
    return p.toString();
  })();

  const { data, mutate } = useSWR<{ items: Asset[]; total: number; stats: any }>(
    `/api/assets?${query}`,
    fetcher,
  );
  const assets = data?.items || [];
  const total = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const pageWindow = useMemo(() => {
    const from = Math.max(0, page - 2);
    const to = Math.min(totalPages - 1, page + 2);
    const w: number[] = [];
    for (let i = from; i <= to; i++) w.push(i);
    return w;
  }, [page, totalPages]);

  // 每个类型标签的全量条数（来自 stats，未被当前筛选影响）
  const stats = data?.stats || {};
  function tabCount(id: string): number | null {
    if (!data?.stats) return null;
    if (!id) return stats.total ?? null;
    return (TAB_RAW_TYPES[id] || []).reduce((s, t) => s + (stats[t] || 0), 0);
  }

  // 负责人下拉选项：「我自己」置顶；下钻带入的未知 owner 补临时项
  const ownerOptions = useMemo(() => {
    const list = (filterOpts?.owners || []).map((o: any) => ({
      value: o.owner_id,
      label: `${o.is_me ? '我自己 · ' : ''}${o.name}（${o.count}）`,
      is_me: !!o.is_me,
    }));
    list.sort((a, b) => (b.is_me ? 1 : 0) - (a.is_me ? 1 : 0));
    if (fOwnerId && !list.some(o => o.value === fOwnerId)) {
      list.unshift({ value: fOwnerId, label: fOwnerName || fOwnerId, is_me: false });
    }
    return list;
  }, [filterOpts, fOwnerId, fOwnerName]);

  function clearAll() {
    setSearchParams({});
    setType('');
    setQ('');
  }

  async function refresh() {
    setRefreshing(true);
    try {
      await api.post('/api/assets/refresh');
      await mutate();
      toast.success('索引刷新完成');
    } catch (err) {
      toast.error('索引刷新失败', { detail: errMsg(err) });
    } finally {
      setRefreshing(false);
    }
  }

  const HTML_SUPPORTED = ['docx', 'doc', 'wiki', 'bitable', 'sheet', 'slides'];

  // 多选：最多 3 篇一起生成 HTML
  const MAX_SELECT = 3;
  const selectableAtMax = selected.length >= MAX_SELECT;
  function toggleSelect(id: string) {
    setSelected(prev =>
      prev.includes(id) ? prev.filter(x => x !== id) : (prev.length >= MAX_SELECT ? prev : [...prev, id]),
    );
  }

  function generateHtml(a: Asset) {
    const params = new URLSearchParams({ doc_token: a.asset_id, title: a.title });
    nav(`/task/html-page?${params.toString()}`);
  }

  function generateHtmlMulti() {
    if (selected.length === 0) return;
    nav(`/task/html-page?doc_tokens=${encodeURIComponent(selected.join(','))}`);
  }

  const ANALYZABLE = ['bitable', 'sheet'];
  function analyzeTable(a: Asset) {
    const params = new URLSearchParams({ asset_id: a.asset_id, type: a.type });
    nav(`/task/base-analysis?${params.toString()}`);
  }

  const isPdf = (a: Asset) => (a.type === 'file' || a.type === 'shortcut') && /\.pdf$/i.test(a.title || '');
  function recognizePdf(a: Asset) {
    nav(`/task/pdf-recognition?asset_id=${encodeURIComponent(a.asset_id)}`);
  }

  // 云盘上传文件（type=file/shortcut）按扩展名归类：Excel 走表格分析，Word/HTML 走生成 HTML。
  const isUploadedFile = (a: Asset) => a.type === 'file' || a.type === 'shortcut';
  const isExcelFile = (a: Asset) => isUploadedFile(a) && /\.(xlsx|xlsm|xls)$/i.test(a.title || '');
  const isDocFile = (a: Asset) => isUploadedFile(a) && /\.(docx|html?|htm)$/i.test(a.title || '');
  // 可「分析」：原生多维表/电子表，或上传的 Excel 文件。
  const isAnalyzable = (a: Asset) => ANALYZABLE.includes(a.type) || isExcelFile(a);
  // 可「生成 HTML」：原生文档类，或上传的 Word/HTML 文件。
  const isHtmlSource = (a: Asset) => HTML_SUPPORTED.includes(a.type) || isDocFile(a);

  // 会议纪要 = 经典妙记(meeting) + 飞书 AI 智能纪要/文字记录（docx，标题前缀），与后端口径一致。
  const isMeetingNotes = (a: Asset) =>
    a.type === 'meeting' ||
    ((a.type === 'docx' || a.type === 'doc') && /^(智能纪要|文字记录)/.test((a.title || '').trim()));
  function summarizeMeeting(a: Asset) {
    nav(`/task/meeting-minutes?asset_id=${encodeURIComponent(a.asset_id)}`);
  }

  return (
    <div style={{ padding: 'var(--space-8)' }}>
      <div style={{ display: 'flex', alignItems: 'center', marginBottom: 'var(--space-4)' }}>
        <h2 style={{ margin: 0, fontSize: 20, fontWeight: 600 }}>飞书文档</h2>
        <div style={{ flex: 1 }} />
        {selected.length > 0 && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginRight: 12 }}>
            <span className="eyebrow">{selected.length}/{MAX_SELECT} 已选</span>
            <button className="btn btn-ghost btn-sm" onClick={() => setSelected([])}>清空</button>
            <button className="btn btn-primary btn-sm" onClick={generateHtmlMulti}>
              <Icon name="page" size={12} /> 生成 HTML（{selected.length}）
            </button>
          </div>
        )}
        <input className="input" placeholder="搜索标题 / owner / 空间 / 摘要 / 标签…" value={q} onChange={e => setQ(e.target.value)} style={{ width: 300, marginRight: 8 }} />
        <button className="btn btn-tonal btn-sm" onClick={refresh} disabled={refreshing}>
          <Icon name="refresh" size={14} /> {refreshing ? '刷新中…' : '刷新索引'}
        </button>
        <button className="btn btn-primary btn-sm" onClick={() => nav('/task/index-enrich')} style={{ marginLeft: 8 }} title="用 qwen3.6-flash 为资产生成摘要 / 分类 / 标签">
          <Icon name="sparkle" size={14} /> AI 摘要回填
        </button>
      </div>

      {/* 类型标签（带全量计数） */}
      <div className="tabs" style={{ marginBottom: 'var(--space-3)' }}>
        {TYPE_TABS.map(t => {
          const cnt = tabCount(t.id);
          return (
            <button key={t.id} className={'tab' + (type === t.id ? ' active' : '')} onClick={() => setType(t.id)}>
              {t.label}
              {cnt != null && <span style={{ marginLeft: 6, fontSize: 11, color: 'var(--text-tertiary)' }}>{cnt}</span>}
            </button>
          );
        })}
      </div>

      {/* 筛选条件：AI 分类 / 所属空间 / 负责人（与搜索框、类型标签组合） */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap', marginBottom: 'var(--space-4)' }}>
        <FilterSelect label="AI 分类" value={fCategory} onChange={v => setParam('category', v)}
          options={(filterOpts?.categories || []).map((c: any) => ({ value: c.name, label: `${c.name}（${c.count}）` }))} />
        <FilterSelect label="所属空间" value={fSpace} onChange={v => setParam('space', v)}
          options={(filterOpts?.spaces || []).map((s: any) => ({ value: s.name, label: `${s.name}（${s.count}）` }))} />
        <FilterSelect label="负责人" value={fOwnerId} onChange={v => setParam('owner_id', v)}
          options={ownerOptions} />

        {drillChips.map((c, i) => (
          <span key={i} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12, padding: '4px 9px', background: 'var(--tint-brand)', color: 'var(--brand-700)', borderRadius: 8 }}>
            {c.kind}：{c.value}
            <span style={{ cursor: 'pointer', display: 'inline-flex' }} title="移除该筛选"
              onClick={() => { const next = new URLSearchParams(searchParams); c.keys.forEach(k => next.delete(k)); setSearchParams(next); }}>
              <Icon name="x" size={11} />
            </span>
          </span>
        ))}

        <span style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>· 共 {total} 条</span>
        <div style={{ flex: 1 }} />
        {(hasFilter || type || q) && (
          <button className="btn btn-ghost btn-sm" onClick={clearAll}>
            <Icon name="x" size={12} /> 清除全部
          </button>
        )}
      </div>

      <div className="card" style={{ padding: 0 }}>
        <table className="table">
          <thead>
            <tr>
              <th style={{ width: 40 }}></th><th>标题</th><th>类型</th><th>所属空间</th><th>负责人</th><th>更新</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            {assets.length === 0 && (
              <tr><td colSpan={7} style={{ textAlign: 'center', padding: 32, color: 'var(--text-tertiary)' }}>
                {hasFilter || type || q ? '该筛选下没有资产。点击右侧"清除全部"查看全部。' : '索引为空。点击右上"刷新索引"从飞书拉取。'}
              </td></tr>
            )}
            {assets.map(a => {
              const supported = isHtmlSource(a);
              const checked = selected.includes(a.asset_id);
              return (
              <tr key={a.asset_id} style={checked ? { background: 'var(--tint-brand)' } : undefined}>
                <td style={{ textAlign: 'center' }}>
                  <input
                    type="checkbox"
                    checked={checked}
                    disabled={!supported || (selectableAtMax && !checked)}
                    onChange={() => toggleSelect(a.asset_id)}
                    title={supported ? (selectableAtMax && !checked ? '最多选择 3 篇' : '勾选以合并生成') : '该类型暂不支持生成 HTML'}
                    style={{ width: 16, height: 16, accentColor: 'var(--brand-500)', cursor: supported ? 'pointer' : 'not-allowed' }}
                  />
                </td>
                <td>
                  <div style={{ fontWeight: 500, color: 'var(--text-primary)' }}>{a.title}</div>
                  {a.summary && (
                    <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 2, maxWidth: 460, lineHeight: 1.5 }}>{a.summary}</div>
                  )}
                  {a.tags?.length > 0 && (
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 4 }}>
                      {a.tags.slice(0, 4).map((t, i) => (
                        <span key={i} style={{ fontSize: 10, padding: '1px 7px', background: 'var(--tint-brand)', color: 'var(--brand-700)', borderRadius: 8 }}>{t}</span>
                      ))}
                    </div>
                  )}
                  <div className="mono" style={{ fontSize: 11, color: 'var(--text-tertiary)', marginTop: 3 }}>{a.asset_id}</div>
                </td>
                <td>
                  <span className="badge">{a.type}</span>
                  {a.category && <div style={{ fontSize: 11, color: 'var(--text-tertiary)', marginTop: 4 }}>{a.category}</div>}
                </td>
                <td>{a.space || '—'}</td>
                <td>{a.owner || '—'}</td>
                <td className="mono" style={{ fontSize: 12 }}>{a.updated || '—'}</td>
                <td>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 4, flexWrap: 'wrap' }}>
                    {isAnalyzable(a) && (
                      <button className="btn btn-tonal btn-sm" onClick={() => analyzeTable(a)} title="按表格数据归纳并出图：柱状/饼/折线/甘特/架构图等">
                        <Icon name="table" size={12} /> 分析
                      </button>
                    )}
                    {isPdf(a) && (
                      <button className="btn btn-tonal btn-sm" onClick={() => recognizePdf(a)} title="下载并 AI 识别该 PDF：全文 / 字段 / 表格 / 合同金额测算">
                        <Icon name="scan" size={12} /> 识别
                      </button>
                    )}
                    {isMeetingNotes(a) && (
                      <button className="btn btn-tonal btn-sm" onClick={() => summarizeMeeting(a)} title="整理这次会议：摘要 / 决策 / 行动项 / 风险（含图片 OCR）">
                        <Icon name="mic" size={12} /> 纪要
                      </button>
                    )}
                    {isHtmlSource(a) ? (
                      <button className="btn btn-ghost btn-sm" onClick={() => generateHtml(a)}>
                        <Icon name="page" size={12} /> 生成 HTML
                      </button>
                    ) : (!isAnalyzable(a) && !isPdf(a) && !isMeetingNotes(a)) ? (
                      <span style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>—</span>
                    ) : null}
                    {a.url && (
                      <a className="btn btn-ghost btn-sm" href={a.url} target="_blank" rel="noreferrer">
                        <Icon name="external" size={12} />
                      </a>
                    )}
                  </div>
                </td>
              </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* 分页 */}
      {total > 0 && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 'var(--space-4)', flexWrap: 'wrap' }}>
          <span style={{ fontSize: 13, color: 'var(--text-tertiary)' }}>
            第 {page * PAGE_SIZE + 1}–{Math.min(total, (page + 1) * PAGE_SIZE)} 条 / 共 {total} 条
          </span>
          <div style={{ flex: 1 }} />
          <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
            <button className="btn btn-ghost btn-sm" disabled={page <= 0} onClick={() => setPage(0)} title="首页">«</button>
            <button className="btn btn-ghost btn-sm" disabled={page <= 0} onClick={() => setPage(p => Math.max(0, p - 1))}>上一页</button>
            {pageWindow[0] > 0 && <span style={{ color: 'var(--text-tertiary)', padding: '0 4px' }}>…</span>}
            {pageWindow.map(n => (
              <button key={n}
                      className={'btn btn-sm ' + (n === page ? 'btn-primary' : 'btn-ghost')}
                      onClick={() => setPage(n)}>{n + 1}</button>
            ))}
            {pageWindow[pageWindow.length - 1] < totalPages - 1 && <span style={{ color: 'var(--text-tertiary)', padding: '0 4px' }}>…</span>}
            <button className="btn btn-ghost btn-sm" disabled={page >= totalPages - 1} onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))}>下一页</button>
            <button className="btn btn-ghost btn-sm" disabled={page >= totalPages - 1} onClick={() => setPage(totalPages - 1)} title="末页">»</button>
          </div>
        </div>
      )}
    </div>
  );
};

const FilterSelect: React.FC<{
  label: string; value: string; options: { value: string; label: string }[]; onChange: (v: string) => void;
}> = ({ label, value, options, onChange }) => (
  <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 13 }}>
    <span style={{ color: 'var(--text-tertiary)' }}>{label}</span>
    <select
      value={value}
      onChange={e => onChange(e.target.value)}
      style={{
        height: 32, padding: '0 8px', minWidth: 132, fontSize: 13,
        borderRadius: 'var(--radius-md)', background: 'var(--surface-elevated)',
        border: `1px solid ${value ? 'var(--brand-500)' : 'var(--border-default)'}`,
        color: value ? 'var(--brand-700)' : 'var(--text-secondary)',
        cursor: 'pointer', maxWidth: 220,
      }}
    >
      <option value="">全部</option>
      {options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  </label>
);

export default AssetsPage;
