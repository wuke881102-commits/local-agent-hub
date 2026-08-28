import React, { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import useSWR from 'swr';
import { Icon } from '../components/icons';
import { fetcher, api, errMsg } from '../api';
import { useToast } from '../components/Toast';

/**
 * 本地邮箱 —— 三个视图：智能看板 / 信息矩阵 / 时间图谱。
 *
 * ## 为什么不是「一个收件箱列表」
 *
 * 早先的版本按「什么到了」组织，那正是 Outlook 已经做的事（时间排序、未读数、
 * Focused Inbox、标记、搜索）。同一根轴上再做一遍，只能得到一个更慢、字段更少、
 * 还不能操作的 Outlook —— 用户没有理由打开它。
 *
 * 三个视图各回答一个 Outlook 回答不了的问题：
 *   智能看板  这些邮件分别是件什么事？（14 类，按「要你做什么动作」切分，点格子筛矩阵）
 *   信息矩阵  这段时间的邮件里，金额、单号、期限、项目分别是什么？（唯一的列表）
 *   时间图谱  这件事是怎么走到今天的？谁让它停了几天？（确定性时间轴 + 参与者共现网）
 *
 * 看板不再单独渲染一遍会话卡片：卡片和矩阵行是同一批数据，同页展示就是重复。
 * 看板只管筛选，矩阵是唯一的列表 —— 这也是「文档地图」的逻辑（分面下钻到列表）。
 *
 * ## 面板 / Outlook 的分工
 *
 * 面板负责让你**知道**；**回复留在 Outlook 里做**。所以每条只有一个动作：
 * 「去 Outlook 回复」。这里不写草稿、不预填、不发送。
 *
 * ## 哪些结论来自云端模型
 *
 * 「类型」里标 ·AI 的那些值，以及矩阵里的 项目 / 事项 / 待定结论，由云端模型判定
 * （用户明确选择了全量分析）。会议 / 告警 / 工单 / 广告是本机硬信号判的，其余全部本机算。
 * 界面上必须能区分这两者 —— 页脚的「本次外发」如实报告发了多少内容出去，
 * 展开「数据从哪来」能看到每一项结论的出处。
 */

type Deadline = { date: string; days_left: number | null; trigger: string; text: string };
/** 只剩「类型」一个维度了。为什么砍掉责任/紧急/重要，见 outlook_tags.DIMENSIONS 的头注。 */
type Tags = { kind: string; kind_from: string };
type AiFields = { kind: string; project: string; matter: string; decision: string };
type Thread = {
  conv_id: string; open_id: string; subject: string; people: string[];
  msg_count: number; chase_count: number; ask: string;
  waiting: boolean; waiting_days: number; waiting_label: string;
  last_received_label: string; replied_before: boolean; replied_days_ago: number | null;
  att_count: number; high_importance: boolean; flagged: boolean;
  is_meeting: boolean; urgent_word: string;
  to_me: boolean | null; cc_me: boolean | null;
  deadline: Deadline | null; section: string; quiet_kind?: string; why: string;
  tags: Tags; ai?: AiFields; promise?: { to: string; text: string; age_days: number };
};
type DimValue = { id: string; label: string; tone: string; ai?: boolean };
type Dimension = { id: string; label: string; hint: string; values: DimValue[] };
/** 一个分面格子的内容：数量 + 几条样本主题（不点进去也能看出桶里是什么）。 */
type Bucket = { count: number; samples: string[] };
type Field = { id: string; label: string; kind: string; width: number };
/**
 * 被挡在「信息矩阵 / 时间图谱」之外的会话数，按原因分开。
 * group = 群组邮件（我的地址既不在收件人也不在抄送里）
 * kind  = 噪音类型（广告 / 订阅资讯 / 工单状态，由 OUTLOOK_SKIP_KINDS 配置）
 * 两个原因必须分开显示：混成一个数字，用户没法判断规则合不合他的意。
 */
type KindTally = { kept: number; group: number; kind: number };
type Excluded = { group: number; kind: number; total: number; skip_kinds: string[];
  // 按类型细分。用于「点了某个类型但矩阵是空的」时说准原因 ——
  // 只报全局总数的话，数字和你刚点的那个格子对不上，比不解释更令人迷惑。
  by_kind?: Record<string, KindTally> };
/** 挂在主题格里的行内标记（金额 / 单号 / 附件 / 外部）。稀疏字段不占列，见后端注释。 */
type Badge = { id: string; label: string; value: string };
type Row = {
  conv_id: string; open_id: string; cells: Record<string, string>;
  badges: Badge[]; att_names: string;
};
type Step = {
  ts: number; at: string; dir: 'in' | 'out'; actor: string; subject: string;
  ask: string; att_count: number; summary: string; gap_days: number; gap_hours: number;
};
type Node_ = { name: string; sent: number; in_to: number; in_cc: number;
  // 最后发言时间与派生角色（后端算好）。共现边已移除，理由见 Participants。
  last_at: string; role: string };
type Graph = {
  conv_id: string; subject: string; steps: Step[]; step_count: number;
  span_days: number; nodes: Node_[];
  last_actor: string; last_dir: string;
  recipients_truncated: boolean; my_replies: number;
};
type Project = {
  conv_id: string; subject: string; people: string[]; msg_count: number;
  last_received_label: string; waiting_label: string;
};
type Summary = {
  due: number; overdue: number; waiting_threads: number; waiting_people: number;
  waiting_max_days: number; promises: number; quiet: number; threads: number; clear: boolean;
};
type AiStat = {
  sent: number; cached: number; batches: number; failed: number; mock: boolean;
  model: string; chars_sent: number; skipped?: string; demo?: boolean;
};
type Diag = {
  scanned: number; stopped_by: string; time_budget_s: number;
  sent_scanned: number; sent_stopped_by: string; sent_conversations: number;
  promise_scan: boolean; promise_bodies_read: number; promises_found: number;
  dropped_duplicates: number; skipped_non_mail: number;
  sender_addr_source: Record<string, number>;
  to_me_source: Record<string, number>;
};
type Inbox = {
  folder: string; elapsed_ms: number; total: number;
  view: { summary: Summary };
  board: { dimensions: Dimension[]; counts: Record<string, Record<string, Bucket>>; threads: Thread[] };
  matrix: { fields: Field[]; rows: Row[]; excluded: Excluded };
  graph: { projects: Project[]; graphs: Record<string, Graph>; excluded: Excluded };
  ai: AiStat;
  partial: boolean; cached_store: boolean | null; sender_skipped: boolean;
  // 真正去读 Outlook 的时刻（随 5 分钟内存缓存一起保留）。
  // cached=true 时它仍是「上次真取数」的时间，不是本次请求的时间。
  fetched_at?: string; cached?: boolean; cache_age_s?: number;
  diagnostics: Diag;
  /** 后端标的「这是编造的假数据」。为真则页面必须挂横幅说清楚。 */
  demo?: boolean;
};
type Probe = { ok: boolean; error?: string;
  // 这三个字段让页面能在**取数之前**判断「这次大概率会超时」。
  // probe 不读地址信息，所以在会挂死的机器上它依然能秒回。
  inbox_items?: number; cached?: boolean | null; store_name?: string };
type Store = { index: number; display_name: string; cached: boolean };

/** api.ts 的 handle 抛的是 `502 {"detail":"…"}`，得把 JSON 里的 detail 抠出来。 */
function errText(e: any): string {
  const raw = (e instanceof Error ? e.message : String(e ?? '')).trim();
  const brace = raw.indexOf('{');
  if (brace >= 0) {
    try {
      const j = JSON.parse(raw.slice(brace));
      if (typeof j?.detail === 'string') return j.detail;
    } catch { /* 不是 JSON 就原样显示 */ }
  }
  return raw || '未知错误';
}

const RED = '#C83A3A';
const AMBER = '#B45309';
const BLUE = '#0F6CBD';
const TONE: Record<string, string> = { hot: RED, warm: AMBER, cool: '#64748B' };

/**
 * 「未分类」在 URL 里的哨兵值。
 *
 * 它的标签值本身是空字符串，而 URL 参数缺失与参数为空不可区分 —— 不用哨兵的话，
 * 「未分类」这个格子永远选不中也筛不动。选 '-' 是因为它不可能是真实的标签 id，
 * 而且在地址栏里看得懂（?kind=-）。
 */
const NONE_TOKEN = '-';
const toToken = (id: string) => (id === '' ? NONE_TOKEN : id);
const fromToken = (v: string) => (v === NONE_TOKEN ? '' : v);

/** ISO → 「08-25 14:30」。同天只给时分，否则带日期 —— “今天 14:30 刷的”和
 *  “前天 14:30 刷的”是完全不同的信息，不能只显示时分。 */
function fmtStamp(iso: string): string {
  if (!iso || iso.length < 16) return '';
  const d = iso.slice(5, 10), t = iso.slice(11, 16);
  const today = new Date();
  const mm = String(today.getMonth() + 1).padStart(2, '0');
  const dd = String(today.getDate()).padStart(2, '0');
  return d === `${mm}-${dd}` ? t : `${d} ${t}`;
}

/** 分节标题。三个功能同页展示，所以每块要有明确的分界和一句话说明它答什么问题。 */
const SectionHead: React.FC<{ icon: string; title: string; hint: string; right?: React.ReactNode }> =
  ({ icon, title, hint, right }) => (
    <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap',
                  marginTop: 6 }}>
      <span style={{ fontSize: 15, fontWeight: 700, display: 'inline-flex',
                     alignItems: 'center', gap: 6 }}>
        <Icon name={icon as any} size={15} /> {title}
      </span>
      <span style={{ fontSize: 11.5, color: 'var(--text-tertiary)' }}>{hint}</span>
      {right && <span style={{ marginLeft: 'auto' }}>{right}</span>}
    </div>
  );

