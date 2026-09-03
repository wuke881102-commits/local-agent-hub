/// <reference types="vite/client" />
// 简单的 fetch 封装。开发期通过 Vite proxy 转发到 127.0.0.1:8787。

const BASE = (import.meta as any).env?.VITE_API_BASE ?? '';

async function handle<T>(r: Response): Promise<T> {
  if (!r.ok) {
    const text = await r.text().catch(() => r.statusText);
    throw new Error(`${r.status} ${text.slice(0, 240)}`);
  }
  const ct = r.headers.get('content-type') || '';
  return (ct.includes('application/json') ? r.json() : r.text()) as Promise<T>;
}

export const api = {
  get: <T,>(path: string) => fetch(BASE + path).then(handle<T>),
  post: <T,>(path: string, body?: unknown) =>
    fetch(BASE + path, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: body == null ? undefined : JSON.stringify(body),
    }).then(handle<T>),
  del: <T,>(path: string) =>
    fetch(BASE + path, { method: 'DELETE' }).then(handle<T>),
};

// 通用 SWR fetcher。SWR 通过 useSWR<T> 的泛型推断 T。
export const fetcher = <T = unknown>(path: string): Promise<T> => api.get<T>(path);

// 把任意 catch 到的错误规整成一句人话，给 toast 用。
export function errMsg(e: unknown): string {
  if (e instanceof Error) return e.message;
  if (typeof e === 'string') return e;
  try {
    return JSON.stringify(e);
  } catch {
    return String(e);
  }
}

export type AuthStatus = {
  authenticated: boolean;        // true 仅当 user identity ready
  stage?: 'needs_init' | 'needs_login' | 'authenticated';
  user_id?: string;
  user_name?: string;
  scopes?: string[];
  app_id?: string;
  brand?: string;
  bot_ready?: boolean;
  needs_init?: boolean;
  needs_login?: boolean;
  hint?: string;
  user_status_message?: string;
  mock_mode?: boolean;
  mock?: boolean;
  error?: string;
};

export type AgentInfo = {
  id: string;
  name: string;
  desc: string;
  writeback: boolean;
  status: string;
  icon?: string;
  color?: string;
  entries?: string[];
  featured?: boolean;
};

export type Scene = {
  id: string;
  title: string;
  subtitle: string;
  agents: string[];
  accent: string;
  icon: string;
  featured?: boolean;
};

export type Asset = {
  asset_id: string;
  type: string;
  title: string;
  url: string;
  owner: string;
  owner_id?: string;
  updated: string;
  space: string;
  tags: string[];
  category: string;
  summary: string;
};

export type TaskSummary = {
  id: string;
  agent_id: string;
  scene: string;
  target: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  error: string | null;
  writeback: string;
  source?: 'local' | 'feishu' | '';
};

export type WritebackProposal = {
  id?: string;
  action_type: string;
  target: string;
  payload: any;
  status?: string;
};

export type TaskDetail = {
  id: string;
  agent_id: string;
  scene: string;
  target: string;
  inputs: any;
  status: string;
  started_at: string;
  finished_at: string | null;
  result_path: string | null;
  error: string | null;
  payload: any | null;
  writeback: WritebackProposal | null;
};

export type Diagnostics = {
  cli: { available: boolean; version: string | null; mode: string; bin: string };
  auth: AuthStatus;
  /** image 只报配置状态（生图不做 live 探测，images.generate 真扣费）；语音不在 ping 覆盖里，只在 env 里。 */
  llm: { text: any; vision: any; image?: any; mock: boolean };
  index: any;
  audit_recent: any[];
  env: any;
};

// SSE helper
/** POST /api/assets/refresh 的返回。刷新索引会顺带排一次「摘要标签回填」。 */
export type RefreshResult = {
  ok: boolean;
  enrich?: {
    started: boolean;
    /** 没起的原因：nothing 全都回填过了 / running 已经在跑 / mock 文字模型是假的 / error 出错了 */
    reason: '' | 'nothing' | 'running' | 'mock' | 'error';
    pending: number;
    task_id: string;
    error?: string;
  };
};

/**
 * 把回填的触发结果说成一句人话，给刷新按钮的 toast 用。
 *
 * 三个刷新入口（工作台 / 飞书文档 / 历史总结）共用这一份，免得各写各的说法。
 * **每种情况都要有话说**：自动触发最糟的失败方式是悄悄没跑，而用户以为跑了。
 */
export function enrichDetail(r?: RefreshResult['enrich']): string {
  if (!r) return '';
  if (r.started) return `摘要标签回填已在后台开始（待补 ${r.pending} 条），去「运行记录」看进度`;
  switch (r.reason) {
    case 'nothing':  return '摘要标签都是全的，这次不用回填';
    case 'running':  return '摘要标签回填已经在跑，这次不重复触发';
    case 'mock':     return '文字模型当前是 mock，跳过回填（否则会把假摘要写进索引）';
    case 'error':    return `摘要标签回填没起来：${r.error || '未知原因'}`;
    default:         return '';
  }
}

export function subscribeTask(
  taskId: string,
  onMessage: (entry: any) => void,
  // info.error 区分「正常收到 _done 收尾」与「连接中断」，便于上层决定是否报警。
  onClose?: (info: { error: boolean }) => void,
): () => void {
  const es = new EventSource(`${BASE}/api/tasks/${taskId}/stream`);
  let settled = false; // onClose 只触发一次；卸载时也置位以静默收尾
  const settle = (error: boolean) => {
    if (settled) return;
    settled = true;
    es.close();
    onClose?.({ error });
  };
  es.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data);
      onMessage(data);
      if (data._done) settle(false);
    } catch {
      /* ignore */
    }
  };
  // 未收到 _done 就触发 onerror = 连接真的断了（而非任务正常结束）。
  es.onerror = () => settle(true);
  return () => {
    settled = true;
    es.close();
  };
}
