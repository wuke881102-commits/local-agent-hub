import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import useSWR from 'swr';
import { Icon } from '../components/icons';
import { ChartCard } from '../components/Charts';
import { Markdown } from '../components/Markdown';
import { api, fetcher, errMsg, TaskDetail, subscribeTask } from '../api';
import WritebackModal from '../modals/WritebackModal';
import { useToast } from '../components/Toast';
import LocalSourcePicker, { SourceTabs, DIR_KEY, FileKind } from '../components/LocalSourcePicker';

type LogEntry = { ts: string; level: string; message: string };

// 支持「本地目录」数据源的 Agent → 限定可选文件类型（不传=全部）
const LOCAL_KINDS: Record<string, FileKind[] | undefined> = {
  'base-analysis': ['excel'],
  'pdf-recognition': ['pdf'],
  'collab-dispatch': ['pdf', 'word', 'excel', 'ppt'],
};

interface AgentMeta {
  id: string;
  scene: string;
  title: string;
  subtitle: string;
  buttonLabel: string;
}

const AGENT_META: Record<string, AgentMeta> = {
  'document-map': {
    id: 'document-map',
    scene: '知识库治理',
    title: '文档地图',
    subtitle: '刷新本地索引，并按来源 / 类型 / 所有者 / 活跃度 / 创建时段 / 空间 / AI 分类自动分组（瞬时，规则计算，点数字即可下钻）。',
    buttonLabel: '刷新文档地图',
  },
  'index-enrich': {
    id: 'index-enrich',
    scene: '知识库治理',
    title: '摘要 / 标签回填',
    subtitle: '用 qwen3.6-flash 读元信息，为每篇资产生成一句话摘要、分类与主题标签，回填本地索引——让搜索与列表预览立刻变有用。',
    buttonLabel: '开始回填',
  },
  'knowledge-governance': {
    id: 'knowledge-governance',
    scene: '知识库治理',
    title: '知识治理',
    subtitle: '打开即按 180 天即时分流（规则，瞬时不联网）。改失修阈值或点「重新分流」即时重算；点「LLM 复核」会先联飞书刷新一次，再让大模型逐条核对归档候选、给更准的理由。',
    buttonLabel: 'LLM 复核归档候选',
  },
  'base-analysis': {
    id: 'base-analysis',
    scene: '表格分析',
    title: '多维表格分析',
    subtitle: '读取多维表格 / 电子表格的结构与数据，做列画像与数据质量体检，再由 AI 规划图型、用 ECharts / 甘特 / 架构图渲染成图表看板，并给出报表建议。',
    buttonLabel: '开始分析',
  },
  'pdf-recognition': {
    id: 'pdf-recognition',
    scene: 'PDF 识别',
    title: 'PDF 识别',
    subtitle: '下载飞书云盘里的 PDF，做全文抽取（含扫描件 OCR）、关键字段抽取、表格识别与逐页要点 / 图表说明。',
    buttonLabel: '开始识别',
  },
  'meeting-minutes': {
    id: 'meeting-minutes',
    scene: '会议沉淀',
    title: '会议纪要',
    subtitle: '读取妙记转写或会议记录文档，整理出会议摘要、决策、行动项（负责人 / 截止）与风险，确认后可沉淀回飞书。',
    buttonLabel: '开始整理',
  },
  'collab-dispatch': {
    id: 'collab-dispatch',
    scene: '协作分发',
    title: '协作分发',
    subtitle: '把其他 Agent 的产出改写为飞书群消息与任务草稿，自动判别通知 / 摘要 / 待办性质，经你确认后再分发到指定群与负责人。',
    buttonLabel: '生成草稿',
  },
};

// 仅登记骨架、核心逻辑待实现的 Agent：点开显示占位页，不渲染运行区。（已全部实现，留空集合备用。）
const STUB_AGENTS = new Set<string>([]);
const STUB_ICON: Record<string, string> = {};

// 哪些 Agent 的产出值得「协作分发」——有可发的消息 / 可建的任务，且能在下拉里区分。
// 排除：文档地图 / 摘要回填（索引维护，无可分发条目）、协作分发自身、
// 知识治理（全库扫描无单个对象，列表里都是「· —」分不清，价值也模糊）。
const DISPATCH_SOURCE_AGENTS = new Set([
  'meeting-minutes', 'base-analysis', 'pdf-recognition', 'html-page',
]);

// 多维表格分析的「推荐模板」：同一套确定性画像，模板只改 AI 解读的侧重。
// 与后端 prompts.CHART_ANALYSIS_TEMPLATES 一一对应。出图导向：AI 读数据规划图型，
// 数据图由 ECharts 精确渲染、甘特用 Mermaid、架构/关系图用 GPT-Image-1 生图。
const ANALYSIS_TEMPLATES = [
  { id: 'auto',         label: '智能图表', desc: 'AI 读数据自动挑 2–4 张最合适的图（柱/饼/折线/散点）（推荐）' },
  { id: 'trend',        label: '趋势 / XY', desc: '折线 / 面积看时间趋势，散点看两数值列关系' },
  { id: 'composition',  label: '构成 / 占比', desc: '饼图 / 环形 / 分组柱，看分类构成与占比' },
  { id: 'ranking',      label: '对比 / 排行', desc: '柱状 / 条形按度量排序，多指标对比（如预算 vs 实际）' },
  { id: 'gantt',        label: '项目甘特图', desc: '识别开始/结束/状态列，生成甘特图（项目进度类表）' },
  { id: 'architecture', label: '架构 / 关系图', desc: '用 GPT-Image-1 生成架构图 / 流程图 / 关系图（概念性）' },
  { id: 'custom',       label: '自定义', desc: '用自然语言描述你想要的图' },
];

// PDF 识别的「识别模板」：同一套确定性抽取，模板只改 AI 归纳的侧重 + 页面显示哪些板块。
// 与后端 prompts.PDF_RECOGNITION_TEMPLATES 一一对应。
const PDF_TEMPLATES = [
  { id: 'summary',  label: '全文摘要', desc: '通读全文，给摘要、要点与逐页要点（推荐）' },
  { id: 'fields',   label: '关键字段抽取', desc: '抽取编号 / 各方 / 金额 / 日期等关键字段 + 表格' },
  { id: 'contract', label: '合同台账', desc: '合同要素 + 自动按年测算金额合计' },
  { id: 'pages',    label: '逐页要点', desc: '逐页列出每页核心内容与图表说明' },
  { id: 'custom',   label: '自定义', desc: '用自然语言描述你想要的识别结果' },
];

