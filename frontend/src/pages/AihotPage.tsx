import React, { useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import useSWR from 'swr';
import { Icon } from '../components/icons';
import { fetcher } from '../api';

/**
 * AIHot 内容和模型 —— 打开即看的数据页（不是任务页）。
 *
 * 两个标签直连后端 /api/aihot/*：抓 AIHOT → 归一化 → 渲染，不经过大模型，
 * 命中缓存约 0.1s。「让 AI 给选型建议 / 编简报」是可选动作，跳到对应 Agent 任务页，
 * 不是看内容的前提。
 */

type Row = {
  rank: number; previous_rank: number | null; rank_change: number;
  name: string; provider: string; slug: string; detail_url: string; released: string;
  score: number; uncertainty: number | null; rank_from: number | null; rank_to: number | null;
  completeness: number | null; confidence: string; metric_count: number | null; summary: string;
  context_tokens: number | null;
  price_in: number | null; price_out: number | null; price_blended: number | null;
  price_label: string; price_source: string; price_source_url: string; price_verified_at: string;
  price_basis: string; value: number | null; value_rank: number | null;
  components: Record<string, { score: number; coverage: number; metricCount: number }>;
};
type LocalModel = {
  tier: string; setting: string; model: string; usage: string;
  on_board: boolean; rank: number | null; score: number | null; value: number | null;
  confidence: string; board_name: string;
};
/** 本机在用、但这张榜不比的那几路（读图 / 兜底读图 / 生图 / 语音）。没有名次和性价比。 */
type OtherModel = { kind: string; setting: string; model: string; provider: string; usage: string };
type Policy = { commercial_use: string; contact: string; policy_version: string };
type Board = {
  source_url: string; updated_label: string; board_count: number; fetched_at: string;
  cached: boolean; cache_age_s: number; policy: Policy; in_out_ratio: number;
  total_on_board: number; providers: string[]; rows: Row[]; local_models: LocalModel[];
  other_models: OtherModel[];
};
type NewsItem = {
  id: string; title: string; summary: string; reason: string; category_zh: string;
  source_name: string; url: string; aihot_url: string; story_url: string;
  published_zh: string; score: number | null; hot_rank?: number;
};
type HotTopic = {
  rank: number; title: string; source_count: number | null; signal_count: number | null;
  story_id: string; story_url: string; url: string; latest_zh: string;
};
type News = {
  source_url: string; window: string; mode: string; fetched_at: string; cached: boolean;
  cache_age_s: number; policy: Policy; categories: { id: string; label: string }[];
  items: NewsItem[]; hot_topics: HotTopic[]; hot_merged: number; hot_error: string;
};
type Story = {
  story: {
    title: string; digest: string; latest: string; url: string;
    source_count: number | null; report_count: number | null;
    timeline: { at: string; title: string; source: string; url: string }[];
  };
};
type Daily = {
  date: string; url: string; lead: string;
  sections: { label: string; items: NewsItem[] }[];
};

const ACCENT = '#4F46E5';
const NEWS_ACCENT = '#0891B2';
const CONF_CN: Record<string, string> = { HIGH: '高', MEDIUM: '中', LOW: '低' };
const CONF_BADGE: Record<string, string> = { HIGH: 'badge-success', MEDIUM: 'badge-warning', LOW: 'badge-error' };

// 站点「算法规则」页（/leaderboard/rules）的原文口径。这一列最容易被误读成
// 「模型可不可信」，实际是「证据够不够」——站点原话：描述证据充分性，而非模型能力。
const CONF_TIP = [
  '证据可信度 = 这个分数背后的证据够不够，不是模型好不好（站点原话：描述证据充分性，而非模型能力）。',
  '高：≥6 个独立证据家族 · 完整度 ≥80% · 标准误 ≤5',
  '中：≥4 个独立证据家族 · 完整度 ≥40% · 标准误 ≤8',
  '低：其余情况',
  '标「中/低」只说明样本还不够、分数可能随后续评测变动，不代表模型差。',
].join('\n');

function cny(v: number | null, label?: string): string {
  if (v === null || v === undefined) return label || '待核验';
  return '¥' + Number(v).toFixed(2).replace(/\.?0+$/, '');
}

/**
 * 上下文窗口的短标签。
 *
 * 站点的 contextWindowTokens 是真实 token 数，且同一句「1M 上下文」在不同厂商那里
 * 有好几个写法：1000000（十进制）、1048576（2^20）、1050000、1310720。直接除以 1e6
 * 会得到 `1.048576M` 这种没人想看的数，所以四舍五入到 2 位并去掉多余的 0
 * （1000000→1M、1048576→1.05M、1310720→1.31M）。精确值放在 title 里，鼠标悬停可查，
 * 不会因为取整把 1050000 和 1048576 的区别彻底抹掉。
 */
function ctxLabel(t: number | null): string {
  if (!t) return '—';
  if (t >= 1_000_000) return `${+(t / 1_000_000).toFixed(2)}M`;
  if (t >= 1000) return `${Math.round(t / 1000)}K`;
  return String(t);
}

function ctxExact(t: number | null): string | undefined {
  return t ? `${t.toLocaleString('en-US')} tokens` : undefined;
}

/** 数据来源 / 抓取时间 / 缓存状态 + 商用授权提示。两个标签共用。 */
const Provenance: React.FC<{
  sourceUrl: string; fetchedAt: string; cached: boolean; ageS: number;
  policy?: Policy; accent: string; onRefresh: () => void; refreshing: boolean;
  children?: React.ReactNode;
}> = ({ sourceUrl, fetchedAt, cached, ageS, policy, accent, onRefresh, refreshing, children }) => (
  <div className="card" style={{ borderTop: `3px solid ${accent}`, marginBottom: 'var(--space-4)' }}>
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
      <a href={sourceUrl} target="_blank" rel="noreferrer" style={{ fontWeight: 600 }}>
        AIHOT <Icon name="external" size={12} />
      </a>
      {children}
      {cached
        ? <span className="badge badge-info" title="复用本地磁盘缓存，没有再打站点接口">缓存 {ageS}s</span>
        : <span className="badge badge-success" title="刚从站点抓取">实时</span>}
      {fetchedAt && <span style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>抓取于 {fetchedAt}</span>}
      <div style={{ flex: 1 }} />
      <button className="btn btn-tonal btn-sm" onClick={onRefresh} disabled={refreshing}
              title="忽略本地缓存，强制从站点重抓">
        <Icon name="refresh" size={13} /> {refreshing ? '刷新中…' : '强制刷新'}
      </button>
    </div>
    {policy?.commercial_use && (
      <div style={{ marginTop: 10, fontSize: 12, color: 'var(--text-tertiary)', lineHeight: 1.6 }}>
        站点声明 <code>X-AIHOT-Commercial-Use: {policy.commercial_use}</code> —— <strong>商用须先取得书面授权</strong>
        {policy.contact ? <>（联系 {policy.contact}）</> : null}。本页仅本机查看。
      </div>
    )}
  </div>
);

/** api.ts 的 fetcher 把错误拼成 `502 {"detail":"…"}`，这里把 detail 掏出来给人看。 */
function errText(e: any): string {
  const raw = (e instanceof Error ? e.message : String(e ?? '')).trim();
  const brace = raw.indexOf('{');
  if (brace >= 0) {
    try {
      const j = JSON.parse(raw.slice(brace));
      if (typeof j?.detail === 'string') return j.detail;
      if (typeof j?.message === 'string') return j.message;
    } catch { /* 不是 JSON 就原样显示 */ }
  }
  return raw || '未知错误';
}

const ErrCard: React.FC<{ error: any; what: string; onRetry?: () => void }> = ({ error, what, onRetry }) => (
  <div className="card" style={{ borderLeft: '3px solid var(--error)' }}>
    <div style={{ fontWeight: 600, marginBottom: 6 }}>{what}取数失败</div>
    <div style={{ fontSize: 13, color: 'var(--text-secondary)', lineHeight: 1.7 }}>{errText(error)}</div>
    {onRetry && (
      <button className="btn btn-tonal btn-sm" style={{ marginTop: 10 }} onClick={onRetry}>
        <Icon name="refresh" size={13} /> 重试
      </button>
    )}
  </div>
);

const Loading: React.FC<{ what: string }> = ({ what }) => (
  <div className="card" style={{ color: 'var(--text-tertiary)' }}>正在从 AIHOT 拉取{what}…</div>
);

// ── 模型榜 ──────────────────────────────────────────────────────────

const LeaderboardTab: React.FC = () => {
  const nav = useNavigate();
  const [limit, setLimit] = useState(30);
  const [provider, setProvider] = useState('');
  const [byValue, setByValue] = useState(false);
  const [open, setOpen] = useState<string>('');   // 展开哪一行的证据分项
  const [bust, setBust] = useState(0);

  const key = `/api/aihot/leaderboard?limit=${limit}&provider=${encodeURIComponent(provider)}`
    + (bust ? `&refresh=true&_=${bust}` : '');
  const { data, error, isLoading, mutate } = useSWR<Board>(key, fetcher, { revalidateOnFocus: false });

  if (error) return <ErrCard error={error} what="模型榜" onRetry={() => mutate()} />;
  if (isLoading || !data) return <Loading what="模型榜" />;

  const mine = new Set(data.local_models.filter(m => m.on_board).map(m => m.board_name));
  const rows = byValue
    ? [...data.rows].sort((a, b) => (b.value ?? -1) - (a.value ?? -1))
    : data.rows;

  return (
    <>
      <Provenance sourceUrl={data.source_url} fetchedAt={data.fetched_at} cached={data.cached}
                  ageS={data.cache_age_s} policy={data.policy} accent={ACCENT}
                  refreshing={false} onRefresh={() => { setBust(Date.now()); mutate(); }}>
        <span style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
          榜单更新于 <strong>{data.updated_label || '未知'}</strong> · 综合 <strong>{data.board_count || '?'}</strong> 家公开榜单
          · 榜上 {data.total_on_board} 个模型
        </span>
      </Provenance>

      {/* 本机三档模型在榜位置 */}
      {data.local_models.length > 0 && (
        <div className="card" style={{ marginBottom: 'var(--space-4)' }}>
          <h3 style={{ marginTop: 0, marginBottom: 4, fontSize: 15 }}>
            本机在用的模型
            <span style={{ fontWeight: 400, fontSize: 12, color: 'var(--text-tertiary)' }}> · 读自 backend/.env</span>
          </h3>
          {data.other_models?.length > 0 && (
            <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 10 }}>
              三档文本模型能在榜上对号，下面几路不能 —— 但都在跑
            </div>
          )}
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
            {data.local_models.map(m => (
              <div key={m.setting} style={{
                border: '1px solid var(--border-subtle)', borderRadius: 10, padding: '8px 12px',
                minWidth: 190, background: m.on_board ? 'var(--brand-50)' : 'var(--surface-subtle)',
              }}>
                <div style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>{m.tier}档 · <code>{m.setting}</code></div>
                <div style={{ fontWeight: 600, margin: '2px 0' }}>{m.model || '—'}</div>
                {m.on_board
                  ? <div style={{ fontSize: 12 }}>
                      <span className="badge badge-brand">第 {m.rank} 名</span>{' '}
                      共识分 {m.score?.toFixed(1)} · 性价比 {m.value?.toFixed(2) ?? '—'}
                    </div>
                  : <span className="badge badge-warning" title="榜单里没有这个型号，无法用榜单数据比较">未上榜</span>}
              </div>
            ))}
          </div>

          {/* 不参与比较的那几路。
              以前这张卡只有三档文本模型，标题却是「本机在用的模型」—— 读起来像一份
              完整清单，实际是「参与榜单比较的模型」。读图/生图/语音也在本机跑，
              生图和语音更是在整个界面里没有第二处能看到。
              这里**不给它们标「未上榜」**：那读起来是排名靠后或查不到，
              而实情是这张榜只比通用文本模型，根本不管这类模型。 */}
          {data.other_models?.length > 0 && (
            <>
              <div style={{ height: 1, background: 'var(--border-subtle)', margin: '14px 0 10px' }} />
              <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 8 }}>
                下面几路本机也在用，但这张榜只比通用文本模型，没有名次和性价比可对照
              </div>
              <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
                {data.other_models.map(m => (
                  <div key={m.setting} style={{
                    border: '1px solid var(--border-subtle)', borderRadius: 10, padding: '8px 12px',
                    minWidth: 190, background: 'var(--surface-subtle)',
                  }}>
                    <div style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>
                      {m.kind} · <code>{m.setting}</code>
                    </div>
                    <div style={{ fontWeight: 600, margin: '2px 0' }}>
                      {m.model || '—'}
                      {m.provider && (
                        <span style={{ fontWeight: 400, fontSize: 12, color: 'var(--text-tertiary)' }}>
                          {' '}· {m.provider}
                        </span>
                      )}
                    </div>
                    <div style={{ fontSize: 12, color: 'var(--text-tertiary)', lineHeight: 1.5 }}>{m.usage}</div>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      )}

      <div className="card">
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginBottom: 12 }}>
          <h3 style={{ margin: 0, fontSize: 15 }}>
            模型榜<span style={{ fontWeight: 400, color: 'var(--text-tertiary)', fontSize: 13 }}> · 前 {rows.length} 名</span>
          </h3>
          <select className="input" style={{ width: 150, height: 30, padding: '2px 8px' }}
                  value={provider} onChange={e => setProvider(e.target.value)}>
            <option value="">全部厂商</option>
            {data.providers.map(p => <option key={p} value={p}>{p}</option>)}
          </select>
          <select className="input" style={{ width: 110, height: 30, padding: '2px 8px' }}
                  value={limit} onChange={e => setLimit(parseInt(e.target.value))}>
            {[10, 20, 30, 50, 100].map(n => <option key={n} value={n}>前 {n} 名</option>)}
          </select>
          <button className="btn btn-tonal btn-sm" onClick={() => setByValue(v => !v)}
                  title="在共识分排序（站点原序）和性价比排序（本机计算）之间切换">
            <Icon name="refresh" size={13} /> {byValue ? '按共识分排序' : '按性价比排序'}
          </button>
          <div style={{ flex: 1 }} />
          <button className="btn btn-tonal btn-sm" onClick={() => nav('/task/aihot-models')}
                  title="在这份榜单上再叠一层 AI：三档选型推荐 + 本机模型换/留判断（约 2.5 分钟）">
            <Icon name="sparkle" size={13} /> 让 AI 给选型建议
          </button>
        </div>

        <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 12, lineHeight: 1.6 }}>
          除<strong>性价比</strong>外都是 AIHOT 榜单口径（
          <a href={`${data.source_url}/leaderboard/rules`} target="_blank" rel="noreferrer">算法规则</a>
          ）。<strong>证据可信度</strong>说的是<strong>证据够不够</strong>，不是模型好不好——标「中 / 低」只意味着
          样本还不足、分数可能随后续评测变动（悬停看判据）。<strong>性价比</strong>为本机计算 = 共识分 ÷ 混合价，
          混合价 =（输入价 + {data.in_out_ratio}×输出价）÷ {1 + Number(data.in_out_ratio)}
          （假设输入:输出 ≈ 1:{data.in_out_ratio}）；价格待核验的模型不参与。
          点任意一行展开各评测家族的分项得分、名次区间与价格出处。
        </div>

        <div style={{ overflowX: 'auto' }}>
          <table className="table">
            <thead><tr>
              <th>排名</th><th>模型 / 厂商</th><th>上线日期</th>
              <th title="该模型被多少评测覆盖到（站点口径 coverage）。只对真正跑出结果的子项累计证据预算；缺榜不直接扣分，只降低确定性。">
                评测完整度
              </th>
              <th>输入成本</th><th>输出成本</th>
              <th title="不是各榜分数的平均值。站点用正则化 Bradley–Terry 模型估计模型在统一能力尺度上的位置：Score = 100/|A| × Σ P(战胜锚点模型 a)，即「平均战胜固定锚点模型的概率 ×100」。">
                AIHOT 共识分
              </th>
              <th title={CONF_TIP}>证据可信度</th>
              <th>性价比*</th><th>上下文</th>
            </tr></thead>
            <tbody>
              {rows.map(r => (
                <React.Fragment key={r.slug || r.rank}>
                  <tr style={{ cursor: 'pointer' }}
                      onClick={() => setOpen(o => (o === r.slug ? '' : r.slug))}>
                    <td className="mono" style={{ fontWeight: 700 }}>{String(r.rank).padStart(2, '0')}</td>
                    <td>
                      {r.detail_url
                        ? <a href={r.detail_url} target="_blank" rel="noreferrer" style={{ fontWeight: 600 }}
                             onClick={e => e.stopPropagation()}>{r.name}</a>
                        : <strong>{r.name}</strong>}
                      {mine.has(r.name) && <> <span className="badge badge-brand">本机在用</span></>}
                      <div style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>{r.provider}</div>
                    </td>
                    <td style={{ whiteSpace: 'nowrap' }}>{r.released || '—'}</td>
                    <td>{r.completeness !== null ? `${r.completeness.toFixed(1)}%` : '—'}</td>
                    <td style={{ whiteSpace: 'nowrap' }}>{cny(r.price_in, r.price_label)}</td>
                    <td style={{ whiteSpace: 'nowrap' }}>{cny(r.price_out, r.price_label)}</td>
                    <td><strong>{r.score.toFixed(1)}</strong></td>
                    <td><span className={`badge ${CONF_BADGE[r.confidence] || 'badge-info'}`}
                              title={CONF_TIP}>
                      {CONF_CN[r.confidence] || r.confidence || '—'}</span></td>
                    <td title={r.value_rank ? `性价比第 ${r.value_rank} 名` : '价格待核验，不参与性价比排名'}>
                      {r.value ? r.value.toFixed(2) : '—'}
                    </td>
                    <td style={{ whiteSpace: 'nowrap', color: 'var(--text-tertiary)', fontSize: 12 }}
                        title={ctxExact(r.context_tokens)}>
                      {ctxLabel(r.context_tokens)}
                    </td>
                  </tr>
                  {open === r.slug && (
                    <tr>
                      <td colSpan={10} style={{ background: 'var(--surface-subtle)' }}>
                        {r.summary && <div style={{ marginBottom: 8, fontSize: 13 }}>{r.summary}</div>}
                        <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 8 }}>
                          共 {r.metric_count ?? '?'} 项评测 · 不确定度 ±{r.uncertainty ?? '?'} ·
                          {/* 名次区间从主表移到这里：日常看榜用不上，但要判断「两个模型的先后是否站得住」时得有它 */}
                          {' '}<span title="考虑不确定度后该模型可能落在的排名区间。两个模型区间大幅重叠 = 它们的先后在统计上不结实。">
                            名次区间 {r.rank_from ? `${r.rank_from}–${r.rank_to}` : '—'}
                          </span> ·
                          {' '}混合价 {r.price_blended ? `¥${r.price_blended}` : '—'} ·
                          {' '}上下文 {r.context_tokens ? `${r.context_tokens.toLocaleString('en-US')} tokens` : '—'}
                          {r.price_source && <>　价格出处：{r.price_source_url
                            ? <a href={r.price_source_url} target="_blank" rel="noreferrer">{r.price_source}</a>
                            : r.price_source}
                            {r.price_verified_at && <>（核验于 {r.price_verified_at}）</>}</>}
                        </div>
                        {r.price_basis && (
                          <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 8 }}>
                            计价口径：{r.price_basis}
                          </div>
                        )}
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                          {Object.entries(r.components || {}).map(([k, v]) => (
                            <div key={k} style={{
                              border: '1px solid var(--border-subtle)', borderRadius: 8,
                              padding: '4px 10px', background: 'var(--surface-elevated)', fontSize: 12,
                            }}>
                              <div style={{ color: 'var(--text-tertiary)' }}>{k}</div>
                              <div><strong>{v.score?.toFixed(1)}</strong>
                                <span style={{ color: 'var(--text-tertiary)' }}> · 覆盖 {(v.coverage * 100).toFixed(0)}%</span>
                              </div>
                            </div>
                          ))}
                        </div>
                      </td>
                    </tr>
                  )}
                </React.Fragment>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
};

