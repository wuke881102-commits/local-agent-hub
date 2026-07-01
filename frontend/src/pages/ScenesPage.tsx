import React from 'react';
import { useNavigate } from 'react-router-dom';
import useSWR from 'swr';
import { Icon } from '../components/icons';
import { fetcher, Scene } from '../api';

const ScenesPage: React.FC = () => {
  const nav = useNavigate();
  const { data } = useSWR<{ items: Scene[] }>('/api/scenes', fetcher);
  const scenes = data?.items || [];

  return (
    <div style={{ padding: 'var(--space-8)' }}>
      <h2 style={{ marginTop: 0, fontSize: 20 }}>任务场景</h2>
      <p style={{ color: 'var(--text-tertiary)' }}>选择一个工作场景，系统将匹配对应 Agent。</p>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 'var(--space-4)' }}>
        {scenes.map(s => (
          <div key={s.id} className="card card-interactive"
               style={{ cursor: 'pointer', borderTop: `3px solid ${s.accent}` }}
               onClick={() => sceneTarget(s.id, nav)}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 8 }}>
              <div style={{ width: 36, height: 36, borderRadius: 10, background: `${s.accent}15`, color: s.accent, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                <Icon name={s.icon} size={18} />
              </div>
              <div style={{ flex: 1 }}>
                <div style={{ fontWeight: 600 }}>{s.title}</div>
                <div style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>{s.subtitle}</div>
              </div>
              {s.featured && <span className="badge badge-brand">推荐</span>}
            </div>
            <div style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>
              使用 Agent：{s.agents.join(' / ')}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};

function sceneTarget(sceneId: string, nav: (p: string) => void): void {
  const map: Record<string, string> = {
    'content':       '/task/html-page',
    'knowledge-gov': '/task/document-map',
    'meeting':       '/task/meeting-minutes',
    'table':         '/task/base-analysis',
    'pdf':           '/task/pdf-recognition',
    'dispatch':      '/task/collab-dispatch',
    'auto-extract':  '/autoextract',
  };
  nav(map[sceneId] || '/scenes');
}

export default ScenesPage;
