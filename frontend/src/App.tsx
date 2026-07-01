import React from 'react';
import { Navigate, Route, Routes, useLocation, useNavigate, useParams } from 'react-router-dom';
import WorkbenchShell from './components/shell/WorkbenchShell';
import Dashboard from './pages/Dashboard';
import TaskHtmlPage from './pages/TaskHtmlPage';
import AssetsPage from './pages/AssetsPage';
import DiagnosticsPage from './pages/DiagnosticsPage';
import TasksPage from './pages/TasksPage';
import ScenesPage from './pages/ScenesPage';
import TaskAgentPage from './pages/TaskAgentPage';
import OrgGraphPage from './pages/OrgGraphPage';
import SummariesPage from './pages/SummariesPage';
import LocalDirPage from './pages/LocalDirPage';
import AutoExtractPage from './pages/AutoExtractPage';

// 会议纪要 / 协作分发 / PDF 等都共用 TaskAgentPage 这一个路由组件。跨 agent 跳转
// （如 会议纪要「分发 / 沉淀飞书」→ 协作分发）不会触发重挂载，导致 taskId、来源任务
// 等 useState 初始值残留上一个 agent 的值（表现：协作分发页错显上一个会议任务、自动分发不触发）。
// 按 agentId 加 key：跨 agent 时强制重挂载、状态归零；同一 agent 内换任务（run 后跳到新任务 id）
// agentId 不变 → 不重挂载，保留模板 / 阈值等左侧输入。
const TaskAgentRoute: React.FC = () => {
  const { agentId } = useParams();
  return <TaskAgentPage key={agentId} />;
};

const App: React.FC = () => {
  const loc = useLocation();
  const nav = useNavigate();

  const crumb: { label: string; onClick?: () => void }[] = [{ label: '工作台', onClick: () => nav('/') }];
  const path = loc.pathname;
  if (path === '/') crumb.length = 1;
  else if (path.startsWith('/task/html-page')) crumb.push({ label: '内容生成', onClick: () => nav('/scenes') }, { label: 'HTML 页面生成' });
  else if (path.startsWith('/task/document-map')) crumb.push({ label: '知识库治理', onClick: () => nav('/scenes') }, { label: '文档地图' });
  else if (path.startsWith('/task/index-enrich')) crumb.push({ label: '知识库治理', onClick: () => nav('/scenes') }, { label: '摘要 / 标签回填' });
  else if (path.startsWith('/task/knowledge-governance')) crumb.push({ label: '知识库治理', onClick: () => nav('/scenes') }, { label: '知识治理' });
  else if (path.startsWith('/task/base-analysis')) crumb.push({ label: '表格分析', onClick: () => nav('/scenes') }, { label: '多维表格分析' });
  else if (path.startsWith('/task/meeting-minutes')) crumb.push({ label: '会议沉淀', onClick: () => nav('/scenes') }, { label: '会议纪要' });
  else if (path.startsWith('/task/pdf-recognition')) crumb.push({ label: 'PDF 识别', onClick: () => nav('/scenes') }, { label: 'PDF 识别' });
  else if (path.startsWith('/task/collab-dispatch')) crumb.push({ label: '协作分发', onClick: () => nav('/scenes') }, { label: '协作分发' });
  else if (path.startsWith('/task/local-image')) crumb.push({ label: '内容生成', onClick: () => nav('/scenes') }, { label: 'HTML 页面生成' });
  else if (path.startsWith('/task')) crumb.push({ label: '任务详情' });
  else if (path.startsWith('/assets')) crumb.push({ label: '飞书文档' });
  else if (path.startsWith('/localdir')) crumb.push({ label: '本地目录' });
  else if (path.startsWith('/autoextract')) crumb.push({ label: '自动化提炼' });
  else if (path.startsWith('/summaries')) crumb.push({ label: '历史总结' });
  else if (path.startsWith('/org')) crumb.push({ label: '组织架构' });
  else if (path.startsWith('/scenes')) crumb.push({ label: '任务场景' });
  else if (path.startsWith('/tasks')) crumb.push({ label: '运行记录' });
  else if (path.startsWith('/diagnostics')) crumb.push({ label: '系统诊断' });

  return (
    <WorkbenchShell crumb={crumb}>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/scenes" element={<ScenesPage />} />
        <Route path="/assets" element={<AssetsPage />} />
        <Route path="/localdir" element={<LocalDirPage />} />
        <Route path="/autoextract" element={<AutoExtractPage />} />
        <Route path="/summaries" element={<SummariesPage />} />
        <Route path="/org" element={<OrgGraphPage />} />
        <Route path="/tasks" element={<TasksPage />} />
        <Route path="/diagnostics" element={<DiagnosticsPage />} />
        <Route path="/task/html-page" element={<TaskHtmlPage />} />
        <Route path="/task/html-page/:taskId" element={<TaskHtmlPage />} />
        <Route path="/task/local-image/:taskId" element={<TaskHtmlPage />} />
        <Route path="/task/:agentId" element={<TaskAgentRoute />} />
        <Route path="/task/:agentId/:taskId" element={<TaskAgentRoute />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </WorkbenchShell>
  );
};

export default App;
