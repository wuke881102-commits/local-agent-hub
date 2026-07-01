import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, errMsg } from '../api';
import { Icon } from './icons';
import { useToast } from './Toast';

// ── 数据契约（对齐 backend/app/routes/localdir.py）──
export type FileKind = 'image' | 'pdf' | 'word' | 'excel' | 'ppt';
export interface LocalFile { name: string; path: string; size: number; mtime: string; kind: FileKind; }
type FilesResp = { directory: string; items: LocalFile[]; counts: Record<string, number> };
type BrowseEntry = { name: string; path: string };
type Browse = { path: string; parent: string | null; is_root: boolean; dirs: BrowseEntry[]; files: any[]; shortcuts: BrowseEntry[] };

export const DIR_KEY = 'localdir-path';
export const fileUrl = (p: string) => `/api/localdir/file?path=${encodeURIComponent(p)}`;
const fmtTime = (s: string) => (s ? s.replace('T', ' ').slice(0, 19) : '—');
const fmtSize = (n: number) => (n > 1e6 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);

// 文件分类元信息：徽标文字 + 主色（列表里用彩色徽标区分类型）
export const KIND_META: Record<FileKind, { label: string; badge: string; color: string }> = {
  image: { label: '截图', badge: 'IMG', color: '#8B5CF6' },
  pdf: { label: 'PDF', badge: 'PDF', color: '#E0567A' },
  word: { label: 'Word', badge: 'DOC', color: '#0095D4' },
  excel: { label: 'Excel', badge: 'XLS', color: '#00AA4F' },
  ppt: { label: 'PPT', badge: 'PPT', color: '#EC6B2D' },
};
const KIND_ORDER: FileKind[] = ['image', 'pdf', 'word', 'excel', 'ppt'];

/** 数据来源分段切换：飞书 / 本地目录。各任务页复用。 */
export const SourceTabs: React.FC<{ mode: 'feishu' | 'local'; onChange: (m: 'feishu' | 'local') => void; feishuLabel?: string }> = ({ mode, onChange, feishuLabel = '飞书' }) => (
  <div style={{ display: 'inline-flex', gap: 4, padding: 3, background: 'var(--surface-subtle)', borderRadius: 'var(--radius-lg)', marginTop: 'var(--space-4)' }}>
    {([['feishu', feishuLabel], ['local', '本地目录']] as const).map(([id, label]) => (
      <button key={id} type="button" onClick={() => onChange(id)}
        style={{
          display: 'inline-flex', alignItems: 'center', gap: 6, padding: '5px 14px', fontSize: 13, cursor: 'pointer',
          border: 'none', borderRadius: 'var(--radius-md)', fontWeight: mode === id ? 600 : 400,
          background: mode === id ? 'var(--surface-elevated)' : 'transparent',
          color: mode === id ? 'var(--text-primary)' : 'var(--text-tertiary)',
          boxShadow: mode === id ? 'var(--shadow-sm)' : 'none',
        }}>
        <Icon name={id === 'feishu' ? 'cloud' : 'desktop'} size={14} /> {label}
      </button>
    ))}
  </div>
);

interface Props {
  dir: string;
  onDirChange: (d: string) => void;
  /** true=可勾选（默认）；false=点击行直接打开 onOpenFile */
  selectable?: boolean;
  /** 多选（默认 true）；false=单选（选新的会替换旧的） */
  multiple?: boolean;
  /** 只显示这些类型（不传=全部）。同时限定分类筛选条 */
  kinds?: FileKind[];
  /** 受控选中的路径数组（selectable 时） */
  selected?: string[];
  onSelChange?: (paths: string[]) => void;
  /** selectable=false 时点击行回调 */
  onOpenFile?: (f: LocalFile) => void;
  /** selectable=false 时每行右侧的操作区（不传则显示「打开」图标）。点击区已阻止冒泡，不会触发整行 onOpenFile。 */
  actions?: (f: LocalFile) => React.ReactNode;
  /** 捕获进行中等场景：每 2s 自动刷新列表 */
  livePoll?: boolean;
  /** 父层 +1 强制重新读取列表（如手动截一张后） */
  reloadToken?: number;
  /** 在「目录条」与「列表」之间插入的内容（如截图控制） */
  children?: React.ReactNode;
}

/**
 * 共享「本地目录数据源」选择器：选目录 → 列文件（可按类型筛/勾选）。
 * 被本地目录页与各任务页（内容生产 / PDF 识别 / 表格分析 / 协作分发）复用。
 */