// ── AI 新闻 ─────────────────────────────────────────────────────────

const StoryTimeline: React.FC<{ id: string }> = ({ id }) => {
  const { data, error } = useSWR<Story>(`/api/aihot/story/${id}`, fetcher, { revalidateOnFocus: false });
  if (error) return <div style={{ fontSize: 12, color: 'var(--error)' }}>时间线拉取失败：{errText(error)}</div>;
  if (!data) return <div style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>正在拉取事件时间线…</div>;
  const s = data.story;
  return (
    <div style={{ marginTop: 8 }}>
      {s.digest && <div style={{ fontSize: 13, lineHeight: 1.8, marginBottom: 8 }}>{s.digest}</div>}
      <div style={{ overflowX: 'auto' }}>
        <table className="table">
          <thead><tr><th>时间</th><th>报道</th><th>来源</th></tr></thead>
          <tbody>
            {s.timeline.map((t, i) => (
              <tr key={i}>
                <td className="mono" style={{ whiteSpace: 'nowrap', fontSize: 12 }}>{t.at}</td>
                <td>{t.url ? <a href={t.url} target="_blank" rel="noreferrer">{t.title}</a> : t.title}</td>
                <td style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>{t.source}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
};

const NewsTab: React.FC = () => {
  const nav = useNavigate();
  const [win, setWin] = useState('24h');
  const [mode, setMode] = useState('selected');
  const [cat, setCat] = useState('');
  const [q, setQ] = useState('');
  const [qLive, setQLive] = useState('');
  const [limit, setLimit] = useState(40);
  const [openStory, setOpenStory] = useState('');
  const [showDaily, setShowDaily] = useState(false);
  const [bust, setBust] = useState(0);

  const key = `/api/aihot/news?window=${win}&mode=${mode}&category=${cat}`
    + `&q=${encodeURIComponent(q)}&limit=${limit}` + (bust ? `&refresh=true&_=${bust}` : '');
  const { data, error, isLoading, mutate } = useSWR<News>(key, fetcher, { revalidateOnFocus: false });
  const { data: daily } = useSWR<Daily>(showDaily ? '/api/aihot/daily' : null, fetcher);

  if (error) return <ErrCard error={error} what="AI 新闻" onRetry={() => mutate()} />;
  if (isLoading || !data) return <Loading what="AI 资讯" />;

  return (
    <>
      <Provenance sourceUrl={data.source_url} fetchedAt={data.fetched_at} cached={data.cached}
                  ageS={data.cache_age_s} policy={data.policy} accent={NEWS_ACCENT}
                  refreshing={false} onRefresh={() => { setBust(Date.now()); mutate(); }}>
        <span style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
          {win === '7d' ? '近 7 天' : '近 24 小时'} · {mode === 'all' ? '全部公开动态' : 'AIHOT 精选'} · {data.items.length} 条
        </span>
      </Provenance>

      {/* 当前热点榜 */}
      {data.hot_topics.length > 0 && (
        <div className="card" style={{ marginBottom: 'var(--space-4)' }}>
          <h3 style={{ marginTop: 0, fontSize: 15 }}>
            当前热点<span style={{ fontWeight: 400, color: 'var(--text-tertiary)', fontSize: 13 }}> · AIHOT 口径,点开看事件时间线</span>
          </h3>
          <div style={{ display: 'grid', gap: 8 }}>
            {data.hot_topics.map(h => (
              <div key={h.rank}>
                <div style={{ display: 'flex', gap: 10, alignItems: 'baseline', flexWrap: 'wrap' }}>
                  <span className="badge badge-warning">第 {h.rank}</span>
                  <span style={{ fontWeight: 600 }}>{h.title}</span>
                  <span style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>
                    {h.source_count ?? '?'} 个来源 / {h.signal_count ?? '?'} 条报道 · {h.latest_zh}
                  </span>
                  {h.story_id && (
                    <button className="btn btn-tonal btn-sm"
                            onClick={() => setOpenStory(s => (s === h.story_id ? '' : h.story_id))}>
                      {openStory === h.story_id ? '收起' : '时间线'}
                    </button>
                  )}
                  {h.url && <a href={h.url} target="_blank" rel="noreferrer" style={{ fontSize: 12 }}>原文</a>}
                </div>
                {openStory === h.story_id && <StoryTimeline id={h.story_id} />}
              </div>
            ))}
          </div>
        </div>
      )}
      {data.hot_error && (
        <div className="card" style={{ marginBottom: 'var(--space-4)', fontSize: 12, color: 'var(--text-tertiary)' }}>
          热点榜本次未取到（{data.hot_error}）——下面的资讯列表不受影响。
        </div>
      )}

      {/* 筛选 + 列表 */}
      <div className="card">
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
          {[{ id: '24h', label: '近 24 小时' }, { id: '7d', label: '近 7 天' }].map(w => (
            <button key={w.id} className={`btn btn-sm ${win === w.id ? 'btn-primary' : 'btn-tonal'}`}
                    onClick={() => setWin(w.id)}>{w.label}</button>
          ))}
          <span style={{ width: 1, height: 20, background: 'var(--border-default)' }} />
          {[{ id: 'selected', label: '精选' }, { id: 'all', label: '全部' }].map(m => (
            <button key={m.id} className={`btn btn-sm ${mode === m.id ? 'btn-primary' : 'btn-tonal'}`}
                    onClick={() => setMode(m.id)}
                    title={m.id === 'selected' ? '站点已按分数筛过，信噪比高' : '未筛的完整公开池'}>
              {m.label}
            </button>
          ))}
          <span style={{ width: 1, height: 20, background: 'var(--border-default)' }} />
          <button className={`btn btn-sm ${cat === '' ? 'btn-primary' : 'btn-tonal'}`}
                  onClick={() => setCat('')}>全部分类</button>
          {data.categories.map(c => (
            <button key={c.id} className={`btn btn-sm ${cat === c.id ? 'btn-primary' : 'btn-tonal'}`}
                    onClick={() => setCat(c.id)}>{c.label}</button>
          ))}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
          <input className="input" style={{ width: 240, height: 30, padding: '2px 8px' }}
                 placeholder="盯题关键词（≥2 字，回车搜索）" value={qLive}
                 onChange={e => setQLive(e.target.value)}
                 onKeyDown={e => { if (e.key === 'Enter') setQ(qLive.trim().length >= 2 ? qLive.trim() : ''); }} />
          {q && <span className="badge badge-info">盯题「{q}」
            <span style={{ cursor: 'pointer', marginLeft: 4 }} onClick={() => { setQ(''); setQLive(''); }}>✕</span>
          </span>}
          <select className="input" style={{ width: 110, height: 30, padding: '2px 8px' }}
                  value={limit} onChange={e => setLimit(parseInt(e.target.value))}>
            {[20, 40, 60, 100].map(n => <option key={n} value={n}>{n} 条</option>)}
          </select>
          <button className={`btn btn-sm ${showDaily ? 'btn-primary' : 'btn-tonal'}`}
                  onClick={() => setShowDaily(v => !v)}
                  title="站点每天 08:00（北京时间）发布的精编日报，按分栏原样展示">
            <Icon name="calendar" size={13} /> 当日日报
          </button>
          <div style={{ flex: 1 }} />
          <button className="btn btn-tonal btn-sm" onClick={() => nav('/task/aihot-news')}
                  title="在这批资讯上再叠一层 AI：编成带来源回链的内部简报（约 1.5 分钟）">
            <Icon name="sparkle" size={13} /> 让 AI 编简报
          </button>
        </div>

        {/* 当日日报（按需展开） */}
        {showDaily && (
          <div style={{ border: '1px solid var(--border-subtle)', borderRadius: 10, padding: 14, marginBottom: 14 }}>
            {!daily ? <span style={{ fontSize: 13, color: 'var(--text-tertiary)' }}>正在拉取当日日报…</span> : (
              <>
                <div style={{ fontWeight: 600, marginBottom: 8 }}>
                  AIHOT 日报 {daily.date}
                  {daily.url && <> · <a href={daily.url} target="_blank" rel="noreferrer" style={{ fontSize: 13 }}>站内页</a></>}
                </div>
                {daily.lead && <p style={{ fontSize: 13, lineHeight: 1.8 }}>{daily.lead}</p>}
                {daily.sections.map((s, i) => (
                  <div key={i} style={{ marginBottom: 10 }}>
                    <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-secondary)' }}>{s.label}</div>
                    <ul style={{ margin: '4px 0 0', paddingLeft: 20, fontSize: 13, lineHeight: 1.8 }}>
                      {s.items.map((it, k) => (
                        <li key={k}>
                          {it.aihot_url ? <a href={it.aihot_url} target="_blank" rel="noreferrer">{it.title}</a> : it.title}
                          {it.summary && <div style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>{it.summary}</div>}
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
              </>
            )}
          </div>
        )}

        {data.items.length === 0 ? (
          <div style={{ color: 'var(--text-tertiary)', fontSize: 13, padding: '12px 0' }}>
            这个窗口没有资讯。试试把窗口改成「近 7 天」、内容池改成「全部」，或清掉盯题关键词。
          </div>
        ) : (
          <div style={{ display: 'grid', gap: 14 }}>
            {data.items.map((it, i) => (
              <div key={it.id || i} style={{
                borderLeft: '3px solid var(--border-default)', paddingLeft: 12,
              }}>
                <div style={{ display: 'flex', gap: 8, alignItems: 'baseline', flexWrap: 'wrap' }}>
                  {it.hot_rank && <span className="badge badge-warning">热 {it.hot_rank}</span>}
                  <span style={{ fontWeight: 600, fontSize: 14 }}>
                    {it.aihot_url ? <a href={it.aihot_url} target="_blank" rel="noreferrer">{it.title}</a> : it.title}
                  </span>
                  <span style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>
                    {it.category_zh} · {it.source_name || '来源未标注'} · {it.published_zh}
                    {it.score !== null && it.score !== undefined && <> · 站点分 {it.score}</>}
                  </span>
                  {it.url && <a href={it.url} target="_blank" rel="noreferrer" style={{ fontSize: 12 }}>原文</a>}
                </div>
                {it.summary && (
                  <div style={{ fontSize: 13, color: 'var(--text-secondary)', lineHeight: 1.7, marginTop: 3 }}>
                    {it.summary}
                  </div>
                )}
                {it.reason && (
                  <div style={{ fontSize: 12, color: 'var(--text-tertiary)', lineHeight: 1.7, marginTop: 3 }}>
                    <strong>站点点评：</strong>{it.reason}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </>
  );
};

// ── 页面 ────────────────────────────────────────────────────────────

const AihotPage: React.FC = () => {
  // 标签直接由 URL 的 ?tab= 决定（不另存 state）：侧栏「AIHot 模型榜」和
  // 「AIHot 新闻简报」是同一个页面的两个入口，各自 nav 到 ?tab=board / ?tab=news。
  // 若用 useState 存初值，在页面已挂载时点另一个入口就不会切换标签。
  const [searchParams, setSearchParams] = useSearchParams();
  const tab: 'board' | 'news' = searchParams.get('tab') === 'news' ? 'news' : 'board';
  const setTab = (t: 'board' | 'news') => setSearchParams({ tab: t }, { replace: true });
  return (
    <div style={{ height: '100%', overflowY: 'auto', padding: 'var(--space-6)' }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 16, marginBottom: 'var(--space-4)', flexWrap: 'wrap' }}>
        <h2 style={{ margin: 0, fontSize: 20 }}>AIHot 内容和模型</h2>
        <div style={{ display: 'flex', gap: 8 }}>
          <button className={`btn btn-sm ${tab === 'board' ? 'btn-primary' : 'btn-tonal'}`}
                  onClick={() => setTab('board')}>
            <Icon name="graph" size={13} /> 模型榜
          </button>
          <button className={`btn btn-sm ${tab === 'news' ? 'btn-primary' : 'btn-tonal'}`}
                  onClick={() => setTab('news')}>
            <Icon name="cloud" size={13} /> AI 新闻
          </button>
        </div>
        <span style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>
          直连 AIHOT 公开数据,打开即看,不经过大模型
        </span>
      </div>
      {tab === 'board' ? <LeaderboardTab /> : <NewsTab />}
    </div>
  );
};

export default AihotPage;