/**
 * 分面格子 —— 照「文档地图」那一套（.facet-tile，见 tokens.css）。
 *
 * 左边名称、右边大号计数、下面列 2~3 条样本主题，整格可点下钻。
 * 关键是那几条样本：只有数字的话，「业务类 4」除了数字什么都没告诉你，
 * 用户得靠点进去试。列出样本就不用点。
 */
const FacetTile: React.FC<{
  label: string; bucket: Bucket; tone: string; ai?: boolean;
  active: boolean; onClick: () => void;
}> = ({ label, bucket, tone, ai, active, onClick }) => (
  <div className="facet-tile" onClick={onClick}
       title={active ? '点一下取消这个筛选' : `只看「${label}」的 ${bucket.count} 个会话`}
       style={{
         padding: 11, borderRadius: 6, background: active ? 'var(--tint-brand)' : 'var(--surface-subtle)',
         borderLeft: `3px solid ${tone}`,
         outline: active ? '1px solid var(--brand-500)' : '1px solid transparent',
       }}>
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 8 }}>
      <strong style={{ fontSize: 13 }}>
        {label}
        {ai && <span style={{ color: BLUE, fontWeight: 400, fontSize: 11 }}> ·AI</span>}
      </strong>
      <span className="facet-count" style={{ fontSize: 20, fontWeight: 600, color: tone }}>
        {bucket.count}
      </span>
    </div>
    {bucket.samples?.length > 0 && (
      <ul style={{ margin: '6px 0 0', paddingLeft: 15, fontSize: 11.5,
                   color: 'var(--text-tertiary)', lineHeight: 1.7 }}>
        {bucket.samples.map((s, i) => (
          <li key={i} style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {s}
          </li>
        ))}
      </ul>
    )}
  </div>
);

/**
 * 「少了几行、为什么少」的说明。矩阵和图谱标题右侧共用。
 *
 * 这块不能省：看板上「广告推广 12」点下去矩阵是空的，用户只会觉得筛选是坏的。
 * 两个原因分开写 —— 混成一个总数，用户没法判断这规则合不合他的意。
 */
const ExcludedNote: React.FC<{ ex?: Excluded }> = ({ ex }) => {
  if (!ex || !ex.total) return null;
  const parts: string[] = [];
  if (ex.group) parts.push(`${ex.group} 个群组邮件`);
  if (ex.kind) parts.push(`${ex.kind} 个噪音类型`);
  return (
    <span style={{ fontSize: 11.5, color: 'var(--text-tertiary)' }}
          title={['群组邮件 = 我的地址既不在收件人也不在抄送里（通讯组或规则投递）。',
                  '噪音类型 = ' + ex.skip_kinds.join(' / ') + '（可用 OUTLOOK_SKIP_KINDS 调整）。',
                  '这些会话在智能看板里照常能看到，只是不占工作视图。'].join('\n')}>
      已挡掉 {parts.join(' + ')}（看板里仍可见）
    </span>
  );
};

const ErrCard: React.FC<{ error: any; onRetry?: () => void }> = ({ error, onRetry }) => (
  <div className="card" style={{ borderLeft: '3px solid var(--error)' }}>
    <div style={{ fontWeight: 600, marginBottom: 6 }}>读取本地邮箱失败</div>
    <div style={{ fontSize: 13, color: 'var(--text-secondary)', lineHeight: 1.7 }}>{errText(error)}</div>
    {onRetry && (
      <button className="btn btn-tonal btn-sm" style={{ marginTop: 10 }} onClick={onRetry}>
        <Icon name="refresh" size={13} /> 重试
      </button>
    )}
  </div>
);

