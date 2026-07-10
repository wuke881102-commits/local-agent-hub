import React from 'react';
import { useNavigate } from 'react-router-dom';
import useSWR from 'swr';
import { Icon } from '../components/icons';
import { api, fetcher, errMsg, Scene, TaskSummary, Diagnostics } from '../api';
import { useAuth } from '../hooks/useAuth';
import { useToast } from '../components/Toast';

const Dashboard: React.FC = () => {
  const nav = useNavigate();
  const toast = useToast();
  const { auth } = useAuth();
  const { data: scenesData } = useSWR<{ items: Scene[] }>('/api/scenes', fetcher);
  const { data: tasksData, mutate: mutateTasks } = useSWR<{ items: TaskSummary[] }>('/api/tasks?limit=8', fetcher, { refreshInterval: 6000 });
  const { data: diag, mutate: mutateDiag } = useSWR<Diagnostics>('/api/diagnostics', fetcher, { refreshInterval: 15000 });
  const [refreshing, setRefreshing] = React.useState(false);

  const scenes = scenesData?.items || [];
  const tasks = tasksData?.items || [];
  const isNeedsLogin = auth?.stage === 'needs_login' || !!auth?.needs_login;

  const indexTotal = diag?.index?.total ?? 0;
  const lastRefreshed = diag?.index?.last_refreshed || '—';
  const llmOk = (diag?.llm?.text?.ok && diag?.llm?.vision?.ok) || diag?.llm?.mock;

  const [deletingTask, setDeletingTask] = React.useState<string | null>(null);
  const [retrying, setRetrying] = React.useState<string | null>(null);

  async function refreshIndex() {
    setRefreshing(true);
    try {
      await api.post('/api/assets/refresh');
      await Promise.all([mutateTasks(), mutateDiag()]);
      toast.success('索引刷新完成');
    } catch (err) {
      toast.error('索引刷新失败', { detail: errMsg(err) });
    } finally {
      setRefreshing(false);
    }
  }

  async function deleteTask(e: React.MouseEvent, t: TaskSummary) {
    e.stopPropagation();
    const label = t.target && t.target !== '—' ? t.target : t.id;
    setDeletingTask(t.id);
    try {
      await api.del(`/api/tasks/${t.id}`);
      await mutateTasks();
      toast.success('已删除任务', { detail: label });
    } catch (err) {
      toast.error('删除失败', { detail: errMsg(err) });
    } finally {
      setDeletingTask(null);
    }
  }

  const wbFailed = (t: TaskSummary) => (t.writeback || '').toLowerCase() === 'failed';
  const taskFailed = (t: TaskSummary) => (t.status || '').toLowerCase() === 'failed';
  const canRetry = (t: TaskSummary) => wbFailed(t) || taskFailed(t);

  async function retryTask(e: React.MouseEvent, t: TaskSummary) {
    e.stopPropagation();
    // 写回/分发失败：复用已生成内容，回到任务页自动打开确认弹窗重发（不重跑 Agent）。
    if (wbFailed(t)) {
      nav(`/task/${t.agent_id}/${t.id}?writeback=1`);
      return;
    }
    // 任务本身失败：用相同输入重跑整个 Agent。
    setRetrying(t.id);
    try {
      const r = await api.post<{ task_id: string }>(`/api/tasks/${t.id}/retry`, {});
      toast.success('已重新运行');
      nav(`/task/${t.agent_id}/${r.task_id}`);
    } catch (err) {
      toast.error('重试失败', { detail: errMsg(err) });
    } finally {
      setRetrying(null);
    }
  }

  return (
    <div style={{ padding: 'var(--space-8)', maxWidth: 1280, margin: '0 auto' }} className="fade-in">
      {/* 页眉 */}
      <div style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', gap: 16, marginBottom: 'var(--space-6)', flexWrap: 'wrap' }}>
        <div>
          <div className="eyebrow">Local Agent Hub</div>
          <h1 className="page-title" style={{ marginTop: 6 }}>工作台概览</h1>
          <div style={{ color: 'var(--text-secondary)', fontSize: 'var(--text-sm)', marginTop: 6, maxWidth: 560 }}>
            把团队飞书文档、知识库、多维表格与会议纪要，整理成可治理、可复用的本地知识资产。
          </div>
        </div>
        <button className="btn btn-primary" onClick={refreshIndex} disabled={refreshing}>
          <Icon name="refresh" size={14} className={refreshing ? 'spin' : ''} />
          {refreshing ? '刷新索引中…' : '刷新索引'}
        </button>
      </div>

      {/* 顶部状态 */}
      <div className="stagger" style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 'var(--space-4)', marginBottom: 'var(--space-8)' }}>
        <StatCard
          label="飞书授权"
          value={auth?.authenticated ? '已授权' : isNeedsLogin ? '差一步' : '未授权'}
          hint={
            auth?.authenticated
              ? (auth.user_name || '已登录')
              : auth?.mock_mode || auth?.mock
                ? 'mock 模式'
                : isNeedsLogin
                  ? 'App 已绑定，待用户登录'
                  : '点击 Dashboard 顶部按钮授权'
          }
          tone={auth?.authenticated ? 'success' : isNeedsLogin ? 'warning' : 'warning'}
        />
        <StatCard label="飞书索引" value={String(indexTotal)} hint={`上次刷新 ${lastRefreshed}`}
                  tone={indexTotal > 0 ? 'success' : 'info'} />
        <StatCard label="模型连通" value={llmOk ? '正常' : '异常'}
                  hint={`${shortModel(diag?.env?.text_model)} / ${shortModel(diag?.env?.vision_model)}`}
                  tone={llmOk ? 'success' : 'error'} />
        <StatCard label="飞书 CLI" value={diag?.cli?.mode === 'live' ? '在线' : (diag?.cli?.mode === 'mock' ? 'Mock' : '回退')}
                  hint={diag?.cli?.version || diag?.cli?.bin || '—'}
                  tone={diag?.cli?.mode === 'live' ? 'success' : 'warning'} />
      </div>

      {/* 任务场景 */}
      <h2 className="section-title" style={{ marginBottom: 'var(--space-4)' }}>任务场景</h2>
      <div className="stagger" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 'var(--space-4)', marginBottom: 'var(--space-8)' }}>
        {scenes.map(s => (
          <div key={s.id}
               className="card card-interactive"
               style={{ overflow: 'hidden' }}
               onClick={() => sceneTarget(s.id, nav)}>
            <div style={{ position: 'absolute', top: 0, left: 0, right: 0, height: 3, background: s.accent, opacity: 0.85 }} />
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12 }}>
              <div style={{ width: 40, height: 40, borderRadius: 12, background: `${s.accent}1A`, color: s.accent, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
                <Icon name={s.icon} size={20} />
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontWeight: 600, fontSize: 15 }}>{s.title}</div>
                <div style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>{s.subtitle}</div>
              </div>
              {s.featured && <span className="badge badge-brand">推荐</span>}
            </div>
            <div style={{ fontSize: 12, color: 'var(--text-tertiary)', display: 'flex', alignItems: 'center', gap: 6 }}>
              <span className="eyebrow" style={{ letterSpacing: '0.04em' }}>Agents</span>
              <span style={{ color: 'var(--text-secondary)' }}>{s.agents.join(' · ')}</span>
            </div>
          </div>
        ))}
      </div>

      {/* 最近任务 */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 'var(--space-4)' }}>
        <h2 className="section-title">最近任务</h2>
        <button className="btn btn-ghost btn-sm" onClick={() => nav('/tasks')}>查看全部 →</button>
      </div>
      <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
        <table className="table" style={{ width: '100%' }}>
          <thead>
            <tr>
              <th>任务</th><th>场景</th><th>Agent</th><th>对象</th><th>状态</th><th>写回</th><th>时间</th><th></th>
            </tr>
          </thead>
          <tbody>
            {tasks.length === 0 && (
              <tr><td colSpan={8} style={{ textAlign: 'center', padding: '24px', color: 'var(--text-tertiary)' }}>
                还没有任务。试试上方"内容生成 / HTML 页面生成"。
              </td></tr>
            )}
            {tasks.map(t => (
              <tr key={t.id} style={{ cursor: 'pointer' }} onClick={() => nav(`/task/${t.agent_id}/${t.id}`)}>
                <td className="mono" style={{ fontSize: 12 }}>{t.id}</td>
                <td>{t.scene || '—'}</td>
                <td>{t.agent_id}</td>
                <td style={{ maxWidth: 240, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={t.target && t.target !== '—' ? t.target : undefined}>{t.target}</td>
                <td><StatusBadge status={t.status} /></td>
                <td><span className="badge">{t.writeback}</span></td>
                <td className="mono" style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>{t.started_at?.slice(5, 16).replace('T', ' ')}</td>
                <td style={{ width: 76, textAlign: 'center', whiteSpace: 'nowrap' }}>
                  {canRetry(t) && (
                    <button
                      className="btn btn-ghost btn-sm"
                      title={wbFailed(t) ? '写回/分发失败：复用已生成内容重试' : '用相同输入重跑该任务'}
                      disabled={retrying === t.id}
                      onClick={(e) => retryTask(e, t)}
                      style={{ padding: 4, color: 'var(--brand-600)' }}
                    >
                      <Icon name="refresh" size={15} />
                    </button>
                  )}
                  <button
                    className="btn btn-ghost btn-sm"
                    title={t.status === 'running' ? '任务运行中，无法删除' : '删除任务'}
                    disabled={t.status === 'running' || deletingTask === t.id}
                    onClick={(e) => deleteTask(e, t)}
                    style={{ padding: 4, color: 'var(--text-tertiary)', opacity: t.status === 'running' ? 0.35 : 1 }}
                  >
                    <Icon name="trash" size={15} />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
};

const StatCard: React.FC<{ label: string; value: string; hint: string; tone: 'success' | 'warning' | 'error' | 'info'; action?: React.ReactNode }> = ({ label, value, hint, tone, action }) => {
  const colorMap = {
    success: 'var(--success)', warning: 'var(--warning)', error: 'var(--error)', info: 'var(--info)',
  };
  const c = colorMap[tone];
  return (
    <div className="card" style={{ padding: 'var(--space-5)', overflow: 'hidden' }}>
      <div style={{ position: 'absolute', top: 0, left: 0, right: 0, height: 3, background: c, opacity: 0.85 }} />
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 8 }}>
        <div style={{ minWidth: 0 }}>
          <div className="eyebrow" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <span style={{ width: 6, height: 6, borderRadius: 3, background: c, flexShrink: 0 }} />
            {label}
          </div>
          <div className="tnum" style={{ fontSize: 30, fontWeight: 700, color: c, marginTop: 10, lineHeight: 1.05, letterSpacing: '-0.02em' }}>{value}</div>
          <div style={{
            fontSize: 12, color: 'var(--text-tertiary)', marginTop: 7, lineHeight: 1.45,
            display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical',
            overflow: 'hidden', wordBreak: 'break-word',
          }} title={hint}>{hint}</div>
        </div>
        {action}
      </div>
    </div>
  );
};

const StatusBadge: React.FC<{ status: string }> = ({ status }) => {
  const map: Record<string, string> = {
    done: 'badge badge-success', preview: 'badge badge-info', review: 'badge badge-warning',
    failed: 'badge badge-error', running: 'badge badge-brand', queued: 'badge',
  };
  const text: Record<string, string> = {
    done: '完成', preview: '预览', review: '待审', failed: '失败', running: '进行中', queued: '排队',
  };
  return <span className={map[status] || 'badge'}>{text[status] || status}</span>;
};

// 模型 id 精简显示：去掉日期后缀与 qwen 档位后缀，qwen 首字母大写。
// 例：qwen3.7-plus → Qwen3.6；gpt-4.1-mini-2025-04-14 → gpt-4.1-mini
function shortModel(m?: string): string {
  if (!m) return '—';
  return m
    .replace(/-\d{4}-\d{2}-\d{2}$/, '')
    .replace(/-(plus|max|flash|turbo|latest)$/i, '')
    .replace(/^qwen/i, 'Qwen');
}

// Map scene card → task launch page. Unknown scenes fall back to the scenes hub.
function sceneTarget(sceneId: string, nav: (p: string) => void): void {
  const map: Record<string, string> = {
    'content':       '/task/html-page',
    'knowledge-gov': '/task/document-map',     // 知识库治理：先建图，再切到治理
    'meeting':       '/task/meeting-minutes',  // 会议沉淀：妙记 / 会议记录整理
    'table':         '/task/base-analysis',    // 表格分析：多维表格分析
    'pdf':           '/task/pdf-recognition',  // PDF 识别：云盘 PDF AI 识别
    'dispatch':      '/task/collab-dispatch',  // 协作分发（Phase B 占位页）
    'auto-extract':  '/autoextract',           // 自动化提炼：按 Enter 留痕 + 定时提炼
  };
  nav(map[sceneId] || '/scenes');
}

export default Dashboard;
