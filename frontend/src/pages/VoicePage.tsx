import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { api, errMsg } from '../api';
import { Icon } from '../components/icons';
import { useToast } from '../components/Toast';

type Dev = { index: number; name: string; channels: number; rate: number; default?: boolean };
type Devices = {
  mic: Dev[]; loopback: Dev[]; note: string;
  mic_available: boolean; loopback_available: boolean;
};
type Seg = { at_s: number; text: string };
type Route = {
  kind: 'mic' | 'loopback'; label: string; connected: boolean;
  provisional: string; segments: Seg[]; sent_seconds: number;
  committed: number; error: string;
};
type FileJob = {
  active: boolean; filename: string; stage: string;
  seconds: number; done_s: number; error: string;
};
type Status = {
  active: boolean; mode: string; started_at: string; elapsed_s: number;
  max_minutes: number; error: string; finishing: boolean; routes: Route[];
  file_job?: FileJob;
  file_max_mb?: number; file_max_minutes?: number;
  distill_available?: boolean;
};
type Note = {
  id: string; created_at: string; mode: string; seconds: number;
  segments: number; summary: string; error: string; transcript_chars?: number;
  transcript?: string; source_file?: string; source_codec?: string;
  distill_error?: string; distilled_at?: string; distill_model?: string;
};

type Mode = 'mic' | 'loopback' | 'both';

// 三种录法。**每次开录都要选，没有默认值、没有记住上次** —— 录系统声音意味着
// 录到会议里其他人的声音，这件事不该靠一个被记住的默认值悄悄发生。
// 所以 mode 的初始状态是 null，而不是某个"合理默认"。
const MODES: { key: Mode; label: string; sends: string; needs: ('mic' | 'loopback')[] }[] = [
  { key: 'mic', label: '麦克风', needs: ['mic'],
    sends: '只把你自己的声音发到云端转写。戴耳机时这是真的；用音箱时对方的声音会从扬声器绕回麦克风。' },
  { key: 'loopback', label: '系统声音', needs: ['loopback'],
    sends: '把这台电脑正在响的声音发到云端 —— 会议里其他人的声音、以及任何在播的音频。' },
  { key: 'both', label: '两路同时', needs: ['mic', 'loopback'],
    sends: '两路都发。好处是说话人分离白送（麦克风那路是你，系统声音那路是别人），不需要声纹识别。' },
];

const fmtTime = (iso: string) => (iso ? iso.replace('T', ' ').slice(5, 16) : '');
const fmtDur = (s: number) => {
  const m = Math.floor(s / 60);
  const r = s % 60;
  return m ? `${m}分${String(r).padStart(2, '0')}秒` : `${r}秒`;
};
const modeLabel = (m: string) =>
  m === 'file' ? '文件' : MODES.find((x) => x.key === m)?.label || m || '';

const STAGE: Record<string, string> = {
  decoding: '正在解码',
  transcribing: '正在转写',
  distilling: '正在提炼',
  done: '完成',
};