/** 顶部那一句。只读这一句就关掉标签页，这个页面也算成立了。 */
const Headline: React.FC<{ s: Summary; range: string }> = ({ s, range }) => {
  if (s.clear) {
    return (
      <div className="card" style={{ borderLeft: `3px solid ${BLUE}` }}>
        <div style={{ fontSize: 16, fontWeight: 600 }}>没有欠着的事。</div>
        <div style={{ fontSize: 12.5, color: 'var(--text-secondary)', marginTop: 5 }}>
          {range}里没有人在等你回话，没有到期的事，也没有你答应过还没做的。
          剩下 {s.quiet} 个会话可以不看。
        </div>
      </div>
    );
  }
  const parts: React.ReactNode[] = [];
  if (s.overdue > 0) parts.push(<b key="o" style={{ color: RED }}>{s.overdue} 件已逾期</b>);
  if (s.due - s.overdue > 0) parts.push(<b key="d" style={{ color: RED }}>{s.due - s.overdue} 件即将到期</b>);
  if (s.waiting_threads > 0) {
    parts.push(
      <span key="w">
        <b style={{ color: AMBER }}>{s.waiting_people} 个人在等你回话</b>
        {s.waiting_max_days > 0 && (
          <span style={{ color: 'var(--text-secondary)' }}>（最久的等了 {s.waiting_max_days} 天）</span>
        )}
      </span>,
    );
  }
  if (s.promises > 0) parts.push(<b key="p" style={{ color: BLUE }}>{s.promises} 件你答应过还没做</b>);
  return (
    <div className="card" style={{ borderLeft: `3px solid ${s.due > 0 ? RED : AMBER}` }}>
      <div style={{ fontSize: 15.5, lineHeight: 1.75 }}>
        {parts.map((p, i) => (
          <React.Fragment key={i}>
            {i > 0 && <span style={{ color: 'var(--text-tertiary)' }}> · </span>}{p}
          </React.Fragment>
        ))}。
      </div>
      <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginTop: 5 }}>
        {range} · 其余 {s.quiet} 个会话可以不看 · 要回复请到矩阵里点「打开」回 Outlook，本页面不发送任何邮件
      </div>
    </div>
  );
};

/**
 * 「收件方式」格。五档：只发我 / 直发我和其他人 / 抄送我 / 不确定 / 我发出。
 *
 * 「群组邮件」不会出现在这里 —— 那一档整行被排除出矩阵（它是通讯组/规则投递进来的，
 * 不是发给我的）。但它照常留在智能看板里，看板要给全貌。
 *
 * 「不确定」用灰色显示而不是藏起来：实测约 1/3 的邮件读不到 PR_MESSAGE_TO_ME。
 * 把读不到当成「不是发给我的」会静默删掉真需要回的邮件。难看好过漏掉。
 */
const ADDR_TONE: Record<string, string> = {
  只发我: RED, 直发我和其他人: AMBER, 抄送我: '#64748B',
  群组邮件: '#94A3B8', 不确定: '#94A3B8', 我发出: BLUE,
};
const ADDR_TIP: Record<string, string> = {
  只发我: '收件人栏里只有我一个 —— 这封百分百是要我处理的',
  直发我和其他人: '我在收件人栏，但不止我一个（按最新那封算）',
  抄送我: '我只在抄送里，知会性质',
  不确定: '这封读不到「我是否在收件人栏」的 MAPI 属性，没有硬判。' +
    '实测约 1/3 的邮件读不到 —— 所以它照常留在矩阵里，不会被当成群组邮件删掉',
  我发出: '这一行来自我自己的已发送邮件（你答应过的事）',
};
const Addressing: React.FC<{ v: string }> = ({ v }) => {
  if (!v) return <span style={{ color: 'var(--text-tertiary)' }}>—</span>;
  return (
    <span style={{ color: ADDR_TONE[v] || 'inherit', fontWeight: v === '只发我' ? 600 : 400 }}
          title={ADDR_TIP[v] || ''}>
      {v}
    </span>
  );
};

/**
 * 主题格：主题 + 行内徽标（金额 / 单号 / 附件 / 外部）。
 *
 * 这几个字段原来各占一列，实测整列都是「—」—— 一列在多数行里是空的，就是在拿
 * 横向滚动换白纸。它们的值都很短，挂在主题后面有就显示、没有不占位。
 */
const SubjectCell: React.FC<{ row: Row }> = ({ row }) => (
  <div>
    <div style={{ overflowWrap: 'anywhere' }}>{row.cells.subject || '(无主题)'}</div>
    {row.badges.length > 0 && (
      <div style={{ marginTop: 3, display: 'flex', gap: 4, flexWrap: 'wrap' }}>
        {row.badges.map((b) => (
          <span key={b.id} className="badge" style={{ fontSize: 10.5 }}
                title={b.id === 'atts' ? row.att_names : `${b.label}：${b.value}`}>
            {b.id === 'org' ? b.value : `${b.label} ${b.value}`}
          </span>
        ))}
      </div>
    )}
  </div>
);

/** 事项格：AI 的一句话 + 待拍板（红色第二行）。两者讲同一件事，不分两列。 */
const MatterCell: React.FC<{ row: Row }> = ({ row }) => {
  const m = row.cells.matter, d = row.cells.decision;
  if (!m && !d) return <span style={{ color: 'var(--text-tertiary)' }}>—</span>;
  return (
    <div>
      {m && <div style={{ overflowWrap: 'anywhere' }}>{m}</div>}
      {d && <div style={{ color: RED, marginTop: 3 }}>待拍板：{d}</div>}
    </div>
  );
};

/**
 * 「在 Outlook 里打开」按钮。
 *
 * 现在这是**唯一**的打开入口（分面格子只筛选，矩阵是唯一的列表），所以它必须有
 * 忙碌态和错误反馈：在被 Object Model Guard 卡住的邮箱上这个调用会超时并返回一句
 * 人话错误，静默吞掉的话用户点了没反应、也不知道为什么。
 */
const OpenButton: React.FC<{ entryId: string; demo: boolean }> = ({ entryId, demo }) => {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  if (!entryId) {
    // 「我答应过」那类来自我自己的已发送邮件，没有可打开的收件。
    return <span style={{ color: 'var(--text-tertiary)', fontSize: 11 }}>—</span>;
  }
  const open = async () => {
    setBusy(true); setErr('');
    try {
      await api.post('/api/outlook/open', { entry_id: entryId });
    } catch (e) { setErr(errText(e)); } finally { setBusy(false); }
  };
  return (
    <>
      <button className="btn btn-tonal btn-sm" onClick={open} disabled={busy || demo}
              title={demo ? '演示数据里的假邮件，Outlook 里不存在这一封'
                : '在 Outlook 里打开这封，去回复'}>
        <Icon name="external" size={12} /> {busy ? '打开中' : '打开'}
      </button>
      {err && (
        <div style={{ fontSize: 11, color: 'var(--error)', marginTop: 4, maxWidth: 200 }}>{err}</div>
      )}
    </>
  );
};


/** 参与者清单。
 *
 * 这里曾经是一张共现关系网（节点排在圆周上、边宽按共现次数）。拆了，因为一个
 * 会话里大家基本都在每封邮件的收件人栁里 —— 每个人跟每个人都有边，画出来是
 * 一张**完全图**，而完全图不区分任何两个人，信息量是零。名字又被挤成缩写，
 * 谁是谁都看不出来。那不是布局问题，是这个指标本身在这个场景下没区分度。
 *
 * 换成四个能直接做决定的事实：球在谁那儿、谁在推、谁收了信一直没回、谁只是旁观。
 */
