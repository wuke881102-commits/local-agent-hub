import { useEffect, useRef } from 'react';
import useSWR from 'swr';
import { fetcher, TaskSummary } from '../api';
import { useToast } from '../components/Toast';

// 进行中的状态：处于这些状态的任务还没跑完，不发"完成"通知。
const LIVE = new Set(['queued', 'running', 'pending', '']);

// 终态文案：任务从进行中切到这些状态时，往铃铛 + toast 里推一条。
const DONE_VERB: Record<string, string> = {
  done: '完成',
  preview: '已生成预览',
  review: '待审核',
};

/**
 * 全局任务完成提醒。
 *
 * 在顶栏（WorkbenchShell）里挂一次即可，跨页面常驻。原理：
 *   1. 轮询 /api/tasks，记录每个任务上次见到的状态；
 *   2. 当某个任务从「进行中」切到「终态」（完成 / 预览 / 待审 / 失败）时，
 *      推一条通知——成功用 success，失败用 error。
 *   3. 首次拿到数据只做「播种」：把当前状态记下来但不发通知，
 *      否则一打开应用就会把历史已完成任务全刷一遍。
 */
export function useTaskNotifications(): void {
  const toast = useToast(); // 稳定引用，放进依赖不会触发重订阅
  const { data } = useSWR<{ items: TaskSummary[] }>(
    '/api/tasks?limit=20',
    fetcher,
    { refreshInterval: 6000 },
  );

  const seen = useRef<Map<string, string>>(new Map());
  const seeded = useRef(false);

  useEffect(() => {
    const items = data?.items;
    if (!items) return;

    // 首屏播种：只记录，不补发历史通知。
    if (!seeded.current) {
      for (const t of items) seen.current.set(t.id, t.status);
      seeded.current = true;
      return;
    }

    for (const t of items) {
      const prev = seen.current.get(t.id);
      const wasLive = prev === undefined || LIVE.has(prev); // 新任务（首见即终态）也算"曾在进行中"
      const nowLive = LIVE.has(t.status);

      // 进行中 → 终态：发一条。终态 → 另一终态（如 preview→done）不重复发。
      if (wasLive && !nowLive && prev !== t.status) {
        const where = t.scene || t.agent_id || '任务';
        const label = t.target && t.target !== '—' ? t.target : where;
        const detail = label === where ? where : `${where} · ${label}`;

        if ((t.status || '').toLowerCase() === 'failed') {
          toast.error('任务失败', { detail });
        } else {
          toast.success(`任务${DONE_VERB[t.status] || '完成'}`, { detail });
        }
      }

      seen.current.set(t.id, t.status);
    }
  }, [data, toast]);
}