const VoicePage: React.FC = () => {
  const toast = useToast();
  const [dev, setDev] = useState<Devices | null>(null);
  const [st, setSt] = useState<Status | null>(null);
  const [notes, setNotes] = useState<Note[]>([]);
  const [mode, setMode] = useState<Mode | null>(null);
  const [micDev, setMicDev] = useState<number | null>(null);
  const [lbDev, setLbDev] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [redistilling, setRedistilling] = useState('');
  const [copied, setCopied] = useState('');
  const copyTimer = useRef<number | null>(null);
  const [open, setOpen] = useState<string>('');
  const [full, setFull] = useState<Record<string, Note>>({});

  const loadDevices = useCallback(async () => {
    try { setDev(await api.get<Devices>('/api/voice/devices')); } catch { /* ignore */ }
  }, []);
  const loadNotes = useCallback(async () => {
    try { setNotes((await api.get<{ notes: Note[] }>('/api/voice/notes?limit=50')).notes || []); }
    catch { /* ignore */ }
  }, []);
  const loadStatus = useCallback(async () => {
    try { setSt(await api.get<Status>('/api/voice/status')); } catch { /* ignore */ }
  }, []);

  useEffect(() => { loadDevices(); loadNotes(); loadStatus(); }, [loadDevices, loadNotes, loadStatus]);

  // 录着（要看逐字稿滚出来）或转写文件（要看进度）时一秒一拉；闲着五秒一次够了。
  const active = !!st?.active;
  const job = st?.file_job;
  // 录着（要看逐字稿滚出来）、转写文件（要看进度）、或正在整理（要第一时间
  // 看到它结束）时一秒一拉。**整理这一路原先漏了**：那三分钟里页面五秒才动一次，
  // 本来就在干等的人还得多等最多五秒才看到结果。
  const finishing = !!st?.finishing;
  useEffect(() => {
    const fast = active || uploading || finishing || !!job?.active;
    const t = window.setInterval(loadStatus, fast ? 1000 : 5000);
    return () => window.clearInterval(t);
  }, [active, uploading, finishing, job?.active, loadStatus]);

  const avail = (m: Mode) => {
    const cfg = MODES.find((x) => x.key === m)!;
    return cfg.needs.every((n) => (n === 'mic' ? dev?.mic_available : dev?.loopback_available));
  };
  const cur = mode ? MODES.find((x) => x.key === mode)! : null;

  const start = async () => {
    if (!mode) return;
    setBusy(true);
    try {
      setSt(await api.post<Status>('/api/voice/start', {
        mode,
        mic_device: micDev,
        loopback_device: lbDev,
      }));
      toast.success(`开始录音 · ${modeLabel(mode)}`, {
        detail: `最长 ${st?.max_minutes || 90} 分钟自动停止`,
      });
    } catch (e) { toast.error('开录失败', { detail: errMsg(e) }); await loadDevices(); }
    finally { setBusy(false); }
  };

  const stop = async () => {
    setBusy(true);
    try {
      const r = await api.post<{ ok: boolean; saved: boolean; message?: string; note?: Note }>(
        '/api/voice/stop', {});
      if (r.saved) toast.success('已存为一条记录', { detail: r.message || '' });
      // 没转出文字时必须说清为什么 —— 录了半天什么都没有，最需要的就是一句解释。
      else toast.info('没有存下记录', { detail: r.message || '' });
      await Promise.all([loadStatus(), loadNotes()]);
      // 直接把 stop 返回的整条塞进 full 缓存再展开 —— 它本来就带着逐字稿全文。
      // 只 setOpen 不填缓存的话，展开后逐字稿会一直停在「读取中…」。
      if (r.note?.id) {
        const n = r.note;
        setFull((v) => ({ ...v, [n.id]: n }));
        setOpen(n.id);
      }
    } catch (e) { toast.error('停止失败', { detail: errMsg(e) }); }
    finally { setBusy(false); }
  };

  // 传文件转写。**直接把 File 当请求体发**，不包 multipart ——
  // 后端收裸请求体是为了让音频不经过系统临时目录（见 routes/voice.py 里的理由），
  // 用 FormData 会让 FastAPI 走 UploadFile，那条路会落盘。
  const upload = async (f: File) => {
    setUploading(true);
    try {
      const r = await fetch(
        `/api/voice/transcribe-file?filename=${encodeURIComponent(f.name)}`,
        { method: 'POST', body: f, headers: { 'content-type': 'application/octet-stream' } });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j?.detail || `${r.status}`);
      if (j.saved) {
        toast.success('已转写并存为一条记录', { detail: j.message || f.name });
        if (j.note?.id) {
          setFull((v) => ({ ...v, [j.note.id]: j.note }));
          setOpen(j.note.id);
        }
      } else {
        toast.error('没能转写', { detail: j.message || '' });
      }
      await Promise.all([loadNotes(), loadStatus()]);
    } catch (e) { toast.error('上传失败', { detail: errMsg(e) }); }
    finally { setUploading(false); }
  };

  const expand = async (id: string) => {
    if (open === id) { setOpen(''); return; }
    setOpen(id);
    if (!full[id]) {
      try {
        const n = await api.get<Note>(`/api/voice/notes/${id}`);
        setFull((v) => ({ ...v, [id]: n }));
      } catch (e) {
        toast.error('读取失败', { detail: errMsg(e) });
        // 失败也要写进缓存：否则界面一直停在「读取中…」，那是句假话。
        setFull((v) => ({ ...v, [id]: { ...(notes.find((x) => x.id === id) as Note), transcript: '（逐字稿读取失败）' } }));
      }
    }
  };

  // 复制提炼结果。
  //
  // navigator.clipboard 只在安全上下文（https / localhost）里才有。本机跑的时候地址是
  // localhost，够用；但从局域网 IP 打开这个页面时它是 undefined。所以留一条 execCommand
  // 的旧路 —— 那个 API 早废弃了，可在不安全上下文里它是唯一还能用的。
  //
  // 复制出去的**就是提炼原文**，不加时间、不加标题、不加「以下内容由…生成」。
  // 粘到飞书里要说什么由你自己写；一个自作主张的抬头只会让人每次都去删。
  const copySummary = async (id: string, text: string) => {
    const done = () => {
      setCopied(id);
      if (copyTimer.current) window.clearTimeout(copyTimer.current);
      copyTimer.current = window.setTimeout(() => setCopied(''), 1800);
    };
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
        done();
        return;
      }
    } catch { /* 落到下面的旧路 */ }
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      if (!ok) throw new Error('execCommand 返回 false');
      done();
    } catch (e) {
      toast.error('复制失败', { detail: `${errMsg(e)} · 提炼内容就在下面，可以手动选中复制` });
    }
  };

  // 组件被卸载（切页）时把「已复制」的计时器清掉，否则 1.8 秒后会往已经没了的组件里 setState。
  useEffect(() => () => { if (copyTimer.current) window.clearTimeout(copyTimer.current); }, []);

  // 重新提炼。逐字稿已经存着，提炼失败（超时、模型抖动）不该让人重录 ——
  // 那段音频早就不在了，重录也录不回同一场。
  const redistill = async (id: string) => {
    setRedistilling(id);
    try {
      const r = await api.post<{ ok: boolean; note: Note; message?: string }>(
        `/api/voice/notes/${id}/distill`);
      if (r.ok) toast.success('提炼完成');
      else toast.error('还是没提炼出来', { detail: r.message || '' });
      setFull((v) => ({ ...v, [id]: r.note }));
      await loadNotes();
    } catch (e) { toast.error('重新提炼失败', { detail: errMsg(e) }); }
    finally { setRedistilling(''); }
  };

  const del = async (id: string) => {
    try {
      await api.del(`/api/voice/notes/${id}`);
      toast.success('已删除');
      if (open === id) setOpen('');
      await loadNotes();
    } catch (e) { toast.error('删除失败', { detail: errMsg(e) }); }
  };

  // 录音中的实时逐字稿：各路定稿段落按时间轴合并，末尾挂上灰色的临时文字。
  const live = useMemo(() => {
    const rs = st?.routes || [];
    const multi = rs.length > 1;
    const rows = rs.flatMap((r) =>
      r.segments.map((s) => ({ at: s.at_s, who: multi ? r.label : '', text: s.text })));
    rows.sort((a, b) => a.at - b.at);
    return { rows, multi, provisional: rs.map((r) => ({ who: multi ? r.label : '', text: r.provisional })).filter((x) => x.text) };
  }, [st]);

  // 实时逐字稿自动跟随底部。
  //
  // **只在用户本来就贴着底的时候才滚。** 无条件滚到底的话，你想往回翻看前面说了什么，
  // 每一轮轮询都会把你拽回最新那行，等于翻不动。所以：贴底就跟着走，一往上翻就停住，
  // 同时冒出「回到最新」——不给这个按钮的话，翻上去之后就再也回不到跟随状态了。
  const boxRef = useRef<HTMLDivElement>(null);
  const [stick, setStick] = useState(true);

  const onScroll = () => {
    const el = boxRef.current;
    if (!el) return;
    // 留 40px（约一行半）余量：差几个像素不该被判成「用户主动翻走了」。
    const near = el.scrollHeight - el.scrollTop - el.clientHeight <= 40;
    setStick((v) => (v === near ? v : near));
  };

  const toBottom = () => {
    const el = boxRef.current;
    if (el) el.scrollTop = el.scrollHeight;
    setStick(true);
  };

  // useLayoutEffect 而不是 useEffect：等浏览器把新行画上屏再滚，会看到一帧的跳动。
  useLayoutEffect(() => {
    if (!stick) return;
    const el = boxRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [live, stick]);

  const overCap = active && st ? st.elapsed_s >= (st.max_minutes * 60 - 60) : false;

  return (
    <div style={{ padding: 'var(--space-8)', maxWidth: 1080, margin: '0 auto' }}>
      <div style={{ marginBottom: 'var(--space-5)' }}>
        <h2 style={{ margin: 0, fontSize: 20, fontWeight: 600 }}>语音速记</h2>
        <div className="eyebrow" style={{ marginTop: 4 }}>
          按下才录，说完出一条文字记录 · 音频不落盘，只存转写和提炼
        </div>
      </div>

      {/* 控制台 */}
      <div className="card" style={{ padding: 'var(--space-5)', marginBottom: 'var(--space-4)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16, flexWrap: 'wrap' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flex: 1, minWidth: 260 }}>
            <span style={{
              width: 38, height: 38, borderRadius: 11, flexShrink: 0,
              background: active ? '#B91C1C15' : 'var(--surface-subtle)',
              color: active ? '#B91C1C' : 'var(--text-tertiary)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}>
              <Icon name="mic" size={19} />
            </span>
            <div>
              <div style={{ fontWeight: 600, fontSize: 14, display: 'flex', alignItems: 'center', gap: 8 }}>
                {st?.finishing ? '正在整理…' : active ? `正在录音 · ${modeLabel(st?.mode || '')}` : '未在录音'}
                {active && <span className="pulse-dot" style={{ width: 8, height: 8, borderRadius: 4, background: '#B91C1C' }} />}
              </div>
              <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginTop: 2 }}>
                {/* 整理期间原先显示的是「选一种录法，然后点开始录音」—— 上面写着
                    「正在整理…」，下面叫你去开始录音，等的人完全不知道它在干什么、
                    还要等多久。这里如实说清在等谁、上限多少、以及最坏情况下也不会
                    白等（逐字稿已经落盘了）。 */}
                {active
                  ? <>已录 <strong className="tnum">{fmtDur(st?.elapsed_s || 0)}</strong>
                      {' · '}最长 {st?.max_minutes} 分钟自动停止</>
                  : finishing
                    ? <>逐字稿已经存下了，正在等文字模型提炼 · 主档最多 90 秒，
                        不出结果就退到快档再 90 秒 · 提炼失败也会保留逐字稿，可以点「重新提炼」</>
                    : '选一种录法，然后点开始录音。后端重启会停掉录音，不会自己恢复。'}
              </div>
            </div>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            {!active
              ? <button className="btn btn-primary btn-sm" disabled={busy || !mode} onClick={start}
                  title={!mode ? '先选一种录法' : ''}>开始录音</button>
              : <button className="btn btn-sm" disabled={busy} onClick={stop}>停止并整理</button>}
          </div>
        </div>

        {/* 录法。没有默认选中项：必须每次主动选一次。 */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 14, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 12.5, color: 'var(--text-secondary)', minWidth: 52 }}>录法</span>
          <div style={{ display: 'inline-flex', gap: 4, background: 'var(--surface-subtle)', padding: 3, borderRadius: 9 }}>
            {MODES.map((m) => {
              const sel = active ? st?.mode === m.key : mode === m.key;
              const can = avail(m.key);
              return (
                <button key={m.key} disabled={active || busy || !can}
                  onClick={() => setMode(m.key)} className="btn btn-sm"
                  title={can ? m.sends : '这台机器缺对应的音频设备'}
                  style={{
                    background: sel ? 'var(--surface-elevated)' : 'transparent',
                    boxShadow: sel ? 'var(--shadow-sm)' : 'none',
                    color: !can ? 'var(--text-tertiary)' : sel ? 'var(--text-primary)' : 'var(--text-tertiary)',
                    fontWeight: sel ? 600 : 400, border: 'none',
                    opacity: can ? 1 : 0.45,
                    cursor: active || !can ? 'default' : 'pointer',
                  }}>
                  {m.label}
                </button>
              );
            })}
          </div>
          {!mode && !active && (
            <span style={{ fontSize: 11.5, color: 'var(--text-tertiary)' }}>
              每次都要选一次 —— 这里刻意不记住上次的选择。
            </span>
          )}
        </div>

        {/* 设备。只有多于一个时才给选择器（一个的时候下拉框只是噪音）。 */}
        {!active && dev && (dev.mic.length > 1 || dev.loopback.length > 1) && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 12.5, color: 'var(--text-secondary)', minWidth: 52 }}>设备</span>
            {dev.mic.length > 1 && (mode === 'mic' || mode === 'both') && (
              <select className="input" style={{ fontSize: 12.5, maxWidth: 300 }}
                value={micDev ?? ''} onChange={(e) => setMicDev(e.target.value ? Number(e.target.value) : null)}>
                <option value="">麦克风：系统默认</option>
                {dev.mic.map((d) => <option key={d.index} value={d.index}>{d.name}</option>)}
              </select>
            )}
            {dev.loopback.length > 1 && (mode === 'loopback' || mode === 'both') && (
              <select className="input" style={{ fontSize: 12.5, maxWidth: 300 }}
                value={lbDev ?? ''} onChange={(e) => setLbDev(e.target.value ? Number(e.target.value) : null)}>
                <option value="">系统声音：默认输出</option>
                {dev.loopback.map((d) => <option key={d.index} value={d.index}>{d.name}</option>)}
              </select>
            )}
          </div>
        )}

        {/* 选定录法后，明确写出这一路会把什么发出去。 */}
        {cur && !active && (
          <div style={{
            marginTop: 12, fontSize: 12, lineHeight: 1.7, borderRadius: 8, padding: '9px 11px',
            background: cur.key === 'mic' ? 'var(--surface-subtle)' : '#B4530910',
            color: cur.key === 'mic' ? 'var(--text-secondary)' : '#B45309',
          }}>
            <Icon name={cur.key === 'mic' ? 'shield' : 'warning'} size={12} />{' '}
            <strong>{cur.label}：</strong>{cur.sends}
          </div>
        )}

        {/* 提炼不可用要在**开录之前**说。录完一场会才发现没提炼，是最难受的失败方式，
            而这件事开录前就已经知道了。 */}
        {st && st.distill_available === false && (
          <div style={{ marginTop: 10, fontSize: 12, lineHeight: 1.7, borderRadius: 8,
                        padding: '9px 11px', background: '#B4530910', color: '#B45309' }}>
            <Icon name="warning" size={12} /> <strong>现在录只会有逐字稿，不会有提炼。</strong>
            文字模型当前是 mock / 离线状态 —— 转写走的是语音模型，提炼走的是文字模型，
            两者吃的不是同一份配置。去「系统诊断」看文字模型那一行。
            配好之后已有的记录可以点「重新提炼」补上，不用重录。
          </div>
        )}

        {dev?.note && (
          <div style={{ marginTop: 8, fontSize: 12, color: 'var(--text-tertiary)' }}>
            <Icon name="warning" size={12} /> {dev.note}
          </div>
        )}

        {overCap && (
          <div style={{ marginTop: 8, fontSize: 12, color: 'var(--warning)' }}>
            <Icon name="warning" size={12} /> 快到 {st?.max_minutes} 分钟上限了，到点会自动停止并整理。
          </div>
        )}

        {(st?.error || (st?.routes || []).some((r) => r.error)) && (
          <div style={{ marginTop: 8, fontSize: 12, color: 'var(--error)' }}>
            <Icon name="warning" size={12} />{' '}
            {[st?.error, ...(st?.routes || []).map((r) => r.error)].filter(Boolean).join('；')}
          </div>
        )}

        {active && (st?.routes || []).length > 0 && (
          <div style={{ marginTop: 10, fontSize: 11.5, color: 'var(--text-tertiary)', display: 'flex', gap: 14, flexWrap: 'wrap' }}>
            {(st?.routes || []).map((r) => (
              <span key={r.kind}>
                {r.label}：{r.connected ? '已连接' : '未连接'} · 已发 <span className="tnum">{r.sent_seconds.toFixed(0)}s</span> 音频 · {r.committed} 段
              </span>
            ))}
            <span>静音期间不发送，所以这个秒数会小于已录时长。</span>
          </div>
        )}
      </div>

      {/* 传文件进来转写 */}
      <div className="card" style={{ padding: 'var(--space-5)', marginBottom: 'var(--space-4)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <div style={{ flex: 1, minWidth: 260 }}>
            <div style={{ fontSize: 13, fontWeight: 600 }}>传一个文件进来转写</div>
            <div style={{ fontSize: 11.5, color: 'var(--text-tertiary)', marginTop: 3, lineHeight: 1.7 }}>
              手机录的会议、别人发来的录音、下载的会议回放都行。音频 / 视频都可以（视频只取音轨）。
              <br />
              m4a · mp3 · wav · aac · flac · ogg · opus · mp4 · mov ……最大 {st?.file_max_mb ?? 200} MB / {st?.file_max_minutes ?? 180} 分钟。
            </div>
          </div>
          <label className="btn btn-sm" style={{ cursor: uploading || job?.active ? 'default' : 'pointer' }}>
            <Icon name="folder" size={12} /> {uploading || job?.active ? '转写中…' : '选择文件'}
            <input type="file" accept="audio/*,video/*" disabled={uploading || !!job?.active}
              style={{ display: 'none' }}
              onChange={(e) => {
                const f = e.target.files?.[0];
                e.target.value = '';        // 允许再选同一个文件
                if (f) upload(f);
              }} />
          </label>
        </div>

        {(uploading || job?.active) && (
          <div style={{ marginTop: 12, fontSize: 12, color: 'var(--text-secondary)' }}>
            <span className="pulse-dot" style={{ display: 'inline-block', width: 7, height: 7, borderRadius: 4, background: 'var(--accent)', marginRight: 7 }} />
            {job?.filename || '正在上传'}
            {job?.stage ? ` · ${STAGE[job.stage] || job.stage}` : ''}
            {job?.stage === 'transcribing' && (job?.seconds ?? 0) > 0
              ? <> · <span className="tnum">{Math.round(job!.done_s)}s / {Math.round(job!.seconds)}s</span></>
              : ''}
            {/* 进度条：转写阶段才有意义，解码和提炼没有可报的比例 */}
            {job?.stage === 'transcribing' && (job?.seconds ?? 0) > 0 && (
              <div style={{ marginTop: 6, height: 4, borderRadius: 2, background: 'var(--surface-subtle)', overflow: 'hidden' }}>
                <div style={{
                  height: '100%', borderRadius: 2, background: 'var(--accent)',
                  width: `${Math.min(100, Math.round((job!.done_s / Math.max(job!.seconds, 1)) * 100))}%`,
                  transition: 'width .4s ease',
                }} />
              </div>
            )}
          </div>
        )}

        {!uploading && !job?.active && job?.error && (
          <div style={{ marginTop: 10, fontSize: 12, color: 'var(--error)' }}>
            <Icon name="warning" size={12} /> {job.error}
          </div>
        )}

        <div style={{ marginTop: 10, fontSize: 11.5, color: 'var(--text-tertiary)', lineHeight: 1.7 }}>
          <Icon name="shield" size={12} /> 文件<strong>不落盘</strong>：字节直接进内存解码，转写完就释放。
          存下来的只有文字。整段音频会发到云端模型做转写 —— 里面有谁的声音，你比我清楚。
        </div>
      </div>

      {/* 实时逐字稿 */}
      {(active || live.rows.length > 0) && (
        <div className="card" style={{ padding: 'var(--space-5)', marginBottom: 'var(--space-4)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
            <div style={{ fontSize: 13, fontWeight: 600 }}>
              实时逐字稿
              <span style={{ fontWeight: 400, fontSize: 11.5, color: 'var(--text-tertiary)', marginLeft: 8 }}>
                灰色是临时结果，会自我修正；定稿后转黑
              </span>
            </div>
            <div style={{ flex: 1 }} />
            {!stick && (
              <button className="btn btn-sm" onClick={toBottom}
                      title="回到最新，并恢复自动跟随">↓ 回到最新</button>
            )}
          </div>
          <div ref={boxRef} onScroll={onScroll}
               style={{ maxHeight: 300, overflowY: 'auto', fontSize: 13, lineHeight: 1.9 }}>
            {live.rows.length === 0 && live.provisional.length === 0 && (
              <div style={{ color: 'var(--text-tertiary)', fontSize: 12 }}>
                还没有识别到语音。{st?.mode === 'loopback' ? '系统声音只有在电脑真的在放声音时才有数据。' : ''}
              </div>
            )}
            {live.rows.map((r, i) => (
              <div key={i}>
                {r.who && <strong style={{ color: r.who === '我' ? 'var(--accent)' : 'var(--text-secondary)', marginRight: 6 }}>{r.who}：</strong>}
                <span>{r.text}</span>
              </div>
            ))}
            {live.provisional.map((p, i) => (
              <div key={`p${i}`} style={{ color: 'var(--text-tertiary)' }}>
                {p.who && <strong style={{ marginRight: 6 }}>{p.who}：</strong>}
                <span>{p.text}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 已有记录 */}
      <div className="card" style={{ padding: 'var(--space-5)', marginBottom: 'var(--space-4)' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
          <div style={{ fontSize: 13, fontWeight: 600 }}>记录 <span className="tnum" style={{ color: 'var(--text-tertiary)', fontWeight: 400 }}>{notes.length}</span></div>
          <button className="btn btn-sm" onClick={loadNotes}><Icon name="refresh" size={12} /> 刷新</button>
        </div>
        {notes.length === 0 && (
          <div style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>还没有记录。录一条试试。</div>
        )}
        {notes.map((n) => (
          <div key={n.id} style={{ borderTop: '1px solid var(--border-subtle)', padding: '9px 0' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
              <button className="btn btn-sm" onClick={() => expand(n.id)} style={{ border: 'none', background: 'transparent', padding: 0 }}>
                <Icon name={open === n.id ? 'x' : 'page'} size={12} />
              </button>
              <span className="tnum" style={{ fontSize: 12.5 }}>{fmtTime(n.created_at)}</span>
              <span style={{ fontSize: 11.5, color: 'var(--text-tertiary)' }}>
                {modeLabel(n.mode)} · {fmtDur(n.seconds)} · {n.segments} 段 · {n.transcript_chars ?? 0} 字
              </span>
              {n.source_file && (
                <span style={{ fontSize: 11.5, color: 'var(--text-secondary)' }}
                      title={n.source_codec ? `编码 ${n.source_codec}` : ''}>
                  <Icon name="folder" size={11} /> {n.source_file}
                </span>
              )}
              {n.error && <span style={{ fontSize: 11.5, color: 'var(--error)' }}><Icon name="warning" size={11} /> {n.error}</span>}
              {/* 折叠状态下也必须看得出有没有提炼 —— 原先这里什么都不显示，
                  于是「提炼好了但你没展开」和「提炼失败了」长得一模一样。 */}
              {n.summary
                ? <span style={{ fontSize: 11.5, color: 'var(--success)' }}>
                    <Icon name="check" size={11} /> 已提炼
                  </span>
                : <span style={{ fontSize: 11.5, color: n.distill_error ? 'var(--warning)' : 'var(--text-tertiary)' }}>
                    <Icon name="warning" size={11} /> 未提炼
                  </span>}
              <span style={{ flex: 1 }} />
              <button className="btn btn-sm" onClick={() => del(n.id)} title="删除这条记录"><Icon name="trash" size={12} /></button>
            </div>
            {open !== n.id && n.summary && (
              <div style={{ paddingLeft: 24, marginTop: 3, fontSize: 12, color: 'var(--text-secondary)',
                            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {n.summary.split(/\r?\n/).find((x) => x.trim() && !x.trim().startsWith('#')) || ''}
              </div>
            )}
            {open === n.id && (
              <div style={{ marginTop: 8, paddingLeft: 24 }}>
                {n.summary && (
                  <>
                    {/* 提炼这块原先没标题，下面的「逐字稿」有。补上标题的同时把复制按钮放这一行的
                        右边 —— 不浮在正文上，长提炼往下滚时也不会盖住字。 */}
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                      <div style={{ fontSize: 11.5, color: 'var(--text-tertiary)' }}>提炼</div>
                      <span style={{ flex: 1 }} />
                      <button className="btn btn-sm" title="复制提炼内容，可直接粘去分享"
                        onClick={() => copySummary(n.id, n.summary)}>
                        <Icon name={copied === n.id ? 'check' : 'copy'} size={12} />{' '}
                        {copied === n.id ? '已复制' : '复制'}
                      </button>
                    </div>
                    <div style={{ fontSize: 12.5, lineHeight: 1.8, whiteSpace: 'pre-wrap',
                                  background: 'var(--surface-subtle)', borderRadius: 8, padding: '10px 12px' }}>
                      {n.summary}
                    </div>
                  </>
                )}
                {/* 没提炼出来时把原因写出来，并且给一个重试的入口。
                    逐字稿在盘上，重试不需要重录。 */}
                {!n.summary && (
                  <div style={{ fontSize: 12, lineHeight: 1.8, borderRadius: 8, padding: '10px 12px',
                                background: '#B4530910', color: '#B45309',
                                display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                    <span style={{ flex: 1, minWidth: 200 }}>
                      <Icon name="warning" size={12} />{' '}
                      {n.distill_error || '这条没有提炼结果。'}
                    </span>
                    <button className="btn btn-sm" disabled={redistilling === n.id}
                      onClick={() => redistill(n.id)}>
                      {redistilling === n.id ? '提炼中…' : '重新提炼'}
                    </button>
                  </div>
                )}
                {n.summary && (n.distill_model || n.distilled_at) && (
                  <div style={{ fontSize: 11, color: 'var(--text-tertiary)', marginTop: 4 }}>
                    {n.distill_model ? `由 ${n.distill_model} 提炼` : ''}
                    {n.distill_model && n.distilled_at ? ' · ' : ''}
                    {n.distilled_at ? `重新提炼于 ${fmtTime(n.distilled_at)}` : ''}
                  </div>
                )}
                <div style={{ fontSize: 11.5, color: 'var(--text-tertiary)', margin: '10px 0 4px' }}>逐字稿</div>
                <div style={{ fontSize: 12.5, lineHeight: 1.8, whiteSpace: 'pre-wrap', maxHeight: 260, overflowY: 'auto' }}>
                  {full[n.id]?.transcript ?? '读取中…'}
                </div>
              </div>
            )}
          </div>
        ))}
      </div>

      <div style={{ fontSize: 11.5, color: 'var(--text-tertiary)', lineHeight: 1.7, background: 'var(--surface-subtle)', borderRadius: 8, padding: '10px 12px' }}>
        <Icon name="shield" size={12} /> <strong>音频不落盘。</strong>
        没有任何一条代码路径把录到的声音写到磁盘上，连可选项都不提供；存下来的只有转写文字和提炼结果。
        <br />
        <Icon name="mic" size={12} /> <strong>只有你点「开始录音」才会录。</strong>
        没有定时录音、没有自动录音。「个人摘记」可以把这里的记录当来源，但它只读<strong>已经录好的文字</strong>，永远不会去开麦克风。
        <br />
        <Icon name="warning" size={12} /> 录音会把语音<strong>实时发到云端模型</strong>做转写，停录后再把逐字稿发一次做提炼。
        选「系统声音」或「两路同时」时，发出去的包含<strong>会议里其他人的声音</strong> —— 这是你每次开录时自己选的。
      </div>
    </div>
  );
};

export default VoicePage;