const Participants: React.FC<{ g: Graph }> = ({ g }) => {
  const ROLE_TONE: Record<string, string> = {
    '主要推动': AMBER, '参与讨论': AMBER,
    // 键必须和后端 outlook_graph.py 里 p["role"] 的字面量完全一致，否则查表落空。
    '收件但未发言': BLUE, '只被抄送': 'var(--text-tertiary)',
  };
  return (
    <div>
      {/* 最有用的单一事实放最上面：球在谁那儿。 */}
      {g.last_actor && (
        <div style={{ fontSize: 12.5, marginBottom: 10, lineHeight: 1.6 }}>
          {g.last_dir === 'in'
            ? <><b>{g.last_actor}</b> 说了最后一句 · <b style={{ color: 'var(--error)' }}>轮到你</b></>
            : <>最后一句是<b>我</b>说的 · 在等对方</>}
        </div>
      )}
      {/* 每行每列都有分界线，且**所有单元格不折行** —— 一旦名字折成两行，
          同一行的四个格子对不齐，表格就不再是表格。宁可横向滚动（外层 overflowX）。 */}
      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', fontSize: 12, borderCollapse: 'collapse',
                        border: '1px solid var(--border-subtle)' }}>
          <tbody>
            {g.nodes.map((n) => (
              <tr key={n.name}>
                <td style={{ padding: '6px 8px', whiteSpace: 'nowrap',
                             border: '1px solid var(--border-subtle)' }}>{n.name}</td>
                <td style={{ padding: '6px 8px', whiteSpace: 'nowrap', color: 'var(--text-tertiary)',
                             border: '1px solid var(--border-subtle)' }}>
                  {n.sent > 0 ? `发言 ${n.sent}` : '—'}
                </td>
                <td style={{ padding: '6px 8px', whiteSpace: 'nowrap', color: 'var(--text-tertiary)',
                             border: '1px solid var(--border-subtle)' }}>
                  {n.last_at || '—'}
                </td>
                <td style={{ padding: '6px 8px', whiteSpace: 'nowrap',
                             border: '1px solid var(--border-subtle)',
                             color: ROLE_TONE[n.role] || 'var(--text-tertiary)' }}>{n.role}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-tertiary)', lineHeight: 1.7, marginTop: 8 }}>
        「发言」= 在这个会话里发过几封。「收件但未发言」= 被直接收件却一次没回（可能大家在等他）。
        {g.recipients_truncated && ' 收件人过多，已截断到前 12 人。'}
      </div>
    </div>
  );
};

const Timeline: React.FC<{ g: Graph }> = ({ g }) => (
  <div>
    <div style={{ fontSize: 12.5, color: 'var(--text-secondary)', marginBottom: 8 }}>
      跨 {g.span_days} 天 · {g.step_count} 步 ·{' '}
      {g.my_replies > 0 ? `我回过 ${g.my_replies} 次` : <b style={{ color: RED }}>我一次都没回</b>}
    </div>
    <div style={{ display: 'flex', flexDirection: 'column' }}>
      {g.steps.map((s, i) => (
        <div key={i} style={{ display: 'flex', gap: 10 }}>
          {/* 竖线 + 圆点。间隔天数标在线上 —— 「谁让这件事停了 4 天」一眼能看出来。 */}
          <div style={{ width: 74, textAlign: 'right', fontSize: 11.5, paddingTop: 2,
                        color: 'var(--text-tertiary)', flexShrink: 0 }}>
            {s.at}
          </div>
          <div style={{ position: 'relative', width: 14, flexShrink: 0 }}>
            <div style={{
              position: 'absolute', left: 5, top: 0, bottom: 0, width: 2,
              background: 'var(--surface-inset)',
            }} />
            <div style={{
              position: 'absolute', left: 1, top: 5, width: 10, height: 10,
              borderRadius: 5, background: s.dir === 'out' ? BLUE : AMBER,
            }} />
          </div>
          <div style={{ flex: 1, paddingBottom: 14, minWidth: 0 }}>
            <div style={{ fontSize: 12.5 }}>
              <b style={{ color: s.dir === 'out' ? BLUE : 'inherit' }}>{s.actor}</b>
              {s.dir === 'out' && (
                <span style={{ color: 'var(--text-tertiary)' }}> 回了一次（内容未读取）</span>
              )}
              {s.gap_days >= 1 && (
                <span style={{ color: s.gap_days >= 3 ? RED : 'var(--text-tertiary)' }}>
                  {' '}· 距上一步 {s.gap_days} 天
                </span>
              )}
              {s.att_count > 0 && <span className="badge" style={{ marginLeft: 6 }}>{s.att_count} 附件</span>}
            </div>
            {(s.summary || s.ask || s.subject) && (
              <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 2,
                            lineHeight: 1.6, overflowWrap: 'anywhere' }}>
                {s.summary || s.ask || s.subject}
              </div>
            )}
          </div>
        </div>
      ))}
    </div>
  </div>
);

