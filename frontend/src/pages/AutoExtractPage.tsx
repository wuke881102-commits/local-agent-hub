import React, { useCallback, useEffect, useRef, useState } from 'react';
import { api, errMsg } from '../api';
import { Icon } from '../components/icons';
import { useToast } from '../components/Toast';
import { fileUrl } from '../components/LocalSourcePicker';
import QuickSendToChat from '../components/QuickSendToChat';

type Status = {
  active: boolean;
  interval_min: number;
  started_at: string;
  last_run_at: string;
  next_run_at: string;
  digest_count: number;
  error: string;
  busy: boolean;
  auto_stopped: boolean;
  directory: string;
  shot_count: number;
  window_shot_count: number;
  last_shot_at: string;
  capture_error: string;
};

type Digest = {
  id: string;
  created_at: string;
  window_label: string;
  n_shots: number;
  n_used: number;
  summary: string;
  highlights: string[];
  operations: string[];
  meetings: string[];
  todos: string[];
  apps: string[];
  markdown?: string;
  error?: boolean;
};

type Shot = { name: string; path: string; size: number; mtime: string };

const INTERVALS = [5, 15, 30, 60];

const SECTIONS: { key: keyof Digest; label: string; color: string; icon: string }[] = [
  { key: 'highlights', label: '重点', color: '#DB2777', icon: 'sparkle' },
  { key: 'operations', label: '操作', color: '#2563EB', icon: 'scan' },
  { key: 'meetings', label: '会议', color: '#EA580C', icon: 'mic' },
  { key: 'todos', label: '待办', color: '#0D9488', icon: 'check' },
];

function fmtTime(iso: string): string {
  if (!iso) return '';
  return iso.replace('T', ' ').slice(5, 16);
}