const TaskAgentPage: React.FC = () => {
  const params = useParams();
  const nav = useNavigate();
  const toast = useToast();
  const agentId = params.agentId as string;
  const existingTaskId = params.taskId;

  const meta = AGENT_META[agentId];
  if (!meta) {
    return <div style={{ padding: 32 }}>未知 Agent: {agentId}</div>;
  }

  // 各 Agent 的输入
  const [staleDays, setStaleDays] = useState(180);
  const [skipRefresh, setSkipRefresh] = useState(false);
  const [mineOnly, setMineOnly] = useState(true);
  const [forceEnrich, setForceEnrich] = useState(false);

  const [searchParams] = useSearchParams();

  // 数据来源（仅 base-analysis / pdf-recognition / collab-dispatch 用）：飞书 / 本地目录。
  // 可由 ?src=local&local_path=<路径> 从「本地目录」深链带入并预选。
  const supportsLocal = agentId in LOCAL_KINDS;
  const seedLocalPath = searchParams.get('local_path') || '';
  const seedLocal = supportsLocal && searchParams.get('src') === 'local' && !!seedLocalPath;
  const seedLocalDir = seedLocal ? seedLocalPath.replace(/[\\/][^\\/]*$/, '') : '';
  const [srcMode, setSrcMode] = useState<'feishu' | 'local'>(seedLocal ? 'local' : 'feishu');
  const [localDir, setLocalDir] = useState<string>(() => { try { return seedLocalDir || localStorage.getItem(DIR_KEY) || ''; } catch { return seedLocalDir; } });
  const [localPaths, setLocalPaths] = useState<string[]>(seedLocal ? [seedLocalPath] : []);
  const isLocal = supportsLocal && srcMode === 'local';

  // base-analysis：要分析的表（可由 ?asset_id=&type= 深链带入）
  const [selAssetId, setSelAssetId] = useState<string>(searchParams.get('asset_id') || '');
  // base-analysis：分析模板（决定 AI 解读侧重；切子表 / 重跑沿用当前选择）
  const [analysisTemplate, setAnalysisTemplate] = useState('auto');
  const [customAnalysis, setCustomAnalysis] = useState('');  // 「自定义」模板下的自然语言要求
  // pdf-recognition：识别模板（决定 AI 归纳侧重与显示哪些板块）
  const [pdfTemplate, setPdfTemplate] = useState('summary');
  const [customPdf, setCustomPdf] = useState('');            // 「自定义」模板下的自然语言要求
  const { data: baseList } = useSWR<{ items: any[] }>(
    agentId === 'base-analysis' ? '/api/assets?type=base&limit=500' : null, fetcher);
  const { data: sheetList } = useSWR<{ items: any[] }>(
    agentId === 'base-analysis' ? '/api/assets?type=sheet&limit=500' : null, fetcher);
  const tableAssets = useMemo(
    () => [...(baseList?.items || []), ...(sheetList?.items || [])],
    [baseList, sheetList]);
  const selAsset = useMemo(
    () => tableAssets.find(a => a.asset_id === selAssetId),
    [tableAssets, selAssetId]);
  const selType = (selAsset?.type || searchParams.get('type') || '').toLowerCase();
  const selKind = selType === 'sheet' ? 'sheet' : 'bitable';

  // pdf-recognition：云盘里的 PDF（按 .pdf 后缀从「文件」类资产里筛），或手动粘贴 token/链接
  const { data: pdfList } = useSWR<{ items: any[] }>(
    agentId === 'pdf-recognition' ? '/api/assets?type=file&limit=500' : null, fetcher);
  const pdfFiles = useMemo(
    () => (pdfList?.items || []).filter((a: any) => /\.pdf$/i.test(a.title || '')),
    [pdfList]);
  const [pdfManual, setPdfManual] = useState('');
  const [forceOcr, setForceOcr] = useState(false);
  const pdfToken = (selAssetId || pdfManual || '').trim();

  // meeting-minutes：会议来源 = 妙记（type=meeting）+ 飞书「AI 智能纪要 / 文字记录」
  // （飞书新版 AI Notes 本质是 docx 文档，不是经典妙记，所以按 docx 拉来再按标题筛）。
  const { data: meetingList } = useSWR<{ items: any[] }>(
    agentId === 'meeting-minutes' ? '/api/assets?type=meeting&limit=500' : null, fetcher);
  const { data: aiNotesList } = useSWR<{ items: any[] }>(
    agentId === 'meeting-minutes' ? '/api/assets?type=docx&limit=1000' : null, fetcher);
  const classicMinutes = meetingList?.items || [];
  const aiNotes = (aiNotesList?.items || []).filter((a: any) => {
    const t = (a.title || '').trim();
    return t.startsWith('智能纪要') || t.startsWith('文字记录');
  });
  // 合并供 start() 反查类型用（妙记→meeting，AI Notes→docx，两者 asset_id 不重叠）
  const meetingAssets = [...classicMinutes, ...aiNotes];
  const [meetingManual, setMeetingManual] = useState('');
  const meetingToken = (selAssetId || meetingManual || '').trim();

  // collab-dispatch：选一个上游任务（或填自由文本）→ 一次生成群消息 + 任务草稿；
  // 发哪些、发哪个群都在「写回飞书」弹窗里挑，所以这里只需要选素材。
  const isDispatch = agentId === 'collab-dispatch';
  const { data: recentTasks } = useSWR<{ items: any[] }>(
    isDispatch ? '/api/tasks?limit=40' : null, fetcher);
  const dispatchableTasks = useMemo(
    () => (recentTasks?.items || []).filter(
      (t: any) => ['done', 'preview'].includes(t.status) && DISPATCH_SOURCE_AGENTS.has(t.agent_id)),
    [recentTasks]);
  const [dispatchSourceId, setDispatchSourceId] = useState<string>(searchParams.get('source_task_id') || '');
  const [dispatchContent, setDispatchContent] = useState('');
  const dispatchReady = !!(dispatchSourceId || dispatchContent.trim());

  const [running, setRunning] = useState(false);
  const [taskId, setTaskId] = useState<string | null>(existingTaskId || null);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [showWriteback, setShowWriteback] = useState(false);

  const { data: task, mutate: mutateTask } = useSWR<TaskDetail>(
    taskId ? `/api/tasks/${taskId}` : null, fetcher, { refreshInterval: 4000 },
  );

  // For document-map: preload rule-based facets so the page is informative
  // before the user clicks any button. Refreshed when the task completes or
  // when the user just navigated here.
  const { data: mapData, mutate: mutateMap } = useSWR<any>(
    agentId === 'document-map' ? '/api/assets/map' : null, fetcher,
  );

  // For knowledge-governance: preload the rule-based triage so the page shows
  // results instantly (no scan click). Re-fetches when threshold / scope change.
  const { data: govData, mutate: mutateGov } = useSWR<any>(
    agentId === 'knowledge-governance' ? `/api/assets/governance?stale_days=${staleDays}&mine_only=${mineOnly}` : null,
    fetcher,
  );
  // 知识治理：分流全部基于本地索引，先取它的上次同步时间，提示数据新鲜度
  // （帮用户判断要不要先联飞书刷新，还是直接「重新分流」就够）。
  const { data: idxStats } = useSWR<any>(
    agentId === 'knowledge-governance' ? '/api/assets/stats' : null, fetcher,
  );

  const logRef = useRef<HTMLDivElement>(null);
  useEffect(() => { logRef.current?.scrollTo(0, logRef.current.scrollHeight); }, [logs]);

  // 从「运行记录」点"重试"带 ?writeback=1 进来时，待任务详情就绪后自动打开写回/分发确认弹窗。
  // 只自动开一次（用户关掉后不再弹）。
  const autoWbRef = useRef(false);
  useEffect(() => {
    if (autoWbRef.current || searchParams.get('writeback') !== '1') return;
    const wb = (task as any)?.writeback;
    if (wb && (wb.status === 'pending' || wb.status === 'failed')) {
      autoWbRef.current = true;
      setShowWriteback(true);
    }
  }, [task, searchParams]);

  // 从「会议纪要」等结果页点「分发 / 沉淀飞书」带 ?source_task_id=&auto=1 进来：
  // 自动跑一次协作分发（生成含「沉淀文档」的草稿），跑完自动弹出写回确认（见上方 ?writeback=1）。
  const autoDispatchRef = useRef(false);
  useEffect(() => {
    if (!isDispatch || autoDispatchRef.current) return;
    if (searchParams.get('auto') !== '1' || !dispatchSourceId) return;
    if (taskId || running) return;
    autoDispatchRef.current = true;
    start();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isDispatch, dispatchSourceId, taskId, running, searchParams]);

  useEffect(() => {
    if (!taskId) return;
    setLogs([]);
    const close = subscribeTask(
      taskId,
      (entry) => {
        if (entry._done) { setRunning(false); mutateTask(); mutateMap?.(); return; }
        if (entry._keepalive) return;
        setLogs(prev => [...prev, entry as LogEntry]);
      },
      ({ error }) => {
        setRunning(false);
        mutateTask();
        mutateMap?.();
        if (error) toast.warning('实时日志连接中断', { detail: '任务可能仍在后台运行，页面将继续轮询其状态。' });
      },
    );
    return close;
  }, [taskId, mutateTask, mutateMap, toast]);

  async function start(targetId?: string) {
    // 多个调用点直接传事件对象（onClick={start}），这里只接受字符串作为子表 id。
    if (typeof targetId !== 'string') targetId = undefined;
    setRunning(true);
    setLogs([]);
    const inputs: Record<string, any> = {};
    if (agentId === 'knowledge-governance') {
      inputs.stale_days = staleDays;
      inputs.skip_refresh = skipRefresh;
      inputs.mine_only = mineOnly;
    } else if (agentId === 'document-map') {
      inputs.skip_refresh = skipRefresh;
    } else if (agentId === 'index-enrich') {
      inputs.force = forceEnrich;
    } else if (agentId === 'base-analysis') {
      if (analysisTemplate === 'custom' && !customAnalysis.trim()) { setRunning(false); return; }
      if (isLocal) {
        if (!localPaths[0]) { setRunning(false); return; }
        inputs.local_path = localPaths[0];
      } else {
        if (!selAssetId) { setRunning(false); return; }
        inputs.asset_id = selAssetId;
        if (selType) inputs.asset_type = selType;
        if (targetId) {
          if (selKind === 'sheet') inputs.sheet_id = targetId;
          else inputs.table_id = targetId;
        }
      }
      inputs.template = analysisTemplate;
      if (analysisTemplate === 'custom') inputs.custom_instruction = customAnalysis.trim();
    } else if (agentId === 'pdf-recognition') {
      if (pdfTemplate === 'custom' && !customPdf.trim()) { setRunning(false); return; }
      if (isLocal) {
        if (!localPaths[0]) { setRunning(false); return; }
        inputs.local_path = localPaths[0];
      } else {
        const tok = (selAssetId || pdfManual || '').trim();
        if (!tok) { setRunning(false); return; }
        inputs.asset_id = tok;
        inputs.asset_type = 'file';
      }
      inputs.template = pdfTemplate;
      if (pdfTemplate === 'custom') inputs.custom_instruction = customPdf.trim();
      if (forceOcr) inputs.force_ocr = true;
    } else if (agentId === 'meeting-minutes') {
      const tok = (selAssetId || meetingManual || '').trim();
      if (!tok) { setRunning(false); return; }
      inputs.asset_id = tok;
      const at = (meetingAssets.find((a: any) => a.asset_id === tok)?.type) || '';
      if (at) inputs.asset_type = at;
    } else if (agentId === 'collab-dispatch') {
      if (isLocal) {
        if (!localPaths[0]) { setRunning(false); return; }
        try {
          const ex = await api.get<{ markdown: string; name: string }>(`/api/localdir/extract?path=${encodeURIComponent(localPaths[0])}`);
          const md = (ex.markdown || '').trim();
          if (!md) { setRunning(false); toast.error('该文件未抽取到文本'); return; }
          inputs.content = `# 来自本地文件：${ex.name}\n\n${md.slice(0, 8000)}`;
        } catch (e) { setRunning(false); toast.error('读取本地文件失败', { detail: errMsg(e) }); return; }
      } else {
        if (!dispatchReady) { setRunning(false); return; }
        // 二选一：选了任务就只发任务，否则发自由文本。
        if (dispatchSourceId) inputs.source_task_id = dispatchSourceId;
        else if (dispatchContent.trim()) inputs.content = dispatchContent.trim();
      }
    }
    try {
      const resp = await api.post<{ task_id: string }>('/api/tasks/run', {
        agent_id: agentId,
        scene: meta.scene,
        inputs,
      });
      setTaskId(resp.task_id);
      // 自动分发流程：跑完直接进入写回确认（?writeback=1 触发上方自动弹窗）。
      const autoWb = autoDispatchRef.current && agentId === 'collab-dispatch';
      nav(`/task/${agentId}/${resp.task_id}${autoWb ? '?writeback=1' : ''}`, { replace: true });
    } catch (err) {
      setRunning(false);
      toast.error(`启动「${meta.title}」失败`, { detail: errMsg(err) });
    }
  }

  // For document-map we prefer the agent's payload (post-task) but fall back
  // to the always-fresh /api/assets/map facets so the page is never blank.
  // gov: 用 LLM 任务结果，但仅当它与当前阈值/范围匹配；否则回退到即时规则分流。
  const govTaskMatches = !!task?.payload
    && task.payload.metrics?.stale_days_threshold === staleDays
    && !!task.payload.metrics?.mine_only === mineOnly;
  const payload = (agentId === 'document-map'
    ? (task?.payload || mapData)
    : agentId === 'knowledge-governance'
      ? (govTaskMatches ? task!.payload : govData)
      : task?.payload);
  // 文档地图已简化为"刷新 + 规则分组"，不再需要左侧参数栏：改为单列布局，
  // 把刷新动作放到结果区顶部（盘点摘要上方），默认刷新索引。
  const isDocMap = agentId === 'document-map';

  // 运行按钮可点判定（统一处理「飞书 / 本地目录」两种数据源）
  const localReady = localPaths.length > 0;
  let canRun = true;
  if (agentId === 'base-analysis') canRun = (isLocal ? localReady : !!selAssetId) && !(analysisTemplate === 'custom' && !customAnalysis.trim());
  else if (agentId === 'pdf-recognition') canRun = (isLocal ? localReady : !!pdfToken) && !(pdfTemplate === 'custom' && !customPdf.trim());
  else if (agentId === 'meeting-minutes') canRun = !!meetingToken;
  else if (agentId === 'collab-dispatch') canRun = isLocal ? localReady : dispatchReady;

  // 模板标识 + 自定义要求：统一放在结果区顶栏（与「HTML 页面生成」一致），不再塞进各结果卡片。
  const isTemplatedAgent = agentId === 'base-analysis' || agentId === 'pdf-recognition';
  const tplOptions = agentId === 'pdf-recognition' ? PDF_TEMPLATES : ANALYSIS_TEMPLATES;
  const usedTemplate: string | undefined = payload?.template;
  const usedTemplateLabel = tplOptions.find(t => t.id === usedTemplate)?.label || usedTemplate;
  const usedCustomInstruction: string = (task?.inputs?.custom_instruction || '').trim();

  // 占位 Agent（Phase B）：核心逻辑尚未实现，给一个诚实的「建设中」落地页，
  // 而不是把运行按钮放出来跑出一个空结果。
  if (STUB_AGENTS.has(agentId)) {
    return (
      <div style={{ height: '100%', overflowY: 'auto', background: 'var(--surface-page)', padding: 'var(--space-6)' }}>
        <div style={{ maxWidth: 560, margin: '8vh auto 0' }}>
          <div className="card" style={{ padding: 32, textAlign: 'center', borderTop: '3px solid var(--brand-500)' }}>
            <div style={{
              width: 56, height: 56, borderRadius: 16, margin: '0 auto 16px',
              background: 'var(--tint-brand)', color: 'var(--brand-700)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}>
              <Icon name={STUB_ICON[agentId] || 'sparkle'} size={26} />
            </div>
            <h2 style={{ margin: '0 0 8px', fontSize: 20, fontWeight: 600 }}>{meta.title}</h2>
            <span className="badge badge-info">Phase B · 建设中</span>
            <p style={{ color: 'var(--text-secondary)', lineHeight: 1.7, marginTop: 16 }}>{meta.subtitle}</p>
            <div style={{ marginTop: 8, fontSize: 13, color: 'var(--text-tertiary)' }}>
              该 Agent 已登记、骨架就绪，核心逻辑将在下一阶段实现。当前点开仅作占位预览，暂不可运行。
            </div>
            <button className="btn btn-tonal" style={{ marginTop: 20 }} onClick={() => nav('/scenes')}>
              返回任务场景
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div style={{ display: 'grid', gridTemplateColumns: isDocMap ? '1fr' : '380px 1fr', height: '100%', overflow: 'hidden' }}>
      {!isDocMap && (
      <div style={{ borderRight: '1px solid var(--border-subtle)', padding: 'var(--space-6)', overflowY: 'auto', background: 'var(--surface-elevated)' }}>
        <h2 style={{ marginTop: 0, fontSize: 18, fontWeight: 600 }}>{meta.title}</h2>
        <p style={{ color: 'var(--text-tertiary)', fontSize: 13 }}>{meta.subtitle}</p>

        {(agentId === 'document-map' || agentId === 'knowledge-governance') && (
          <>
            {agentId === 'knowledge-governance' && (
              <>
                <div style={{ marginTop: 'var(--space-4)' }}>
                  <label className="label" title="超过这么多天没有任何更新的文档，视为「失修」并纳入治理分流">失修阈值（天）</label>
                  <input className="input" type="number" min={30} max={3650}
                         value={staleDays} onChange={e => setStaleDays(parseInt(e.target.value) || 180)} />
                  <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginTop: 4, lineHeight: 1.5 }}>
                    超过这么多天没更新的文档算「失修」纳入治理。常用：90 = 一季度、180 = 半年、365 = 一年。改这里会即时重新分流。
                  </div>
                </div>
                <label style={{ display: 'flex', gap: 8, alignItems: 'flex-start', marginTop: 'var(--space-4)', fontSize: 13 }}>
                  <input type="checkbox" checked={mineOnly} onChange={e => setMineOnly(e.target.checked)} style={{ marginTop: 3 }} />
                  <span>
                    <strong>只治理我创建的文档</strong>
                    <div style={{ color: 'var(--text-tertiary)', fontSize: 12 }}>
                      别人共享 / 知识库的文档你无权归档，默认只看 owner 是你本人的。取消勾选则扫描全部。
                    </div>
                  </span>
                </label>
              </>
            )}
          </>
        )}

        {agentId === 'index-enrich' && (
          <label style={{ display: 'flex', gap: 8, alignItems: 'flex-start', marginTop: 'var(--space-4)', fontSize: 13 }}>
            <input type="checkbox" checked={forceEnrich} onChange={e => setForceEnrich(e.target.checked)} style={{ marginTop: 3 }} />
            <span>
              <strong>强制重跑</strong>（覆盖已回填的资产）
              <div style={{ color: 'var(--text-tertiary)', fontSize: 12 }}>
                默认只补"还没回填"的资产，速度快。勾选后会对全部资产重新生成摘要 / 分类 / 标签——调整 prompt 或想刷新结果时再用。
              </div>
            </span>
          </label>
        )}

        {agentId === 'base-analysis' && (
          <div style={{ marginTop: 'var(--space-4)' }}>
            <SourceTabs mode={srcMode} onChange={setSrcMode} />
            {srcMode === 'feishu' ? (<div style={{ marginTop: 'var(--space-4)' }}>
            <label className="label">选择要分析的表</label>
            <select className="input" value={selAssetId} onChange={e => setSelAssetId(e.target.value)} style={{ width: '100%' }}>
              <option value="">— 选择多维表格 / 电子表格 —</option>
              <optgroup label={`多维表格（${(baseList?.items || []).length}）`}>
                {(baseList?.items || []).map(a => (
                  <option key={a.asset_id} value={a.asset_id}>{a.title}</option>
                ))}
              </optgroup>
              <optgroup label={`电子表格（${(sheetList?.items || []).length}）`}>
                {(sheetList?.items || []).map(a => (
                  <option key={a.asset_id} value={a.asset_id}>{a.title}</option>
                ))}
              </optgroup>
            </select>
            <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginTop: 6, lineHeight: 1.5 }}>
              共 {tableAssets.length} 张表可分析。也可从「飞书文档」页对某张表点「分析」直达。
              {selAsset && <div style={{ marginTop: 4 }}>已选：<strong>{selAsset.title}</strong>（{selKind === 'sheet' ? '电子表格' : '多维表格'}）</div>}
            </div>
            </div>) : (<div style={{ marginTop: 'var(--space-4)' }}>
              <LocalSourcePicker dir={localDir} onDirChange={setLocalDir} multiple={false} kinds={LOCAL_KINDS['base-analysis']} selected={localPaths} onSelChange={setLocalPaths} />
              <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginTop: 6, lineHeight: 1.5 }}>
                选本地的 Excel（.xlsx）或 CSV 文件。每个工作表逐张做列画像与 AI 出图。
                {localPaths[0] && <div style={{ marginTop: 4 }}>已选：<strong>{localPaths[0].split(/[\\/]/).pop()}</strong></div>}
              </div>
            </div>)}

            <div style={{ marginTop: 'var(--space-4)' }}>
              <label className="label">分析模板</label>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {ANALYSIS_TEMPLATES.map(t => (
                  <label key={t.id} style={{
                    display: 'flex', gap: 10, padding: 10, alignItems: 'flex-start',
                    border: '1px solid', borderColor: analysisTemplate === t.id ? 'var(--brand-500)' : 'var(--border-subtle)',
                    borderRadius: 'var(--radius-lg)', background: analysisTemplate === t.id ? 'var(--brand-50)' : 'var(--surface-elevated)',
                    cursor: 'pointer',
                  }}>
                    <input type="radio" name="analysis_template" value={t.id} checked={analysisTemplate === t.id} onChange={() => setAnalysisTemplate(t.id)} style={{ marginTop: 4 }} />
                    <div>
                      <div style={{ fontWeight: 500 }}>{t.label}</div>
                      <div style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>{t.desc}</div>
                    </div>
                  </label>
                ))}
              </div>
              {analysisTemplate === 'custom' && (
                <textarea className="textarea" style={{ width: '100%', marginTop: 8 }} rows={3}
                          placeholder="用自然语言描述你想要的分析，例如：找出成交额最高的前 5 个地区并说明可能原因；或：检查金额列是否有异常值"
                          value={customAnalysis} onChange={e => setCustomAnalysis(e.target.value)} />
              )}
            </div>
          </div>
        )}

        {agentId === 'pdf-recognition' && (
          <div style={{ marginTop: 'var(--space-4)' }}>
            <SourceTabs mode={srcMode} onChange={setSrcMode} />
            {srcMode === 'feishu' ? (<div style={{ marginTop: 'var(--space-4)' }}>
            <label className="label">选择云盘里的 PDF</label>
            <select className="input" value={selAssetId} onChange={e => setSelAssetId(e.target.value)} style={{ width: '100%' }}>
              <option value="">— 选择 PDF 文件 —</option>
              {pdfFiles.map((a: any) => (
                <option key={a.asset_id} value={a.asset_id}>{a.title}</option>
              ))}
            </select>
            <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginTop: 6, lineHeight: 1.5 }}>
              共 {pdfFiles.length} 个 PDF（来自本地索引的云盘文件）。索引里没有？可在下方手动粘贴文件 token 或链接。
            </div>
            <label className="label" style={{ marginTop: 12, display: 'block' }}>或手动输入 file_token / 链接</label>
            <input className="input" style={{ width: '100%' }} placeholder="boxcn… 或 https://…/file/boxcn…"
                   value={pdfManual} onChange={e => setPdfManual(e.target.value)} disabled={!!selAssetId} />
            </div>) : (<div style={{ marginTop: 'var(--space-4)' }}>
              <LocalSourcePicker dir={localDir} onDirChange={setLocalDir} multiple={false} kinds={LOCAL_KINDS['pdf-recognition']} selected={localPaths} onSelChange={setLocalPaths} />
              <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginTop: 6, lineHeight: 1.5 }}>
                选本地目录里的 PDF 做识别（全文抽取 / 字段 / 表格 / 逐页要点）。
                {localPaths[0] && <div style={{ marginTop: 4 }}>已选：<strong>{localPaths[0].split(/[\\/]/).pop()}</strong></div>}
              </div>
            </div>)}

            <div style={{ marginTop: 'var(--space-4)' }}>
              <label className="label">识别模板</label>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {PDF_TEMPLATES.map(t => (
                  <label key={t.id} style={{
                    display: 'flex', gap: 10, padding: 10, alignItems: 'flex-start',
                    border: '1px solid', borderColor: pdfTemplate === t.id ? 'var(--brand-500)' : 'var(--border-subtle)',
                    borderRadius: 'var(--radius-lg)', background: pdfTemplate === t.id ? 'var(--brand-50)' : 'var(--surface-elevated)',
                    cursor: 'pointer',
                  }}>
                    <input type="radio" name="pdf_template" value={t.id} checked={pdfTemplate === t.id} onChange={() => setPdfTemplate(t.id)} style={{ marginTop: 4 }} />
                    <div>
                      <div style={{ fontWeight: 500 }}>{t.label}</div>
                      <div style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>{t.desc}</div>
                    </div>
                  </label>
                ))}
              </div>
              {pdfTemplate === 'custom' && (
                <textarea className="textarea" style={{ width: '100%', marginTop: 8 }} rows={3}
                          placeholder="用自然语言描述你想要识别的内容，例如：抽取所有金额与对应条款；或：总结每一章的结论"
                          value={customPdf} onChange={e => setCustomPdf(e.target.value)} />
              )}
            </div>

            <label style={{ display: 'flex', gap: 8, alignItems: 'flex-start', marginTop: 12, fontSize: 13 }}>
              <input type="checkbox" checked={forceOcr} onChange={e => setForceOcr(e.target.checked)} style={{ marginTop: 3 }} />
              <span>
                <strong>强制逐页 OCR</strong>
                <div style={{ color: 'var(--text-tertiary)', fontSize: 12 }}>
                  默认自动判断：有文字层直接抽取，扫描页才用视觉 OCR。勾选则所有页都走视觉模型（更慢，文字层错乱时再用）。
                </div>
              </span>
            </label>
          </div>
        )}

        {agentId === 'meeting-minutes' && (
          <div style={{ marginTop: 'var(--space-4)' }}>
            <label className="label">选择会议来源</label>
            <select className="input" value={selAssetId} onChange={e => setSelAssetId(e.target.value)} style={{ width: '100%' }}>
              <option value="">— 选择会议来源 —</option>
              {classicMinutes.length > 0 && (
                <optgroup label="妙记（Lark Minutes）">
                  {classicMinutes.map((a: any) => (
                    <option key={a.asset_id} value={a.asset_id}>{a.title}</option>
                  ))}
                </optgroup>
              )}
              {aiNotes.length > 0 && (
                <optgroup label="AI 智能纪要 / 文字记录（飞书文档）">
                  {aiNotes.map((a: any) => (
                    <option key={a.asset_id} value={a.asset_id}>{a.title}</option>
                  ))}
                </optgroup>
              )}
            </select>
            <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginTop: 6, lineHeight: 1.5 }}>
              共 {classicMinutes.length} 个妙记 + {aiNotes.length} 篇 AI 智能纪要 / 文字记录文档（来自本地索引）。
              {classicMinutes.length === 0 && aiNotes.length > 0 && (
                <>　你没有经典「妙记」，但飞书的「AI Notes / 智能纪要」本质是文档，已自动列在上面，可直接选。</>
              )}
              <br/>其它<strong>会议记录文档</strong>（飞书文档）也支持——把它的链接粘到下面。
            </div>
            <label className="label" style={{ marginTop: 12, display: 'block' }}>或粘贴妙记 / 会议文档链接或 token</label>
            <input className="input" style={{ width: '100%' }} placeholder="https://…/minutes/obcn… 或 https://…/docx/…"
                   value={meetingManual} onChange={e => setMeetingManual(e.target.value)} disabled={!!selAssetId} />
            <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginTop: 6, lineHeight: 1.5 }}>
              妙记转写需要相应读取权限；取不到时会自动提示改用会议记录文档。整理结果可一键沉淀回飞书新建文档。
            </div>
          </div>
        )}

        {agentId === 'collab-dispatch' && (
          <div style={{ marginTop: 'var(--space-4)' }}>
            <SourceTabs mode={srcMode} onChange={setSrcMode} feishuLabel="任务 / 文本" />
            {srcMode === 'feishu' ? (<div style={{ marginTop: 'var(--space-4)' }}>
            <label className="label">分发素材来源（二选一）</label>
            <select className="input" value={dispatchSourceId} disabled={!!dispatchContent.trim()} onChange={e => setDispatchSourceId(e.target.value)} style={{ width: '100%' }}>
              <option value="">{dispatchableTasks.length ? '— 选择一个已完成的任务 —' : '— 暂无可分发的任务 —'}</option>
              {dispatchableTasks.map((t: any) => (
                <option key={t.id} value={t.id}>{AGENT_META[t.agent_id]?.title || t.agent_id} · {t.target || t.id}</option>
              ))}
            </select>
            <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginTop: 6, lineHeight: 1.5 }}>
              只列出有可分发产出的任务（会议纪要 / 表格分析 / PDF 识别 / HTML 页面）。会议纪要的行动项会原样带出。与下方自由文本<strong>二选一</strong>{dispatchContent.trim() ? '（已填自由文本，下拉已禁用）' : ''}。
            </div>
            <label className="label" style={{ marginTop: 12, display: 'block' }}>或，自由文本素材</label>
            <textarea className="textarea" style={{ width: '100%' }} rows={4}
                      placeholder={dispatchSourceId ? '已选择上方任务；如要改用自由文本，请先把上方下拉清空' : '粘贴要分发的内容：会议结论、待办、通知…'}
                      value={dispatchContent} disabled={!!dispatchSourceId} onChange={e => setDispatchContent(e.target.value)} />
            <div style={{ marginTop: 14, fontSize: 12, color: 'var(--text-tertiary)', lineHeight: 1.7, background: 'var(--surface-subtle)', padding: 10, borderRadius: 8 }}>
              会同时生成<strong>群消息</strong>和<strong>任务</strong>草稿。点「生成草稿」后，在「写回飞书」弹窗里逐项挑：建哪些任务、要不要发群消息、<strong>发到哪个群</strong>——确认后才真正执行。
            </div>
            </div>) : (<div style={{ marginTop: 'var(--space-4)' }}>
              <LocalSourcePicker dir={localDir} onDirChange={setLocalDir} multiple={false} kinds={LOCAL_KINDS['collab-dispatch']} selected={localPaths} onSelChange={setLocalPaths} />
              <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginTop: 6, lineHeight: 1.5 }}>
                选本地目录里的文件（PDF/Word/Excel/PPT）作分发素材：抽取其文本后生成群消息 + 任务草稿。
                {localPaths[0] && <div style={{ marginTop: 4 }}>已选：<strong>{localPaths[0].split(/[\\/]/).pop()}</strong></div>}
              </div>
            </div>)}
          </div>
        )}

        {agentId === 'knowledge-governance' ? (
          <div style={{ marginTop: 'var(--space-5)' }}>
            {/* 新鲜度提示：两个动作都基于本地索引，先让用户知道它有多新 */}
            <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 12, lineHeight: 1.6 }}>
              {idxStats?.last_refreshed
                ? <>本地索引上次同步飞书：<strong>{String(idxStats.last_refreshed).replace('T', ' ').slice(0, 16)}</strong>。下面两个动作都基于它。</>
                : '下面两个动作都基于现有本地索引。'}
            </div>

            {/* 动作一：本地重算，瞬时、不联网、不用 AI */}
            <button className="btn btn-primary" disabled={running} onClick={() => mutateGov?.()}
                    title="用现有本地索引、按当前失修阈值重新分档。瞬时完成，不联飞书、不调用大模型。">
              <Icon name="refresh" size={14} /> 重新分流（本地 · 瞬时）
            </button>
            <div style={{ fontSize: 12, color: 'var(--text-tertiary)', margin: '6px 0 18px', lineHeight: 1.6 }}>
              只用现有本地索引重算三档，<strong>不联飞书、不用 AI</strong>。改了阈值 / 范围想立刻看结果时用。
            </div>

            {/* 动作二：联飞书刷新 + 大模型复核 */}
            <button className="btn btn-tonal" disabled={running} onClick={() => start()}
                    title="先联飞书刷新本地索引（除非勾选下方「跳过」），再让大模型逐条复核归档候选，给更准的理由与置信度（约 1–2 分钟）。">
              <Icon name="sparkle" size={14} /> {running ? '复核中…' : meta.buttonLabel}
            </button>
            <label style={{ display: 'flex', gap: 8, alignItems: 'flex-start', marginTop: 8, fontSize: 12, color: 'var(--text-secondary)' }}>
              <input type="checkbox" checked={skipRefresh} onChange={e => setSkipRefresh(e.target.checked)} style={{ marginTop: 2 }} />
              <span>
                跳过联飞书刷新，直接用现有本地索引复核（更快）
                <div style={{ color: 'var(--text-tertiary)', marginTop: 2 }}>
                  不勾：先同步飞书最新增删改（顺带清理已删文件）再复核；勾上：省掉同步、只用现有数据。<strong>仅影响此按钮</strong>。
                </div>
              </span>
            </label>
          </div>
        ) : (
          <div style={{ marginTop: 'var(--space-5)', display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <button className="btn btn-primary"
                    disabled={running || !canRun}
                    onClick={() => start()}>
              <Icon name="sparkle" size={14} /> {running ? '运行中…' : meta.buttonLabel}
            </button>
          </div>
        )}

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
      )}

      <div style={{ display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        {!isDocMap && (
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
          {isTemplatedAgent && payload && usedTemplateLabel && (
            <span style={{
              display: 'inline-flex', alignItems: 'center', gap: 6,
              padding: '3px 10px', background: 'var(--brand-50)', color: 'var(--brand-700)',
              border: '1px solid var(--brand-200)', borderRadius: 'var(--radius-full)', fontSize: 12,
            }} title="本次使用的模板">
              <Icon name="page" size={12} /> 模板：{usedTemplateLabel}
            </span>
          )}
          <div style={{ flex: 1 }} />
          {task?.writeback && task.writeback.status === 'pending' && (
            <button className="btn btn-primary btn-sm" onClick={() => setShowWriteback(true)}>
              <Icon name="check" size={14} /> 写回飞书
            </button>
          )}
          {task?.writeback && task.writeback.status === 'failed' && (
            <button className="btn btn-primary btn-sm" onClick={() => setShowWriteback(true)} title="上次写回 / 分发失败，复用已生成内容重试（如补授权后重发）">
              <Icon name="refresh" size={14} /> 重试写回
            </button>
          )}
        </div>
        )}

        {/* 自定义模板：把用户当时给出的自然语言要求展示在顶栏下方（与 HTML 页面生成一致） */}
        {!isDocMap && isTemplatedAgent && payload && usedTemplate === 'custom' && usedCustomInstruction && (
          <div style={{
            padding: '8px 24px', borderBottom: '1px solid var(--border-subtle)',
            background: 'var(--surface-elevated)', fontSize: 12, color: 'var(--text-tertiary)',
            display: 'flex', gap: 8, alignItems: 'baseline',
          }}>
            <span style={{ fontWeight: 600, whiteSpace: 'nowrap' }}>自定义要求</span>
            <span style={{ color: 'var(--text-primary)', whiteSpace: 'pre-wrap' }}>{usedCustomInstruction}</span>
          </div>
        )}

        <div style={{ flex: 1, overflowY: 'auto', padding: 'var(--space-6)', background: 'var(--surface-page)' }}>
          {isDocMap && (
            <div style={{ marginBottom: 16 }}>
              <p style={{ margin: '0 0 12px', color: 'var(--text-tertiary)', fontSize: 13 }}>{meta.subtitle}</p>
              <div className="card" style={{ padding: 16, display: 'flex', alignItems: 'center', gap: 16, flexWrap: 'wrap' }}>
                <button className="btn btn-primary" disabled={running} onClick={() => start()}>
                  <Icon name="refresh" size={14} /> {running ? '刷新中…' : '刷新文档地图'}
                </button>
                <label style={{ display: 'flex', gap: 6, alignItems: 'center', fontSize: 13, color: 'var(--text-secondary)' }}>
                  <input type="checkbox" checked={skipRefresh} onChange={e => setSkipRefresh(e.target.checked)} />
                  跳过索引刷新（仅用现有本地索引重算）
                </label>
                <button className="btn btn-tonal btn-sm" onClick={() => nav('/task/knowledge-governance')}
                        title="扫描陈旧（三档分流）/ 重复 / 无主文档并给处置建议">
                  <Icon name="shield" size={14} /> 陈旧 / 重复治理 →
                </button>
                <div style={{ flex: 1 }} />
                {task?.status && (
                  <span className={`badge badge-${task.status === 'done' ? 'success' : task.status === 'failed' ? 'error' : 'brand'}`}>{task.status}</span>
                )}
                {payload?.last_refreshed && (
                  <span style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>
                    索引更新于 {String(payload.last_refreshed).replace('T', ' ').slice(0, 16)}
                  </span>
                )}
              </div>
              {(running || logs.length > 0) && (
                <div ref={logRef} style={{
                  marginTop: 12, background: 'var(--surface-dark)', color: '#cad7e1',
                  borderRadius: 'var(--radius-md)', padding: 12,
                  fontFamily: 'var(--font-mono)', fontSize: 12, lineHeight: 1.6,
                  maxHeight: 200, overflowY: 'auto',
                }}>
                  {logs.map((l, i) => (
                    <div key={i} style={{ color: l.level === 'error' ? '#FF8B8B' : l.level === 'warn' ? '#FFD466' : '#9BF1BD' }}>
                      <span style={{ color: '#737A82', marginRight: 8 }}>{l.ts?.slice(11)}</span>
                      <span>{l.message}</span>
                    </div>
                  ))}
                  {running && <div style={{ color: '#5FBA89' }} className="pulse">▌刷新中…</div>}
                </div>
              )}
            </div>
          )}
          {task?.error && (
            <div className="card" style={{ background: '#FFF4F4', borderColor: '#FFD0D0', padding: 16, marginBottom: 16 }}>
              <strong style={{ color: 'var(--error)' }}>错误：</strong>
              <span style={{ color: 'var(--text-secondary)', marginLeft: 8 }}>{task.error}</span>
            </div>
          )}
          {!payload && !task?.error && !isDocMap && agentId !== 'knowledge-governance' && (
            <div style={{ textAlign: 'center', color: 'var(--text-tertiary)', marginTop: 80 }}>
              <Icon name="sparkle" size={48} />
              <div style={{ marginTop: 12 }}>填写参数后点击「{meta.buttonLabel}」开始</div>
            </div>
          )}
          {payload && agentId === 'document-map' && <DocumentMapResult p={payload} />}
          {payload && agentId === 'index-enrich' && <IndexEnrichResult p={payload} />}
          {payload && agentId === 'knowledge-governance' && <KnowledgeGovResult p={payload} />}
          {payload && agentId === 'base-analysis' && <BaseAnalysisResult p={payload} />}
          {payload && agentId === 'pdf-recognition' && <PdfRecognitionResult p={payload} />}
          {payload && agentId === 'meeting-minutes' && <MeetingMinutesResult p={payload} taskId={taskId} onDispatch={(tid) => nav(`/task/collab-dispatch?source_task_id=${tid}&auto=1`)} />}
          {payload && agentId === 'collab-dispatch' && <CollabDispatchResult p={payload} />}
        </div>
      </div>

      {showWriteback && task?.writeback && (
        <WritebackModal
          proposal={task.writeback}
          taskId={task.id}
          onClose={() => setShowWriteback(false)}
          onDone={() => { setShowWriteback(false); mutateTask(); }}
        />
      )}
    </div>
  );
};