const OutlookPage: React.FC = () => {
  const [sp, setSp] = useSearchParams();
  const [showDiag, setShowDiag] = useState(false);

  const conv = sp.get('conv') || '';
  // 演示模式（?demo=1）：整页只认编造的假数据，**一次 COM 调用、一次模型调用都不发**。
  const demo = sp.get('demo') === '1';
  const [reloading, setReloading] = useState(false);
  const [switching, setSwitching] = useState('');
  const toast = useToast();

  // 回看范围。三个档，**都含今天**，单位是自然日：
  //   1 = 只看今天    3 = 今天和前 2 天    7 = 今天和前 6 天
  // 默认 7 —— 一个完整工作周，也是唯一一个不需要解释的默认值。
  // 默认**当天**，不是 7 天。真实收件箱一天就有几十个会话，7 天几百个 —— 打开就
  // 是一屏扫不完的东西，而且在线模式下取数很可能直接超时。当天最快也最可用，
  // 要看更久点上面的按钮即可（选择会写进 URL，刷新后保留）。
  const RANGE_DAYS = Math.max(1, Math.min(30, parseInt(sp.get('days') || '1', 10) || 1));
  const rangeLabel = RANGE_DAYS === 1 ? '当天' : `近 ${RANGE_DAYS} 天`;
  const set = (k: string, v: string) => {
    const next = new URLSearchParams(sp);
    if (v) next.set(k, v); else next.delete(k);
    setSp(next, { replace: true });
  };

  // probe / stores 在演示模式下必须传 null —— 它们自己会去碰 COM，一旦挂住整页又回到转圈。
  const { data: probe, mutate: mutateProbe } = useSWR<Probe>(
    demo ? null : '/api/outlook/probe', fetcher, { revalidateOnFocus: false });
  const { data: storeData, mutate: mutateStores } =
    useSWR<{ stores: Store[]; selected: string }>(
      !demo && probe?.ok ? '/api/outlook/stores' : null, fetcher, { revalidateOnFocus: false });

  // 「现在读的是哪个邮箱」以 **probe.store_name** 为准，不以 selected 为准：
  // 前者是后端真正打开的那个文件夹自己报出来的，后者只是我们的意愿。选中的邮箱
  // 如果已经不在 profile 里（共享邮箱权限被收回是常事），后端会回落到默认邮箱，
  // 这时两者不一致 —— 下面会把这个不一致显式说出来，而不是装作没事。
  // （旧代码用 stores[0]，那是在赌「第一个就是默认那个」。）
  const readingStore = probe?.store_name || storeData?.selected || '';
  const storeFellBack = !!(storeData?.selected && probe?.store_name
                           && storeData.selected !== probe.store_name);

  // 用**显式的起始日期**，不用 window_days。
  // window_days 在后端是 `now - N 天`，也就是一个滚动的 N×24 小时窗口 —— 早上九点
  // 点「近 3 天」，实际起点是三天前的早上九点，会把那天上午的邮件切掉一半。
  // 而 since=YYYY-MM-DD 在后端是解析到**当天零点**的，所以 N 天就是干净的 N 个
  // 自然日、且一定含今天。三个按钮的语义因此没有歧义。
  const sinceDay = (() => {
    const d = new Date();
    d.setDate(d.getDate() - (RANGE_DAYS - 1));
    const p = (n: number) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  })();
  const params = new URLSearchParams({ limit: '200', since: sinceDay });
  if (demo) params.set('demo', '1');
  const key = demo || probe?.ok ? `/api/outlook/inbox?${params}` : null;
  const { data, error, isLoading, mutate } = useSWR<Inbox>(key, fetcher, {
    revalidateOnFocus: false,
    // 在线模式下一次取数可能几十秒，不要因为自动重试把 Outlook 打爆。
    shouldRetryOnError: false,
  });

  // 筛选值放 URL 里：刷新不丢、能把某个视图贴给别人。
  // **维度列表由后端给**，不在这里硬编码 —— 早先写死了 duty/urgency/weight/kind
  // 四个键，后来后端删掉了前三个（顶部结论和矩阵的「状态」列已经说了同样的事，
  // 而「紧急程度」实测 87% 落在同一个桶里、「重要性」是弱信号凑的加权分）。
  // 一旦这里写死，书签里留着 ?duty=xxx 的 URL 就会拿它去比一个不存在的标签，
  // 把矩阵筛成空表。跟着后端走就不会脱节。
  const dims = data?.board.dimensions || [];
  // filters 存的是**URL 原值**：'' = 没有筛选，NONE_TOKEN = 筛「未分类」。
  //
  // 为什么要哨兵：「未分类」这个值的 id 就是空字符串（见 outlook_tags.DIMENSIONS），
  // 而 URL 里「没有这个参数」和「参数为空」是同一个东西。直接用 '' 的后果是三重的：
  // 未分类格子在没筛选时就显示为选中、点它等于清空筛选、`!v` 短路让过滤完全不生效
  // —— 表现就是「点了未分类，矩阵却把所有行都显示出来」。
  const filters: Record<string, string> = {};
  dims.forEach((d) => { filters[d.id] = sp.get(d.id) || ''; });
  const activeFilters = Object.entries(filters).filter(([, v]) => v);
  const clearFilters = () => {
    const next = new URLSearchParams(sp);
    dims.forEach((d) => next.delete(d.id));
    setSp(next, { replace: true });
  };

  /**
   * 真正去 Outlook 重读一遍。
   *
   * 必须带 `refresh=true`：后端有 5 分钟内存缓存，光调 mutate() 只会把缓存原样
   * 还回来 —— 那个按钮就在撒谎（这正是它之前的毛病）。
   *
   * 注意 AI 那一层**不会**因此重复外发：它按邮件内容哈希缓存，内容没变就不再发给
   * 模型。所以刷新的代价是重读 Outlook，不是重发一遍邮件内容。
   */
  const reload = async () => {
    if (!key) return;
    setReloading(true);
    try {
      const fresh = await fetcher<Inbox>(`${key}&refresh=true`);
      await mutate(fresh, { revalidate: false });
    } catch {
      // 失败让 SWR 走正常的错误路径，这里不额外弹东西
      await mutate();
    } finally {
      setReloading(false);
    }
  };

  /**
   * 换一个邮箱来读。
   *
   * 三份数据都要作废：stores（选中项）、probe（正在读哪个、多少封、缓存模式）、
   * 以及看板本身。只作废最后一份的话，页脚的邮箱名会跟着变、内容却还是上一个
   * 邮箱的 —— 那比不给切更容易让人做出错误判断。
   *
   * reload() 带 refresh=true，绕过后端那 5 分钟内存缓存。后端 set_store_choice
   * 也会清一次缓存，两道是刻意的：单靠前端刷新参数，别的入口（个人摘记）绕不过。
   */
  const switchStore = async (name: string) => {
    if (!name || name === readingStore || switching) return;
    setSwitching(name);
    try {
      await api.post('/api/outlook/store', { name });
      await Promise.all([mutateStores(), mutateProbe()]);
      await reload();
      toast.success(`已切换到 ${name}`);
    } catch (e) {
      toast.error('切换邮箱失败', { detail: errMsg(e) });
      await mutateStores();          // 把下拉拨回后端的真实状态，别停在没生效的选项上
    } finally {
      setSwitching('');
    }
  };

  const board = data?.board;
  // 分面格子筛的是**矩阵**，不再另外渲染一遍卡片。
  // 卡片和矩阵行本来就是同一批会话 —— 三个视图同页时那就是同样的东西显示两遍。
  // 这也正是「文档地图」的逻辑：格子下钻到列表，而列表只有一个。
  const tagsOf: Record<string, Tags> = {};
  (board?.threads || []).forEach((t) => { tagsOf[t.conv_id] = t.tags; });
  const rows = (data?.matrix.rows || []).filter((r) => {
    const tg = tagsOf[r.conv_id];
    if (!tg) return true;      // 拿不到标签就不藏它 —— 宁可多显示一行，不要静默丢数据
    // v 是 URL 原值，比较前要把哨兵换回真实标签值（NONE_TOKEN → ''）。
    return Object.entries(filters).every(([d, v]) => !v || (tg as any)[d] === fromToken(v));
  });
  const graphs = data?.graph?.graphs || {};
  const projects = data?.graph?.projects || [];
  const activeConv = conv || projects[0]?.conv_id || '';
  const g = graphs[activeConv];

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {/* ── 演示数据横幅：假数据和真数据长得一样，第一眼就必须看出是假的 ── */}
      {data?.demo && (
        <div className="card" style={{
          borderLeft: `3px solid ${AMBER}`, background: 'var(--surface-inset)',
          display: 'flex', gap: 12, alignItems: 'flex-start', flexWrap: 'wrap',
        }}>
          <div style={{ flex: 1, minWidth: 260 }}>
            <div style={{ fontWeight: 600, marginBottom: 4 }}>这是演示数据 —— 邮件全部是编造的</div>
            <div style={{ fontSize: 12.5, color: 'var(--text-secondary)', lineHeight: 1.7 }}>
              没读任何真实邮箱，<b>没发出一次 Outlook 调用，也没调用任何模型</b>。
              地址都是 <span className="mono">@example.com</span>（保留域名）。
              <br />
              分区、等待天数、诉求抽取、承诺抽取、时间轴、关系网<b>全是真算的</b>；
              只有「项目 / 事项 / 待拍板」和部分「类型」标签是我预先编好的
              —— 真实模式下它们来自云端模型。
            </div>
          </div>
          <button className="btn btn-tonal btn-sm" onClick={() => set('demo', '')}>
            退出演示，读真实邮箱
          </button>
        </div>
      )}

      {probe && !probe.ok && (
        <div className="card" style={{ borderLeft: '3px solid var(--error)' }}>
          <div style={{ fontWeight: 600, marginBottom: 6 }}>连不上本地邮箱</div>
          <div style={{ fontSize: 13, color: 'var(--text-secondary)', lineHeight: 1.7 }}>{probe.error}</div>
          <button className="btn btn-tonal btn-sm" style={{ marginTop: 10 }} onClick={() => set('demo', '1')}>
            先看演示数据
          </button>
        </div>
      )}

      {/* 取数前的风险提醒。为什么要有这一块：实测在「在线模式 + 数万封收件箱」
          的机器上，完整取数会转圈 45 秒然后给一句「COM 调用无法从外部取消」——
          那是实现细节，既没说原因也没给出路。probe 已经知道这两件事，提前说。 */}
      {!demo && probe?.ok && (probe.cached === false || (probe.inbox_items ?? 0) > 20000) && (
        <div className="card" style={{ borderLeft: '3px solid var(--warning)' }}>
          <div style={{ fontWeight: 600, marginBottom: 6 }}>这个邮箱取数可能会超时</div>
          <div style={{ fontSize: 13, color: 'var(--text-secondary)', lineHeight: 1.75 }}>
            {probe.store_name ? <>正在读 <b>{probe.store_name}</b>，</> : null}
            收件箱 <b className="tnum">{(probe.inbox_items ?? 0).toLocaleString()}</b> 封
            {probe.cached === false && <>，而且是<b>在线模式</b>（没有本地缓存）——
              每读一封都是一次网络往返，实测约 300ms/封</>}。
            <br />
            <b>根治：</b>在 Outlook 里开启「缓存的 Exchange 模式」（账户设置 → 双击账户）。
            <br />
            <b>临时：</b>先按下面的「当天」或「近 3 天」，要扯的邮件少很多。
          </div>
          <button className="btn btn-tonal btn-sm" style={{ marginTop: 10 }} onClick={() => set('demo', '1')}>
            或者先看演示数据
          </button>
        </div>
      )}

      {/* ── 读哪个邮箱 ───────────────────────────────────────────
          只有 ≥2 个邮箱时才出现：一个邮箱的下拉框是纯噪音。
          放在「看多久」之前，因为它决定的是数据从哪来，比时间范围更靠前。
          收件箱和已发送一起跟着切 —— 分开会让「我回过没有」的判断整个反掉。 */}
      {!demo && (storeData?.stores?.length ?? 0) > 1 && (
        <div className="card" style={{ padding: '9px 12px', display: 'flex', gap: 8,
                                       alignItems: 'center', flexWrap: 'wrap' }}>
          <span style={{ fontSize: 12, fontWeight: 600 }}>读哪个邮箱</span>
          <select className="input" style={{ width: 240, height: 30, padding: '2px 8px' }}
                  value={readingStore} disabled={!!switching}
                  onChange={(e) => switchStore(e.target.value)}>
            {storeData!.stores.map((st) => (
              <option key={st.index} value={st.display_name}>
                {st.display_name}{st.cached ? '' : '（在线模式）'}
              </option>
            ))}
          </select>
          <span style={{ fontSize: 11.5, color: 'var(--text-tertiary)' }}>
            {switching
              ? `正在切到 ${switching}，要重新去 Outlook 读一遍…`
              : '收件箱和已发送一起切 · 一次只读一个邮箱 · 选择会记住'}
          </span>
          {storeFellBack && (
            <span style={{ fontSize: 11.5, color: 'var(--warning)', width: '100%' }}>
              选过的「{storeData!.selected}」不在当前 Outlook profile 里（改过账户，
              或共享邮箱权限被收回），已回落到 <b>{probe!.store_name}</b>。
              重新选一个就能消掉这条。
            </span>
          )}
        </div>
      )}

      {/* ── 时间范围 + 刷新 ─────────────────────────────────────
          放在最上面：它决定了下面所有三块看到的是哪一段时间，先看到范围再看内容。
          三个档都**含今天**，单位是自然日（见 sinceDay 的注释：不用滚动窗口）。 */}
      <div className="card" style={{ padding: '9px 12px', display: 'flex', gap: 8,
                                     alignItems: 'center', flexWrap: 'wrap' }}>
        <span style={{ fontSize: 12, fontWeight: 600 }}>看多久</span>
        {[{ d: 1, label: '当天', tip: '只看今天' },
          { d: 3, label: '近 3 天', tip: '今天和前 2 天，共 3 个自然日' },
          { d: 7, label: '近 7 天', tip: '今天和前 6 天，共 7 个自然日' }].map((r) => (
          <button key={r.d} title={r.tip}
                  className={`btn btn-sm ${RANGE_DAYS === r.d ? 'btn-primary' : 'btn-tonal'}`}
                  onClick={() => set('days', r.d === 7 ? '' : String(r.d))}>
            {r.label}
          </button>
        ))}
        <span style={{ fontSize: 11.5, color: 'var(--text-tertiary)' }}>
          从 {sinceDay} 起（含今天）
        </span>
        {/* 上次刷新时间。放在按钮旁边而不是页脚：判断「这些数据新不新」是看到
            内容的第一个问题，得和刷新动作在同一处。
            cached 时额外标一句「缓存」—— 否则用户会以为刚刚重取过。 */}
        <span style={{ marginLeft: 'auto', fontSize: 11.5, color: 'var(--text-tertiary)',
                       whiteSpace: 'nowrap' }}
              title={data?.cached
                       ? `这份数据来自 ${data.cache_age_s ?? 0} 秒前的内存缓存，不是刚刚去 Outlook 读的。点「刷新邮件」强制重读。`
                       : '上一次真正去 Outlook 读取的时间'}>
          {data?.fetched_at
            ? <>上次刷新 <strong className="tnum">{fmtStamp(data.fetched_at)}</strong>
                {data.cached && <span style={{ marginLeft: 5 }}>· 缓存</span>}</>
            : ''}
        </span>
        <button className="btn btn-tonal btn-sm"
                onClick={reload} disabled={!key || reloading}
                title="重新去 Outlook 读一遍，跳过缓存">
          <Icon name="refresh" size={13} /> {reloading ? '正在读取…' : '刷新邮件'}
        </button>
      </div>

      {data && <Headline s={data.view.summary} range={rangeLabel} />}

      {isLoading && (
        <div className="card" style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
          正在读本地邮箱并分析…（在线模式的邮箱可能要几十秒）
        </div>
      )}

      {error && (
        <>
          <ErrCard error={error} onRetry={() => mutate()} />
          {!demo && (
            <button className="btn btn-tonal btn-sm" style={{ alignSelf: 'flex-start' }}
                    onClick={() => set('demo', '1')}>
              读不出来？先看演示数据
            </button>
          )}
        </>
      )}

      {/* ── 智能看板 ──────────────────────────────────────────
          格式照「文档地图」：一个维度一张 card，里面是分面格子网格。
          三个功能同页展示，所以每块顶上有分节标题。 */}
      {data && board && (
        <>
          <SectionHead icon="funnel" title="智能看板"
                       hint="按「这封邮件要你做什么动作」分类 · 点格子筛下面的矩阵，再点一下取消"
                       right={activeFilters.length > 0 ? (
                         <button className="btn btn-tonal btn-sm" onClick={clearFilters}>
                           清掉 {activeFilters.length} 个筛选
                         </button>
                       ) : undefined} />
          {board.dimensions.map((dim) => {
            const buckets = board.counts[dim.id] || {};
            const vals = dim.values.filter((v) => (buckets[v.id]?.count || 0) > 0);
            if (!vals.length) return null;
            return (
              <div key={dim.id} className="card" style={{ padding: 14 }}>
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 10,
                              marginBottom: 10, flexWrap: 'wrap' }}>
                  <h3 style={{ margin: 0, fontSize: 14 }}>{dim.label}</h3>
                  <span style={{ fontSize: 11.5, color: 'var(--text-tertiary)' }}>{dim.hint}</span>
                </div>
                <div style={{ display: 'grid',
                              gridTemplateColumns: 'repeat(auto-fit, minmax(215px, 1fr))', gap: 10 }}>
                  {vals.map((v) => (
                    <FacetTile key={v.id || 'none'} label={v.label} bucket={buckets[v.id]}
                               tone={TONE[v.tone] || '#64748B'} ai={v.ai}
                               active={filters[dim.id] === toToken(v.id)}
                               onClick={() => set(dim.id, filters[dim.id] === toToken(v.id)
                                                          ? '' : toToken(v.id))} />
                  ))}
                </div>
              </div>
            );
          })}
        </>
      )}

      {/* ── 信息矩阵 ─────────────────────────────────────────── */}
      {data && (
        <SectionHead icon="table" title="信息矩阵"
                     hint={activeFilters.length > 0
                       ? `已按上面的筛选缩小到 ${rows.length} / ${data.matrix.rows.length} 行`
                       : '一行一个会话 · 只列跟我有关的：群组邮件不进这张表'}
                     right={<ExcludedNote ex={data.matrix.excluded} />} />
      )}
      {data && (
        <div className="card" style={{ padding: 0 }}>
          {/* 宽表必须自己横向滚动，不能让整页横向滚。 */}
          <div style={{ overflowX: 'auto' }}>
            <table className="table" style={{ minWidth: 1000, fontSize: 12 }}>
              <thead>
                <tr>
                  {data.matrix.fields.map((f) => (
                    <th key={f.id} style={{ minWidth: f.width, whiteSpace: 'nowrap' }}
                        title={f.kind === 'ai' ? '这一列由云端模型抽取' : '本机计算'}>
                      {f.label}
                      {f.kind === 'ai' && <span style={{ color: BLUE }}> ·AI</span>}
                    </th>
                  ))}
                  <th style={{ minWidth: 80 }}></th>
                </tr>
              </thead>
              <tbody>
                {!rows.length && (() => {
                  // 先看是不是「点了看板里某个类型、而那一类整体被挡了」——
                  // 这是空表最常见的原因，而且它看起来完全像功能坏了。
                  const k = filters.kind || '';
                  const t = k ? data.matrix.excluded.by_kind?.[k] : undefined;
                  const label = dims.find((d) => d.id === 'kind')?.values
                    .find((v) => v.id === k)?.label || k;
                  if (k && t && t.kept === 0 && (t.group + t.kind) > 0) {
                    const why: string[] = [];
                    if (t.group) why.push(`${t.group} 个是群组邮件（我的地址既不在收件人也不在拄送里）`);
                    if (t.kind) why.push(`${t.kind} 个属于噪音类型（${data.matrix.excluded.skip_kinds.join(' / ')}）`);
                    return (
                      <tr><td colSpan={data.matrix.fields.length + 1}
                              style={{ color: 'var(--text-secondary)', padding: '14px 10px', lineHeight: 1.7 }}>
                        <b>「{label}」的 {t.group + t.kind} 个会话全部不进这张表。</b>
                        <br />{why.join('；')}。
                        <br />看板给的是全貌（“这段时间有多少封”本身就是有用的信息），
                        这张表只给要你动手的 —— 所以两边对不上是正常的。
                      </td></tr>
                    );
                  }
                  return (
                  <tr><td colSpan={data.matrix.fields.length + 1}
                          style={{ color: 'var(--text-secondary)', padding: '14px 10px' }}>
                    这个筛选下没有需要我动手的会话。
                    {data.matrix.excluded.total > 0 && (
                      <> 这段时间有 {data.matrix.excluded.total} 个会话被挡在这张表外面
                        {data.matrix.excluded.group > 0 &&
                          `：${data.matrix.excluded.group} 个是群组邮件（我的地址既不在收件人也不在抄送里）`}
                        {data.matrix.excluded.kind > 0 &&
                          `${data.matrix.excluded.group > 0 ? '，' : '：'}${data.matrix.excluded.kind} 个是噪音类型（广告 / 订阅资讯 / 工单状态）`}
                        。它们都在上面的智能看板里能看到。</>
                    )}
                  </td></tr>
                  );
                })()}
                {rows.map((r) => (
                  <tr key={r.conv_id}>
                    {data.matrix.fields.map((f) => (
                      <td key={f.id} style={{ overflowWrap: 'anywhere' }}>
                        {f.id === 'addressing' ? <Addressing v={r.cells.addressing} />
                          : f.id === 'subject' ? <SubjectCell row={r} />
                          : f.id === 'matter' ? <MatterCell row={r} />
                          : r.cells[f.id]
                            || <span style={{ color: 'var(--text-tertiary)' }}>—</span>}
                      </td>
                    ))}
                    <td><OpenButton entryId={r.open_id} demo={!!data.demo} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div style={{ padding: '9px 12px', fontSize: 11.5, color: 'var(--text-tertiary)',
                        lineHeight: 1.7 }}>
            <b>一行 = 一个会话</b>（不是一封邮件）。
            <b>收件方式</b>取整个会话：会话里只要有一封是直接发给我的就算「直发我」——
            常见的形态是最初直接问我、后面的追问群发抄送一圈，只看最新那封会判成「抄送我」。
            读不到那个 MAPI 属性时显示「不确定」而不是猜（实测约 1/3 的邮件读不到）。
            <br />
            主题后面的<b>徽标</b>（金额 / 单号 / 附件 / 外部域名）原来各占一列，实测整列都是
            「—」—— 一列在多数行里是空的，就是在拿横向滚动换白纸，所以改成有就显示、没有不占位。
            金额和单号是本机正则抽的，只在主题和正文前 1200 字里找（更靠后多半是引用历史，
            抽出来的数字属于上一轮讨论）；「+N」表示同一会话里还命中了 N 个其他值。
            标 ·AI 的两列由云端模型抽取，「待拍板」并在事项格里。
          </div>
        </div>
      )}

      {/* ── 时间图谱 ─────────────────────────────────────────── */}
      {data && (
        <>
          <SectionHead icon="graph" title="时间图谱"
                       hint="这件事怎么走到今天的 · 时间轴和关系网都是本机算的，没有 AI 参与"
                       right={<ExcludedNote ex={data.graph.excluded} />} />
          {!projects.length && (
            <div className="card" style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
              {rangeLabel}里没有需要我动手、且往来两封以上的会话 ——
              时间轴需要一来一回才画得出来，而群组邮件和噪音类型（广告 / 订阅资讯 /
              工单状态）不画。
            </div>
          )}
          {projects.length > 0 && (
            <div className="card" style={{ padding: '9px 12px' }}>
              <div style={{ display: 'flex', gap: 7, flexWrap: 'wrap', alignItems: 'center' }}>
                <span style={{ fontSize: 12, fontWeight: 600 }}>选一件事</span>
                {projects.map((p) => (
                  <button key={p.conv_id}
                          className={`btn btn-sm ${p.conv_id === activeConv ? 'btn-primary' : 'btn-tonal'}`}
                          onClick={() => set('conv', p.conv_id)}
                          title={p.subject}>
                    {p.subject.slice(0, 18)}{p.subject.length > 18 ? '…' : ''} · {p.msg_count} 封
                  </button>
                ))}
              </div>
              <div style={{ fontSize: 11, color: 'var(--text-tertiary)', marginTop: 6 }}>
                按往来封数排 —— 一来一回才有时间轴可言，单封邮件画出来是一个点。
              </div>
            </div>
          )}
          {g && (
            <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
              <div className="card" style={{ flex: '1 1 420px', minWidth: 300 }}>
                <div style={{ fontSize: 13.5, fontWeight: 600, marginBottom: 8,
                              overflowWrap: 'anywhere' }}>
                  {g.subject}
                </div>
                <Timeline g={g} />
              </div>
              {/* 宽度从 330 提到 460：内部名字像「Gao, Chen(Group)」「Dong, Rain(IT)」
                  这种带部门后缀的，330 宽必然折行，折完一行变三行、表格就散了。 */}
              <div className="card" style={{ flex: '0 1 460px', minWidth: 400 }}>
                <div style={{ fontSize: 13.5, fontWeight: 600, marginBottom: 8 }}>参与者关系</div>
                <Participants g={g} />
              </div>
            </div>
          )}
        </>
      )}

      {data?.partial && (
        <div className="card" style={{ borderLeft: `3px solid ${AMBER}`, fontSize: 12.5, lineHeight: 1.7 }}>
          <b>只取到了一部分，三个视图都不完整。</b>
          扫了 {data.diagnostics.scanned} 封就到了 {data.diagnostics.time_budget_s} 秒上限
          （stopped_by = {data.diagnostics.stopped_by}）。在 Outlook 里开启缓存模式可以取全。
        </div>
      )}

      {/* ── 页脚 + 本次外发 ──────────────────────────────────── */}
      <div className="card" style={{ padding: '9px 12px', fontSize: 11.5, display: 'flex',
                                     gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
        <span style={{ color: 'var(--text-tertiary)' }}>{rangeLabel}</span>
        {readingStore && (
          <span style={{ color: 'var(--text-tertiary)' }}>
            读 <b>{readingStore}</b> · 不发送 · 不修改邮件
          </span>
        )}
        {data && (
          <span style={{ color: 'var(--text-tertiary)' }}>
            {data.total} 封 → {data.view.summary.threads} 个会话 · {data.elapsed_ms} ms
          </span>
        )}
        {/* 用户同意了全量外发，但他有权在每一次都看见到底发出去了多少。 */}
        {data?.ai && (
          <span style={{ color: data.ai.chars_sent > 0 ? AMBER : 'var(--text-tertiary)' }}>
            {data.ai.chars_sent > 0
              ? `本次外发 ${data.ai.batches} 批 / ${(data.ai.chars_sent / 1000).toFixed(1)}k 字 → ${data.ai.model}`
              : `本次未外发（${data.ai.skipped || '全部命中缓存'}）`}
            {data.ai.cached > 0 && ` · 缓存命中 ${data.ai.cached}`}
            {data.ai.failed > 0 && ` · 失败 ${data.ai.failed}`}
          </span>
        )}
        {/* 刷新按钮在页面顶部（跟时间范围放一起）。这里不再放第二个 —— 两个按钮
            做同一件事，用户会以为它们不一样。 */}
        <button className="btn btn-tonal btn-sm" style={{ marginLeft: 'auto' }}
                onClick={() => setShowDiag(!showDiag)}
                title="上面那些结论分别是怎么算出来的、哪些内容发去过云端模型">
          {showDiag ? '收起' : '数据从哪来'}
        </button>
      </div>

      {showDiag && data && (
        <div className="card" style={{ fontSize: 12, lineHeight: 1.85, color: 'var(--text-secondary)' }}>
          <div>「我在收件人栏吗」的来源：{Object.entries(data.diagnostics.to_me_source)
            .map(([k, n]) => `${k} ${n}`).join(' · ')}</div>
          <div>发件人地址来源：{Object.entries(data.diagnostics.sender_addr_source)
            .map(([k, n]) => `${k} ${n}`).join(' · ')}</div>
          <div>
            「这个会话我回过没有」：扫了 {data.diagnostics.sent_scanned} 封已发送、
            {data.diagnostics.sent_conversations} 个会话
            {data.diagnostics.sent_stopped_by === 'time_budget' && '（未扫完，可能把已回过的误判成在等你）'}
          </div>
          <div>
            承诺扫描：{data.diagnostics.promise_scan
              ? `读了 ${data.diagnostics.promise_bodies_read} 封已发送正文，抽到 ${data.diagnostics.promises_found} 条`
              : '已关闭（在线模式邮箱下读正文太慢，会把整页预算吃光）'}
          </div>
          <div>
            AI 语义层：模型 {data.ai.model || '(未配置)'} ·
            新分析 {data.ai.sent} · 缓存 {data.ai.cached} · 失败 {data.ai.failed}
            {data.ai.skipped && ` · ${data.ai.skipped}`}
            <br />
            外发内容只有<b>主题 + 正文前 400 字</b>；不发附件、不发附件名、不发收件人栏。
            同一封邮件按内容哈希只发一次。
          </div>
          {data.sender_skipped && (
            <div style={{ color: AMBER }}>
              这个邮箱上跳过了发件人读取（任何跟发件人有关的属性都会让 Outlook 无限期挂住），
              所以没有发件人名字，关系网也会缺人。其余判定照常。
            </div>
          )}
          {data.cached_store === false && (
            <div style={{ color: AMBER }}>
              这个邮箱是<b>在线模式</b>（无本地缓存），每个字段一次网络往返，实测约 6 秒/封。
              在 Outlook 里开启「缓存的 Exchange 模式」可以快两个数量级。
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export default OutlookPage;
