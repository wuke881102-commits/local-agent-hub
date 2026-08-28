import React, { useCallback, useEffect, useState } from 'react';
import { api, errMsg } from '../api';
import { Icon } from '../components/icons';
import { useToast } from '../components/Toast';

type Push = { at: string; ok: boolean | null; error: string; needs_login: boolean; sent: number };

type Status = {
  active: boolean;
  every_min: number;
  sources: { digest: boolean; mail: boolean };
  started_at: string;
  last_run_at: string;
  next_run_at: string;
  last_result: string;
  error: string;
  busy: boolean;
  min_every: number;
  max_every: number;
  digest_available: boolean;
  mail_failed: boolean;
  push: Push;
};

// 频率刻意从 30 分钟起、到一天一次。摘记是给人看的半日报/日报，不是监控告警——
// 更密的频率只会让你静音它。
const EVERY = [30, 60, 120, 240, 480, 1440];

const everyLabel = (m: number) =>
  m >= 1440 ? '一天一次' : m >= 60 ? `${m / 60} 小时` : `${m} 分钟`;

const SOURCES: { key: 'digest' | 'mail'; label: string; hint: string }[] = [
  { key: 'digest', label: '自动化提炼',
    hint: '窗口内的工作留痕（比如 15 分钟一次，四小时就是 16 条）由模型合并成一段。这段时间没开提炼 → 这一段不出现。' },
  { key: 'mail', label: '本地邮箱',
    hint: '窗口内收到的邮件合并成一段；窗口内没新邮件则退回「谁在等你」。会真去读一次 Outlook。' },
];

function fmtTime(iso: string): string {
  if (!iso) return '';
  return iso.replace('T', ' ').slice(5, 16);
}