const AutoExtractPage: React.FC = () => {
  const toast = useToast();
  const [st, setSt] = useState<Status | null>(null);
  const [digests, setDigests] = useState<Digest[]>([]);
  const [shots, setShots] = useState<Shot[]>([]);
  const [interval, setIntervalMin] = useState<number>(15);
  const [busy, setBusy] = useState(false);
  const [showShots, setShowShots] = useState(false);
  const [shotScope, setShotScope] = useState<'session' | 'all'>('session');
  const [countdown, setCountdown] = useState<number>(0);

  const loadStatus = useCallback(async () => {
    try {
      const s = await api.get<Status>('/api/autoextract/status');
      setSt(s);
      if (!s.active && s.interval_min) setIntervalMin((v) => (s.interval_min ? s.interval_min : v));
    } catch { /* ignore */ }
  }, []);
  const loadDigests = useCallback(async () => {
    try { setDigests((await api.get<{ items: Digest[] }>('/api/autoextract/digests?limit=50')).items || []); }
    catch { /* ignore */ }
  }, []);
  const loadShots = useCallback(async () => {
    try { setShots((await api.get<{ items: Shot[] }>(`/api/autoextract/shots?limit=60&scope=${shotScope}`)).items || []); }
    catch { /* ignore */ }
  }, [shotScope]);

  useEffect(() => { loadStatus(); loadDigests(); loadShots(); }, [loadStatus, loadDigests, loadShots]);

  // 轮询状态 + 截图数（捕获中更勤）
  const active = !!st?.active;
  useEffect(() => {
    const t = window.setInterval(() => { loadStatus(); if (active) loadShots(); }, active ? 3000 : 8000);
    return () => window.clearInterval(t);
  }, [active, loadStatus, loadShots]);

  // 下一次提炼倒计时（本地 tick，每秒）
  const nextRef = useRef<string>('');
  nextRef.current = st?.next_run_at || '';
  useEffect(() => {
    const t = window.setInterval(() => {
      const n = nextRef.current;
      if (!n) { setCountdown(0); return; }
      const ms = new Date(n).getTime() - Date.now();
      setCountdown(ms > 0 ? Math.floor(ms / 1000) : 0);
    }, 1000);
    return () => window.clearInterval(t);
  }, []);

  const start = async () => {
    setBusy(true);
    try {
      const s = await api.post<Status>('/api/autoextract/start', { interval_min: interval });
      setSt(s);
      toast.success('已开启自动化提炼', { detail: `每 ${interval} 分钟自动提炼一次 · 在任意窗口按 Enter 留痕` });
    } catch (e) { toast.error('开启失败', { detail: errMsg(e) }); }
    finally { setBusy(false); }
  };
  const stop = async () => {
    setBusy(true);
    try { setSt(await api.post<Status>('/api/autoextract/stop', {})); toast.success('已停止'); }
    catch (e) { toast.error('停止失败', { detail: errMsg(e) }); }
    finally { setBusy(false); }
  };
  const distillNow = async () => {
    setBusy(true);
    try {
      const r = await api.post<{ ok: boolean; digest: Digest | null; message?: string }>('/api/autoextract/distill', {});
      if (r.digest) toast.success('提炼完成', { detail: `已基于 ${r.digest.n_shots} 张截图生成一条记录` });
      else toast.info('暂无新截图', { detail: r.message || '这段时间还没有新的截图。' });
      await Promise.all([loadStatus(), loadDigests(), loadShots()]);
    } catch (e) { toast.error('提炼失败', { detail: errMsg(e) }); }
    finally { setBusy(false); }
  };
  const revealFolder = async () => {
    try { await api.post('/api/autoextract/reveal', {}); }
    catch (e) { toast.error('打开文件夹失败', { detail: errMsg(e) }); }
  };
  const clearShots = async () => {
    try {
      const r = await api.del<{ cleared: number }>(`/api/autoextract/shots?scope=${shotScope}`);
      toast.success(shotScope === 'session' ? '已清空本次会话截图' : '已清空全部截图', { detail: `删除 ${r.cleared} 张` });
      await Promise.all([loadShots(), loadStatus()]);
    } catch (e) { toast.error('清空失败', { detail: errMsg(e) }); }
  };
  const clearDigests = async () => {
    try { await api.del('/api/autoextract/digests'); await Promise.all([loadStatus(), loadDigests()]); toast.success('已清空记录'); }
    catch (e) { toast.error('清空失败', { detail: errMsg(e) }); }
  };

  const fmtCountdown = (s: number) => {
    if (s <= 0) return '即将提炼…';
    const m = Math.floor(s / 60), sec = s % 60;
    return `${m}:${String(sec).padStart(2, '0')}`;
  };

  return (
    <div style={{ padding: 'var(--space-8)', maxWidth: 1080, margin: '0 auto' }}>
      <div style={{ marginBottom: 'var(--space-5)' }}>
        <h2 style={{ margin: 0, fontSize: 20, fontWeight: 600 }}>自动化提炼</h2>
        <div className="eyebrow" style={{ marginTop: 4 }}>
          工作中按 Enter 自动留痕截图 · 每隔一段时间用大模型提炼这段时间的工作（说明 / 重点 / 操作 / 会议）
        </div>
      </div>

      {/* 控制台 */}
      <div className="card" style={{ padding: 'var(--space-5)', marginBottom: 'var(--space-4)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16, flexWrap: 'wrap' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flex: 1, minWidth: 240 }}>
            <span style={{
              width: 38, height: 38, borderRadius: 11, flexShrink: 0,
              background: active ? '#0EA5E915' : 'var(--surface-subtle)',
              color: active ? '#0EA5E9' : 'var(--text-tertiary)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}>
              <Icon name="funnel" size={19} />
            </span>
            <div>
              <div style={{ fontWeight: 600, fontSize: 14, display: 'flex', alignItems: 'center', gap: 8 }}>
                {active ? '提炼进行中' : '未开启'}
                {active && <span className="pulse-dot" style={{ width: 8, height: 8, borderRadius: 4, background: 'var(--success)' }} />}
              </div>
              <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginTop: 2 }}>
                {active
                  ? <><span title="本次提炼频率窗口内、待下次提炼的截图数（每次提炼后归零重新累计）">已截 <strong className="tnum">{st?.window_shot_count ?? 0}</strong> 张</span> · {st?.busy ? '正在提炼…' : <>下次提炼 <strong className="tnum">{fmtCountdown(countdown)}</strong></>} · 最长 10 小时</>
                  : (st?.auto_stopped ? '已达最长 10 小时，已自动停止。可再次点「开始」' : '开启后，在任意窗口按 Enter 即截当前窗口（最长 10 小时自动停止）')}
              </div>
            </div>
          </div>

          {/* 频率选择（仅未开启时可改） */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ fontSize: 12.5, color: 'var(--text-secondary)' }}>提炼频率</span>
            <div style={{ display: 'inline-flex', gap: 4, background: 'var(--surface-subtle)', padding: 3, borderRadius: 9 }}>
              {INTERVALS.map((m) => {
                const sel = active ? st?.interval_min === m : interval === m;
                return (
                  <button key={m}
                    disabled={active || busy}
                    onClick={() => setIntervalMin(m)}
                    className="btn btn-sm"
                    style={{
                      background: sel ? 'var(--surface-elevated)' : 'transparent',
                      boxShadow: sel ? 'var(--shadow-sm)' : 'none',
                      color: sel ? 'var(--text-primary)' : 'var(--text-tertiary)',
                      fontWeight: sel ? 600 : 400, border: 'none',
                      cursor: active ? 'default' : 'pointer',
                    }}>
                    {m}分钟
                  </button>
                );
              })}
            </div>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            {!active ? (
              <button className="btn btn-primary btn-sm" disabled={busy} onClick={start}>
                <Icon name="camera" size={14} /> 开始
              </button>
            ) : (
              <button className="btn btn-sm" style={{ background: 'var(--error)', color: '#fff' }} disabled={busy} onClick={stop}>
                <Icon name="x" size={14} /> 停止
              </button>
            )}
            <button className="btn btn-secondary btn-sm" disabled={busy || st?.busy} onClick={distillNow}>
              <Icon name="sparkle" size={14} /> 立即提炼
            </button>
          </div>
        </div>

        {(st?.error || st?.capture_error) && (
          <div style={{ marginTop: 12, fontSize: 12, color: 'var(--error)' }}>
            <Icon name="warning" size={12} /> {st?.error || st?.capture_error}
          </div>
        )}

        <div style={{ marginTop: 14, fontSize: 11.5, color: 'var(--text-tertiary)', lineHeight: 1.6, background: 'var(--surface-subtle)', borderRadius: 8, padding: '9px 11px' }}>
          <Icon name="shield" size={12} /> 这些截图只用于本场景的自动提炼，<strong>不会出现在「内容生成」的文件选择里</strong>，存放于应用私有目录。
          捕获期间「全局」生效——在本应用里按 Enter 也会截一张，编辑文字时可先「停止」。
        </div>

        {st?.directory && (
          <div style={{ marginTop: 10, display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 11.5, color: 'var(--text-tertiary)' }}>截图目录</span>
            <code className="mono" style={{ fontSize: 11.5, color: 'var(--text-secondary)', background: 'var(--surface-subtle)', padding: '2px 7px', borderRadius: 6, wordBreak: 'break-all' }}>{st.directory}</code>
            <button className="btn btn-ghost btn-sm" onClick={revealFolder}><Icon name="folder" size={13} /> 打开文件夹</button>
          </div>
        )}
      </div>

      {/* 截图透明展示（可折叠） */}
      <div className="card" style={{ padding: 'var(--space-4)', marginBottom: 'var(--space-4)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <button className="btn btn-ghost btn-sm" style={{ padding: 0 }} onClick={() => { setShowShots((v) => !v); if (!showShots) loadShots(); }}>
            <Icon name="chevron-right" size={14} style={{ transform: showShots ? 'rotate(90deg)' : 'none', transition: 'transform 150ms' }} />
            最近截图（{shots.length}）
          </button>
          {/* 「本次会话」= 自开始提炼以来；把开始时刻显示出来，避免误以为是「当天全部」 */}
          {shotScope === 'session' && (
            <span style={{ fontSize: 11.5, color: 'var(--text-tertiary)' }}>
              {st?.started_at ? `自 ${fmtTime(st.started_at)} 开始提炼以来` : '尚未开始提炼'}
            </span>
          )}
          <div style={{ flex: 1 }} />
          {/* 范围切换：本次会话 / 全部 */}
          <div style={{ display: 'inline-flex', gap: 3, background: 'var(--surface-subtle)', padding: 3, borderRadius: 8 }}>
            {(['session', 'all'] as const).map((sc) => {
              const sel = shotScope === sc;
              return (
                <button key={sc} className="btn btn-sm"
                  title={sc === 'session' ? '自开始提炼以来的截图（点停止再开始 / 后端重启才会重置）' : '私有目录里的全部截图（跨天保留）'}
                  onClick={() => { setShotScope(sc); setShowShots(true); }}
                  style={{
                    background: sel ? 'var(--surface-elevated)' : 'transparent',
                    boxShadow: sel ? 'var(--shadow-sm)' : 'none',
                    color: sel ? 'var(--text-primary)' : 'var(--text-tertiary)',
                    fontWeight: sel ? 600 : 400, border: 'none', fontSize: 12,
                  }}>
                  {sc === 'session' ? '本次会话' : '全部'}
                </button>
              );
            })}
          </div>
          {shots.length > 0 && (
            <button className="btn btn-ghost btn-sm" style={{ color: 'var(--text-tertiary)' }} onClick={clearShots}>
              <Icon name="trash" size={13} /> 清空截图
            </button>
          )}
        </div>
        {showShots && (
          shots.length === 0
            ? <div style={{ fontSize: 12.5, color: 'var(--text-tertiary)', marginTop: 10 }}>
                {shotScope === 'session' ? '本次会话还没有截图。点「开始」后在工作窗口按 Enter 试试（本应用窗口会自动跳过）。' : '私有目录里还没有截图。'}
              </div>
            : <div style={{ display: 'flex', gap: 8, overflowX: 'auto', marginTop: 12, paddingBottom: 4 }} className="scroll">
                {shots.map((s) => (
                  <a key={s.path} href={fileUrl(s.path)} target="_blank" rel="noreferrer"
                     title={`${s.name} · ${fmtTime(s.mtime)}`}
                     style={{ flexShrink: 0, width: 132, height: 84, borderRadius: 8, overflow: 'hidden', border: '1px solid var(--border-default)', background: 'var(--surface-subtle)' }}>
                    <img src={fileUrl(s.path)} alt={s.name} loading="lazy"
                         style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
                  </a>
                ))}
              </div>
        )}
      </div>

      {/* 提炼记录时间线 */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', margin: '4px 2px 12px' }}>
        <div style={{ fontWeight: 600, fontSize: 14 }}>提炼记录 <span style={{ color: 'var(--text-tertiary)', fontWeight: 400 }}>· {digests.length}</span></div>
        {digests.length > 0 && (
          <button className="btn btn-ghost btn-sm" style={{ color: 'var(--text-tertiary)' }} onClick={clearDigests}>
            <Icon name="trash" size={13} /> 清空记录
          </button>
        )}
      </div>

      {digests.length === 0 ? (
        <div className="card" style={{ padding: 'var(--space-8)', textAlign: 'center', color: 'var(--text-tertiary)' }}>
          <Icon name="funnel" size={28} style={{ opacity: 0.4 }} />
          <div style={{ marginTop: 10, fontSize: 13.5 }}>还没有提炼记录</div>
          <div style={{ marginTop: 4, fontSize: 12 }}>开启后按 Enter 留痕，到点自动提炼；也可点「立即提炼」马上生成一条。</div>
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
          {digests.map((d) => (
            <div key={d.id} className="card" style={{ padding: 'var(--space-5)', borderLeft: `3px solid ${d.error ? 'var(--warning)' : '#0EA5E9'}` }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginBottom: 10 }}>
                <span style={{ fontWeight: 600, fontSize: 13.5 }}>{d.window_label || fmtTime(d.created_at)}</span>
                <span className="badge" style={{ background: 'var(--surface-subtle)', color: 'var(--text-tertiary)' }}>{d.n_shots} 张截图</span>
                {d.apps?.slice(0, 4).map((a, i) => (
                  <span key={i} className="badge badge-brand" style={{ fontWeight: 400 }}>{a}</span>
                ))}
              </div>
              {d.summary && (
                <div style={{ fontSize: 13.5, lineHeight: 1.7, color: 'var(--text-primary)', marginBottom: 12 }}>{d.summary}</div>
              )}
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: 'var(--space-3)' }}>
                {SECTIONS.map((sec) => {
                  const items = (d[sec.key] as string[]) || [];
                  if (!items.length) return null;
                  return (
                    <div key={sec.key as string}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11.5, fontWeight: 600, color: sec.color, marginBottom: 6, textTransform: 'none' }}>
                        <Icon name={sec.icon} size={12} /> {sec.label}
                      </div>
                      <ul style={{ margin: 0, paddingLeft: 16, fontSize: 12.5, lineHeight: 1.7, color: 'var(--text-secondary)' }}>
                        {items.map((it, i) => <li key={i}>{it}</li>)}
                      </ul>
                    </div>
                  );
                })}
              </div>
              {/* 分发：直接发到群（跳过草稿） / 转到协作分发 */}
              {!d.error && (
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginTop: 14, paddingTop: 12, borderTop: '1px solid var(--border-subtle)' }}>
                  <span style={{ fontSize: 11.5, color: 'var(--text-tertiary)' }}>分发到群</span>
                  <QuickSendToChat text={d.markdown || d.summary || ''} />
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

export default AutoExtractPage;