// ── 文档地图结果渲染 ──────────────────────────────────────────────

const DocumentMapResult: React.FC<{ p: any }> = ({ p }) => {
  const nav = useNavigate();
  const spaces = p.by_space || [];
  const types = p.by_type || [];
  const origins = p.by_origin || [];
  const owners = p.by_owner || [];
  const recency = p.by_recency || [];
  const created = p.by_created || [];
  const category = p.by_category || [];

  // 点击分面 → 跳到「本地资产」列表，并带上对应的过滤条件。
  function drill(params: Record<string, string>) {
    nav(`/assets?${new URLSearchParams(params).toString()}`);
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div className="card" style={{ padding: 20 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 8 }}>
          <Icon name="folder" size={20} />
          <h3 style={{ margin: 0 }}>盘点摘要</h3>
        </div>
        <Markdown source={p.summary} style={{ color: 'var(--text-secondary)' }} />
        <div style={{ display: 'flex', gap: 24, marginTop: 16, fontSize: 13 }}>
          <Stat label="文档总数" value={p.total} />
          <Stat label="资产类型" value={types.length} />
          <Stat label="所在空间" value={spaces.length} />
          <Stat label="所有者" value={owners.length === 10 ? '10+' : owners.length} />
        </div>
      </div>

      {/* 来源分布 */}
      {origins.length > 0 && (
        <div className="card" style={{ padding: 20 }}>
          <h3 style={{ marginTop: 0 }}>按来源</h3>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 12 }}>
            {origins.map((o: any) => (
              <div key={o.name} className="facet-tile" title={`查看「${o.name}」的 ${o.count} 条资产`}
                   onClick={() => drill({ origin: o.name })}
                   style={{ padding: 12, background: 'var(--surface-subtle)', borderRadius: 8, border: '1px solid transparent' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
                  <strong>{o.name}</strong>
                  <span className="facet-count" style={{ fontSize: 20, fontWeight: 600, color: 'var(--brand-700)' }}>{o.count}</span>
                </div>
                {o.samples && (
                  <ul style={{ margin: '6px 0 0', paddingLeft: 16, fontSize: 12, color: 'var(--text-tertiary)' }}>
                    {o.samples.slice(0, 3).map((s: any, i: number) => (
                      <li key={i} style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.title}</li>
                    ))}
                  </ul>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 时段分布 */}
      {recency.length > 0 && (
        <div className="card" style={{ padding: 20 }}>
          <h3 style={{ marginTop: 0 }}>按活跃度</h3>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 12 }}>
            {recency.map((r: any) => (
              <div key={r.name} className="facet-tile" title={`查看「${r.name}」的 ${r.count} 条资产`}
                   onClick={() => drill({ recency: r.name })}
                   style={{ padding: 12, borderLeft: '3px solid var(--brand-500)', background: 'var(--surface-subtle)', borderRadius: 4 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
                  <div>{r.name}</div>
                  <span className="badge facet-count">{r.count}</span>
                </div>
                {r.samples?.length > 0 && (
                  <ul style={{ margin: '6px 0 0', paddingLeft: 16, fontSize: 12, color: 'var(--text-tertiary)' }}>
                    {r.samples.slice(0, 2).map((s: any, i: number) => (
                      <li key={i} style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.title}</li>
                    ))}
                  </ul>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 创建时段分布（按创建年份，与"按活跃度"互补） */}
      {created.length > 0 && (
        <div className="card" style={{ padding: 20 }}>
          <h3 style={{ marginTop: 0 }}>按创建时段</h3>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 12 }}>
            {created.map((c: any) => (
              <div key={c.year} className="facet-tile" title={`查看 ${c.name} 创建的 ${c.count} 条资产`}
                   onClick={() => drill({ created_year: c.year })}
                   style={{ padding: 12, borderLeft: '3px solid var(--info)', background: 'var(--surface-subtle)', borderRadius: 4 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
                  <div>{c.name}</div>
                  <span className="badge facet-count">{c.count}</span>
                </div>
                {c.samples?.length > 0 && (
                  <ul style={{ margin: '6px 0 0', paddingLeft: 16, fontSize: 12, color: 'var(--text-tertiary)' }}>
                    {c.samples.slice(0, 2).map((s: any, i: number) => (
                      <li key={i} style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.title}</li>
                    ))}
                  </ul>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 按 AI 分类（「摘要 / 标签回填」生成的业务分类，可点击下钻） */}
      {category.length > 0 && (
        <div className="card" style={{ padding: 20, borderTop: '3px solid var(--brand-500)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
            <h3 style={{ margin: 0 }}>按 AI 分类</h3>
            <span className="badge badge-brand">qwen 回填</span>
          </div>
          <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 12 }}>
            点任意分类下钻到该类资产。分类由「摘要 / 标签回填」生成；未回填的归到「未分类」。
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 12 }}>
            {category.map((c: any) => (
              <div key={c.name} className="facet-tile" title={`查看「${c.name}」的 ${c.count} 条资产`}
                   onClick={() => drill({ category: c.name })}
                   style={{ padding: 12, background: 'var(--surface-subtle)', borderRadius: 8, border: '1px solid transparent' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
                  <strong>{c.name}</strong>
                  <span className="facet-count badge">{c.count}</span>
                </div>
                {c.samples?.length > 0 && (
                  <ul style={{ margin: '6px 0 0', paddingLeft: 16, fontSize: 12, color: 'var(--text-tertiary)' }}>
                    {c.samples.slice(0, 2).map((s: any, i: number) => (
                      <li key={i} style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.title}</li>
                    ))}
                  </ul>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
        {/* Top Owners */}
        <div className="card" style={{ padding: 20 }}>
          <h3 style={{ marginTop: 0 }}>Top 所有者 (前 10)</h3>
          <table className="table" style={{ width: '100%', fontSize: 13 }}>
            <tbody>
              {owners.map((o: any) => (
                <tr key={o.owner_id} style={{ cursor: 'pointer' }}
                    title={`查看 ${o.name.replace(' · 你自己', '')} 的 ${o.count} 条资产`}
                    onClick={() => drill({ owner_id: o.owner_id, owner_name: o.name.replace(' · 你自己', '') })}>
                  <td>
                    {o.is_me && <span className="badge badge-brand" style={{ marginRight: 6 }}>我</span>}
                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{o.name}</span>
                  </td>
                  <td style={{ textAlign: 'right' }}><span className="badge">{o.count}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* By Type */}
        <div className="card" style={{ padding: 20 }}>
          <h3 style={{ marginTop: 0 }}>按类型</h3>
          <table className="table" style={{ width: '100%', fontSize: 13 }}>
            <tbody>
              {types.map((t: any) => (
                <tr key={t.name} style={{ cursor: 'pointer' }}
                    title={`查看「${t.name}」的 ${t.count} 条资产`}
                    onClick={() => drill({ type_exact: t.type || '', type_label: t.name })}>
                  <td>{t.name}</td>
                  <td style={{ textAlign: 'right' }}><span className="badge">{t.count}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* By Space */}
      {spaces.length > 0 && (
        <div className="card" style={{ padding: 20 }}>
          <h3 style={{ marginTop: 0 }}>按空间 / 文件夹（前 20）</h3>
          <table className="table" style={{ width: '100%', fontSize: 13 }}>
            <tbody>
              {spaces.map((s: any) => (
                <tr key={s.name} style={{ cursor: 'pointer' }}
                    title={`查看「${s.name}」的 ${s.count} 条资产`}
                    onClick={() => drill({ space: s.name })}>
                  <td>{s.name}</td>
                  <td style={{ textAlign: 'right' }}><span className="badge">{s.count}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
};

// ── 摘要 / 标签回填结果渲染 ──────────────────────────────────────

const IndexEnrichResult: React.FC<{ p: any }> = ({ p }) => {
  const nav = useNavigate();
  const cats = p.by_category || [];
  const sample = p.sample || [];
  const maxCat = cats.reduce((m: number, c: any) => Math.max(m, c.count || 0), 0) || 1;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div className="card" style={{ padding: 20 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 8 }}>
          <Icon name="sparkle" size={20} />
          <h3 style={{ margin: 0 }}>回填概览</h3>
          <div style={{ flex: 1 }} />
          <button className="btn btn-tonal btn-sm" onClick={() => nav('/assets')}>
            <Icon name="external" size={12} /> 去飞书文档查看
          </button>
        </div>
        <div style={{ display: 'flex', gap: 32, marginTop: 8, fontSize: 13 }}>
          <Stat label="本次新回填" value={p.enriched_now} />
          <Stat label="累计已回填" value={p.enriched_total} />
          <Stat label="资产总数" value={p.total} />
          <Stat label="覆盖率" value={p.coverage != null ? `${p.coverage}%` : '—'} />
        </div>
        {p.total > 0 && (
          <div style={{ marginTop: 16, height: 8, borderRadius: 4, background: 'var(--surface-subtle)', overflow: 'hidden' }}>
            <div style={{ width: `${p.coverage || 0}%`, height: '100%', background: 'var(--brand-500)', transition: 'width .4s ease' }} />
          </div>
        )}
      </div>

      {cats.length > 0 && (
        <div className="card" style={{ padding: 20 }}>
          <h3 style={{ marginTop: 0 }}>分类分布（AI 归类）</h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {cats.map((c: any) => (
              <div key={c.name} style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                <div style={{ width: 90, fontSize: 13, textAlign: 'right', color: 'var(--text-secondary)' }}>{c.name}</div>
                <div style={{ flex: 1, height: 18, background: 'var(--surface-subtle)', borderRadius: 4, overflow: 'hidden' }}>
                  <div style={{ width: `${Math.max(4, (c.count / maxCat) * 100)}%`, height: '100%', background: 'var(--tint-brand)', borderRight: '2px solid var(--brand-500)' }} />
                </div>
                <div style={{ width: 40, fontSize: 13, fontWeight: 600 }}>{c.count}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {sample.length > 0 && (
        <div className="card" style={{ padding: 20 }}>
          <h3 style={{ marginTop: 0 }}>回填样例（最近更新的 {sample.length} 条）</h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            {sample.map((s: any, i: number) => (
              <div key={i} style={{ borderLeft: '3px solid var(--brand-500)', paddingLeft: 12 }}>
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
                  <strong style={{ fontSize: 14 }}>
                    {s.url ? <a href={s.url} target="_blank" rel="noopener noreferrer">{s.title}</a> : s.title}
                  </strong>
                  {s.category && <span className="badge badge-brand">{s.category}</span>}
                </div>
                {s.summary && <div style={{ fontSize: 13, color: 'var(--text-secondary)', marginTop: 2 }}>{s.summary}</div>}
                {Array.isArray(s.tags) && s.tags.length > 0 && (
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 6 }}>
                    {s.tags.map((t: string, ti: number) => (
                      <span key={ti} style={{ fontSize: 11, padding: '1px 8px', background: 'var(--surface-subtle)', borderRadius: 10, color: 'var(--text-tertiary)' }}>{t}</span>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
};

// ── 知识治理结果渲染 ──────────────────────────────────────────────

const KnowledgeGovResult: React.FC<{ p: any }> = ({ p }) => {
  const m = p.metrics || {};
  const rec = p.recommendations || {};
  const triage = p.stale_triage || {};
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div className="card" style={{ padding: 20 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 8 }}>
          <Icon name="shield" size={20} />
          <h3 style={{ margin: 0 }}>治理结论</h3>
          <span className="badge badge-brand">{m.mine_only ? '仅我创建的' : '全部文档'}</span>
        </div>
        <Markdown source={rec.overall || p.overall} style={{ color: 'var(--text-secondary)' }} />
        <div style={{ display: 'flex', gap: 24, marginTop: 16, fontSize: 13, flexWrap: 'wrap' }}>
          <Stat label="总资产" value={m.total_assets} />
          <Stat label={`失修(>${m.stale_days_threshold}天)`} value={m.stale_count} />
          <Stat label="建议归档" value={m.archive_count} tone="warning" />
          <Stat label="长青参考" value={m.evergreen_count} />
          <Stat label="待复核" value={m.review_count} />
          <Stat label="无主" value={m.no_owner_count} tone="warning" />
          <Stat label="重复组" value={m.dup_groups} tone="warning" />
        </div>
      </div>

      <StaleTriageSection
        title="🗑 建议归档"
        accent="var(--warning)"
        items={triage.archive || []}
        csvName="建议归档清单"
        defaultOpen
        empty="没有可归档的失修文档 🎉"
        hint="时效型类别 / 含废弃信号且长期未更新。确认后可在飞书归档或删除——本工具只给清单，不自动删除。"
      />
      <StaleTriageSection
        title="📌 长青参考"
        accent="var(--brand-500)"
        items={triage.evergreen || []}
        csvName="长青参考清单"
        empty="无"
        hint="制度 / 合规 / 技术 / 培训等常青类别，虽久未更新但可能仍然有效，建议确认后保留。"
      />
      <StaleTriageSection
        title="🔍 待复核"
        accent="var(--text-tertiary)"
        items={triage.review || []}
        csvName="待复核清单"
        empty="无"
        hint="类别不明确，需人工判断去留。"
      />

      <GovSection
        title="无主文档"
        items={p.no_owner}
        empty="所有文档都有 owner"
        renderRow={(d, i) => (
          <tr key={d.asset_id || i}>
            <td><a href={d.url} target="_blank" rel="noopener noreferrer">{d.title}</a></td>
            <td>{d.space || '—'}</td>
            <td className="mono" style={{ fontSize: 12 }}>{d.updated?.slice(0, 10) || '—'}</td>
          </tr>
        )}
        headers={['标题', '空间', '上次更新']}
        recs={rec.no_owner_recommendations}
      />

      {p.duplicates?.length > 0 && (
        <div className="card" style={{ padding: 20 }}>
          <h3 style={{ marginTop: 0 }}>重复嫌疑（标题归一化后相同）</h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            {p.duplicates.map((g: any, gi: number) => (
              <div key={gi} style={{ borderLeft: '3px solid var(--warning)', paddingLeft: 12 }}>
                <div style={{ fontWeight: 500, marginBottom: 4 }}>组 {gi + 1}（{g.items.length} 篇）</div>
                <ul style={{ margin: 0, paddingLeft: 20, fontSize: 13 }}>
                  {g.items.map((d: any) => (
                    <li key={d.asset_id}>
                      <a href={d.url} target="_blank" rel="noopener noreferrer">{d.title}</a>
                      <span style={{ color: 'var(--text-tertiary)', marginLeft: 6 }}>· {d.owner || '无主'} · {d.updated?.slice(0, 10) || '—'}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
          {rec.dup_recommendations?.length > 0 && (
            <div style={{ marginTop: 12, fontSize: 13, color: 'var(--text-secondary)' }}>
              <div className="label" style={{ marginBottom: 4 }}>建议</div>
              <ul style={{ margin: 0, paddingLeft: 20 }}>
                {rec.dup_recommendations.map((r: any, i: number) => (
                  <li key={i}><strong>{r.action}</strong>：{r.reason}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
};

// ── 陈旧内容三档分流卡片 ─────────────────────────────────────────

const CONF_STYLE: Record<string, React.CSSProperties> = {
  high: { background: '#FDE8E8', color: '#C0392B' },
  medium: { background: '#FFF3E0', color: '#B9770E' },
  low: { background: 'var(--surface-subtle)', color: 'var(--text-tertiary)' },
};
const CONF_LABEL: Record<string, string> = { high: '高', medium: '中', low: '低' };

function exportCsv(name: string, items: any[]): void {
  const headers = ['标题', '类别', '置信度', '理由', '最后更新', '负责人', '空间', '链接'];
  const esc = (v: any) => `"${(v == null ? '' : String(v)).replace(/"/g, '""')}"`;
  const rows = items.map((d) => [
    d.title, d.category || '', CONF_LABEL[d.confidence] || d.confidence || '', d.reason || '',
    (d.updated || '').slice(0, 10), d.owner || '', d.space || '', d.url || '',
  ].map(esc).join(','));
  const csv = '﻿' + [headers.map(esc).join(','), ...rows].join('\r\n'); // BOM → Excel 正确识别 UTF-8
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${name}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

const StaleTriageSection: React.FC<{
  title: string; accent: string; items: any[]; csvName: string; hint: string; empty: string; defaultOpen?: boolean;
}> = ({ title, accent, items, csvName, hint, empty, defaultOpen }) => {
  const [open, setOpen] = useState(!!defaultOpen);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const SHOW = 50;
  // 勾选用稳定 key（asset_id 缺失时退化为下标）。选择覆盖全部 items（含未展开的 >50 项）。
  const keyOf = (d: any, i: number) => d.asset_id || `idx-${i}`;
  const allSelected = items.length > 0 && selected.size === items.length;
  const toggleOne = (k: string) =>
    setSelected(prev => { const n = new Set(prev); n.has(k) ? n.delete(k) : n.add(k); return n; });
  const toggleAll = () =>
    setSelected(prev => (prev.size === items.length ? new Set() : new Set(items.map(keyOf))));
  const selectedItems = items.filter((d, i) => selected.has(keyOf(d, i)));
  if (!items || items.length === 0) {
    return (
      <div className="card" style={{ padding: 20 }}>
        <h3 style={{ marginTop: 0 }}>{title}</h3>
        <div style={{ color: 'var(--text-tertiary)' }}>{empty}</div>
      </div>
    );
  }
  return (
    <div className="card" style={{ padding: 20, borderLeft: `3px solid ${accent}` }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <h3 style={{ margin: 0, cursor: 'pointer' }} onClick={() => setOpen(o => !o)}>
          {title} <span style={{ fontWeight: 400, fontSize: 14, color: 'var(--text-tertiary)' }}>· {items.length} 篇</span>
        </h3>
        <div style={{ flex: 1 }} />
        <button className="btn btn-ghost btn-sm" onClick={() => setOpen(o => !o)}>{open ? '收起' : '展开'}</button>
        {open && (
          <button className="btn btn-ghost btn-sm" onClick={toggleAll}>
            {allSelected ? '清空' : '全选'}
          </button>
        )}
        {selected.size > 0 && (
          <button className="btn btn-tonal btn-sm" onClick={() => exportCsv(`${csvName}-选中${selected.size}`, selectedItems)}
                  title="只导出勾选的这些，拿去逐条在飞书处理">
            <Icon name="external" size={12} /> 导出选中 ({selected.size})
          </button>
        )}
        <button className="btn btn-tonal btn-sm" onClick={() => exportCsv(csvName, items)} title="导出本类全部">
          <Icon name="external" size={12} /> 导出 CSV
        </button>
      </div>
      <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginTop: 4 }}>{hint}</div>
      {open && (
        <div style={{ marginTop: 12, display: 'flex', flexDirection: 'column' }}>
          {items.slice(0, SHOW).map((d: any, i: number) => {
            const k = keyOf(d, i);
            return (
              <div key={k} style={{ padding: '8px 0', borderTop: i ? '1px solid var(--border-subtle)' : 'none', display: 'flex', gap: 10, alignItems: 'flex-start' }}>
                <input type="checkbox" checked={selected.has(k)} onChange={() => toggleOne(k)}
                       style={{ marginTop: 4, flexShrink: 0, accentColor: 'var(--brand-500)' }} aria-label="选中此项" />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' }}>
                    {d.confidence && (
                      <span style={{ fontSize: 10, padding: '1px 7px', borderRadius: 8, ...CONF_STYLE[d.confidence] }}>
                        {CONF_LABEL[d.confidence] || d.confidence}
                      </span>
                    )}
                    <strong style={{ fontSize: 14 }}>
                      {d.url ? <a href={d.url} target="_blank" rel="noopener noreferrer">{d.title}</a> : d.title}
                    </strong>
                    {d.category && <span className="badge">{d.category}</span>}
                  </div>
                  {d.reason && <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 2 }}>{d.reason}</div>}
                  <div style={{ fontSize: 11, color: 'var(--text-tertiary)', marginTop: 2 }}>
                    {(d.updated || '').slice(0, 10) || '—'} · {d.owner || '无主'} · {d.space || '—'}
                  </div>
                </div>
              </div>
            );
          })}
          {items.length > SHOW && (
            <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginTop: 8 }}>
              … 还有 {items.length - SHOW} 篇未展开（「全选 / 导出选中」仍涵盖全部 {items.length} 篇）
            </div>
          )}
        </div>
      )}
    </div>
  );
};

const GovSection: React.FC<{ title: string; items: any[]; empty: string; headers: string[]; renderRow: (d: any, i: number) => React.ReactNode; recs?: any[] }> = ({ title, items, empty, headers, renderRow, recs }) => {
  if (!items || items.length === 0) {
    return (
      <div className="card" style={{ padding: 20 }}>
        <h3 style={{ marginTop: 0 }}>{title}</h3>
        <div style={{ color: 'var(--text-tertiary)' }}>{empty}</div>
      </div>
    );
  }
  return (
    <div className="card" style={{ padding: 20 }}>
      <h3 style={{ marginTop: 0 }}>{title} <span style={{ fontWeight: 400, fontSize: 14, color: 'var(--text-tertiary)' }}>· {items.length} 篇</span></h3>
      <table className="table" style={{ width: '100%', fontSize: 13 }}>
        <thead><tr>{headers.map(h => <th key={h}>{h}</th>)}</tr></thead>
        <tbody>{items.slice(0, 30).map(renderRow)}</tbody>
      </table>
      {recs && recs.length > 0 && (
        <div style={{ marginTop: 12, fontSize: 13, color: 'var(--text-secondary)' }}>
          <div className="label" style={{ marginBottom: 4 }}>处置建议</div>
          <ul style={{ margin: 0, paddingLeft: 20 }}>
            {recs.slice(0, 10).map((r: any, i: number) => (
              <li key={i}><strong>{r.action || r.suggested_owner_hint || '—'}</strong>：{r.reason}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
};

// ── 多维表格分析结果渲染 ─────────────────────────────────────────

const TYPE_LABEL: Record<string, string> = {
  number: '数值', date: '日期', bool: '布尔', text: '文本', empty: '空', mixed: '混排',
};
const TYPE_COLOR: Record<string, string> = {
  number: 'var(--brand-700)', date: 'var(--info)', bool: '#8B5CF6',
  text: 'var(--text-secondary)', empty: 'var(--text-tertiary)', mixed: 'var(--warning)',
};

// 问数据：自然语言 → 后端 LLM 翻译成查询 → Python 精确计算。数字不经模型，绝不被编造。
function fmtVal(v: any): string {
  if (v === null || v === undefined || v === '') return '—';
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(2);
  return String(v);
}

const AskAnswer: React.FC<{ item: any }> = ({ item }) => {
  const res = item.result || {};
  return (
    <div style={{ borderLeft: '3px solid var(--brand-500)', paddingLeft: 12 }}>
      <div style={{ fontWeight: 600, fontSize: 14 }}>{item.q}</div>
      {item.explanation && <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginTop: 2 }}>{item.explanation}</div>}
      {!res.ok ? (
        <div style={{ fontSize: 13, color: 'var(--text-tertiary)', marginTop: 6 }}>没有可用结果。</div>
      ) : res.result_type === 'scalar' ? (
        <div style={{ marginTop: 8 }}>
          <span className="tnum" style={{ fontSize: 30, fontWeight: 700, color: 'var(--brand-700)', letterSpacing: '-0.02em' }}>{fmtVal(res.scalar)}</span>
          <span style={{ fontSize: 13, color: 'var(--text-tertiary)', marginLeft: 8 }}>{res.scalar_label}</span>
        </div>
      ) : (
        <div style={{ overflowX: 'auto', marginTop: 8 }}>
          <table className="table" style={{ fontSize: 13, whiteSpace: 'nowrap' }}>
            <thead><tr>{(res.columns || []).map((c: string, i: number) => <th key={i}>{c}</th>)}</tr></thead>
            <tbody>
              {(res.rows || []).map((row: any[], ri: number) => (
                <tr key={ri}>{row.map((cell, ci) => <td key={ci} style={{ textAlign: ci === row.length - 1 ? 'right' : 'left' }}>{fmtVal(cell)}</td>)}</tr>
              ))}
            </tbody>
          </table>
          {(res.rows || []).length === 0 && <div style={{ fontSize: 13, color: 'var(--text-tertiary)' }}>没有命中任何行。</div>}
        </div>
      )}
      <div style={{ fontSize: 11, color: 'var(--text-tertiary)', marginTop: 6 }}>
        命中 {res.matched_rows ?? '—'} / {res.total_rows ?? '—'} 行{item.sampled ? ' · 采样数据' : ''}{res.note ? ` · ${res.note}` : ''}
      </div>
    </div>
  );
};

const AskTableCard: React.FC<{ assetId: string; assetType: string; targetId: string }> = ({ assetId, assetType, targetId }) => {
  const [q, setQ] = useState('');
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState('');
  const [history, setHistory] = useState<any[]>([]);

  async function ask(text: string) {
    const question = text.trim();
    if (!question || loading) return;
    setLoading(true); setErr('');
    try {
      const r = await api.post<any>('/api/base/ask', { asset_id: assetId, asset_type: assetType, target_id: targetId, question });
      if (!r?.ok) setErr(r?.error || '查询失败');
      else { setHistory(h => [{ q: question, explanation: r.explanation, result: r.result, sampled: r.sampled }, ...h]); setQ(''); }
    } catch (e: any) {
      setErr(String(e?.message || e).slice(0, 200));
    } finally { setLoading(false); }
  }

  const examples = ['一共有多少条记录？', '哪一类最多？按主要分类统计数量', '数值最大的前 5 条是什么？'];

  return (
    <div className="card" style={{ padding: 20, borderTop: '3px solid var(--brand-500)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
        <Icon name="sparkle" size={18} />
        <h3 style={{ margin: 0 }}>问数据</h3>
        <span className="badge badge-brand">AI 翻译 · Python 精算</span>
      </div>
      <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 12, lineHeight: 1.6 }}>
        用大白话问这张表，例如「按地区汇总销售额」「今年成交了多少单」。AI 只负责把问题翻译成查询，<strong>数字由程序在真实数据上精确算出</strong>，不会被模型编造。
      </div>
      <div style={{ display: 'flex', gap: 8 }}>
        <input className="input" style={{ flex: 1 }} value={q} placeholder="输入你的问题，回车提问…"
               onChange={e => setQ(e.target.value)}
               onKeyDown={e => { if (e.key === 'Enter') ask(q); }} disabled={loading} />
        <button className="btn btn-primary" disabled={loading || !q.trim()} onClick={() => ask(q)}>
          <Icon name="sparkle" size={14} className={loading ? 'spin' : ''} /> {loading ? '计算中…' : '提问'}
        </button>
      </div>
      {history.length === 0 && !loading && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 10 }}>
          {examples.map(ex => (
            <button key={ex} className="btn btn-ghost btn-sm" onClick={() => { setQ(ex); ask(ex); }}>{ex}</button>
          ))}
        </div>
      )}
      {err && <div style={{ marginTop: 10, fontSize: 13, color: 'var(--error)' }}>{err}</div>}
      {history.length > 0 && (
        <div style={{ marginTop: 14, display: 'flex', flexDirection: 'column', gap: 14 }}>
          {history.map((h, i) => <AskAnswer key={i} item={h} />)}
        </div>
      )}
    </div>
  );
};

const BaseAnalysisResult: React.FC<{ p: any }> = ({ p }) => {
  // 新结构以 tables[] 为主；兼容旧任务（无 tables[]）：用顶层扁平字段拼成单表块。
  const tables: any[] = Array.isArray(p.tables) && p.tables.length
    ? p.tables
    : [{
        analyzed: p.analyzed,
        metrics: p.metrics || {},
        columns: p.columns || [],
        preview: p.preview || { headers: [], rows: [] },
        summary: p.summary || '',
        charts: p.charts || [],
        note: p.note || '',
        sampled: p.sampled,
      }];
  const multi = tables.length > 1;
  const targetCount = (p.targets || tables).length;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {/* 共享头部：资产标题 + 类型 + 空间（+ 多表时的张数说明） */}
      <div className="card" style={{ padding: 20 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <Icon name="table" size={20} />
          <h3 style={{ margin: 0 }}>
            {p.url ? <a href={p.url} target="_blank" rel="noopener noreferrer">{p.title}</a> : p.title}
          </h3>
          <span className="badge badge-brand">{p.kind === 'sheet' ? '电子表格' : '多维表格'}</span>
          {p.space && <span style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>· {p.space}</span>}
        </div>
        {multi && (
          <div style={{ marginTop: 8, fontSize: 13, color: 'var(--text-secondary)' }}>
            共 {targetCount} 张{p.kind === 'sheet' ? '工作表' : '数据表'}，已逐张分析（点标题可折叠 / 展开）
            {p.truncated && <span style={{ color: 'var(--warning)', marginLeft: 6 }}>· 数量较多，仅展示前 {tables.length} 张</span>}
          </div>
        )}
      </div>

      {/* 逐张表区块 */}
      {tables.map((t: any, i: number) => (
        <TableSection key={t.analyzed?.id || i} t={t} assetId={p.asset_id} kind={p.kind} index={i} total={tables.length} />
      ))}
    </div>
  );
};

const TableSection: React.FC<{ t: any; assetId: string; kind: string; index: number; total: number }> = ({ t, assetId, kind, index, total }) => {
  const multi = total > 1;
  const [open, setOpen] = useState(true);
  const [showFields, setShowFields] = useState(false);
  const m = t.metrics || {};
  const columns = t.columns || [];
  const preview = t.preview || { headers: [], rows: [] };
  const charts: any[] = t.charts || [];
  const summary: string = t.summary || '';
  const name = t.analyzed?.name || `表 ${index + 1}`;

  // 空表 / 无权限：仅一行提示。
  if (t.empty) {
    return (
      <div className="card" style={{ padding: '14px 20px', borderLeft: '3px solid var(--border-strong)' }}>
        <strong>{multi ? `表 ${index + 1}/${total} · ` : ''}{name}</strong>
        <span style={{ color: 'var(--text-tertiary)', fontSize: 13, marginLeft: 8 }}>{t.note || '空表，已跳过。'}</span>
      </div>
    );
  }

  const body = (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16, padding: multi ? '16px 20px 20px' : 0 }}>
      {/* 概览统计 */}
      <div style={{ display: 'flex', gap: 28, fontSize: 13, flexWrap: 'wrap', alignItems: 'flex-end' }}>
        <Stat label="行数" value={m.row_count} />
        <Stat label="列数" value={m.column_count} />
        <Stat label="整体填充率" value={m.overall_fill != null ? `${m.overall_fill}%` : '—'} />
        {!multi && t.sampled && <span className="badge" title="数据量较大，仅分析采样行" style={{ alignSelf: 'center' }}>采样分析</span>}
      </div>

      {/* AI 看点 */}
      {summary && (
        <div className="card" style={{ padding: '14px 18px', borderLeft: '3px solid var(--brand-500)', background: 'var(--surface-subtle)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
            <Icon name="sparkle" size={16} /><strong style={{ fontSize: 13 }}>AI 看点</strong>
          </div>
          <Markdown source={summary} style={{ color: 'var(--text-secondary)', fontSize: 13 }} />
        </div>
      )}

      {/* 图表画廊 */}
      {charts.length > 0 ? (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(420px, 1fr))', gap: 16 }}>
          {charts.map((c: any, i: number) => <ChartCard key={i} chart={c} idx={i} />)}
        </div>
      ) : (
        <div className="card" style={{ padding: '14px 20px', color: 'var(--text-tertiary)', fontSize: 13 }}>
          {t.note || '没有生成图表。'}
        </div>
      )}

      {/* 问数据（自然语言查询） */}
      <AskTableCard assetId={assetId} assetType={kind} targetId={t.analyzed?.id || ''} />

      {/* 字段一览 + 数据预览（默认折叠） */}
      <div>
        <button className="btn btn-ghost btn-sm" onClick={() => setShowFields(s => !s)}>
          {showFields ? '收起字段与数据预览' : `查看字段一览（${columns.length} 列）与数据预览`}
        </button>
        {showFields && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16, marginTop: 12 }}>
            <div className="card" style={{ padding: 20 }}>
              <h3 style={{ marginTop: 0 }}>字段一览（{columns.length} 列）</h3>
              <div style={{ overflowX: 'auto' }}>
                <table className="table" style={{ width: '100%', fontSize: 13 }}>
                  <thead>
                    <tr><th>列名</th><th>类型</th><th style={{ width: 160 }}>填充率</th><th>去重</th><th>概览</th></tr>
                  </thead>
                  <tbody>
                    {columns.map((c: any) => (
                      <tr key={c.index}>
                        <td style={{ maxWidth: 220, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={c.name}>{c.name}</td>
                        <td>
                          <span style={{ color: TYPE_COLOR[c.inferred_type] || 'var(--text-secondary)', fontWeight: 500 }}>{TYPE_LABEL[c.inferred_type] || c.inferred_type}</span>
                          {c.pii && <span className="badge badge-error" style={{ marginLeft: 4 }}>{c.pii}</span>}
                        </td>
                        <td>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                            <div style={{ flex: 1, height: 8, background: 'var(--surface-subtle)', borderRadius: 4, overflow: 'hidden', minWidth: 60 }}>
                              <div style={{ width: `${c.fill_rate}%`, height: '100%', background: c.fill_rate < 50 ? 'var(--warning)' : 'var(--brand-500)' }} />
                            </div>
                            <span style={{ fontSize: 11, width: 42, textAlign: 'right' }}>{c.fill_rate}%</span>
                          </div>
                        </td>
                        <td>{c.distinct}</td>
                        <td style={{ fontSize: 12, color: 'var(--text-tertiary)', maxWidth: 280 }}>{colOverview(c)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
            {preview.rows?.length > 0 && (
              <div className="card" style={{ padding: 20 }}>
                <h3 style={{ marginTop: 0 }}>数据预览（前 {preview.rows.length} 行）</h3>
                <div style={{ overflowX: 'auto' }}>
                  <table className="table" style={{ fontSize: 12, whiteSpace: 'nowrap' }}>
                    <thead><tr>{preview.headers.map((h: string, i: number) => <th key={i}>{h}</th>)}</tr></thead>
                    <tbody>
                      {preview.rows.map((row: any[], ri: number) => (
                        <tr key={ri}>{row.map((cell, ci) => <td key={ci} style={{ maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis' }} title={cell}>{cell}</td>)}</tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );

  // 单表：直接渲染 body（与旧版单表观感一致）。
  if (!multi) return <div className="card" style={{ padding: 20 }}>{body}</div>;

  // 多表：每张表一个可折叠区块，标题栏显示表名 + 关键统计。
  return (
    <div className="card" style={{ padding: 0, overflow: 'hidden', borderTop: '3px solid var(--brand-500)' }}>
      <button
        onClick={() => setOpen(o => !o)}
        title={open ? '点击折叠' : '点击展开'}
        style={{
          width: '100%', display: 'flex', alignItems: 'center', gap: 10, padding: '12px 20px',
          background: 'var(--surface-subtle)', border: 'none',
          borderBottom: open ? '1px solid var(--border)' : 'none',
          cursor: 'pointer', textAlign: 'left', color: 'var(--text-primary)',
        }}>
        <Icon name="chevron-right" size={16} style={{ transform: open ? 'rotate(90deg)' : 'none', transition: 'transform .15s', flexShrink: 0 }} />
        <Icon name="table" size={16} />
        <strong style={{ fontSize: 15 }}>{name}</strong>
        <span className="badge">{index + 1}/{total}</span>
        {t.sampled && <span className="badge" title="数据量较大，仅分析采样行">采样</span>}
        <span style={{ marginLeft: 'auto', fontSize: 12, color: 'var(--text-tertiary)' }}>
          {m.row_count ?? '—'} 行 · {m.column_count ?? '—'} 列 · 填充 {m.overall_fill != null ? `${m.overall_fill}%` : '—'}
        </span>
      </button>
      {open && body}
    </div>
  );
};

function colOverview(c: any): string {
  if (c.numeric) {
    const n = c.numeric;
    let s = `范围 ${n.min} ~ ${n.max} · 均值 ${n.mean}`;
    if (n.outliers) s += ` · 离群 ${n.outliers}`;
    return s;
  }
  if (c.top_values?.length) {
    return '常见：' + c.top_values.slice(0, 3).map((t: any) => `${t.value}(${t.count})`).join('、');
  }
  if (c.text_len) return `文本长度 ${c.text_len.min}~${c.text_len.max}`;
  return '—';
}

const Stat: React.FC<{ label: string; value: any; tone?: 'warning' }> = ({ label, value, tone }) => (
  <div>
    <div className="label" style={{ marginBottom: 2 }}>{label}</div>
    <div style={{ fontSize: 24, fontWeight: 600, color: tone === 'warning' ? 'var(--warning)' : 'var(--text-primary)' }}>{value ?? '—'}</div>
  </div>
);

// ── PDF 识别结果渲染 ──────────────────────────────────────────────

const CUR_SYMBOL: Record<string, string> = { CNY: '¥', USD: '$', HKD: 'HK$', EUR: '€', JPY: '¥', GBP: '£', SGD: 'S$' };
function fmtMoney(n: number): string {
  if (n === null || n === undefined || isNaN(n)) return '—';
  return Number(n).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
}

const ContractFinanceCard: React.FC<{ f: any; explicit?: boolean }> = ({ f, explicit }) => {
  if (!f) return null;
  // 非「合同台账」模板（全文摘要等）只是自动顺带测算：没测到金额 / 出错时不渲染空卡，避免噪声。
  // 「合同台账」模板是用户明确要金额，空结果也保留提示，让用户知道确实跑过。
  if (f.error) {
    if (!explicit) return null;
    return (
      <div className="card" style={{ padding: 16, borderLeft: '3px solid var(--warning)', background: '#FFFBF2' }}>
        <strong>合同金额测算未完成</strong>
        <span style={{ color: 'var(--text-tertiary)', marginLeft: 8, fontSize: 13 }}>（{f.error}）</span>
      </div>
    );
  }
  if (f.empty) {
    if (!explicit) return null;
    return (
      <div className="card" style={{ padding: 16, borderLeft: '3px solid var(--border-strong)' }}>
        <strong>合同金额测算</strong>
        <div style={{ color: 'var(--text-tertiary)', fontSize: 13, marginTop: 4 }}>未在文档中找到带具体金额的款项。</div>
      </div>
    );
  }
  const byCur: any[] = f.by_currency || [];
  const items: any[] = f.items || [];
  const conditional: any[] = f.conditional_items || [];

  return (
    <div className="card" style={{ padding: 20, borderTop: '3px solid #C8881A' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
        <Icon name="table" size={18} />
        <h3 style={{ margin: 0 }}>合同金额测算</h3>
        <span className="badge" style={{ background: '#C8881A1A', color: '#9A6608' }}>每笔金额 · 按年合计</span>
      </div>
      <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 14, lineHeight: 1.6 }}>
        金额由程序逐笔加总（周期性款项按月/季/年展开），模型只负责把条款读成结构化数据，不做算术。请对照原文核对。
      </div>

      {/* 分币种 · 按年合计 */}
      {byCur.map((c, ci) => (
        <div key={ci} style={{ marginBottom: 16 }}>
          {byCur.length > 1 && (
            <div className="label" style={{ marginBottom: 6 }}>币种：{c.currency}</div>
          )}
          <div style={{ display: 'grid', gridTemplateColumns: `repeat(auto-fit, minmax(120px, 1fr))`, gap: 10 }}>
            {(c.years || []).map((y: any, yi: number) => (
              <div key={yi} style={{ padding: '10px 14px', background: 'var(--surface-subtle)', borderRadius: 8 }}>
                <div className="label" style={{ marginBottom: 2 }}>{y.year} 年</div>
                <div style={{ fontSize: 17, fontWeight: 600, color: 'var(--text-primary)' }}>
                  {CUR_SYMBOL[c.currency] || ''}{fmtMoney(y.total)}
                </div>
              </div>
            ))}
            <div style={{ padding: '10px 14px', background: '#C8881A14', borderRadius: 8, border: '1px solid #C8881A33' }}>
              <div className="label" style={{ marginBottom: 2, color: '#9A6608' }}>合计（{c.currency}）</div>
              <div style={{ fontSize: 19, fontWeight: 700, color: '#9A6608' }}>
                {CUR_SYMBOL[c.currency] || ''}{fmtMoney(c.total)}
              </div>
            </div>
          </div>
        </div>
      ))}

      {/* 款项明细 */}
      {items.length > 0 && (
        <div style={{ marginTop: 8 }}>
          <div className="label" style={{ marginBottom: 6 }}>款项明细（{items.length} 笔）</div>
          <div style={{ overflowX: 'auto' }}>
            <table className="table" style={{ fontSize: 12, width: '100%' }}>
              <thead>
                <tr>
                  <th>款项</th><th>类型</th><th>单笔/期</th><th>期间</th><th>小计</th><th>出处</th>
                </tr>
              </thead>
              <tbody>
                {items.map((it, i) => (
                  <tr key={i}>
                    <td>{it.label}{it.note && <span style={{ color: 'var(--text-tertiary)' }}> · {it.note}</span>}</td>
                    <td>
                      {it.type === 'recurring' ? (FREQ_CN[it.frequency] || '周期') : '一次性'}
                      {it.escalation_pct ? <span style={{ color: 'var(--warning)' }}> ↑{it.escalation_pct}%/年</span> : null}
                    </td>
                    <td className="tnum">{CUR_SYMBOL[it.currency] || ''}{fmtMoney(it.amount)}</td>
                    <td style={{ color: 'var(--text-tertiary)', whiteSpace: 'nowrap' }}>
                      {it.start ? String(it.start).slice(0, 10) : '—'}{it.end ? ` ~ ${String(it.end).slice(0, 10)}` : ''}
                    </td>
                    <td className="tnum" style={{ fontWeight: 600 }}>{CUR_SYMBOL[it.currency] || ''}{fmtMoney(it.total)}</td>
                    <td style={{ maxWidth: 200, color: 'var(--text-tertiary)', overflow: 'hidden', textOverflow: 'ellipsis' }} title={it.quote}>{it.quote || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* 按比例/按量结算（无法自动测算） */}
      {conditional.length > 0 && (
        <div style={{ marginTop: 14 }}>
          <div className="label" style={{ marginBottom: 6 }}>按比例 / 按量结算（无法自动测算，需人工核算）</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {conditional.map((c, i) => (
              <div key={i} style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
                <strong>{c.label}</strong>
                {c.basis && <span> · 基准：{c.basis}</span>}
                {c.note && <span style={{ color: 'var(--text-tertiary)' }}> · {c.note}</span>}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 提示与假设 */}
      {(f.warnings?.length > 0 || f.assumptions?.length > 0) && (
        <div style={{ marginTop: 14, fontSize: 12, lineHeight: 1.7 }}>
          {(f.warnings || []).map((w: string, i: number) => (
            <div key={`w${i}`} style={{ color: 'var(--warning)' }}>⚠ {w}</div>
          ))}
          {(f.assumptions || []).map((a: string, i: number) => (
            <div key={`a${i}`} style={{ color: 'var(--text-tertiary)' }}>· {a}</div>
          ))}
        </div>
      )}
    </div>
  );
};

const FREQ_CN: Record<string, string> = {
  monthly: '每月', quarterly: '每季', semiannual: '每半年', yearly: '每年', once: '一次性',
};

const PdfRecognitionResult: React.FC<{ p: any }> = ({ p }) => {
  const llm = p.llm || {};
  const keyFields: any[] = p.key_fields || [];
  const tables: any[] = p.tables || [];
  const pagePoints: any[] = p.page_points || [];
  const figures: any[] = p.figures || [];
  const highlights: string[] = p.highlights || [];

  // 纯视角：每个识别模板只显示对应板块。
  const tpl = p.template || 'summary';
  const isCustom = tpl === 'custom';   // 自定义视角信息开放，全部显示
  const show = {
    highlights: isCustom || tpl === 'summary' || tpl === 'fields',
    finance: isCustom || tpl !== 'pages',          // 金额测算卡片（仅「合同台账」模板会在空/错时显示提示，其余模板空结果自动隐藏）
    fields: isCustom || tpl === 'fields' || tpl === 'contract',
    tables: isCustom || tpl !== 'pages',
    pages: isCustom || tpl === 'summary' || tpl === 'pages',
    figures: isCustom || tpl === 'summary' || tpl === 'pages',
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {/* 概览 */}
      <div className="card" style={{ padding: 20, borderTop: '3px solid #6A4DD4' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <Icon name="scan" size={20} />
          <h3 style={{ margin: 0 }}>{p.title || '未命名 PDF'}</h3>
          {p.doc_type && <span className="badge" style={{ background: '#6A4DD41A', color: '#5A3FC0' }}>{p.doc_type}</span>}
          <div style={{ flex: 1 }} />
          {p.url && (
            <a className="btn btn-ghost btn-sm" href={p.url} target="_blank" rel="noreferrer">
              <Icon name="external" size={14} /> 在飞书打开
            </a>
          )}
        </div>

        <div style={{ display: 'flex', gap: 24, marginTop: 16, flexWrap: 'wrap' }}>
          <Stat label="总页数" value={p.page_count} />
          <Stat label="已分析" value={p.analyzed_pages} tone={p.truncated ? 'warning' : undefined} />
          <Stat label="扫描页" value={p.scanned_pages} />
          <Stat label="OCR 页" value={p.ocr_pages} />
          <Stat label="图示" value={p.figure_pages} />
          <Stat label="表格" value={tables.length} />
          <Stat label="正文字数" value={p.total_chars} />
        </div>
        {p.truncated && (
          <div style={{ fontSize: 12, color: 'var(--warning)', marginTop: 10 }}>
            ⚠ 文档较长，仅分析了前 {p.analyzed_pages} 页（共 {p.page_count} 页）。如需全部，可调大 max_pages。
          </div>
        )}
      </div>

      {/* AI 解读 */}
      <div className="card" style={{ padding: 20, borderTop: '3px solid var(--brand-500)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
          <Icon name="sparkle" size={18} />
          <h3 style={{ margin: 0 }}>AI 识别摘要</h3>
        </div>
        {llm.error ? (
          <div style={{ color: 'var(--text-tertiary)', fontSize: 13 }}>AI 解读失败（{llm.error}），仅展示下方抽取结果。</div>
        ) : llm.skipped ? (
          <div style={{ color: 'var(--text-tertiary)', fontSize: 13 }}>已跳过 AI 解读。</div>
        ) : (
          <>
            {p.summary && <Markdown source={p.summary} style={{ color: 'var(--text-secondary)' }} />}
            {show.highlights && highlights.length > 0 && (
              <ul style={{ margin: '12px 0 0', paddingLeft: 20, color: 'var(--text-secondary)', lineHeight: 1.8 }}>
                {highlights.map((h, i) => <li key={i}>{h}</li>)}
              </ul>
            )}
            {!p.summary && (!show.highlights || highlights.length === 0) && (
              <div style={{ color: 'var(--text-tertiary)', fontSize: 13 }}>模型未给出摘要。</div>
            )}
          </>
        )}
      </div>

      {/* 合同金额测算（合同类）。仅「合同台账」模板视为用户明确诉求，空/错时也提示；其余模板空结果隐藏。 */}
      {show.finance && <ContractFinanceCard f={p.finance} explicit={tpl === 'contract'} />}

      {/* 关键字段 */}
      {show.fields && keyFields.length > 0 && (
        <div className="card" style={{ padding: 20 }}>
          <h3 style={{ marginTop: 0 }}>关键字段 <span style={{ fontWeight: 400, fontSize: 14, color: 'var(--text-tertiary)' }}>· {keyFields.length} 项</span></h3>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: 10 }}>
            {keyFields.map((f, i) => (
              <div key={i} style={{ padding: '10px 14px', background: 'var(--surface-subtle)', borderRadius: 8, borderLeft: '3px solid #6A4DD4' }}>
                <div className="label" style={{ marginBottom: 2 }}>{f.name}</div>
                <div style={{ fontSize: 14, color: 'var(--text-primary)', wordBreak: 'break-word' }}>{f.value || '—'}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 表格 */}
      {show.tables && tables.map((t, ti) => (
        <div key={ti} className="card" style={{ padding: 20 }}>
          <h3 style={{ marginTop: 0, marginBottom: 4 }}>
            {t.title || `表格 ${ti + 1}`}
            <span style={{ fontWeight: 400, fontSize: 13, color: 'var(--text-tertiary)', marginLeft: 8 }}>
              第 {t.page} 页 · {t.n_rows} 行 × {t.n_cols} 列
            </span>
          </h3>
          {t.insight && <div style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 10 }}>{t.insight}</div>}
          <div style={{ overflowX: 'auto' }}>
            <table className="table" style={{ fontSize: 12, whiteSpace: 'nowrap' }}>
              <thead><tr>{(t.headers || []).map((h: string, i: number) => <th key={i}>{h}</th>)}</tr></thead>
              <tbody>
                {(t.rows || []).map((row: any[], ri: number) => (
                  <tr key={ri}>{row.map((cell, ci) => <td key={ci} style={{ maxWidth: 240, overflow: 'hidden', textOverflow: 'ellipsis' }} title={cell}>{cell}</td>)}</tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}

      {/* 逐页要点 */}
      {show.pages && pagePoints.length > 0 && (
        <div className="card" style={{ padding: 20 }}>
          <h3 style={{ marginTop: 0 }}>逐页要点</h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {pagePoints.map((pp, i) => (
              <div key={i} style={{ display: 'flex', gap: 12, alignItems: 'flex-start' }}>
                <span style={{ fontSize: 11, padding: '2px 9px', borderRadius: 10, background: '#6A4DD41A', color: '#5A3FC0', flexShrink: 0, marginTop: 2 }}>
                  P{pp.page}
                </span>
                <ul style={{ margin: 0, paddingLeft: 18, color: 'var(--text-secondary)', lineHeight: 1.7, fontSize: 13 }}>
                  {(pp.points || []).map((pt: string, pi: number) => <li key={pi}>{pt}</li>)}
                </ul>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 图表说明 */}
      {show.figures && figures.length > 0 && (
        <div className="card" style={{ padding: 20 }}>
          <h3 style={{ marginTop: 0 }}>图表说明 <span style={{ fontWeight: 400, fontSize: 14, color: 'var(--text-tertiary)' }}>· {figures.length} 张</span></h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {figures.map((f, i) => (
              <div key={i} style={{ padding: '10px 14px', background: 'var(--surface-subtle)', borderRadius: 'var(--radius-lg)', fontSize: 13 }}>
                <div style={{ color: 'var(--text-tertiary)', marginBottom: 4 }}>🖼 第 {f.page} 页</div>
                <Markdown source={f.desc} style={{ color: 'var(--text-secondary)' }} />
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 全文预览 */}
      {p.full_text_preview && (
        <div className="card" style={{ padding: 20 }}>
          <details>
            <summary style={{ cursor: 'pointer', fontWeight: 600 }}>全文预览（前 {p.full_text_preview.length} 字）</summary>
            <pre style={{
              marginTop: 12, whiteSpace: 'pre-wrap', wordBreak: 'break-word',
              fontFamily: 'var(--font-mono)', fontSize: 12, lineHeight: 1.6,
              color: 'var(--text-secondary)', maxHeight: 420, overflowY: 'auto',
              background: 'var(--surface-subtle)', padding: 12, borderRadius: 8,
            }}>{p.full_text_preview}</pre>
          </details>
        </div>
      )}
    </div>
  );
};

// ── 会议纪要结果渲染 ──────────────────────────────────────────────

const MEETING_ACCENT = '#EA580C';

const MeetingMinutesResult: React.FC<{ p: any; taskId?: string | null; onDispatch?: (taskId: string) => void }> = ({ p, taskId, onDispatch }) => {
  const llm = p.llm || {};
  const decisions: string[] = p.decisions || [];
  const actions: any[] = p.action_items || [];
  const risks: string[] = p.risks || [];
  const attendees: string[] = p.attendees || [];
  const figures: string[] = p.figures || [];
  const durationMin = p.duration_ms ? Math.round(Number(p.duration_ms) / 60000) : null;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {/* 概览 */}
      <div className="card" style={{ padding: 20, borderTop: `3px solid ${MEETING_ACCENT}` }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <Icon name="mic" size={20} />
          <h3 style={{ margin: 0 }}>{p.title || '未命名会议'}</h3>
          {p.source_type && <span className="badge" style={{ background: '#EA580C1A', color: '#B4470E' }}>{p.source_type}</span>}
          <div style={{ flex: 1 }} />
          {taskId && onDispatch && (p.summary || decisions.length > 0 || actions.length > 0) && (
            <button className="btn btn-tonal btn-sm" onClick={() => onDispatch(taskId)} title="进「协作分发」：把会议行动项 / 摘要改写成飞书任务 / 群消息，逐项勾选确认后分发">
              <Icon name="send" size={13} /> 分发到飞书
            </button>
          )}
          {p.url && (
            <a className="btn btn-ghost btn-sm" href={p.url} target="_blank" rel="noreferrer">
              <Icon name="external" size={14} /> 在飞书打开
            </a>
          )}
        </div>
        <div style={{ display: 'flex', gap: 24, marginTop: 16, flexWrap: 'wrap' }}>
          <Stat label="决策" value={decisions.length} />
          <Stat label="行动项" value={actions.length} />
          <Stat label="风险" value={risks.length} />
          {durationMin != null && <Stat label="时长(分)" value={durationMin} />}
          <Stat label="正文字数" value={p.char_count} />
          {figures.length > 0 && <Stat label="图示OCR" value={figures.length} />}
        </div>
        {attendees.length > 0 && (
          <div style={{ marginTop: 12, fontSize: 13, color: 'var(--text-secondary)' }}>
            参会人：{attendees.join('、')}
          </div>
        )}
      </div>

      {/* 会议摘要 */}
      <div className="card" style={{ padding: 20, borderTop: '3px solid var(--brand-500)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
          <Icon name="sparkle" size={18} />
          <h3 style={{ margin: 0 }}>会议摘要</h3>
        </div>
        {llm.error ? (
          <div style={{ color: 'var(--text-tertiary)', fontSize: 13 }}>AI 整理失败（{llm.error}），仅展示下方正文预览。</div>
        ) : llm.skipped ? (
          <div style={{ color: 'var(--text-tertiary)', fontSize: 13 }}>已跳过 AI 整理。</div>
        ) : p.summary ? (
          <Markdown source={p.summary} style={{ color: 'var(--text-secondary)' }} />
        ) : (
          <div style={{ color: 'var(--text-tertiary)', fontSize: 13 }}>模型未给出摘要。</div>
        )}
      </div>

      {/* 决策事项 */}
      {decisions.length > 0 && (
        <div className="card" style={{ padding: 20 }}>
          <h3 style={{ marginTop: 0 }}>决策事项 <span style={{ fontWeight: 400, fontSize: 14, color: 'var(--text-tertiary)' }}>· {decisions.length} 项</span></h3>
          <ol style={{ margin: 0, paddingLeft: 20, color: 'var(--text-secondary)', lineHeight: 1.9 }}>
            {decisions.map((d, i) => <li key={i}>{d}</li>)}
          </ol>
        </div>
      )}

      {/* 行动项 */}
      {actions.length > 0 && (
        <div className="card" style={{ padding: 20 }}>
          <h3 style={{ marginTop: 0 }}>行动项 <span style={{ fontWeight: 400, fontSize: 14, color: 'var(--text-tertiary)' }}>· {actions.length} 项</span></h3>
          <div style={{ overflowX: 'auto' }}>
            <table className="table" style={{ fontSize: 13 }}>
              <thead><tr><th>行动项</th><th style={{ width: 110 }}>负责人</th><th style={{ width: 120 }}>截止</th><th>备注</th></tr></thead>
              <tbody>
                {actions.map((a, i) => (
                  <tr key={i}>
                    <td style={{ color: 'var(--text-primary)' }}>{a.task}</td>
                    <td>{a.owner ? a.owner : <span style={{ color: 'var(--text-tertiary)' }}>未指派</span>}</td>
                    <td className="mono" style={{ fontSize: 12 }}>{a.due ? a.due : <span style={{ color: 'var(--text-tertiary)' }}>—</span>}</td>
                    <td style={{ color: 'var(--text-tertiary)', fontSize: 12 }}>{a.note || ''}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginTop: 8 }}>
            负责人 / 截止仅在会议内容明确时填写，未指派的留空——不臆造。
          </div>
        </div>
      )}

      {/* 风险与阻塞 */}
      {risks.length > 0 && (
        <div className="card" style={{ padding: 20, borderLeft: '3px solid var(--warning)' }}>
          <h3 style={{ marginTop: 0 }}>风险与阻塞 <span style={{ fontWeight: 400, fontSize: 14, color: 'var(--text-tertiary)' }}>· {risks.length} 项</span></h3>
          <ul style={{ margin: 0, paddingLeft: 20, color: 'var(--text-secondary)', lineHeight: 1.9 }}>
            {risks.map((r, i) => <li key={i}>{r}</li>)}
          </ul>
        </div>
      )}

      {/* 图示识别（文档内嵌图片 OCR / 图示，已并入正文供整理） */}
      {figures.length > 0 && (
        <div className="card" style={{ padding: 20 }}>
          <h3 style={{ marginTop: 0 }}>
            图示识别 <span style={{ fontWeight: 400, fontSize: 14, color: 'var(--text-tertiary)' }}>· {figures.length} 张（已并入正文参与整理）</span>
          </h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {figures.map((f, i) => (
              <div key={i} style={{ padding: '10px 14px', background: 'var(--surface-subtle)', borderRadius: 'var(--radius-lg)' }}>
                <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--text-tertiary)', marginBottom: 4 }}>图 {i + 1}</div>
                <Markdown source={f} style={{ color: 'var(--text-secondary)' }} />
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 原始内容预览 */}
      {p.content_preview && (
        <div className="card" style={{ padding: 20 }}>
          <details>
            <summary style={{ cursor: 'pointer', fontWeight: 600 }}>会议原文预览（前 {p.content_preview.length} 字）</summary>
            <pre style={{
              marginTop: 12, whiteSpace: 'pre-wrap', wordBreak: 'break-word',
              fontFamily: 'var(--font-mono)', fontSize: 12, lineHeight: 1.6,
              color: 'var(--text-secondary)', maxHeight: 420, overflowY: 'auto',
              background: 'var(--surface-subtle)', padding: 12, borderRadius: 8,
            }}>{p.content_preview}</pre>
          </details>
        </div>
      )}
    </div>
  );
};

// ── 协作分发结果渲染 ──────────────────────────────────────────────

const DISPATCH_ACCENT = '#C83A3A';

const KIND_LABEL: Record<string, string> = { notice: '通知 / 同步', digest: '摘要 / 总结', action: '含待办' };

const CollabDispatchResult: React.FC<{ p: any }> = ({ p }) => {
  const tasks: any[] = p.tasks || [];
  const message: string = p.message || '';
  const kindLabel = KIND_LABEL[p.kind] || '';
  const infoOnly = ['notice', 'digest'].includes((p.kind || '').toLowerCase());
  const srcLabel = p.source?.agent_id ? (AGENT_META[p.source.agent_id]?.title || p.source.agent_id) : '';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div className="card" style={{ padding: 20, borderTop: `3px solid ${DISPATCH_ACCENT}` }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <Icon name="send" size={20} />
          <h3 style={{ margin: 0 }}>分发草稿</h3>
          {kindLabel && <span className="badge badge-brand">{kindLabel}</span>}
          {srcLabel && <span className="badge">来源 · {srcLabel}</span>}
        </div>
        <div style={{ display: 'flex', gap: 24, marginTop: 16, flexWrap: 'wrap' }}>
          <Stat label="待建任务" value={p.task_dispatch_count ?? tasks.length} />
          <Stat label="群消息" value={p.message ? 1 : 0} />
          <Stat label="分发项合计" value={p.dispatch_count} />
        </div>
        {p.llm_error && (
          <div style={{ fontSize: 12, color: 'var(--warning)', marginTop: 10 }}>
            AI 生成失败（{p.llm_error}）。
          </div>
        )}
        {!p.llm_error && tasks.length === 0 && (
          <div style={{ fontSize: 13, color: 'var(--text-secondary)', marginTop: 10, lineHeight: 1.6 }}>
            AI 判断这是<strong>信息型</strong>内容——无需建任务，发一条群消息同步即可。
          </div>
        )}
        <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginTop: 10 }}>
          点右上「写回飞书」确认后才会真正发消息 / 建任务。
        </div>
      </div>

      {/* 群消息草稿 */}
      {message && (
        <div className="card" style={{ padding: 20 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8, flexWrap: 'wrap' }}>
            <Icon name="send" size={18} />
            <h3 style={{ margin: 0 }}>群消息草稿</h3>
            <span className="badge">目标群在「写回飞书」时选择</span>
          </div>
          <pre style={{
            whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: 0,
            fontFamily: 'inherit', fontSize: 13, lineHeight: 1.7, color: 'var(--text-secondary)',
            background: 'var(--surface-subtle)', padding: 14, borderRadius: 8,
          }}>{message}</pre>
        </div>
      )}

      {/* 任务草稿 */}
      {tasks.length > 0 && (
        <div className="card" style={{ padding: 20 }}>
          <h3 style={{ marginTop: 0 }}>任务草稿 <span style={{ fontWeight: 400, fontSize: 14, color: 'var(--text-tertiary)' }}>· {tasks.length} 条</span></h3>
          {infoOnly && (
            <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 8, lineHeight: 1.6 }}>
              AI 判断为信息型，这些是<strong>候选任务</strong>——「写回飞书」时默认不建，需要哪条再勾选。
            </div>
          )}
          <div style={{ overflowX: 'auto' }}>
            <table className="table" style={{ fontSize: 13 }}>
              <thead><tr><th>任务</th><th style={{ width: 110 }}>负责人</th><th style={{ width: 120 }}>截止</th><th>备注</th></tr></thead>
              <tbody>
                {tasks.map((t, i) => (
                  <tr key={i}>
                    <td style={{ color: 'var(--text-primary)' }}>{t.title}</td>
                    <td>{t.owner ? t.owner : <span style={{ color: 'var(--text-tertiary)' }}>—</span>}</td>
                    <td className="mono" style={{ fontSize: 12 }}>{t.due ? t.due : <span style={{ color: 'var(--text-tertiary)' }}>未定</span>}</td>
                    <td style={{ color: 'var(--text-tertiary)', fontSize: 12 }}>{t.note || ''}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {!message && tasks.length === 0 && (
        <div className="card" style={{ padding: 20, color: 'var(--text-tertiary)' }}>
          没有生成可分发的内容。换个素材，或同时勾选「生成群消息 / 任务」再试。
        </div>
      )}
    </div>
  );
};

export default TaskAgentPage;
