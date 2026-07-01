// 全局通知系统 —— 轻量 toast + 顶栏铃铛通知中心。
// 任何组件用 useToast() 取得 push/success/error/... 即可弹出提示；
// 弹出的提示同时进入 history，供顶栏铃铛回看（未读计数 + 下拉面板）。
//
// 关键：动作 API（useToast）与历史（useToastCenter）拆成两个 context。
// 动作对象保持稳定引用（push 数次也不变），这样把 toast 放进 useEffect 依赖
// 也不会触发重订阅 —— 否则会出现「弹一条 → 重订阅 → 再弹一条」的雪崩。
import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { Icon } from './icons';

export type ToastKind = 'success' | 'error' | 'warning' | 'info';

export interface ToastItem {
  id: string;
  kind: ToastKind;
  title: string;
  detail?: string;
  ts: number;
  read: boolean;
}

interface PushOptions {
  detail?: string;
  duration?: number; // ms；0 = 不自动消失
}

// 稳定的动作 API（引用不随 history 变化）。
interface ToastActions {
  push: (kind: ToastKind, title: string, opts?: PushOptions) => string;
  success: (title: string, opts?: PushOptions) => string;
  error: (title: string, opts?: PushOptions) => string;
  warning: (title: string, opts?: PushOptions) => string;
  info: (title: string, opts?: PushOptions) => string;
  dismiss: (id: string) => void;
}

// 通知中心（铃铛）所需的状态，随 history 变化。
interface ToastCenter {
  history: ToastItem[];
  unread: number;
  markAllRead: () => void;
  clearHistory: () => void;
}

const ToastActionsCtx = createContext<ToastActions | null>(null);
const ToastCenterCtx = createContext<ToastCenter | null>(null);

export function useToast(): ToastActions {
  const ctx = useContext(ToastActionsCtx);
  if (!ctx) throw new Error('useToast 必须在 <ToastProvider> 内使用');
  return ctx;
}

export function useToastCenter(): ToastCenter {
  const ctx = useContext(ToastCenterCtx);
  if (!ctx) throw new Error('useToastCenter 必须在 <ToastProvider> 内使用');
  return ctx;
}

const KIND_META: Record<ToastKind, { icon: string; color: string }> = {
  success: { icon: 'check', color: 'var(--success)' },
  error: { icon: 'warning', color: 'var(--error)' },
  warning: { icon: 'warning', color: 'var(--warning)' },
  info: { icon: 'bell', color: 'var(--info)' },
};

const DEFAULT_DURATION: Record<ToastKind, number> = {
  success: 3200,
  info: 4200,
  warning: 6000,
  error: 8000,
};

const MAX_HISTORY = 50;

