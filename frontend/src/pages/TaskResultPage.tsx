import React, { useEffect, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import useSWR from 'swr';
import { Icon } from '../components/icons';
import { fetcher, TaskDetail, subscribeTask } from '../api';

type LogEntry = { ts: string; level: string; message: string };

/**
 * 通用「内容生产结果」页：按 task_id 展示生成的 HTML 预览 + 下载。
 * 供「本地目录」的读图/读文档任务（local-image）从运行记录点进来查看结果。
 */
const TaskResultPage: React.FC = () => {
  const { taskId } = useParams();
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const logRef = useRef<HTMLDivElement>(null);

  const { data: task, mutate } = useSWR<TaskDetail>(
    taskId ? `/api/tasks/${taskId}` : null, fetcher,
    { refreshInterval: (d) => (d && d.status === 'running' ? 3000 : 0) },
  );

  useEffect(() => { logRef.current?.scrollTo(0, logRef.current.scrollHeight); }, [logs]);

  // 任务仍在跑 → 订阅实时日志
  useEffect(() => {
    if (!taskId || !task || task.status !== 'running') return;
    setLogs([]);
    const close = subscribeTask(
      taskId,
      (entry) => {
        if (entry._done) { mutate(); return; }
        if (entry._keepalive) return;
        setLogs((prev) => [...prev, entry as LogEntry]);
      },
      () => mutate(),
    );
    return close;
  }, [taskId, task?.status, mutate]); // eslint-disable-line react-hooks/exhaustive-deps

  const status = task?.status;
  const previewUrl = taskId && task?.result_path ? `/api/tasks/${taskId}/preview` : null;
  const title = task?.payload?.title || task?.target || '内容生成';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      <div style={{ padding: '12px 24px', borderBottom: '1px solid var(--border-subtle)', display: 'flex', alignItems: 'center', gap: 12, background: 'var(--surface-elevated)', flexWrap: 'wrap' }}>
        <Icon name="sparkle" size={16} style={{ color: 'var(--brand-600, var(--text-primary))' }} />
        <span style={{ fontWeight: 600 }}>{title}</span>
        <span className="mono" style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>{taskId}</span>
        {status && (
          <span className={`badge badge-${status === 'done' ? 'success' : status === 'failed' ? 'error' : 'brand'}`}>
            {status === 'done' ? '完成' : status === 'failed' ? '失败' : status === 'running' ? '生成中' : status}
          </span>
        )}
        {task?.payload?.n_images != null && (
          <span style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>
            截图 {task.payload.n_images} · 文档 {task.payload.n_docs ?? 0}
          </span>
        )}
        <div style={{ flex: 1 }} />
        {previewUrl && (
          <>
            <a className="btn btn-secondary btn-sm" href={previewUrl} target="_blank" rel="noreferrer"><Icon name="external" size={14} /> 新标签打开</a>
            <a className="btn btn-ghost btn-sm" href={`/api/tasks/${taskId}/download`}><Icon name="page" size={14} /> 下载 HTML</a>
          </>
        )}
      </div>

      <div style={{ flex: 1, background: 'var(--surface-inset)', overflow: 'hidden' }}>
        {previewUrl ? (
          <iframe src={previewUrl} title="preview" style={{ width: '100%', height: '100%', border: 'none', background: '#fff' }} />
        ) : status === 'failed' ? (
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', flexDirection: 'column', gap: 12, color: 'var(--error)', padding: 24, textAlign: 'center' }}>
            <Icon name="warning" size={40} />
            <div style={{ maxWidth: 520 }}>{task?.error || '生成失败'}</div>
          </div>
        ) : status === 'running' ? (
          <div style={{ padding: 24, maxWidth: 720, margin: '0 auto' }}>
            <div style={{ color: 'var(--text-secondary)', fontSize: 14, marginBottom: 12 }}><span className="pulse">▌</span> 正在生成内容…</div>
            <div ref={logRef} style={{ background: 'var(--surface-dark)', color: '#cad7e1', borderRadius: 'var(--radius-md)', padding: 12, fontFamily: 'var(--font-mono)', fontSize: 12, lineHeight: 1.6, maxHeight: 360, overflowY: 'auto' }}>
              {logs.map((l, i) => (
                <div key={i} style={{ color: l.level === 'error' ? '#FF8B8B' : l.level === 'warn' ? '#FFD466' : '#9BF1BD' }}>
                  <span style={{ color: '#737A82', marginRight: 8 }}>{l.ts?.slice(11)}</span>{l.message}
                </div>
              ))}
            </div>
          </div>
        ) : (
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', color: 'var(--text-tertiary)', flexDirection: 'column', gap: 12 }}>
            <Icon name="page" size={48} />
            <div>没有可预览的内容（任务可能未产出结果）。</div>
          </div>
        )}
      </div>
    </div>
  );
};

export default TaskResultPage;
