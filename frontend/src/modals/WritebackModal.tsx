import React, { useMemo, useState } from 'react';
import useSWR from 'swr';
import { Icon } from '../components/icons';
import { api, fetcher, WritebackProposal } from '../api';

type Props = {
  proposal: WritebackProposal;
  taskId: string;
  onClose: () => void;
  onDone: () => void;
};

const WritebackModal: React.FC<Props> = ({ proposal, taskId, onClose, onDone }) => {
  const p = proposal.payload || {};
  const at = proposal.action_type;
  const isDispatch = at === 'batch_dispatch' || at === 'send_im';
  const target = proposal.target || '/';

  const rawItems: any[] = at === 'batch_dispatch'
    ? (p.items || [])
    : at === 'send_im'
      ? [{ action_type: 'send_im', label: target, payload: { chat_id: p.chat_id, text: p.text } }]
      : [];
  const items = rawItems.map((it, i) => ({ ...it, _i: i }));
  const msgItems = items.filter(it => it.action_type === 'send_im');
  const taskItems = items.filter(it => it.action_type === 'create_task');
  const hasMsg = msgItems.length > 0;
  const msgText: string = msgItems[0]?.payload?.text || '';

  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<any>(null);
  // 默认都不勾：任务、群消息全部 opt-in，由用户逐项勾选要落地的动作。
  const [selected, setSelected] = useState<Set<number>>(() => new Set());
  const [msgChats, setMsgChats] = useState<Set<string>>(new Set());
  const [chatQuery, setChatQuery] = useState('');

  const { data: chatData } = useSWR<{ items: any[] }>(
    isDispatch && hasMsg ? '/api/dispatch/chats' : null, fetcher);
  const chats = chatData?.items || [];
  const filteredChats = useMemo(() => {
    const q = chatQuery.trim().toLowerCase();
    return q ? chats.filter((c: any) => (c.name || '').toLowerCase().includes(q)) : chats;
  }, [chats, chatQuery]);

  const title = p.title || '未命名草稿';
  const contentMd = p.content_markdown || '';
  const previewText = p.preview_text || '';

  const heading = isDispatch ? '协作分发 · 确认' : '写回飞书 · 确认';
  const selTaskCount = taskItems.filter(it => selected.has(it._i)).length;
  const selMsgCount = hasMsg ? msgChats.size : 0;
  const selCount = selTaskCount + selMsgCount;
  const confirmLabel = isDispatch ? `确认分发（${selCount}）` : '确认写回飞书';

  function toggle(i: number) {
    setSelected(prev => { const n = new Set(prev); n.has(i) ? n.delete(i) : n.add(i); return n; });
  }
  function toggleChat(cid: string) {
    setMsgChats(prev => { const n = new Set(prev); n.has(cid) ? n.delete(cid) : n.add(cid); return n; });
  }

  async function confirm() {
    if (!proposal.id) return;
    setConfirming(true); setError(null);
    try {
      const body: any = { queue_id: proposal.id };
      if (at === 'batch_dispatch') {
        const chosen: any[] = [];
        for (const t of taskItems) {
          if (selected.has(t._i)) { const { _i, ...rest } = t; chosen.push(rest); }
        }
        // 群消息 fan-out：每选一个群就发一条。
        if (hasMsg && msgText) {
          for (const cid of msgChats) {
            const c = chats.find((x: any) => x.chat_id === cid);
            chosen.push({ action_type: 'send_im', label: `群消息 → ${c?.name || cid}`, payload: { chat_id: cid, text: msgText, markdown: true } });
          }
        }
        body.edits = { items: chosen };
      }
      const resp = await api.post<{ ok: boolean; result: any }>('/api/writeback/confirm', body);
      setResult(resp.result);
      const hasFail = resp.result && typeof resp.result.fail_count === 'number' && resp.result.fail_count > 0;
      if (!hasFail) setTimeout(onDone, 1800);
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      setConfirming(false);
    }
  }

  async function reject() {
    if (!proposal.id) return;
    setConfirming(true); setError(null);
    try {
      await api.post('/api/writeback/reject', { queue_id: proposal.id, reason: '用户拒绝' });
      onDone();  // 成功即卸载弹窗，无需重置 confirming
    } catch (e: any) {
      setError(e?.message || String(e));
      setConfirming(false);
    }
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <div>
            <div style={{ fontSize: 16, fontWeight: 600 }}>{heading}</div>
            <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginTop: 2 }}>
              所有写回 / 分发飞书的动作都必须经过你的确认。任务 {taskId}
            </div>
          </div>
          <button className="btn btn-ghost btn-icon" onClick={onClose}><Icon name="x" size={14} /></button>
        </div>

        <div className="modal-body">
          {result ? (
            <DispatchResult result={result} isDispatch={isDispatch} />
          ) : isDispatch ? (
            <>
              <Row label="已选" value={
                <span>
                  {selTaskCount > 0 && <>{selTaskCount} 个任务</>}
                  {selTaskCount > 0 && selMsgCount > 0 && '　·　'}
                  {selMsgCount > 0 && <>群消息发往 {selMsgCount} 个群</>}
                  {selCount === 0 && <span style={{ color: 'var(--warning)' }}>未选任何项（勾选要分发的项）</span>}
                </span>
              } />

              {hasMsg && (
                <div style={{ marginTop: 12 }}>
                  <div style={dividerLabel}>群消息 · 发送到这些群（可多选）</div>
                  {msgChats.size > 0 && (
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, margin: '8px 0' }}>
                      {[...msgChats].map(cid => {
                        const c = chats.find((x: any) => x.chat_id === cid);
                        return (
                          <span key={cid} className="badge badge-brand" style={{ cursor: 'pointer' }} onClick={() => toggleChat(cid)} title="移除">
                            {c?.name || cid} <Icon name="x" size={10} />
                          </span>
                        );
                      })}
                    </div>
                  )}
                  <input className="input" placeholder="搜索群名…" value={chatQuery}
                         onChange={e => setChatQuery(e.target.value)} style={{ marginTop: 6 }} />
                  <div className="scroll" style={{ maxHeight: 148, overflowY: 'auto', border: '1px solid var(--border-subtle)', borderRadius: 'var(--radius-md)', marginTop: 6, padding: 4 }}>
                    {filteredChats.length === 0 ? (
                      <div style={{ padding: 8, fontSize: 12, color: 'var(--text-tertiary)' }}>{chats.length ? '无匹配的群' : '未取到群列表'}</div>
                    ) : filteredChats.slice(0, 60).map((c: any) => (
                      <label key={c.chat_id} style={{ display: 'flex', gap: 8, alignItems: 'center', padding: '4px 6px', fontSize: 13, cursor: 'pointer' }}>
                        <input type="checkbox" checked={msgChats.has(c.chat_id)} onChange={() => toggleChat(c.chat_id)}
                               style={{ accentColor: 'var(--brand-500)', flexShrink: 0 }} />
                        <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{c.name}</span>
                        {c.external && <span style={{ fontSize: 10, color: 'var(--warning)', flexShrink: 0 }}>外部</span>}
                      </label>
                    ))}
                  </div>
                  <pre style={{ ...preStyle, opacity: msgChats.size ? 1 : 0.5 }}>{msgText}</pre>
                </div>
              )}

              {taskItems.length > 0 && (
                <div style={{ marginTop: 14 }}>
                  <div style={dividerLabel}>将创建的飞书任务（默认不建 · 勾选要建的）</div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 6 }}>
                    {taskItems.map(t => {
                      const due = t.payload?.due || '';
                      const on = selected.has(t._i);
                      return (
                        <label key={'t' + t._i} style={{ display: 'flex', gap: 8, alignItems: 'baseline', fontSize: 13, cursor: 'pointer', opacity: on ? 1 : 0.5 }}>
                          <input type="checkbox" checked={on} onChange={() => toggle(t._i)}
                                 style={{ width: 15, height: 15, marginTop: 2, accentColor: 'var(--brand-500)', flexShrink: 0 }} />
                          <span style={{ flex: 1 }}>{t.payload?.title}</span>
                          {due && <span className="mono" style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>截止 {due}</span>}
                        </label>
                      );
                    })}
                  </div>
                </div>
              )}
            </>
          ) : (
            <>
              <Row label="操作类型" value={<span className="badge badge-brand">{at}</span>} />
              <Row label="目标位置" value={target} />
              <Row label="文档标题" value={<strong>{title}</strong>} />
              <Row label="摘要" value={previewText || '—'} />
              <Row label="可撤销" value="本次操作创建新文档；写回后可在飞书侧手动删除以撤销。" />
              {p.source_ref && (
                <Row label="来源文档" value={
                  <span>{p.source_ref.title}{p.source_ref.url && <> · <a href={p.source_ref.url} target="_blank" rel="noreferrer">打开</a></>}</span>
                } />
              )}
              <div style={{ marginTop: 16 }}>
                <div style={dividerLabel}>将写入的完整内容（Markdown）</div>
                <pre style={preStyle}>{contentMd}</pre>
              </div>
            </>
          )}
          {error && (
            <div className="alert alert-error" style={{ marginTop: 12 }}>
              <Icon name="warning" size={16} /> <div>{error}</div>
            </div>
          )}
        </div>

        {!result && (
          <div className="modal-footer">
            <button className="btn btn-danger btn-sm" onClick={reject} disabled={confirming}>拒绝</button>
            <button className="btn btn-secondary" onClick={onClose} disabled={confirming}>暂不{isDispatch ? '分发' : '写回'}</button>
            <button className="btn btn-primary" onClick={confirm} disabled={confirming || !proposal.id || (isDispatch && selCount === 0)}>
              <Icon name="check" size={14} />
              {confirming ? (isDispatch ? '分发中…' : '写回中…') : confirmLabel}
            </button>
          </div>
        )}
      </div>
    </div>
  );
};