const LocalSourcePicker: React.FC<Props> = ({
  dir, onDirChange, selectable = true, multiple = true, kinds,
  selected = [], onSelChange, onOpenFile, actions, livePoll = false, reloadToken = 0, children,
}) => {
  const toast = useToast();
  const [files, setFiles] = useState<LocalFile[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [filter, setFilter] = useState<FileKind | 'all'>('all');
  const [loading, setLoading] = useState(false);

  // 目录浏览弹窗
  const [browserOpen, setBrowserOpen] = useState(false);
  const [browse, setBrowse] = useState<Browse | null>(null);
  const [pathInput, setPathInput] = useState('');

  const allowKind = useCallback((k: FileKind) => !kinds || kinds.includes(k), [kinds]);

  const loadFiles = useCallback(async (d: string, quiet = false) => {
    if (!d) { setFiles([]); setCounts({}); return; }
    if (!quiet) setLoading(true);
    try {
      const r = await api.get<FilesResp>(`/api/localdir/files?dir=${encodeURIComponent(d)}`);
      const items = (r.items || []).filter((f) => allowKind(f.kind));
      setFiles(items);
      const c: Record<string, number> = {};
      for (const f of items) c[f.kind] = (c[f.kind] || 0) + 1;
      setCounts(c);
    } catch (e) {
      if (!quiet) toast.error('读取目录失败', { detail: errMsg(e) });
    } finally { if (!quiet) setLoading(false); }
  }, [toast, allowKind]);

  useEffect(() => { loadFiles(dir); }, [dir, reloadToken, loadFiles]);

  // 实时轮询（如捕获中）
  useEffect(() => {
    if (!livePoll || !dir) return;
    const t = window.setInterval(() => loadFiles(dir, true), 2000);
    return () => window.clearInterval(t);
  }, [livePoll, dir, loadFiles]);

  // ── 目录浏览 ──
  const openBrowser = async () => { setBrowserOpen(true); await navigate(dir || ''); };
  const navigate = async (path: string | null) => {
    try {
      const q = path ? `?path=${encodeURIComponent(path)}` : '';
      const r = await api.get<Browse>(`/api/localdir/browse${q}`);
      setBrowse(r);
      setPathInput(r.path || '');
    } catch (e) { toast.error('无法打开目录', { detail: errMsg(e) }); }
  };
  const chooseDir = (p: string) => {
    onDirChange(p);
    try { localStorage.setItem(DIR_KEY, p); } catch { /* ignore */ }
    setBrowserOpen(false);
  };

  // ── 选择 ──
  const shown = useMemo(() => (filter === 'all' ? files : files.filter((f) => f.kind === filter)), [files, filter]);
  const presentKinds = useMemo(() => KIND_ORDER.filter((k) => allowKind(k) && (counts[k] || 0) > 0), [counts, allowKind]);
  const selSet = useMemo(() => new Set(selected), [selected]);

  const onRowClick = (f: LocalFile) => {
    if (!selectable) { onOpenFile?.(f); return; }
    if (!onSelChange) return;
    if (multiple) {
      onSelChange(selSet.has(f.path) ? selected.filter((p) => p !== f.path) : [...selected, f.path]);
    } else {
      onSelChange(selSet.has(f.path) ? [] : [f.path]);
    }
  };

  return (
    <div>
      {/* 目录条 */}
      <div className="card" style={{ padding: 'var(--space-4)', marginBottom: 'var(--space-4)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <Icon name="folder" size={16} style={{ color: 'var(--text-tertiary)' }} />
          <span style={{ fontSize: 13, color: dir ? 'var(--text-primary)' : 'var(--text-tertiary)', fontFamily: 'var(--font-mono)', wordBreak: 'break-all', flex: 1, minWidth: 140 }}>
            {dir || '未选择目录'}
          </span>
          <button className="btn btn-secondary btn-sm" onClick={openBrowser}><Icon name="folder" size={14} /> 选择目录…</button>
          {dir && <button className="btn btn-ghost btn-sm" onClick={() => loadFiles(dir)}><Icon name="refresh" size={14} /> 刷新</button>}
        </div>
        {children}
      </div>

      {/* 分类筛选 */}
      {files.length > 0 && presentKinds.length > 1 && (
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 10 }}>
          <button onClick={() => setFilter('all')} style={{ ...S.chip, ...(filter === 'all' ? S.chipOn : {}) }}>全部 {files.length}</button>
          {presentKinds.map((k) => (
            <button key={k} onClick={() => setFilter(k)}
              style={{ ...S.chip, ...(filter === k ? { background: KIND_META[k].color, color: '#fff', borderColor: KIND_META[k].color } : {}) }}>
              <span style={{ width: 7, height: 7, borderRadius: 2, background: filter === k ? '#fff' : KIND_META[k].color, display: 'inline-block' }} />
              {KIND_META[k].label} {counts[k]}
            </button>
          ))}
        </div>
      )}

      {/* 列表 */}
      {loading ? (
        <div style={{ color: 'var(--text-tertiary)', fontSize: 13, padding: 20 }}>加载中…</div>
      ) : !dir ? (
        <div className="card" style={{ textAlign: 'center', padding: 'var(--space-6)', color: 'var(--text-tertiary)' }}>
          <Icon name="desktop" size={28} style={{ opacity: 0.4 }} />
          <div style={{ marginTop: 10, fontSize: 13 }}>先「选择目录」。</div>
        </div>
      ) : files.length === 0 ? (
        <div className="card" style={{ textAlign: 'center', padding: 'var(--space-6)', color: 'var(--text-tertiary)' }}>
          <Icon name="folder" size={28} style={{ opacity: 0.4 }} />
          <div style={{ marginTop: 10, fontSize: 13 }}>
            该目录暂无{kinds ? `可用的「${kinds.map((k) => KIND_META[k].label).join(' / ')}」文件` : '可用文件'}。
          </div>
        </div>
      ) : (
        <div className="card" style={{ padding: 4, overflow: 'hidden', maxHeight: 360, overflowY: 'auto' }}>
          {shown.map((f, i) => {
            const on = selSet.has(f.path);
            const meta = KIND_META[f.kind];
            return (
              <div key={f.path} style={{ ...S.row, background: on ? 'var(--surface-hover)' : 'transparent', borderTop: i === 0 ? 'none' : '1px solid var(--border-subtle)' }}
                onClick={() => onRowClick(f)} title={`${f.name}\n${fmtTime(f.mtime)} · ${fmtSize(f.size)}`}>
                {f.kind === 'image' ? (
                  <img src={fileUrl(f.path)} alt={f.name} loading="lazy" style={S.lead} />
                ) : (
                  <span style={{ ...S.lead, background: meta.color + '1A', color: meta.color, fontWeight: 700, fontSize: 11, letterSpacing: '0.02em' }}>{meta.badge}</span>
                )}
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 13.5, color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{f.name}</div>
                  <div style={{ fontSize: 11.5, color: 'var(--text-tertiary)', marginTop: 1 }}>
                    <span style={{ color: meta.color, fontWeight: 600 }}>{meta.label}</span> · {fmtTime(f.mtime).slice(5)} · {fmtSize(f.size)}
                  </div>
                </div>
                {selectable ? (
                  <span style={{ ...S.cbox, ...(multiple ? {} : { borderRadius: '50%' }), ...(on ? { background: 'var(--brand-500, #00AA4F)', borderColor: 'var(--brand-500, #00AA4F)', color: '#fff' } : {}) }}>
                    {on && <Icon name="check" size={13} />}
                  </span>
                ) : actions ? (
                  <div style={{ flexShrink: 0, display: 'flex', alignItems: 'center', gap: 4, flexWrap: 'wrap', justifyContent: 'flex-end' }} onClick={(e) => e.stopPropagation()}>
                    {actions(f)}
                  </div>
                ) : (
                  <Icon name="external" size={14} style={{ color: 'var(--text-tertiary)', flexShrink: 0 }} />
                )}
              </div>
            );
          })}
          {shown.length === 0 && <div style={{ padding: 18, fontSize: 12.5, color: 'var(--text-tertiary)', textAlign: 'center' }}>该分类下暂无文件。</div>}
        </div>
      )}

      {/* 目录浏览弹窗 */}
      {browserOpen && (
        <div style={S.modalMask} onClick={() => setBrowserOpen(false)}>
          <div style={S.modal} onClick={(e) => e.stopPropagation()}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
              <Icon name="folder" size={16} />
              <strong style={{ fontSize: 14 }}>选择目录</strong>
              <div style={{ flex: 1 }} />
              <button className="btn btn-ghost btn-icon" onClick={() => setBrowserOpen(false)}><Icon name="x" size={15} /></button>
            </div>
            <div style={{ display: 'flex', gap: 6, marginBottom: 10 }}>
              <input value={pathInput} onChange={(e) => setPathInput(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') navigate(pathInput); }}
                placeholder="粘贴路径，回车前往…"
                style={{ flex: 1, fontSize: 12.5, fontFamily: 'var(--font-mono)', padding: '7px 10px', borderRadius: 'var(--radius-md)', border: '1px solid var(--border-default)', background: 'var(--surface-elevated)', color: 'var(--text-primary)' }} />
              <button className="btn btn-secondary btn-sm" onClick={() => navigate(pathInput)}>前往</button>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
              {browse && !browse.is_root && (
                <button className="btn btn-ghost btn-sm" onClick={() => navigate(browse.parent)}>
                  <Icon name="chevron-right" size={13} style={{ transform: 'rotate(180deg)' }} /> 上一级
                </button>
              )}
              <span style={{ fontSize: 12, color: 'var(--text-tertiary)', fontFamily: 'var(--font-mono)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {browse?.is_root ? '此电脑' : browse?.path}
              </span>
            </div>
            <div className="scroll" style={{ maxHeight: 320, overflowY: 'auto', border: '1px solid var(--border-subtle)', borderRadius: 'var(--radius-md)' }}>
              {browse?.shortcuts?.map((s) => (
                <div key={s.path} style={S.browseRow} onClick={() => navigate(s.path)}>
                  <Icon name="external" size={14} style={{ color: 'var(--text-tertiary)' }} /><span style={{ flex: 1 }}>{s.name}</span>
                  <span style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>快捷入口</span>
                </div>
              ))}
              {browse?.dirs?.map((d) => (
                <div key={d.path} style={S.browseRow} onClick={() => navigate(d.path)}>
                  <Icon name="folder" size={14} style={{ color: 'var(--text-tertiary)' }} /><span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{d.name}</span>
                  <Icon name="chevron-right" size={13} style={{ color: 'var(--text-tertiary)' }} />
                </div>
              ))}
              {browse && !browse.is_root && browse.dirs.length === 0 && browse.files.length === 0 && (
                <div style={{ padding: 16, fontSize: 12, color: 'var(--text-tertiary)' }}>（空目录）</div>
              )}
              {browse && browse.files.length > 0 && (
                <div style={{ padding: '6px 12px', fontSize: 11, color: 'var(--text-tertiary)' }}>含 {browse.files.length} 个可用文件</div>
              )}
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 14 }}>
              <button className="btn btn-ghost btn-sm" onClick={() => setBrowserOpen(false)}>取消</button>
              <button className="btn btn-primary btn-sm" disabled={!browse || browse.is_root} onClick={() => browse && chooseDir(browse.path)}>
                <Icon name="check" size={14} /> 选择此目录
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

const S: Record<string, React.CSSProperties> = {
  chip: { display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12.5, padding: '4px 10px', borderRadius: 999, border: '1px solid var(--border-default)', background: 'var(--surface-elevated)', color: 'var(--text-secondary)', cursor: 'pointer', fontFamily: 'inherit' },
  chipOn: { background: 'var(--text-primary)', color: '#fff', borderColor: 'var(--text-primary)' },
  row: { display: 'flex', alignItems: 'center', gap: 12, padding: '8px 10px', cursor: 'pointer', borderRadius: 8 },
  lead: { width: 40, height: 40, borderRadius: 8, flexShrink: 0, objectFit: 'cover', display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'var(--surface-subtle)' },
  cbox: { width: 22, height: 22, borderRadius: 6, flexShrink: 0, border: '1.5px solid var(--border-default)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'transparent' },
  modalMask: { position: 'fixed', inset: 0, background: 'rgba(15,23,32,0.42)', backdropFilter: 'blur(2px)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 50 },
  modal: { width: 'min(560px, 92vw)', background: 'var(--surface-elevated)', borderRadius: 'var(--radius-2xl)', border: '1px solid var(--border-default)', boxShadow: 'var(--shadow-lg)', padding: 'var(--space-5)' },
  browseRow: { display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px', fontSize: 13, color: 'var(--text-secondary)', cursor: 'pointer', borderBottom: '1px solid var(--border-subtle)' },
};

export default LocalSourcePicker;
