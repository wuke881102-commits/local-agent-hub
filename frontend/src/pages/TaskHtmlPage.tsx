import React, { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import useSWR from 'swr';
import { Icon } from '../components/icons';
import { api, fetcher, errMsg, Asset, TaskDetail, subscribeTask } from '../api';
import { useToast } from '../components/Toast';
import LocalSourcePicker, { DIR_KEY, SourceTabs } from '../components/LocalSourcePicker';

type LogEntry = { ts: string; level: string; message: string };

const PAGE_TYPES = [
  { id: 'internal_wiki', label: '内部知识页', desc: '专题 Wiki / 制度说明 / FAQ' },
  { id: 'project',       label: '项目展示页', desc: '项目介绍 / 阶段汇报 / 指标看板' },
  { id: 'announcement',  label: '公告/活动页', desc: '活动方案 / 公告 / 通知' },
  { id: 'custom',        label: '自定义',     desc: '用自然语言描述你想要的页面' },
];

const TaskHtmlPage: React.FC = () => {
  const params = useParams();
  const [search] = useSearchParams();
  const nav = useNavigate();
  const toast = useToast();
  const existingTaskId = params.taskId;

  // Allow deep-linking from the Assets page:
  //   单篇 ?doc_token=xxx&title=yyy   或   多篇 ?doc_tokens=a,b,c（最多 3）
  const seedMulti = (search.get('doc_tokens') || '').split(',').map(s => s.trim()).filter(Boolean);
  const seedSingle = search.get('doc_token') || '';
  const seedTokens = Array.from(new Set(seedMulti.length ? seedMulti : (seedSingle ? [seedSingle] : []))).slice(0, 3);
  const [docTokens, setDocTokens] = useState<string[]>(seedTokens);
  const [manualToken, setManualToken] = useState('');

  // 数据来源：飞书资产 / 本地目录。可由 ?src=local&local_path=<路径> 从「本地目录」深链带入并预选。
  const seedLocalPath = search.get('local_path') || '';
  const seedLocal = search.get('src') === 'local' && !!seedLocalPath;
  const seedLocalDir = seedLocal ? seedLocalPath.replace(/[\\/][^\\/]*$/, '') : '';
  const [srcMode, setSrcMode] = useState<'feishu' | 'local'>(seedLocal ? 'local' : 'feishu');
  const [localDir, setLocalDir] = useState<string>(() => { try { return seedLocalDir || localStorage.getItem(DIR_KEY) || ''; } catch { return seedLocalDir; } });
  const [localPaths, setLocalPaths] = useState<string[]>(seedLocal ? [seedLocalPath] : []);

  const [pageType, setPageType] = useState('internal_wiki');
  const [layoutMode, setLayoutMode] = useState<'template' | 'freeform'>('template'); // 版式：套模板 / AI 直出
  const [customInstruction, setCustomInstruction] = useState('');  // 「自定义」模板下的自然语言要求
  const [running, setRunning] = useState(false);
  const [taskId, setTaskId] = useState<string | null>(existingTaskId || null);
  const [logs, setLogs] = useState<LogEntry[]>([]);

  // Agent dispatches by asset type: docx/doc/wiki → markdown export,
  // bitable → table records, sheet → cell values, slides → XML text.
  const HTML_SUPPORTED = ['docx', 'doc', 'wiki', 'bitable', 'sheet', 'slides'];
  const { data: assetsData } = useSWR<{ items: Asset[] }>('/api/assets?limit=200', fetcher);
  const assets = (assetsData?.items || []).filter(a => HTML_SUPPORTED.includes(a.type));

  // 多来源：最多 3 篇，下拉/粘贴均添加到列表，可逐个移除。
  const MAX_SOURCES = 3;
  const atMax = docTokens.length >= MAX_SOURCES;
  const labelFor = (tok: string) => {
    const a = assets.find(x => x.asset_id === tok);
    return a ? `[${a.type}] ${a.title}` : tok;
  };
  const addToken = (tok: string) => {
    const t = (tok || '').trim();
    if (!t) return;
    setDocTokens(prev => (prev.includes(t) || prev.length >= MAX_SOURCES ? prev : [...prev, t]));
  };
  const removeToken = (tok: string) => setDocTokens(prev => prev.filter(t => t !== tok));

  const { data: task, mutate: mutateTask } = useSWR<TaskDetail>(
    taskId ? `/api/tasks/${taskId}` : null, fetcher, { refreshInterval: 4000 },
  );

  const logRef = useRef<HTMLDivElement>(null);
  useEffect(() => { logRef.current?.scrollTo(0, logRef.current.scrollHeight); }, [logs]);

  // Subscribe to SSE when taskId changes
  useEffect(() => {
    if (!taskId) return;
    setLogs([]);
    const close = subscribeTask(
      taskId,
      (entry) => {
        if (entry._done) {
          setRunning(false);
          mutateTask();
          return;
        }
        if (entry._keepalive) return;
        setLogs(prev => [...prev, entry as LogEntry]);
      },
      ({ error }) => {
        setRunning(false);
        mutateTask();
        if (error) toast.warning('实时日志连接中断', { detail: '任务可能仍在后台运行，页面将继续轮询其状态。' });
      },
    );
    return close;
  }, [taskId, mutateTask, toast]);

  // 本地目录数据源：走通用 local-image agent（读图/读文档→HTML），跳到内容生成结果页
  async function startLocal() {
    if (localPaths.length === 0) return;
    setRunning(true);
    try {
      const resp = await api.post<{ task_id: string }>('/api/tasks/run', {
        agent_id: 'local-image',
        scene: '内容生成',
        inputs: {
          files: localPaths,
          page_type: pageType,
          layout_mode: layoutMode,
          custom_instruction: pageType === 'custom' ? customInstruction.trim() : '',
          title: `本地内容生成 · ${localPaths.length} 个文件`,
        },
      });
      // 与飞书来源一致：留在本页（左输入 + 右预览），不跳到独立结果页。
      setTaskId(resp.task_id);
      nav(`/task/html-page/${resp.task_id}`, { replace: true });
    } catch (err) {
      setRunning(false);
      toast.error('启动本地内容生成失败', { detail: errMsg(err) });
    }
  }

  async function start() {
    if (docTokens.length === 0) return;
    setRunning(true);
    setLogs([]);
    try {
      const resp = await api.post<{ task_id: string }>('/api/tasks/run', {
        agent_id: 'html-page',
        scene: '内容生成',
        inputs: {
          doc_tokens: docTokens,
          doc_token: docTokens[0], // 向后兼容
          page_type: pageType,
          layout_mode: layoutMode,
          custom_instruction: pageType === 'custom' ? customInstruction.trim() : '',
        },
      });
      setTaskId(resp.task_id);
      nav(`/task/html-page/${resp.task_id}`, { replace: true });
    } catch (err) {
      setRunning(false);
      toast.error('启动 HTML 生成失败', { detail: errMsg(err) });
    }
  }

  function downloadHtml() {
    if (!taskId) return;
    window.open(`/api/tasks/${taskId}/download`, '_blank');
  }

  const previewUrl = taskId && task?.result_path ? `/api/tasks/${taskId}/preview` : null;

  // 任务用的是哪个模板（来自启动时的 inputs，任务存在即可读）；自定义则带上自然语言要求。
  const usedPageType: string | undefined = task?.inputs?.page_type;
  const usedLabel = PAGE_TYPES.find(t => t.id === usedPageType)?.label || usedPageType;
  const usedCustom: string = (task?.inputs?.custom_instruction || '').trim();
  const usedLayout: string = (task?.inputs?.layout_mode || 'template');

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '380px 1fr', height: '100%', overflow: 'hidden' }}>
      {/* 左：输入与日志 */}
      <div style={{ borderRight: '1px solid var(--border-subtle)', padding: 'var(--space-6)', overflowY: 'auto', background: 'var(--surface-elevated)' }}>
        <h2 style={{ marginTop: 0, fontSize: 18, fontWeight: 600 }}>HTML 页面生成</h2>
        <p style={{ color: 'var(--text-tertiary)', fontSize: 13 }}>
          {srcMode === 'feishu'
            ? '选择 1–3 篇飞书资产（文档 / 多维表 / 电子表 / 幻灯片）与模板，Agent 会按类型抽取各篇内容、合并后套入 Lumen-light 页面。生成后可在本地预览、下载 HTML。'
            : '从本地目录选若干文件（截图 / PDF / Word / Excel / PPT），AI 读图 / 读文档后直出一页 HTML。'}
        </p>

        {/* 数据来源切换 */}
        <SourceTabs mode={srcMode} onChange={setSrcMode} />

        {srcMode === 'feishu' ? (
        <div style={{ marginTop: 'var(--space-5)' }}>
          <label className="label" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <span>来源文档</span>
            <span className="eyebrow">{docTokens.length}/{MAX_SOURCES}</span>
          </label>

          {/* 已选来源 chips */}
          {docTokens.length > 0 && (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 8 }}>
              {docTokens.map((tok, i) => (
                <span key={tok} style={{
                  display: 'inline-flex', alignItems: 'center', gap: 6, maxWidth: '100%',
                  padding: '4px 6px 4px 10px', background: 'var(--brand-50)', color: 'var(--brand-700)',
                  border: '1px solid var(--brand-200)', borderRadius: 'var(--radius-full)', fontSize: 12,
                }}>
                  <span className="mono" style={{ fontSize: 10, opacity: 0.7 }}>{i + 1}</span>
                  <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 220 }} title={labelFor(tok)}>{labelFor(tok)}</span>
                  <button onClick={() => removeToken(tok)} title="移除"
                          style={{ width: 18, height: 18, padding: 0, borderRadius: '50%', background: 'transparent', border: 'none', color: 'var(--brand-700)', cursor: 'pointer', display: 'inline-flex', alignItems: 'center', justifyContent: 'center' }}>
                    <Icon name="x" size={12} />
                  </button>
                </span>
              ))}
            </div>
          )}

          <select className="input" value="" disabled={atMax}
                  onChange={e => { addToken(e.target.value); e.currentTarget.value = ''; }}
                  style={{ marginBottom: 8 }}>
            <option value="">{atMax ? '— 已达上限（最多 3 篇）—' : '— 从飞书索引添加 —'}</option>
            {assets.filter(a => !docTokens.includes(a.asset_id)).map(a => (
              <option key={a.asset_id} value={a.asset_id}>[{a.type}] {a.title} · {a.space || '—'}</option>
            ))}
          </select>

          <div style={{ display: 'flex', gap: 6 }}>
            <input className="input" placeholder="或粘贴文档 token / URL" value={manualToken} disabled={atMax}
                   onChange={e => setManualToken(e.target.value)}
                   onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addToken(manualToken); setManualToken(''); } }} />
            <button className="btn btn-secondary" disabled={atMax || !manualToken.trim()}
                    onClick={() => { addToken(manualToken); setManualToken(''); }}>添加</button>
          </div>
          <div style={{ fontSize: 11, color: 'var(--text-tertiary)', marginTop: 6 }}>
            可选 1–3 篇；多篇会合并抽取、生成一个页面，页脚保留全部来源引用。
          </div>
        </div>
        ) : (
        <div style={{ marginTop: 'var(--space-5)' }}>
          <label className="label">来源文件</label>
          <LocalSourcePicker dir={localDir} onDirChange={setLocalDir} multiple selected={localPaths} onSelChange={setLocalPaths} />
          <div style={{ fontSize: 11, color: 'var(--text-tertiary)', marginTop: 6, lineHeight: 1.6 }}>
            已选 {localPaths.length} 个文件 · 文档（PDF/Word/Excel/PPT）遵循下方「版式 / 页面模板」；截图始终以视觉直出，页面模板作为版面定位。
          </div>
        </div>
        )}

        <div style={{ marginTop: 'var(--space-4)' }}>
          <label className="label">版式</label>
          <div style={{ display: 'flex', gap: 6 }}>
            {([
              { id: 'template', label: '套模板', desc: '规整稳定·一致' },
              { id: 'freeform', label: '自由版式', desc: 'AI 直出·丰富' },
            ] as const).map(m => (
              <button key={m.id} type="button" onClick={() => setLayoutMode(m.id)}
                style={{
                  flex: 1, textAlign: 'left', padding: '8px 10px', cursor: 'pointer',
                  border: '1px solid', borderColor: layoutMode === m.id ? 'var(--brand-500)' : 'var(--border-subtle)',
                  borderRadius: 'var(--radius-lg)', background: layoutMode === m.id ? 'var(--brand-50)' : 'var(--surface-elevated)',
                }}>
                <div style={{ fontWeight: 600, fontSize: 13, color: layoutMode === m.id ? 'var(--brand-700)' : 'var(--text-primary)' }}>{m.label}</div>
                <div style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>{m.desc}</div>
              </button>
            ))}
          </div>
          {layoutMode === 'freeform' && (
            <div style={{ fontSize: 11, color: 'var(--text-tertiary)', marginTop: 6, lineHeight: 1.6 }}>
              让 AI 按内置 Lumen-light 设计系统直接生成完整 HTML，自动用表格/卡片/徽章等组件。更丰富，但较慢（约 1–6 分钟）、偶有版式波动；数字会做核验，请人工复核。内容很大时（尤其多表格的电子表格/多维表格）可能超时，这类建议用「套模板」。
            </div>
          )}
        </div>

        <div style={{ marginTop: 'var(--space-4)' }}>
          <label className="label">页面模板{layoutMode === 'freeform' ? '（作为版面定位）' : ''}</label>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {PAGE_TYPES.map(t => (
              <label key={t.id} style={{
                display: 'flex', gap: 10, padding: 10, alignItems: 'flex-start',
                border: '1px solid', borderColor: pageType === t.id ? 'var(--brand-500)' : 'var(--border-subtle)',
                borderRadius: 'var(--radius-lg)', background: pageType === t.id ? 'var(--brand-50)' : 'var(--surface-elevated)',
                cursor: 'pointer',
              }}>
                <input type="radio" name="page_type" value={t.id} checked={pageType === t.id} onChange={() => setPageType(t.id)} style={{ marginTop: 4 }} />
                <div>
                  <div style={{ fontWeight: 500 }}>{t.label}</div>
                  <div style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>{t.desc}</div>
                </div>
              </label>
            ))}
          </div>
          {pageType === 'custom' && (
            <textarea className="textarea" style={{ width: '100%', marginTop: 8 }} rows={3}
                      placeholder="用自然语言描述你想要的页面，例如：做一页面向客户的产品介绍，突出三大卖点与一句话定位"
                      value={customInstruction} onChange={e => setCustomInstruction(e.target.value)} />
          )}
        </div>

        <div style={{ marginTop: 'var(--space-5)', display: 'flex', gap: 8 }}>
          {srcMode === 'feishu' ? (
            <button className="btn btn-primary" disabled={docTokens.length === 0 || running || (pageType === 'custom' && !customInstruction.trim())} onClick={start}>
              <Icon name="sparkle" size={14} /> {running ? '生成中…' : '生成 HTML 页面'}
            </button>
          ) : (
            <button className="btn btn-primary" disabled={localPaths.length === 0 || running || (pageType === 'custom' && !customInstruction.trim())} onClick={startLocal}>
              <Icon name="sparkle" size={14} /> {running ? '生成中…' : '生成 HTML 页面'}
            </button>
          )}
          {taskId && (
            <button className="btn btn-secondary" onClick={downloadHtml} disabled={!task?.result_path}>
              <Icon name="external" size={14} /> 下载
            </button>
          )}
        </div>

        {/* 日志 */}
        {(running || logs.length > 0) && (
          <div style={{ marginTop: 'var(--space-6)' }}>
            <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, textTransform: 'uppercase', color: 'var(--text-tertiary)', marginBottom: 8 }}>运行日志</div>
            <div ref={logRef} style={{
              background: 'var(--surface-dark)', color: '#cad7e1',
              borderRadius: 'var(--radius-md)', padding: 12,
              fontFamily: 'var(--font-mono)', fontSize: 12, lineHeight: 1.6,
              maxHeight: 320, overflowY: 'auto',
            }}>
              {logs.map((l, i) => (
                <div key={i} style={{ color: l.level === 'error' ? '#FF8B8B' : l.level === 'warn' ? '#FFD466' : '#9BF1BD' }}>
                  <span style={{ color: '#737A82', marginRight: 8 }}>{l.ts?.slice(11)}</span>
                  <span>{l.message}</span>
                </div>
              ))}
              {running && <div style={{ color: '#5FBA89' }} className="pulse">▌运行中…</div>}
            </div>
          </div>
        )}
      </div>

      {/* 右：预览 */}
      <div style={{ display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        <div style={{
          padding: '12px 24px', borderBottom: '1px solid var(--border-subtle)',
          display: 'flex', alignItems: 'center', gap: 12, background: 'var(--surface-elevated)',
        }}>
          <span className="mono" style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>
            {taskId ? `task ${taskId}` : '尚未开始任务'}
          </span>
          {task?.status && (
            <span className={`badge badge-${task.status === 'preview' ? 'info' : task.status === 'done' ? 'success' : task.status === 'failed' ? 'error' : 'brand'}`}>
              {task.status}
            </span>
          )}
          {usedLabel && (
            <span style={{
              display: 'inline-flex', alignItems: 'center', gap: 6,
              padding: '3px 10px', background: 'var(--brand-50)', color: 'var(--brand-700)',
              border: '1px solid var(--brand-200)', borderRadius: 'var(--radius-full)', fontSize: 12,
            }} title="本次生成使用的页面模板">
              <Icon name="page" size={12} /> 模板：{usedLabel}
            </span>
          )}
          {usedLayout === 'freeform' && (
            <span style={{
              display: 'inline-flex', alignItems: 'center', gap: 6,
              padding: '3px 10px', background: 'var(--brand-500)', color: '#fff',
              borderRadius: 'var(--radius-full)', fontSize: 12,
            }} title="本次使用 AI 直出的自由版式">
              <Icon name="sparkle" size={12} /> 自由版式
            </span>
          )}
          <div style={{ flex: 1 }} />
        </div>

        {/* 自定义模板：展示用户当时给出的自然语言要求 */}
        {usedPageType === 'custom' && usedCustom && (
          <div style={{
            padding: '8px 24px', borderBottom: '1px solid var(--border-subtle)',
            background: 'var(--surface-elevated)', fontSize: 12, color: 'var(--text-tertiary)',
            display: 'flex', gap: 8, alignItems: 'baseline',
          }}>
            <span style={{ fontWeight: 600, whiteSpace: 'nowrap' }}>自定义要求</span>
            <span style={{ color: 'var(--text-primary)', whiteSpace: 'pre-wrap' }}>{usedCustom}</span>
          </div>
        )}

        <div style={{ flex: 1, background: 'var(--surface-inset)', overflow: 'hidden' }}>
          {previewUrl ? (
            <iframe src={previewUrl} title="preview"
                    style={{ width: '100%', height: '100%', border: 'none', background: '#fff' }} />
          ) : (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', color: 'var(--text-tertiary)', flexDirection: 'column', gap: 12 }}>
              <Icon name="page" size={48} />
              <div>左侧填写参数后，预览将在此显示</div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default TaskHtmlPage;