export const ToastProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const [history, setHistory] = useState<ToastItem[]>([]);
  const timers = useRef<Record<string, number>>({});

  const dismiss = useCallback((id: string) => {
    setToasts(prev => prev.filter(t => t.id !== id));
    const tm = timers.current[id];
    if (tm) {
      clearTimeout(tm);
      delete timers.current[id];
    }
  }, []);

  const push = useCallback(
    (kind: ToastKind, title: string, opts?: PushOptions): string => {
      const id = `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
      const item: ToastItem = { id, kind, title, detail: opts?.detail, ts: Date.now(), read: false };
      setToasts(prev => [...prev, item]);
      setHistory(prev => [item, ...prev].slice(0, MAX_HISTORY));
      const duration = opts?.duration ?? DEFAULT_DURATION[kind];
      if (duration > 0) {
        timers.current[id] = window.setTimeout(() => dismiss(id), duration);
      }
      return id;
    },
    [dismiss],
  );

  const markAllRead = useCallback(
    () => setHistory(prev => (prev.some(h => !h.read) ? prev.map(h => ({ ...h, read: true })) : prev)),
    [],
  );
  const clearHistory = useCallback(() => setHistory([]), []);

  // 稳定的动作对象：仅依赖 push/dismiss（二者皆为稳定的 useCallback），
  // 因此弹再多 toast 这个引用都不会变。
  const actions = useMemo<ToastActions>(
    () => ({
      push,
      success: (t, o) => push('success', t, o),
      error: (t, o) => push('error', t, o),
      warning: (t, o) => push('warning', t, o),
      info: (t, o) => push('info', t, o),
      dismiss,
    }),
    [push, dismiss],
  );

  const center = useMemo<ToastCenter>(
    () => ({
      history,
      unread: history.filter(h => !h.read).length,
      markAllRead,
      clearHistory,
    }),
    [history, markAllRead, clearHistory],
  );

  return (
    <ToastActionsCtx.Provider value={actions}>
      <ToastCenterCtx.Provider value={center}>
        {children}
        <div className="toast-viewport" aria-live="polite" aria-atomic="false">
          {toasts.map(t => {
            const m = KIND_META[t.kind];
            return (
              <div key={t.id} className="toast" role="status" style={{ borderLeftColor: m.color }}>
                <span className="toast-icon" style={{ color: m.color }}>
                  <Icon name={m.icon} size={16} />
                </span>
                <div className="toast-body">
                  <div className="toast-title">{t.title}</div>
                  {t.detail && <div className="toast-detail">{t.detail}</div>}
                </div>
                <button className="toast-close" onClick={() => dismiss(t.id)} title="关闭" aria-label="关闭">
                  <Icon name="x" size={13} />
                </button>
              </div>
            );
          })}
        </div>
      </ToastCenterCtx.Provider>
    </ToastActionsCtx.Provider>
  );
};

function relTime(ts: number): string {
  const s = Math.floor((Date.now() - ts) / 1000);
  if (s < 30) return '刚刚';
  if (s < 60) return `${s} 秒前`;
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  return new Date(ts).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

// 顶栏铃铛 —— 显示未读数，点击展开通知中心。替代原先的死按钮。
export const NotificationBell: React.FC = () => {
  const { history, unread, markAllRead, clearHistory } = useToastCenter();
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  function toggle() {
    setOpen(o => {
      const next = !o;
      if (next) window.setTimeout(markAllRead, 1200);
      return next;
    });
  }

  // 点击面板以外的任意区域（或按 Esc）即关闭。
  // 不能只靠一个 position:fixed 的遮罩层 —— 顶栏有 backdrop-filter，会成为
  // fixed 定位的包含块，遮罩因此只盖住顶栏那一条，点内容区关不掉。这里直接在
  // document 上监听，凭 wrapRef 判断点击是否落在铃铛+面板之外。
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: PointerEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  return (
    <div ref={wrapRef} style={{ position: 'relative' }}>
      <button
        className="btn btn-ghost btn-icon"
        title="通知"
        aria-label={unread > 0 ? `通知（${unread} 条未读）` : '通知'}
        onClick={toggle}
        style={{ position: 'relative' }}
      >
        <Icon name="bell" size={16} />
        {unread > 0 && <span className="notif-badge">{unread > 9 ? '9+' : unread}</span>}
      </button>
      {open && (
        <div className="notif-panel" role="dialog" aria-label="通知中心">
            <div className="notif-head">
              <span style={{ fontWeight: 600, fontSize: 13 }}>通知</span>
              <div style={{ flex: 1 }} />
              {history.length > 0 && (
                <button className="btn btn-ghost btn-sm" onClick={() => { clearHistory(); setOpen(false); }}>
                  清空
                </button>
              )}
            </div>
            <div className="notif-list scroll">
              {history.length === 0 && <div className="notif-empty">暂无通知</div>}
              {history.map(h => {
                const m = KIND_META[h.kind];
                return (
                  <div key={h.id} className="notif-item">
                    <span style={{ color: m.color, marginTop: 1, flexShrink: 0 }}>
                      <Icon name={m.icon} size={15} />
                    </span>
                    <div style={{ minWidth: 0, flex: 1 }}>
                      <div className="notif-title">{h.title}</div>
                      {h.detail && <div className="notif-detail">{h.detail}</div>}
                      <div className="notif-time">{relTime(h.ts)}</div>
                    </div>
                  </div>
                );
              })}
            </div>
        </div>
      )}
    </div>
  );
};

export default ToastProvider;
