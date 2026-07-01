import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Icon } from '../components/icons';
import LocalSourcePicker, { DIR_KEY, fileUrl, LocalFile } from '../components/LocalSourcePicker';

/**
 * 本地目录页：纯文件浏览 / 数据源。
 * - 截图采集（按 Enter 留痕 + 定时提炼）已独立为「自动化提炼」场景。
 * - 内容生产已迁到各任务场景（内容生成 / PDF 识别 / 表格分析 / 协作分发），
 *   在那里把「本地目录」作为数据源即可选用此处的文件。
 */
const LocalDirPage: React.FC = () => {
  const nav = useNavigate();
  const [dir, setDir] = useState<string>(() => { try { return localStorage.getItem(DIR_KEY) || ''; } catch { return ''; } });
  const persistDir = (p: string) => { setDir(p); try { localStorage.setItem(DIR_KEY, p); } catch { /* ignore */ } };

  return (
    <div style={{ padding: 'var(--space-8)', maxWidth: 1180, margin: '0 auto' }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12, marginBottom: 'var(--space-5)' }}>
        <div style={{ flex: 1 }}>
          <h2 style={{ margin: 0, fontSize: 20, fontWeight: 600 }}>本地目录</h2>
          <div className="eyebrow" style={{ marginTop: 4 }}>选一个本地目录浏览文件 · 文件可在「任务场景」里作为数据源生产</div>
        </div>
      </div>

      {/* 指引：内容生成已迁到任务场景 */}
      <div className="card" style={{ padding: 'var(--space-4)', marginBottom: 'var(--space-3)', display: 'flex', alignItems: 'center', gap: 12, background: 'var(--surface-subtle)' }}>
        <Icon name="sparkle" size={18} style={{ color: 'var(--brand-600, var(--text-primary))', flexShrink: 0 }} />
        <div style={{ flex: 1, fontSize: 13, color: 'var(--text-secondary)', lineHeight: 1.6 }}>
          想用这些文件生产内容？到「任务场景」选 <strong>内容生成 / PDF 识别 / 表格分析 / 协作分发</strong>，把<strong>数据来源切到「本地目录」</strong>即可选用此处的文件。
        </div>
        <button className="btn btn-secondary btn-sm" onClick={() => nav('/scenes')}>去任务场景 <Icon name="chevron-right" size={13} /></button>
      </div>

      {/* 指引：截图采集已迁到「自动化提炼」 */}
      <div className="card" style={{ padding: 'var(--space-4)', marginBottom: 'var(--space-5)', display: 'flex', alignItems: 'center', gap: 12, background: 'var(--surface-subtle)' }}>
        <Icon name="scan" size={18} style={{ color: '#0EA5E9', flexShrink: 0 }} />
        <div style={{ flex: 1, fontSize: 13, color: 'var(--text-secondary)', lineHeight: 1.6 }}>
          想边工作边按 <kbd className="kbd">Enter</kbd> 自动留痕、再定时提炼工作内容？请到 <strong>「自动化提炼」</strong> 场景——它的截图独立存放，不会出现在这里。
        </div>
        <button className="btn btn-secondary btn-sm" onClick={() => nav('/autoextract')}>去自动化提炼 <Icon name="chevron-right" size={13} /></button>
      </div>

      {/* 目录 + 文件列表（点击行打开预览；右侧操作与「飞书文档」一致：分析 / 识别 / 生成 HTML / 打开） */}
      <LocalSourcePicker
        dir={dir}
        onDirChange={persistDir}
        selectable={false}
        onOpenFile={(f: LocalFile) => window.open(fileUrl(f.path), '_blank')}
        actions={(f: LocalFile) => {
          const enc = encodeURIComponent(f.path);
          return (
            <>
              {f.kind === 'excel' && (
                <button className="btn btn-tonal btn-sm" onClick={() => nav(`/task/base-analysis?src=local&local_path=${enc}`)} title="读取表格数据，AI 规划并渲染图表看板">
                  <Icon name="table" size={12} /> 分析
                </button>
              )}
              {f.kind === 'pdf' && (
                <button className="btn btn-tonal btn-sm" onClick={() => nav(`/task/pdf-recognition?src=local&local_path=${enc}`)} title="AI 识别该 PDF：全文 / 字段 / 表格 / 合同台账">
                  <Icon name="scan" size={12} /> 识别
                </button>
              )}
              <button className="btn btn-ghost btn-sm" onClick={() => nav(`/task/html-page?src=local&local_path=${enc}`)} title="把该文件生产成 HTML 页面">
                <Icon name="page" size={12} /> 生成 HTML
              </button>
              <a className="btn btn-ghost btn-sm" href={fileUrl(f.path)} target="_blank" rel="noreferrer" title="在新标签打开 / 预览">
                <Icon name="external" size={12} />
              </a>
            </>
          );
        }}
      />
    </div>
  );
};

export default LocalDirPage;
