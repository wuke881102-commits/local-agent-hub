import { useEffect, useRef, useState } from 'react';
import useSWR from 'swr';
import { api, fetcher, errMsg, AuthStatus } from '../api';
import { useToast } from '../components/Toast';

export type LoginInfo = {
  verification_uri?: string;
  user_code?: string;
  expires_in?: number;
  interval?: number;
  mock?: boolean;
  mock_mode?: boolean;
  error?: string;
};

export function useAuth() {
  const toast = useToast();
  const { data, mutate } = useSWR<AuthStatus>('/api/auth/status', fetcher, { refreshInterval: 10000 });
  const [loginInfo, setLoginInfo] = useState<LoginInfo | null>(null);
  const [loggingIn, setLoggingIn] = useState(false);
  const [polling, setPolling] = useState(false);
  const pollTimer = useRef<number | null>(null);

  async function startLogin(opts?: { force?: boolean }): Promise<LoginInfo | null> {
    setLoggingIn(true);
    setLoginInfo(null);
    try {
      // force=true：已授权状态下重新发起登录，用于补授新增 scope（如发群消息 im:message）。
      const info = await api.post<LoginInfo>('/api/auth/login' + (opts?.force ? '?force=true' : ''));
      setLoginInfo(info);
      // user clicked → new tab won't be blocked
      if (info.verification_uri) {
        window.open(info.verification_uri, '_blank', 'noopener,noreferrer');
      }
      setPolling(true);
      return info;
    } catch (err) {
      toast.error('获取授权链接失败', { detail: errMsg(err) });
      return null;
    } finally {
      setLoggingIn(false);
    }
  }

  function cancelPolling() {
    setPolling(false);
    if (pollTimer.current) { clearInterval(pollTimer.current); pollTimer.current = null; }
  }

  function clearLogin() {
    setLoginInfo(null);
    cancelPolling();
  }

  useEffect(() => {
    if (!polling) return;
    pollTimer.current = window.setInterval(async () => {
      try {
        const status = await api.get<AuthStatus>('/api/auth/status');
        if (status.authenticated && !status.mock_mode && !status.mock) {
          cancelPolling();
          setLoginInfo(null);
          mutate(status);
        }
      } catch {/* ignore */}
    }, 2000);
    const stop = window.setTimeout(() => cancelPolling(), 5 * 60 * 1000);
    return () => {
      if (pollTimer.current) clearInterval(pollTimer.current);
      clearTimeout(stop);
      pollTimer.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [polling]);

  return {
    auth: data,
    refresh: mutate,
    loggingIn,
    polling,
    loginInfo,
    startLogin,
    clearLogin,
  };
}
