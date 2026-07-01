import React from 'react';
import useSWR from 'swr';
import { Icon } from '../components/icons';
import { api, fetcher, errMsg } from '../api';
import { useToast } from '../components/Toast';
import { DIR_KEY } from '../components/LocalSourcePicker';

// ── 类型 ───────────────────────────────────────────────────────────────
type Companion = { days: number; first_doc_date: string; first_doc_title: string };
type Stats = { updated: number; created: number; meetings: number; contracts: number; spaces: number; local_files?: number };
type CatBucket = { name: string; count: number };
type TimelineItem = {
  asset_id: string; title: string; url: string; type: string;
  category: string; summary: string; updated: string; is_new: boolean;
};
type DayGroup = { date: string; weekday: string; items: TimelineItem[] };
type Summary = {
  period: string; offset: number; range_label: string;
  range_start: string; range_end: string; has_prev: boolean; has_next: boolean;
  data_through: string;
  companion: Companion; stats: Stats; by_category: CatBucket[]; timeline: DayGroup[];
};
type Highlight = { asset_id: string; title: string; url: string; type: string; category: string; reason: string };
type Narrative = { narrative: string; highlights: Highlight[]; range_label: string; error?: string };

type Period = 'week' | 'month' | 'year';

const PERIODS: { id: Period; label: string }[] = [
  { id: 'week', label: '本周' },
  { id: 'month', label: '本月' },
  { id: 'year', label: '本年' },
];

const TYPE_ZH: Record<string, string> = {
  doc: '云文档', docx: '云文档', wiki: '知识库', sheet: '电子表格',
  bitable: '多维表格', base: '多维表格', slides: '幻灯片', file: '文件',
  shortcut: '快捷方式', meeting: '会议纪要', mindnote: '思维导图', folder: '文件夹',
  // 本地目录文件分类
  image: '截图', pdf: 'PDF', word: 'Word', excel: 'Excel', ppt: 'PPT',
};

const STAT_META: { key: keyof Stats; label: string; hint: string }[] = [
  { key: 'updated', label: '动过文档', hint: '本期有更新的飞书文档总数' },
  { key: 'created', label: '新建', hint: '本期新创建' },
  { key: 'meetings', label: '会议纪要', hint: '妙记 / 智能纪要' },
  { key: 'contracts', label: '合同', hint: 'PDF / 合同类' },
  { key: 'spaces', label: '涉及空间', hint: '跨越的知识库 / 云空间数' },
];
const LOCAL_STAT: { key: keyof Stats; label: string; hint: string } =
  { key: 'local_files', label: '本地文件', hint: '本地目录中本期改动的文件' };

const openAsset = (url: string) => { if (url) window.open(url, '_blank', 'noopener,noreferrer'); };

// 主题分布配色（与品牌绿同调，循环取色）。
const CAT_COLORS = ['#10A37F', '#3B82C4', '#C2843B', '#8B6FC4', '#C45B7C', '#5AA9A0', '#B0883A'];

