import React from 'react';
import { Icon } from './icons';
import { useAuth } from '../hooks/useAuth';

/** 顶部条幅式授权 CTA — 未授权时常驻显示在 WorkbenchShell。
 *  点击 → 后端拿 verification_uri → 自动新开标签 → 轮询状态 → 完成后自动消失。 */
export const AuthBanner: React.FC = () => {
  const { auth, loggingIn, polling, loginInfo, startLogin, clearLogin, refresh } = useAuth();

  // 已完全授权（用户身份就绪）+ 非 mock → 不显示
  if (!auth || (auth.authenticated && !auth.mock_mode && !auth.mock)) return null;

  const mockMode = auth.mock_mode || auth.mock;
  const stage = auth.stage;
  const isNeedsLogin = stage === 'needs_login' || auth.needs_login;

  return (
    <div style={styles.wrap}>
      <div style={styles.iconWrap}>
        <Icon name="warning" size={16} />
      </div>
      <div style={{ flex: 1, fontSize: 13 }}>
        {mockMode ? (
          <>
            <strong>当前为 Mock 模式</strong>
            <span style={{ color: 'var(--text-tertiary)', marginLeft: 8 }}>
              飞书 CLI 未安装或未授权。索引与写回不会访问真实飞书；仅用于演示 UI。
            </span>
          </>
        ) : polling ? (
          <>
            <strong>等待你在新标签页完成飞书授权…</strong>
            {loginInfo?.user_code && (
              <span style={{ marginLeft: 8 }}>
                授权码：<code className="mono">{loginInfo.user_code}</code>
              </span>
            )}
            {loginInfo?.verification_uri && (
              <a
                href={loginInfo.verification_uri}
                target="_blank"
                rel="noreferrer"
                style={{ marginLeft: 12, color: 'var(--brand-700)' }}
              >
                重新打开授权页 <Icon name="external" size={11} />
              </a>
            )}
          </>
        ) : isNeedsLogin ? (
          <>
            <strong>飞书 App 已配置，但你的个人身份还未登录</strong>
            <span style={{ color: 'var(--text-tertiary)', marginLeft: 8 }}>
              点击下方按钮完成第二步：以你本人身份授权 lark-cli 访问你的飞书文档/会议/任务。
            </span>
          </>
        ) : (
          <>
            <strong>尚未完成飞书授权</strong>
            <span style={{ color: 'var(--text-tertiary)', marginLeft: 8 }}>
              点击下方按钮，系统会自动打开授权页。完成后本工作台会自动激活。
            </span>
          </>
        )}
      </div>
      <div style={{ display: 'flex', gap: 8 }}>
        {polling ? (
          <>
            <button className="btn btn-secondary btn-sm" onClick={() => refresh()}>
              <Icon name="refresh" size={12} /> 检查状态
            </button>
            <button className="btn btn-ghost btn-sm" onClick={clearLogin}>
              取消
            </button>
          </>
        ) : (
          <button className="btn btn-primary btn-sm" onClick={() => startLogin()} disabled={loggingIn}>
            <Icon name="check" size={12} /> {loggingIn ? '获取授权链接…' : '立即授权'}
          </button>
        )}
      </div>
    </div>
  );
};

const styles: Record<string, React.CSSProperties> = {
  wrap: {
    display: 'flex',
    alignItems: 'center',
    gap: 12,
    padding: '10px 24px',
    background: 'linear-gradient(90deg, var(--warning-bg), #FFF6E0)',
    borderBottom: '1px solid #F0D699',
    color: '#6B4900',
  },
  iconWrap: {
    width: 28,
    height: 28,
    borderRadius: 8,
    background: 'var(--warning)',
    color: '#fff',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    flexShrink: 0,
  },
};

export default AuthBanner;
