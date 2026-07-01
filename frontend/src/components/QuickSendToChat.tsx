import React, { useState } from 'react';
import useSWR from 'swr';
import { Icon } from './icons';
import { api, fetcher, errMsg } from '../api';
import { useToast } from './Toast';

type Chat = { chat_id: string; name: string };

/**
 * 「直接发送到群」：选目标群 → 点一次「直接发送」即立即发出，跳过草稿 / 写回确认弹窗，
 * 无任何二次确认。内容由 `text` 传入（如自动化提炼的提炼原文 Markdown）。
 */
const QuickSendToChat: React.FC<{ text: string; disabled?: boolean; hint?: string }> = ({ text, disabled, hint }) => {
  const toast = useToast();
  const { data } = useSWR<{ items: Chat[] }>('/api/dispatch/chats', fetcher);
  const chats = data?.items || [];
  const [chatId, setChatId] = useState('');
  const [sending, setSending] = useState(false);
  const chat = chats.find((c) => c.chat_id === chatId);

  const send = async () => {
    if (!chatId) { toast.error('请先选择目标群'); return; }
    if (!text.trim()) { toast.error('没有可发送的内容'); return; }
    setSending(true);
    try {
      await api.post('/api/dispatch/send', { chat_id: chatId, text, markdown: true });
      toast.success('已发送到群', { detail: chat?.name || chatId });
    } catch (e) { toast.error('发送失败', { detail: errMsg(e) }); }
    finally { setSending(false); }
  };

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
      <select className="input" value={chatId} onChange={(e) => setChatId(e.target.value)}
              style={{ minWidth: 180, maxWidth: 280, height: 32, padding: '0 10px', fontSize: 13 }} disabled={disabled || sending}>
        <option value="">{chats.length ? '— 选择目标群 —' : '— 暂无可发送的群 —'}</option>
        {chats.map((c) => <option key={c.chat_id} value={c.chat_id}>{c.name}</option>)}
      </select>
      <button className="btn btn-primary btn-sm" onClick={send}
              disabled={disabled || sending || !chatId || !text.trim()}>
        <Icon name="send" size={13} /> {sending ? '发送中…' : '直接发送'}
      </button>
      {hint && <span style={{ fontSize: 11.5, color: 'var(--text-tertiary)' }}>{hint}</span>}
    </div>
  );
};

export default QuickSendToChat;