const SummariesPage: React.FC = () => {
  const toast = useToast();
  const [period, setPeriod] = React.useState<Period>('week');
  const [offset, setOffset] = React.useState(0);
  // 本地目录（用户在「本地目录 / 内容生成」里选的那个）一并纳入回顾。
  const localDir = (() => { try { return localStorage.getItem(DIR_KEY) || ''; } catch { return ''; } })();
  const key = `/api/summaries?period=${period}&offset=${offset}${localDir ? `&local_dir=${encodeURIComponent(localDir)}` : ''}`;
  const { data, isLoading, mutate } = useSWR<Summary>(key, fetcher, { revalidateOnFocus: false });
  const [refreshing, setRefreshing] = React.useState(false);

  async function refreshIndex() {
    setRefreshing(true);
    try {
      await api.post('/api/assets/refresh');
      await mutate();
      toast.success('索引已刷新', { detail: '已拉取飞书最新改动' });
    } catch (e) {
      toast.error('索引刷新失败', { detail: errMsg(e) });
    } finally {
      setRefreshing(false);
    }
  }

  // 叙述回顾按 (period, offset) 缓存在前端，切回时不必重生成。
  const [narrCache, setNarrCache] = React.useState<Record<string, Narrative>>({});
  const [narrLoading, setNarrLoading] = React.useState(false);
  const narr = narrCache[key];

  // 切周期时回到当前期。
  const pickPeriod = (p: Period) => { setPeriod(p); setOffset(0); };

  async function genNarrative() {
    setNarrLoading(true);
    try {
      const r = await api.post<Narrative>('/api/summaries/narrative', { period, offset, local_dir: localDir || undefined });
      if (r.error) toast.error('生成回顾失败', { detail: r.error });
      else if (!r.narrative) toast.info('本期没有可回顾的内容');
      setNarrCache(prev => ({ ...prev, [key]: r }));
    } catch (e) {
      toast.error('生成回顾失败', { detail: errMsg(e) });
    } finally {
      setNarrLoading(false);
    }
  }

  const c = data?.companion;
  const stats = data?.stats;
  const maxCat = Math.max(1, ...(data?.by_category || []).map(b => b.count));
  const localN = stats?.local_files || 0;
  const empty = !isLoading && data && stats && stats.updated === 0 && localN === 0;
  // 配置了本地目录才显示「本地文件」格子。
  const statMeta = localDir ? [...STAT_META, LOCAL_STAT] : STAT_META;

  return (
    <div style={{ padding: 'var(--space-8)', maxWidth: 1080, margin: '0 auto' }} className="fade-in">
      {/* ── 陪伴横幅（账号级，不随周期变化）── */}
      <div style={S.hero}>
        <div style={S.heroGlow} aria-hidden />
        <div style={{ position: 'relative' }}>
          <div className="eyebrow" style={{ color: 'rgba(255,255,255,0.78)' }}>时间轴回顾</div>
          {c && c.days > 0 ? (
            <>
              <div style={S.heroTitle}>
                👋 你已与飞书同行 <span style={{ fontSize: 38, fontWeight: 800 }}>{c.days.toLocaleString()}</span> 天
              </div>
              <div style={S.heroSub}>
                从 {c.first_doc_date} 的第一个文档
                {c.first_doc_title ? <>《{trim(c.first_doc_title, 28)}》</> : null} 开始
              </div>
            </>
          ) : (
            <div style={S.heroTitle}>👋 欢迎来到工作回顾</div>
          )}
        </div>
      </div>

      {/* ── 周期切换 + 上一期 / 下一期 ── */}
      <div style={S.controls}>
        <div style={S.segment}>
          {PERIODS.map(p => (
            <button key={p.id} onClick={() => pickPeriod(p.id)}
              className="btn"
              style={{ ...S.segBtn, ...(period === p.id ? S.segActive : null) }}>
              {p.label}
            </button>
          ))}
        </div>
        <div style={{ flex: 1 }} />
        <button className="btn btn-ghost btn-icon" onClick={() => setOffset(o => o - 1)}
          title="上一期" aria-label="上一期">
          <Icon name="chevron-right" size={16} style={{ transform: 'rotate(180deg)' }} />
        </button>
        <div style={{ textAlign: 'center', minWidth: 150 }}>
          <div style={{ fontWeight: 700, fontSize: 'var(--text-base)', color: 'var(--text-primary)' }}>
            {data?.range_label || '—'}
          </div>
          {data && (
            <div style={{ fontSize: 11, color: 'var(--text-tertiary)', fontFamily: 'var(--font-mono)' }}>
              {data.range_start} ~ {data.range_end}
            </div>
          )}
        </div>
        <button className="btn btn-ghost btn-icon" onClick={() => setOffset(o => o + 1)}
          disabled={!data?.has_next} title="下一期" aria-label="下一期">
          <Icon name="chevron-right" size={16} />
        </button>
      </div>

      {/* ── 数据截至提示（消除"本周为空=我没干活"的误解）── */}
      {data?.data_through && (
        <div style={S.cutoff}>
          <Icon name="refresh" size={12} style={{ color: 'var(--text-tertiary)' }} />
          <span>数据截至 {fmtStamp(data.data_through)}</span>
          <button className="btn btn-ghost btn-sm" onClick={refreshIndex} disabled={refreshing}
            style={{ padding: '2px 8px', color: 'var(--brand-700)' }}>
            <Icon name="refresh" size={12} className={refreshing ? 'spin' : ''} />
            {refreshing ? '刷新中…' : '点此刷新'}
          </button>
        </div>
      )}

      {/* ── 数字概览卡 ── */}
      <div style={S.statRow}>
        {statMeta.map(m => (
          <div key={m.key} className="card" style={S.statCard} title={m.hint}>
            <div style={S.statNum}>{stats ? (stats[m.key] ?? 0) : '—'}</div>
            <div style={S.statLabel}>{m.label}</div>
          </div>
        ))}
      </div>

      {empty ? (
        <div className="card" style={{ textAlign: 'center', padding: 'var(--space-8)', color: 'var(--text-tertiary)' }}>
          <Icon name="calendar" size={32} style={{ opacity: 0.4 }} />
          <div style={{ marginTop: 12, fontSize: 'var(--text-sm)' }}>
            {data?.range_label} 没有动过的文档。试试切换到其它周期，或先到「飞书文档」刷新索引。
          </div>
        </div>
      ) : (
        <>
          {/* ── AI 叙述回顾 ── */}
          <div className="card" style={{ marginBottom: 'var(--space-5)' }}>
            <div style={S.cardHead}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={S.dotBrand} />
                <span style={{ fontWeight: 600 }}>本期回顾</span>
                <span className="eyebrow" style={{ color: 'var(--text-tertiary)' }}>AI · 快档</span>
              </div>
              <button className="btn btn-tonal btn-sm" onClick={genNarrative} disabled={narrLoading || isLoading}>
                <Icon name={narrLoading ? 'refresh' : 'sparkle'} size={13} className={narrLoading ? 'spin' : ''} />
                {narr ? '重新生成' : '生成回顾'}
              </button>
            </div>
            {narr?.narrative ? (
              <>
                <p style={S.narrative}>{narr.narrative}</p>
                {narr.highlights.length > 0 && (
                  <div style={{ marginTop: 14 }}>
                    <div className="eyebrow" style={{ marginBottom: 8 }}>重点 · Top {narr.highlights.length}</div>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                      {narr.highlights.map((h, i) => (
                        <div key={h.asset_id + i} style={S.highlight} onClick={() => openAsset(h.url)}
                          title={h.url ? '在飞书打开' : ''}>
                          <span style={S.rank}>{i + 1}</span>
                          <div style={{ flex: 1, minWidth: 0 }}>
                            <div style={S.hlTitle}>{h.title}</div>
                            {h.reason && <div style={S.hlReason}>{h.reason}</div>}
                          </div>
                          {h.url && <Icon name="external" size={13} style={{ color: 'var(--text-tertiary)', flexShrink: 0 }} />}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </>
            ) : (
              <p style={{ color: 'var(--text-tertiary)', fontSize: 'var(--text-sm)', margin: '4px 0 0', lineHeight: 1.7 }}>
                点「生成回顾」让 AI 基于本期动过的 {stats?.updated ?? 0} 篇文档{localN ? ` 与 ${localN} 个本地文件` : ''}，写一段工作回顾并挑出重点。
                （只读标题与摘要，不读全文，秒级返回）
              </p>
            )}
          </div>

          {/* ── 主题分布 ── */}
          {data && data.by_category.length > 0 && (
            <div className="card" style={{ marginBottom: 'var(--space-5)' }}>
              <div style={{ fontWeight: 600, marginBottom: 14 }}>按主题</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                {data.by_category.map((b, i) => (
                  <div key={b.name} style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                    <div style={S.catName}>{b.name}</div>
                    <div style={S.barTrack}>
                      <div style={{
                        ...S.barFill,
                        width: `${Math.max(6, (b.count / maxCat) * 100)}%`,
                        background: CAT_COLORS[i % CAT_COLORS.length],
                      }} />
                    </div>
                    <div style={S.catCount}>{b.count}</div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* ── 时间轴 ── */}
          <div className="card">
            <div style={{ fontWeight: 600, marginBottom: 6 }}>时间轴</div>
            {data?.timeline.map(day => (
              <div key={day.date} style={S.dayBlock}>
                <div style={S.dayHead}>
                  <span style={S.dayDot} />
                  <span style={{ fontWeight: 600, fontSize: 'var(--text-sm)' }}>{fmtDay(day.date)}</span>
                  <span style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>{day.weekday}</span>
                  <span style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>· {day.items.length} 篇</span>
                </div>
                <div style={S.dayItems}>
                  {day.items.map(it => (
                    <div key={it.asset_id} style={S.tlItem} onClick={() => openAsset(it.url)}
                      title={it.url ? '在飞书打开' : ''}
                      onMouseEnter={e => (e.currentTarget.style.background = 'var(--surface-hover)')}
                      onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}>
                      <span style={S.typeTag}>{TYPE_ZH[it.type] || it.type}</span>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={S.tlTitle}>
                          {it.is_new && <span style={S.newBadge}>新</span>}
                          {it.title}
                        </div>
                        {it.summary && <div style={S.tlSummary}>{it.summary}</div>}
                      </div>
                      {it.category && <span style={S.catChip}>{it.category}</span>}
                      <span style={S.tlTime}>{(it.updated || '').slice(11, 16)}</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
};

// ── helpers ──────────────────────────────────────────────────────────────
function trim(s: string, n: number) { return s.length > n ? s.slice(0, n) + '…' : s; }
function fmtDay(iso: string) {
  // "2026-06-09" → "6 月 9 日"
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  if (!m) return iso;
  return `${Number(m[2])} 月 ${Number(m[3])} 日`;
}
function fmtStamp(iso: string) {
  // "2026-06-05T09:28:11" → "2026-06-05 09:28"
  const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(iso);
  return m ? `${m[1]} ${m[2]}` : iso.slice(0, 16).replace('T', ' ');
}

// ── styles ───────────────────────────────────────────────────────────────
const S: Record<string, React.CSSProperties> = {
  hero: {
    position: 'relative', overflow: 'hidden',
    borderRadius: 'var(--radius-2xl)', padding: '26px 28px',
    background: 'var(--grad-brand)', color: '#fff',
    boxShadow: 'var(--shadow-brand)', marginBottom: 'var(--space-6)',
  },
  heroGlow: {
    position: 'absolute', top: -60, right: -40, width: 220, height: 220,
    borderRadius: '50%', background: 'rgba(255,255,255,0.16)', filter: 'blur(8px)',
  },
  heroTitle: { fontSize: 22, fontWeight: 700, marginTop: 8, letterSpacing: '-0.01em', display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' },
  heroSub: { marginTop: 8, fontSize: 'var(--text-sm)', color: 'rgba(255,255,255,0.85)' },

  controls: { display: 'flex', alignItems: 'center', gap: 8, marginBottom: 'var(--space-3)' },
  cutoff: { display: 'flex', alignItems: 'center', gap: 6, marginBottom: 'var(--space-4)', fontSize: 12, color: 'var(--text-tertiary)' },
  segment: { display: 'inline-flex', background: 'var(--surface-subtle)', borderRadius: 'var(--radius-lg)', padding: 3, gap: 2 },
  segBtn: { padding: '6px 16px', fontSize: 'var(--text-sm)', background: 'transparent', color: 'var(--text-secondary)', borderRadius: 'var(--radius-md)', fontWeight: 500 },
  segActive: { background: 'var(--surface-elevated)', color: 'var(--brand-700)', boxShadow: 'var(--shadow-sm)', fontWeight: 600 },

  statRow: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(108px, 1fr))', gap: 'var(--space-3)', marginBottom: 'var(--space-5)' },
  statCard: { padding: '16px 14px', textAlign: 'center' },
  statNum: { fontSize: 26, fontWeight: 700, color: 'var(--text-primary)', lineHeight: 1.1, fontFamily: 'var(--font-mono)' },
  statLabel: { fontSize: 12, color: 'var(--text-tertiary)', marginTop: 4 },

  cardHead: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 },
  dotBrand: { width: 8, height: 8, borderRadius: 4, background: 'var(--brand-500)' },
  narrative: { margin: '4px 0 0', fontSize: 'var(--text-base)', lineHeight: 1.8, color: 'var(--text-primary)' },
  highlight: { display: 'flex', alignItems: 'center', gap: 10, padding: '8px 10px', borderRadius: 'var(--radius-md)', background: 'var(--surface-subtle)', cursor: 'pointer' },
  rank: { width: 20, height: 20, borderRadius: 6, flexShrink: 0, background: 'var(--brand-50)', color: 'var(--brand-700)', fontSize: 12, fontWeight: 700, display: 'flex', alignItems: 'center', justifyContent: 'center' },
  hlTitle: { fontSize: 'var(--text-sm)', fontWeight: 500, color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
  hlReason: { fontSize: 12, color: 'var(--text-tertiary)', marginTop: 1 },

  catName: { width: 96, flexShrink: 0, fontSize: 'var(--text-sm)', color: 'var(--text-secondary)', textAlign: 'right', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
  barTrack: { flex: 1, height: 18, borderRadius: 6, background: 'var(--surface-subtle)', overflow: 'hidden' },
  barFill: { height: '100%', borderRadius: 6, transition: 'width 400ms var(--ease)' },
  catCount: { width: 32, textAlign: 'right', fontSize: 'var(--text-sm)', fontWeight: 600, color: 'var(--text-secondary)', fontFamily: 'var(--font-mono)' },

  dayBlock: { paddingLeft: 6, marginTop: 14 },
  dayHead: { display: 'flex', alignItems: 'center', gap: 8, paddingBottom: 6 },
  dayDot: { width: 9, height: 9, borderRadius: 5, background: 'var(--brand-400)', boxShadow: '0 0 0 3px var(--brand-50)' },
  dayItems: { display: 'flex', flexDirection: 'column', gap: 2, marginLeft: 4, paddingLeft: 14, borderLeft: '2px solid var(--border-subtle)' },
  tlItem: { display: 'flex', alignItems: 'center', gap: 10, padding: '8px 10px', borderRadius: 'var(--radius-md)', cursor: 'pointer', transition: 'background 120ms' },
  typeTag: { flexShrink: 0, fontSize: 11, padding: '2px 7px', borderRadius: 5, background: 'var(--surface-subtle)', color: 'var(--text-tertiary)', fontWeight: 500 },
  tlTitle: { fontSize: 'var(--text-sm)', fontWeight: 500, color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
  newBadge: { fontSize: 10, fontWeight: 700, color: 'var(--brand-700)', background: 'var(--brand-50)', borderRadius: 4, padding: '1px 5px', marginRight: 6 },
  tlSummary: { fontSize: 12, color: 'var(--text-tertiary)', marginTop: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
  catChip: { flexShrink: 0, fontSize: 11, padding: '2px 8px', borderRadius: 10, background: 'var(--brand-50)', color: 'var(--brand-700)' },
  tlTime: { flexShrink: 0, fontSize: 11, color: 'var(--text-tertiary)', fontFamily: 'var(--font-mono)', width: 36, textAlign: 'right' },
};

export default SummariesPage;
