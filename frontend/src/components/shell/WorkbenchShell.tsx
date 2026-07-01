import React, { useEffect, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import useSWR from 'swr';
import { Icon } from '../icons';
import { fetcher, AuthStatus, AgentInfo } from '../../api';
import { AuthBanner } from '../AuthBanner';
import { CommandPalette } from '../CommandPalette';
import { NotificationBell, useToast } from '../Toast';
import { useAuth } from '../../hooks/useAuth';
import { useTaskNotifications } from '../../hooks/useTaskNotifications';
import { APP_VERSION } from '../../version';

const AGENT_TASK_PATHS: Record<string, string> = {
  'html-page': '/task/html-page',
  'document-map': '/task/document-map',
  'index-enrich': '/task/index-enrich',
  'knowledge-governance': '/task/knowledge-governance',
  'base-analysis': '/task/base-analysis',
  'pdf-recognition': '/task/pdf-recognition',
  'meeting-minutes': '/task/meeting-minutes',
  'collab-dispatch': '/task/collab-dispatch',
  'auto-extract': '/autoextract',
};

const NAV_ITEMS = [
  { id: 'home',     label: '工作台',     icon: 'home',    path: '/' },
  { id: 'scenes',   label: '任务场景',   icon: 'sparkle', path: '/scenes' },
  { id: 'tasks',    label: '运行记录',   icon: 'list',    path: '/tasks' },
  { id: 'assets',   label: '飞书文档',   icon: 'folder',  path: '/assets' },
  { id: 'localdir', label: '本地目录',   icon: 'desktop',  path: '/localdir' },
  { id: 'summaries',label: '历史总结',   icon: 'calendar',path: '/summaries' },
  { id: 'org',      label: '组织架构',   icon: 'graph',   path: '/org' },
];

interface Crumb { label: string; onClick?: () => void; }

type Props = {
  crumb: Crumb[];
  children: React.ReactNode;
  headerActions?: React.ReactNode;
};

export const WorkbenchShell: React.FC<Props> = ({ crumb, children, headerActions }) => {
  const nav = useNavigate();
  const loc = useLocation();
  const { data: auth } = useSWR<AuthStatus>('/api/auth/status', fetcher, { refreshInterval: 15000 });
  const { data: agentsData } = useSWR<{ items: AgentInfo[] }>('/api/agents', fetcher);
  const agents = agentsData?.items || [];
  // 「本地内容」(local-image) 只在内容生成页内部使用、侧栏点不开；用「自动化提炼」占其位，
  // 既给该 Agent 一个可点入口，又避免一条灰着点不动的死项。
  const sidebarAgents: AgentInfo[] = [
    ...agents.filter(a => a.id !== 'local-image'),
    { id: 'auto-extract', name: '自动化提炼', desc: '按 Enter 留痕截图 · 定时提炼工作', writeback: false, status: 'ready', color: '#0EA5E9' },
  ];
  // 重新授权（补授新增 scope，如发群消息 im:message）。已授权状态下也可触发。
  const { startLogin, loginInfo, loggingIn, polling, refresh: refreshAuth } = useAuth();
  const toast = useToast();
  // 全局任务完成提醒：任务跑完（完成 / 预览 / 待审 / 失败）自动进右上角铃铛。
  useTaskNotifications();

  const [paletteOpen, setPaletteOpen] = useState(false);
  const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform);

  // 侧边栏整条收起 / 展开，状态记进 localStorage，刷新后保持。
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    try { return localStorage.getItem('sidebar-collapsed') === '1'; } catch { return false; }
  });
  const toggleCollapsed = () => setCollapsed(c => {
    const next = !c;
    try { localStorage.setItem('sidebar-collapsed', next ? '1' : '0'); } catch { /* ignore */ }
    return next;
  });

  // ⌘K / Ctrl+K 全局唤起命令面板。
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault();
        setPaletteOpen(o => !o);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const authed = auth?.authenticated;
  const needsLogin = auth?.stage === 'needs_login' || !!auth?.needs_login;
  const userName = auth?.user_name || (authed ? '已登录' : needsLogin ? '差一步登录' : '未授权');
  const statusText = authed ? '已授权 · 飞书' : needsLogin ? 'App 已绑定 · 待登录' : '未授权 · 飞书';
  const statusDot = authed ? 'var(--success)' : needsLogin ? 'var(--warning)' : 'var(--text-tertiary)';

  return (
    <div style={styles.root}>
      <aside
        style={{
          ...styles.sidebar,
          width: collapsed ? 0 : 256,
          minWidth: 0,
          opacity: collapsed ? 0 : 1,
          borderRight: collapsed ? 'none' : '1px solid var(--border-default)',
          pointerEvents: collapsed ? 'none' : 'auto',
        }}
        className="scroll"
        aria-hidden={collapsed}
      >
        <div style={styles.brand}>
          <div style={styles.brandMark}>
            <Icon name="logo" size={22} />
          </div>
          <div style={{ minWidth: 0 }}>
            <div style={{ fontWeight: 700, fontSize: 14, color: 'var(--text-primary)', letterSpacing: '-0.01em' }}>本地 Agent 工作台</div>
            <div className="eyebrow" style={{ marginTop: 1 }}>
              Local Agent Hub · v{APP_VERSION} {auth?.mock_mode || auth?.mock ? '· mock' : ''}
            </div>
          </div>
        </div>

        <div style={styles.section}>
          <div style={styles.sectionLabel}>导航</div>
          {NAV_ITEMS.map(n => {
            const active = (n.path === '/' ? loc.pathname === '/' : loc.pathname.startsWith(n.path));
            return (
              <button key={n.id}
                      onClick={() => nav(n.path)}
                      className={`nav-btn${active ? ' active' : ''}`}>
                <Icon name={n.icon} size={16} />
                <span>{n.label}</span>
              </button>
            );
          })}
        </div>

        <div style={styles.section}>
          <div style={styles.sectionLabel}>Agents</div>
          {sidebarAgents.map(a => {
            const taskPath = AGENT_TASK_PATHS[a.id];
            const clickable = !!taskPath;
            return (
              <div
                key={a.id}
                onClick={() => clickable && nav(taskPath)}
                className="agent-row"
                style={{ cursor: clickable ? 'pointer' : 'default', opacity: clickable ? 1 : 0.55 }}
                title={clickable ? `打开 ${a.name}` : `${a.name}（暂未开放）`}
              >
                <span style={{ ...styles.agentDot, background: a.color || 'var(--brand-500)', opacity: a.status === 'ready' ? 1 : 0.35 }} />
                <span style={{ flex: 1, color: 'var(--text-secondary)', fontSize: 13, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{a.name.replace(' Agent', '')}</span>
                <span className="eyebrow" style={{ letterSpacing: '0.04em' }}>
                  {a.status === 'ready' ? '就绪' : '空闲'}
                </span>
              </div>
            );
          })}
        </div>

        <div style={{ marginTop: 'auto', padding: 'var(--space-4)' }}>
          <div className="user-card">
            <div style={styles.avatar}>{(userName || '客').slice(0, 1)}</div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 13, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{userName}</div>
              <div style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                  <span style={{ width: 6, height: 6, borderRadius: 3, background: statusDot }} />
                  {statusText}
                </span>
              </div>
            </div>
          </div>
          {!collapsed && authed && (
            <button
              className="btn btn-ghost btn-sm"
              style={{ width: '100%', marginTop: 8, justifyContent: 'center', color: 'var(--text-secondary)' }}
              disabled={loggingIn || polling}
              title="重新授权飞书，补齐新增权限（如发群消息 im:message）。会在新标签打开飞书授权页，由你本人点同意。"
              onClick={async () => {
                const info = await startLogin({ force: true });
                if (info?.verification_uri) {
                  toast.success('已打开飞书授权页', { detail: '在新标签点「同意」后，回来点下方「我已完成 · 刷新」。' });
                }
              }}
            >
              <Icon name="refresh" size={12} /> {loggingIn ? '获取链接…' : polling ? '授权中…' : '重新授权'}
            </button>
          )}
          {!collapsed && loginInfo?.verification_uri && (
            <div style={{ marginTop: 8, padding: 8, borderRadius: 8, background: 'var(--surface-subtle)', fontSize: 11, color: 'var(--text-secondary)', lineHeight: 1.6 }}>
              已在新标签打开飞书授权页。
              {loginInfo.user_code && <> 授权码 <code className="mono">{loginInfo.user_code}</code>。</>}
              <a href={loginInfo.verification_uri} target="_blank" rel="noreferrer" style={{ color: 'var(--brand-700)', marginLeft: 4 }}>重新打开</a>
              <button className="btn btn-primary btn-sm" style={{ width: '100%', marginTop: 6, justifyContent: 'center' }} onClick={() => refreshAuth()}>
                <Icon name="check" size={12} /> 我已完成 · 刷新
              </button>
            </div>
          )}
        </div>
      </aside>

      <main style={styles.main}>
        <header style={styles.topbar}>
          <button
            className="btn btn-ghost btn-icon"
            onClick={toggleCollapsed}
            title={collapsed ? '展开侧边栏' : '收起侧边栏'}
            aria-label={collapsed ? '展开侧边栏' : '收起侧边栏'}
          >
            <Icon name="sidebar" size={16} />
          </button>
          <div style={styles.crumbs}>
            {crumb.map((c, i) => (
              <React.Fragment key={i}>
                {i > 0 && <Icon name="chevron-right" size={14} style={{ color: 'var(--text-tertiary)' }} />}
                <span onClick={c.onClick}
                      style={{
                        color: i === crumb.length - 1 ? 'var(--text-primary)' : 'var(--text-tertiary)',
                        fontWeight: i === crumb.length - 1 ? 500 : 400,
                        cursor: c.onClick ? 'pointer' : 'default',
                      }}>
                  {c.label}
                </span>
              </React.Fragment>
            ))}
          </div>
          <div style={{ flex: 1 }} />
          <button className="search-box" type="button" onClick={() => setPaletteOpen(true)}
                  style={{ cursor: 'pointer', font: 'inherit' }} title="搜索（⌘K / Ctrl+K）">
            <Icon name="search" size={14} />
            <span style={{ fontSize: 13, flex: 1, textAlign: 'left' }}>搜索资产、Agent、任务…</span>
            <span className="kbd">{isMac ? '⌘' : 'Ctrl'} K</span>
          </button>
          {headerActions}
          <NotificationBell />
        </header>

        <AuthBanner />

        <div style={styles.content} className="scroll">
          {children}
        </div>
      </main>

      {paletteOpen && (
        <CommandPalette agents={agents} onClose={() => setPaletteOpen(false)} />
      )}
    </div>
  );
};

const styles: Record<string, React.CSSProperties> = {
  root: { display: 'flex', height: '100vh', overflow: 'hidden' },
  sidebar: {
    width: 256, background: 'var(--surface-elevated)',
    borderRight: '1px solid var(--border-default)',
    display: 'flex', flexDirection: 'column', overflowY: 'auto', flexShrink: 0,
    transition: 'width 220ms cubic-bezier(0.4, 0, 0.2, 1), opacity 160ms ease',
  },
  brand: {
    display: 'flex', alignItems: 'center', gap: 11,
    padding: 'var(--space-5) var(--space-4) var(--space-4)',
    borderBottom: '1px solid var(--border-subtle)',
  },
  brandMark: {
    width: 38, height: 38, borderRadius: 11, flexShrink: 0,
    background: 'var(--grad-brand)', color: '#fff',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    boxShadow: 'var(--shadow-brand)',
  },
  section: { padding: '14px 12px 4px' },
  sectionLabel: {
    padding: '6px 11px 8px', fontSize: 11, fontFamily: 'var(--font-mono)',
    textTransform: 'uppercase', color: 'var(--text-tertiary)', letterSpacing: '0.08em',
  },
  agentDot: { width: 8, height: 8, borderRadius: 4, flexShrink: 0 },
  avatar: {
    width: 34, height: 34, borderRadius: '50%',
    background: 'var(--grad-brand)',
    color: '#fff', fontSize: 14, fontWeight: 600,
    display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
    boxShadow: 'var(--shadow-brand)',
  },
  main: { flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0, background: 'transparent' },
  topbar: {
    display: 'flex', alignItems: 'center', gap: 12,
    padding: '12px 28px', borderBottom: '1px solid var(--border-subtle)',
    background: 'rgba(255, 255, 255, 0.72)',
    backdropFilter: 'blur(10px)', WebkitBackdropFilter: 'blur(10px)',
    flexShrink: 0, zIndex: 5,
  },
  crumbs: { display: 'flex', alignItems: 'center', gap: 6, fontSize: 13 },
  content: { flex: 1, overflowY: 'auto' },
};

export default WorkbenchShell;