const DispatchResult: React.FC<{ result: any; isDispatch: boolean }> = ({ result, isDispatch }) => {
  if (isDispatch && result && typeof result.ok_count === 'number') {
    const rows: any[] = result.results || [];
    return (
      <div>
        <div className={`alert ${result.fail_count ? 'alert-warning' : 'alert-success'}`} style={{ marginBottom: 12 }}>
          <Icon name="check" size={16} />
          <div>已分发 {result.dispatched} 项：成功 {result.ok_count}{result.fail_count ? ` · 失败 ${result.fail_count}` : ''}。</div>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {rows.map((r, i) => (
            <div key={i} style={{ fontSize: 13 }}>
              <div style={{ display: 'flex', gap: 8, alignItems: 'baseline' }}>
                <Icon name={r.ok ? 'check' : 'warning'} size={13} style={{ color: r.ok ? 'var(--success)' : 'var(--error)', flexShrink: 0 }} />
                <span style={{ flex: 1 }}>{r.label || r.action_type}</span>
                {r.ok && r.action_type === 'create_doc' && r.result?.url && (
                  <a href={r.result.url} target="_blank" rel="noreferrer" style={{ fontSize: 11 }}>打开文档</a>
                )}
                <span style={{ fontSize: 11, color: r.ok ? 'var(--success)' : 'var(--error)' }}>{r.ok ? '成功' : '失败'}</span>
              </div>
              {!r.ok && r.error && (
                <div style={{ marginLeft: 21, marginTop: 3, fontSize: 12, color: 'var(--error)', lineHeight: 1.55, wordBreak: 'break-word' }}>{r.error}</div>
              )}
            </div>
          ))}
        </div>
      </div>
    );
  }
  return (
    <div className="alert alert-success" style={{ marginBottom: 12 }}>
      <Icon name="check" size={16} />
      <div>
        {isDispatch ? '已分发！' : <>写回成功！{result?.url && <>已创建文档：<a href={result.url} target="_blank" rel="noreferrer">{result.title || result.document_id}</a></>}</>}
      </div>
    </div>
  );
};

const Row: React.FC<{ label: string; value: React.ReactNode }> = ({ label, value }) => (
  <div style={{ display: 'grid', gridTemplateColumns: '96px 1fr', gap: 12, padding: '6px 0', alignItems: 'baseline' }}>
    <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, textTransform: 'uppercase', color: 'var(--text-tertiary)' }}>{label}</div>
    <div style={{ fontSize: 13 }}>{value}</div>
  </div>
);

const dividerLabel: React.CSSProperties = {
  fontFamily: 'var(--font-mono)', fontSize: 11, textTransform: 'uppercase', color: 'var(--text-tertiary)',
};
const preStyle: React.CSSProperties = {
  background: 'var(--surface-inset)', border: '1px solid var(--border-subtle)',
  padding: 'var(--space-4)', borderRadius: 'var(--radius-md)', marginTop: 8,
  fontFamily: 'var(--font-mono)', fontSize: 12, color: 'var(--text-secondary)',
  maxHeight: 240, overflow: 'auto', whiteSpace: 'pre-wrap',
};

export default WritebackModal;
