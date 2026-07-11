import React from 'react';
import { useNavigate } from 'react-router-dom';
import useSWR from 'swr';
import { api, fetcher, errMsg, TaskSummary } from '../api';
import { Icon } from '../components/icons';
import { useToast } from '../components/Toast';
import { SourceTag } from './Dashboard';

const TasksPage: React.FC = () => {
  const nav = useNavigate();
  const toast = useToast();
  const { data, mutate } = useSWR<{ items: TaskSummary[] }>('/api/tasks?limit=80', fetcher, { refreshInterval: 4000 });
  const tasks = data?.items || [];
  const [deleting, setDeleting] = React.useState<string | null>(null);
  const [retrying, setRetrying] = React.useState<string | null>(null);

  const wbFailed = (t: TaskSummary) => (t.writeback || '').toLowerCase() === 'failed';
  const taskFailed = (t: TaskSummary) => (t.status || '').toLowerCase() === 'failed';
  const canRetry = (t: TaskSummary) => wbFailed(t) || taskFailed(t);

  async function retryTask(e: React.MouseEvent, t: TaskSummary) {
    e.stopPropagation();
    if (wbFailed(t)) {
      nav(`/task/${t.agent_id}/${t.id}?writeback=1`);  // 复用已生成内容，回任务页重发
      return;
    }
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

  async function del(e: React.MouseEvent, t: TaskSummary) {
    e.stopPropagation();
    const label = t.target && t.target !== '—' ? t.target : t.id;
    setDeleting(t.id);
    try {
      await api.del(`/api/tasks/${t.id}`);
      await mutate();
      toast.success('已删除任务', { detail: label });
    } catch (err) {
      toast.error('删除失败', { detail: errMsg(err) });
    } finally {
      setDeleting(null);
    }
  }

  return (
    <div style={{ padding: 'var(--space-8)' }}>
      <h2 style={{ marginTop: 0, fontSize: 20 }}>运行记录</h2>
      <div className="card" style={{ padding: 0 }}>
        <table className="table">
          <thead>
            <tr>
              <th>任务</th><th>Agent</th><th>场景</th><th>对象</th><th>状态</th><th>写回</th><th>开始</th><th>结束</th><th></th>
            </tr>
          </thead>
          <tbody>
            {tasks.map(t => (
              <tr key={t.id} style={{ cursor: 'pointer' }} onClick={() => nav(`/task/${t.agent_id}/${t.id}`)}>
                <td className="mono" style={{ fontSize: 12 }}>{t.id}</td>
                <td>{t.agent_id}</td>
                <td>{t.scene || '—'}</td>
                <td><SourceTag source={t.source} />{t.target}</td>
                <td><span className="badge">{t.status}</span></td>
                <td><span className="badge">{t.writeback}</span></td>
                <td className="mono" style={{ fontSize: 11 }}>{t.started_at}</td>
                <td className="mono" style={{ fontSize: 11 }}>{t.finished_at || '—'}</td>
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
                    disabled={t.status === 'running' || deleting === t.id}
                    onClick={(e) => del(e, t)}
                    style={{ padding: 4, color: 'var(--text-tertiary)', opacity: t.status === 'running' ? 0.35 : 1 }}
                  >
                    <Icon name="trash" size={15} />
                  </button>
                </td>
              </tr>
            ))}
            {tasks.length === 0 && (
              <tr><td colSpan={9} style={{ textAlign: 'center', padding: 24, color: 'var(--text-tertiary)' }}>暂无任务</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
};

export default TasksPage;