const MemoPage: React.FC = () => {
  const toast = useToast();
  const [st, setSt] = useState<Status | null>(null);
  const [every, setEvery] = useState<number>(240);
  // 两个来源默认都勾（与后端 _state 同步）。缺了邮件那一半就只剩自己的操作
  // 留痕，看不到别人推给你的事。
  const [src, setSrc] = useState<{ digest: boolean; mail: boolean }>({ digest: true, mail: true });
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const s = await api.get<Status>('/api/memo/status');
      setSt(s);
      // 只在未开启时同步表单，否则用户正在改的选择会被轮询覆盖掉。
      if (!s.active) {
        setEvery(s.every_min || 240);
        setSrc({ digest: !!s.sources?.digest, mail: !!s.sources?.mail });
      }
    } catch { /* ignore */ }
  }, []);

  useEffect(() => {
    load();
    const t = window.setInterval(load, 5000);
    return () => window.clearInterval(t);
  }, [load]);

  const active = !!st?.active;
  const noSource = !src.digest && !src.mail;

  const start = async () => {
    setBusy(true);
    try {
      setSt(await api.post<Status>('/api/memo/start', { every_min: every, sources: src }));
      toast.success('已开启个人摘记', {
        detail: `每 ${everyLabel(every)}汇总一次 · 只发给你自己`,
      });
    } catch (e) { toast.error('开启失败', { detail: errMsg(e) }); }
    finally { setBusy(false); }
  };

  const stop = async () => {
    setBusy(true);
    try { setSt(await api.post<Status>('/api/memo/stop', {})); toast.success('已停止'); }
    catch (e) { toast.error('停止失败', { detail: errMsg(e) }); }
    finally { setBusy(false); }
  };

  const runNow = async () => {
    setBusy(true);
    try {
      const r = await api.post<{ ok: boolean; sent: boolean; message?: string; error?: string }>('/api/memo/run', {});
      if (r.sent) toast.success('已推送到你的飞书');
      // 没发也要说清为什么——手动点一下没反应最容易让人以为坏了。
      else if (r.ok) toast.info('没有推送', { detail: r.message || '这段时间没有新内容。' });
      else toast.error('推送失败', { detail: r.error || '' });
      load();
    } catch (e) { toast.error('推送失败', { detail: errMsg(e) }); }
    finally { setBusy(false); }
  };

  return (
    <div style={{ padding: 'var(--space-8)', maxWidth: 1080, margin: '0 auto' }}>
      <div style={{ marginBottom: 'var(--space-5)' }}>
        <h2 style={{ margin: 0, fontSize: 20, fontWeight: 600 }}>个人摘记</h2>
        <div className="eyebrow" style={{ marginTop: 4 }}>
          按你设的频率，把各处的进展合成一条，发到你自己的飞书 · 只发给你自己，不发给任何其他人
        </div>
      </div>

      {/* 控制台 */}
      <div className="card" style={{ padding: 'var(--space-5)', marginBottom: 'var(--space-4)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16, flexWrap: 'wrap' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flex: 1, minWidth: 240 }}>
            <span style={{
              width: 38, height: 38, borderRadius: 11, flexShrink: 0,
              background: active ? '#0F766E15' : 'var(--surface-subtle)',
              color: active ? '#0F766E' : 'var(--text-tertiary)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}>
              <Icon name="calendar" size={19} />
            </span>
            <div>
              <div style={{ fontWeight: 600, fontSize: 14, display: 'flex', alignItems: 'center', gap: 8 }}>
                {active ? '定时摘记进行中' : '未开启'}
                {active && <span className="pulse-dot" style={{ width: 8, height: 8, borderRadius: 4, background: 'var(--success)' }} />}
              </div>
              <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginTop: 2 }}>
                {active
                  ? <>每 {everyLabel(st?.every_min || every)}一次 · {st?.busy ? '正在汇总…' : <>下次 <strong className="tnum">{fmtTime(st?.next_run_at || '')}</strong></>}</>
                  : '开启后按频率自动汇总并发到你的飞书。后端重启会回到未开启，需要再点一次。'}
              </div>
            </div>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            {!active
              ? <button className="btn btn-primary btn-sm" disabled={busy || noSource} onClick={start}
                  title={noSource ? '至少要选一个来源' : ''}>开始</button>
              : <button className="btn btn-sm" disabled={busy} onClick={stop}>停止</button>}
            <button className="btn btn-sm" disabled={busy || noSource} onClick={runNow}>立即推一条</button>
          </div>
        </div>

        {/* 频率 */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 14, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 12.5, color: 'var(--text-secondary)', minWidth: 52 }}
                title="频率就是窗口长度：四小时一次 → 每条摘记只讲过去四小时。两个来源在窗口内都空 → 不发。">频率</span>
          <div style={{ display: 'inline-flex', gap: 4, background: 'var(--surface-subtle)', padding: 3, borderRadius: 9 }}>
            {EVERY.map((m) => {
              const sel = active ? st?.every_min === m : every === m;
              return (
                <button key={m} disabled={active || busy} onClick={() => setEvery(m)} className="btn btn-sm"
                  style={{
                    background: sel ? 'var(--surface-elevated)' : 'transparent',
                    boxShadow: sel ? 'var(--shadow-sm)' : 'none',
                    color: sel ? 'var(--text-primary)' : 'var(--text-tertiary)',
                    fontWeight: sel ? 600 : 400, border: 'none',
                    cursor: active ? 'default' : 'pointer',
                  }}>
                  {everyLabel(m)}
                </button>
              );
            })}
          </div>
        </div>

        {/* 来源 */}
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, marginTop: 12, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 12.5, color: 'var(--text-secondary)', minWidth: 52, paddingTop: 6 }}>来源</span>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {SOURCES.map((s) => {
              const on = active ? !!st?.sources?.[s.key] : src[s.key];
              return (
                <label key={s.key} title={s.hint}
                  style={{
                    display: 'flex', alignItems: 'flex-start', gap: 7, fontSize: 13,
                    cursor: active ? 'default' : 'pointer', maxWidth: 620,
                    color: on ? 'var(--text-primary)' : 'var(--text-tertiary)',
                  }}>
                  <input type="checkbox" checked={on} disabled={active || busy}
                    onChange={(e) => setSrc((v) => ({ ...v, [s.key]: e.target.checked }))}
                    style={{ marginTop: 2 }} />
                  <span>
                    <strong style={{ fontWeight: on ? 600 : 400 }}>{s.label}</strong>
                    <span style={{ fontSize: 11.5, color: 'var(--text-tertiary)', marginLeft: 6 }}>{s.hint}</span>
                  </span>
                </label>
              );
            })}
          </div>
        </div>

        {noSource && !active && (
          <div style={{ marginTop: 10, fontSize: 12, color: 'var(--warning)' }}>
            <Icon name="warning" size={12} /> 至少要选一个来源。
          </div>
        )}

        {st?.last_result && (
          <div style={{ marginTop: 12, fontSize: 12, color: 'var(--text-tertiary)' }}>
            <Icon name="check" size={12} /> {st.last_result}
            {st.last_run_at ? `（${fmtTime(st.last_run_at)}）` : ''}
          </div>
        )}

        {/* 推送失败必须显眼。needs_login 尤其：授权 7 天滚动过期，重试无用，
            而你会以为它还在替你盯着——静默停摆是这类功能最糟的失败方式。 */}
        {st?.push?.ok === false && (
          <div style={{ marginTop: 8, fontSize: 12, color: 'var(--error)' }}>
            <Icon name="warning" size={12} />{' '}
            {st.push.needs_login
              ? '飞书推送已停：授权已过期（用户身份 7 天滚动）。重新登录后自动恢复。'
              : `飞书推送失败：${st.push.error}`}
            {st.push.at ? `（${fmtTime(st.push.at)}）` : ''}
          </div>
        )}

        {st?.mail_failed && (
          <div style={{ marginTop: 8, fontSize: 12, color: 'var(--error)' }}>
            <Icon name="warning" size={12} /> 上次没读到邮件。Outlook 若弹出「程序正在访问地址信息」需要你点一下；
            摘记不会因此停掉，其余来源照常推送。
          </div>
        )}
      </div>

      <div style={{ fontSize: 11.5, color: 'var(--text-tertiary)', lineHeight: 1.7, background: 'var(--surface-subtle)', borderRadius: 8, padding: '10px 12px' }}>
        <Icon name="shield" size={12} /> <strong>收件人只能是你自己。</strong>
        身份取自本机飞书登录态，接口不提供指定收件人的入口，也不会发到任何群。
        <br />
        <Icon name="info" size={12} /> 以你自己的身份发送（内置应用没有机器人发消息权限），所以飞书里显示为你自己发的。
        用户身份授权是 <strong>7 天滚动</strong>的：正常用着一直有效，连续七天不碰本应用需要重新登录。
        <br />
        <Icon name="warning" size={12} /> 勾选「本地邮箱」意味着每次汇总会真去读一次 Outlook，
        并按现有设置把邮件主题和正文前 400 字发到云端模型做语义分析 —— 那一刻没有人在旁边确认。
      </div>
    </div>
  );
};

export default MemoPage;
