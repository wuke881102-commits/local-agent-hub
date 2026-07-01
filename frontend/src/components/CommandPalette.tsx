import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Icon } from './icons';
import { api, Asset, AgentInfo } from '../api';

// 与 WorkbenchShell 同款：哪些 agent 有可跳转的任务页。
const AGENT_TASK_PATHS: Record<string, string> = {
  'html-page': '/task/html-page',
  'document-map': '/task/document-map',
  'index-enrich': '/task/index-enrich',
  'knowledge-governance': '/task/knowledge-governance',
  'base-analysis': '/task/base-analysis',
  'pdf-recognition': '/task/pdf-recognition',
  'meeting-minutes': '/task/meeting-minutes',
  'collab-dispatch': '/task/collab-dispatch',
};

// 页面级跳转目标（命令面板兼作启动器：空查询时直接列出）。
const NAV_TARGETS = [
  { label: '工作台', sub: '首页仪表盘', icon: 'home', path: '/', kw: 'home dashboard gongzuotai 工作台 首页 仪表盘' },
  { label: '任务场景', sub: '按场景选择 Agent', icon: 'sparkle', path: '/scenes', kw: 'scenes changjing 场景 任务' },
  { label: '飞书文档', sub: '浏览已索引的飞书内容', icon: 'folder', path: '/assets', kw: 'assets zichan feishu 资产 文件 文档 飞书' },
  { label: '本地目录', sub: '浏览本地文件 · 任务数据源', icon: 'desktop', path: '/localdir', kw: 'localdir bendi 本地 目录 文件 文档' },
  { label: '自动化提炼', sub: '按 Enter 留痕 · 定时提炼工作', icon: 'funnel', path: '/autoextract', kw: 'autoextract tilian 自动化 提炼 截图 捕获 screenshot 日志 工作' },
  { label: '运行记录', sub: '历史任务与状态', icon: 'list', path: '/tasks', kw: 'tasks jilu 任务 记录 运行' },
  { label: '组织架构', sub: '成员与汇报关系', icon: 'graph', path: '/org', kw: 'org zuzhi 组织 架构 成员' },
  { label: '系统诊断', sub: 'CLI / 授权 / 模型 / 索引', icon: 'gear', path: '/diagnostics', kw: 'diagnostics zhenduan 诊断 系统 健康' },
];

const HTML_SUPPORTED = ['docx', 'doc', 'wiki', 'bitable', 'sheet', 'slides'];
const ANALYZABLE = ['bitable', 'sheet'];

function isPdfAsset(a: Asset): boolean {
  const t = (a.type || '').toLowerCase();
  return (t === 'file' || t === 'shortcut') && /\.pdf$/i.test(a.title || '');
}

// 资产类型 → 图标 / 中文名 / 强调色（与侧栏 Agent 配色同源，一眼分辨类型）。
function assetMeta(a: Asset): { icon: string; label: string; color: string } {
  if (isPdfAsset(a)) return { icon: 'scan', label: 'PDF', color: '#6A4DD4' };
  switch ((a.type || '').toLowerCase()) {
    case 'doc':
    case 'docx': return { icon: 'page', label: '文档', color: '#2563EB' };
    case 'wiki': return { icon: 'shield', label: '知识库', color: '#0D9488' };
    case 'bitable':
    case 'base': return { icon: 'table', label: '多维表格', color: '#F0A800' };
    case 'sheet': return { icon: 'table', label: '电子表格', color: '#16A34A' };
    case 'slides': return { icon: 'page', label: '幻灯片', color: '#DB2777' };
    case 'meeting': return { icon: 'mic', label: '会议纪要', color: '#0095D4' };
    case 'file':
    case 'shortcut': return { icon: 'folder', label: '文件', color: '#737A82' };
    default: return { icon: 'page', label: a.type || '资产', color: '#737A82' };
  }
}

type Chip = { label: string; icon: string; run: () => void };

type Flat = {
  key: string;
  icon: string;
  color: string;
  title: string;
  sub: string;
  summary?: string;
  chips?: Chip[];
  activate: () => void;
};

type Props = {
  onClose: () => void;
  agents: AgentInfo[];
};

export const CommandPalette: React.FC<Props> = ({ onClose, agents }) => {
  const nav = useNavigate();
  const [q, setQ] = useState('');
  const [assets, setAssets] = useState<Asset[]>([]);
  const [loading, setLoading] = useState(false);
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const reqRef = useRef(0);

  const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform);

  useEffect(() => { inputRef.current?.focus(); }, []);

  // 防抖后端搜索：覆盖 标题/空间/负责人 + AI 回填的摘要/标签/分类。
  useEffect(() => {
    const query = q.trim();
    if (!query) { setAssets([]); setLoading(false); return; }
    setLoading(true);
    const id = ++reqRef.current;
    const handle = setTimeout(async () => {
      try {
        const data = await api.get<{ items: Asset[] }>(`/api/assets?q=${encodeURIComponent(query)}&limit=24`);
        if (id === reqRef.current) { setAssets(data.items || []); setLoading(false); }
      } catch {
        if (id === reqRef.current) { setAssets([]); setLoading(false); }
      }
    }, 180);
    return () => clearTimeout(handle);
  }, [q]);

  // 资产的情景动作（与本地资产页同源逻辑）。
  function assetActions(a: Asset): Chip[] {
    const t = (a.type || '').toLowerCase();
    const acts: Chip[] = [];
    if (isPdfAsset(a)) {
      acts.push({ label: '识别', icon: 'scan', run: () => { nav(`/task/pdf-recognition?asset_id=${encodeURIComponent(a.asset_id)}`); onClose(); } });
    }
    if (t === 'meeting') {
      acts.push({ label: '整理纪要', icon: 'mic', run: () => { nav(`/task/meeting-minutes?asset_id=${encodeURIComponent(a.asset_id)}`); onClose(); } });
    }
    if (ANALYZABLE.includes(t)) {
      acts.push({ label: '分析', icon: 'table', run: () => { nav(`/task/base-analysis?asset_id=${encodeURIComponent(a.asset_id)}&type=${encodeURIComponent(a.type)}`); onClose(); } });
    }
    if (HTML_SUPPORTED.includes(t)) {
      acts.push({ label: '生成 HTML', icon: 'page', run: () => { nav(`/task/html-page?doc_token=${encodeURIComponent(a.asset_id)}&title=${encodeURIComponent(a.title)}`); onClose(); } });
    }
    return acts;
  }

  // 主动作（行点击 / 回车）：优先在飞书打开；无链接则退回第一个情景动作。
  function openAsset(a: Asset) {
    if (a.url) { window.open(a.url, '_blank', 'noopener,noreferrer'); onClose(); return; }
    const acts = assetActions(a);
    if (acts.length) acts[0].run(); else onClose();
  }

  const navMatches = useMemo(() => {
    const query = q.trim().toLowerCase();
    if (!query) return NAV_TARGETS;
    return NAV_TARGETS.filter(n => `${n.label} ${n.sub} ${n.kw}`.toLowerCase().includes(query));
  }, [q]);

  const agentMatches = useMemo(() => {
    const query = q.trim().toLowerCase();
    const list = (agents || []).filter(a => AGENT_TASK_PATHS[a.id]);
    if (!query) return list;
    return list.filter(a => `${a.name} ${a.desc || ''}`.toLowerCase().includes(query));
  }, [q, agents]);

  // 渲染顺序 = 键盘遍历顺序：资产 → 前往（页面 + Agent）。
  const groups = useMemo(() => {
    const g: { title: string; items: Flat[] }[] = [];

    if (assets.length) {
      g.push({
        title: '资产',
        items: assets.map(a => {
          const m = assetMeta(a);
          const sub = `${m.label}${a.space ? ' · ' + a.space : a.owner ? ' · ' + a.owner : ''}`;
          return {
            key: 'a-' + a.asset_id,
            icon: m.icon, color: m.color,
            title: a.title || '(未命名)',
            sub,
            summary: (a.summary || '').trim() || undefined,
            chips: assetActions(a),
            activate: () => openAsset(a),
          } as Flat;
        }),
      });
    }

    const goItems: Flat[] = [
      ...navMatches.map(n => ({
        key: 'n-' + n.path, icon: n.icon, color: '#00AA4F',
        title: n.label, sub: n.sub,
        activate: () => { nav(n.path); onClose(); },
      } as Flat)),
      ...agentMatches.map(a => ({
        key: 'ag-' + a.id, icon: a.icon || 'sparkle', color: a.color || '#00AA4F',
        title: a.name.replace(' Agent', ''), sub: a.desc || 'Agent',
        activate: () => { nav(AGENT_TASK_PATHS[a.id]); onClose(); },
      } as Flat)),
    ];
    if (goItems.length) g.push({ title: q.trim() ? '前往' : '快速前往', items: goItems });

    return g;
  }, [assets, navMatches, agentMatches, q]);

  const flat = useMemo(() => groups.flatMap(g => g.items), [groups]);

  useEffect(() => { setActive(0); }, [q]);
  useEffect(() => { setActive(a => Math.min(a, Math.max(0, flat.length - 1))); }, [flat.length]);
  useEffect(() => {
    const el = listRef.current?.querySelector(`[data-idx="${active}"]`) as HTMLElement | null;
    el?.scrollIntoView({ block: 'nearest' });
  }, [active]);

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive(i => Math.min(flat.length - 1, i + 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive(i => Math.max(0, i - 1)); }
    else if (e.key === 'Enter') { e.preventDefault(); flat[active]?.activate(); }
    else if (e.key === 'Escape') { e.preventDefault(); onClose(); }
  }

  let idx = -1;
  const hasQuery = !!q.trim();

  return (
    <div className="cmdk-overlay" onMouseDown={onClose}>
      <div className="cmdk-panel" onMouseDown={e => e.stopPropagation()}>
        <div className="cmdk-search">
          <Icon name="search" size={18} />
          <input
            ref={inputRef}
            className="cmdk-input"
            placeholder="搜索文档、知识库、表格、PDF、Agent…"
            value={q}
            onChange={e => setQ(e.target.value)}
            onKeyDown={onKeyDown}
          />
          {loading
            ? <Icon name="refresh" size={15} className="spin" style={{ color: 'var(--text-tertiary)' }} />
            : <span className="kbd">esc</span>}
        </div>

        <div className="cmdk-results scroll" ref={listRef}>
          {flat.length === 0 ? (
            <div className="cmdk-empty">
              {hasQuery
                ? <>没有匹配“{q.trim()}”的资产或页面。<br />试试文档标题、空间名或 AI 摘要里的关键词。</>
                : '开始输入以搜索你有权限访问的飞书内容。'}
            </div>
          ) : (
            groups.map(group => (
              <div key={group.title}>
                <div className="cmdk-group-label">{group.title}</div>
                {group.items.map(item => {
                  idx++;
                  const myIdx = idx;
                  return (
                    <div
                      key={item.key}
                      data-idx={myIdx}
                      className={'cmdk-item' + (myIdx === active ? ' active' : '')}
                      onMouseMove={() => setActive(myIdx)}
                      onClick={item.activate}
                    >
                      <span className="cmdk-icon" style={{ background: item.color }}>
                        <Icon name={item.icon} size={16} />
                      </span>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div className="cmdk-title">{item.title}</div>
                        <div className="cmdk-sub">{item.sub}</div>
                        {item.summary && (
                          <div className="cmdk-sub" style={{ color: 'var(--text-secondary)', marginTop: 1 }}>{item.summary}</div>
                        )}
                      </div>
                      {item.chips && item.chips.length > 0 && (
                        <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
                          {item.chips.map((c, ci) => (
                            <button
                              key={ci}
                              className="cmdk-chip"
                              onClick={e => { e.stopPropagation(); c.run(); }}
                              title={c.label}
                            >
                              <Icon name={c.icon} size={12} /> {c.label}
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            ))
          )}
        </div>

        <div className="cmdk-footer">
          <span><span className="kbd">↑</span> <span className="kbd">↓</span> 选择</span>
          <span><span className="kbd">↵</span> {hasQuery ? '打开' : '前往'}</span>
          <span><span className="kbd">esc</span> 关闭</span>
          <div style={{ flex: 1 }} />
          <span style={{ opacity: 0.8 }}>{isMac ? '⌘' : 'Ctrl'} K 随时唤起</span>
        </div>
      </div>
    </div>
  );
};

export default CommandPalette;
