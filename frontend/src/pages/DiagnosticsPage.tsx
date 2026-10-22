import React from 'react';
import useSWR from 'swr';
import { Icon } from '../components/icons';
import { authUsable, fetcher, Diagnostics } from '../api';
import { useAuth } from '../hooks/useAuth';

const DiagnosticsPage: React.FC = () => {
  const { data, error, mutate } = useSWR<Diagnostics>('/api/diagnostics', fetcher, { refreshInterval: 10000 });
  const { loggingIn, polling, loginInfo, startLogin } = useAuth();

  if (error) return (
    <div style={{ padding: 24 }}>
      <div style={{ marginBottom: 12, color: 'var(--error)' }}>诊断接口请求失败：{String((error as any)?.message || error)}</div>
      <button className="btn btn-tonal btn-sm" onClick={() => mutate()}><Icon name="refresh" size={14} /> 重试</button>
    </div>
  );
  if (!data) return <div style={{ padding: 24, color: 'var(--text-secondary)' }}>加载中…（首次检测含模型连通性探测，最多约 12 秒）</div>;

  // 真正的修在后端（feishu/cli.py 的 _scope_list 已把它收成数组）。这里再兜一层，
  // 是因为这一页的失败方式太差：拿到字符串就 .map，整页白屏 —— 连「后端挂了」
  // 这种它本该告诉你的事都看不见了。诊断页尤其不能自己先死。
  const scopes: string[] = Array.isArray(data.auth.scopes)
    ? data.auth.scopes
    : String(data.auth.scopes || '').split(/\s+/).filter(Boolean);

  return (
    <div style={{ padding: 'var(--space-8)' }}>
      <div style={{ display: 'flex', alignItems: 'center', marginBottom: 'var(--space-4)' }}>
        <h2 style={{ margin: 0, fontSize: 20, fontWeight: 600 }}>系统诊断</h2>
        <div style={{ flex: 1 }} />
        <button className="btn btn-tonal btn-sm" onClick={() => mutate()}><Icon name="refresh" size={14} /> 重新检测</button>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 'var(--space-4)' }}>
        <Section title="飞书 CLI" tone={data.cli.available ? 'success' : 'error'}>
          <KV k="状态" v={
            <span className={`badge ${data.cli.available ? 'badge-success' : 'badge-error'}`}>
              {data.cli.mode}
            </span>
          } />
          <KV k="版本" v={<span className="mono">{data.cli.version || '—'}</span>} />
          {/* 捆绑的 CLI 落后多少。这一栏是有来历的：构建脚本曾把它冻在首次构建那版，
              一路落后 45 个小版本没人发现，等某次发版一口气跨过去，文档抽取、错误信息、
              图片下载同一天全坏，而每一项的表象都像是「你的文档有问题」。
              问不出来时显示「未获知」而不是「已最新」—— 编一个安心的结论最要不得。 */}
          <KV k="CLI 更新" v={
            !data.cli.update
              ? <span style={{ color: 'var(--text-secondary)' }} title="mock 模式、网络不通或命令超时时问不出来。这不等于已是最新。">未获知</span>
              : data.cli.update.behind === 0
                ? <span className="badge badge-success">已是最新</span>
                : <a href={data.cli.update.url || undefined} target="_blank" rel="noreferrer"
                     className={'badge' + ((data.cli.update.behind ?? 99) >= 10 ? ' badge-warning' : '')}
                     title={`捆绑 ${data.cli.update.current} · 最新 ${data.cli.update.latest}。飞书的所有功能都踩在它上面；落后太多时一次性升级会同时吞下多处破坏性改动。升级方式：改 build\\build_installer.cmd 的 LARKCLIVER 后重新打包。`}>
                    {data.cli.update.behind === null
                      ? `${data.cli.update.current} → ${data.cli.update.latest}`
                      : `落后 ${data.cli.update.behind} 个版本`}
                  </a>
          } />
          <KV k="二进制" v={<span className="mono">{data.cli.bin}</span>} />
          {data.cli.mode !== 'live' && (
            <div className="alert alert-warning" style={{ marginTop: 8 }}>
              <Icon name="warning" size={16} />
              <div>
                飞书 CLI 不可用，目前处于 <strong>mock 模式</strong>。<br/>
                安装方法：<code>npx @larksuite/cli@latest install</code>，安装后重启服务。
              </div>
            </div>
          )}
        </Section>

        {/* 这一页是用来排查的，所以三个状态要分开显示；但 needs_refresh 的**色调是绿的**
            —— 它不需要用户做任何事，标黄只会让人以为出了问题、又去点一次授权。
            剩余天数来自保活服务（authkeep），它是「还剩几天必须产生一次用户调用」的真答案。 */}
        <Section title="飞书授权" tone={authUsable(data.auth) ? 'success' : 'warning'}>
          <KV k="状态" v={
            data.auth.user_status === 'ready' || data.auth.authenticated
              ? <span className="badge badge-success">已授权</span>
              : data.auth.user_recoverable
                ? <span className="badge badge-success" title="access token 过期了，但 refresh token 还在；下一次真实的用户身份调用会自动续期，不需要你做任何事">
                    已授权 · 令牌将自动续期
                  </span>
                : <span className="badge badge-warning">未授权</span>} />
          {data.auth_keepalive?.days_left !== null && data.auth_keepalive?.days_left !== undefined && (
            <KV k="授权剩余" v={
              <span className={"badge " + (data.auth_keepalive.days_left > 2 ? "badge-success" : "badge-warning")}
                    title="7 天滚动窗口：每次真实的用户身份调用都会把它推到 7 天后。保活服务每 12 小时自动做一次。">
                {data.auth_keepalive.days_left} 天
              </span>} />
          )}
          <KV k="用户" v={data.auth.user_name || data.auth.user_id || '—'} />
          <KV k="权限范围" v={
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, alignItems: 'center' }}>
              {scopes.slice(0, 8).map(s => <span key={s} className="badge">{s}</span>)}
              {scopes.length > 8 &&
                <span style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>等 {scopes.length} 项</span>}
              {!scopes.length && '—'}
            </div>
          } />
          {!authUsable(data.auth) ? (
            <button className="btn btn-primary btn-sm" onClick={() => startLogin()} disabled={loggingIn || polling} style={{ marginTop: 12 }}>
              <Icon name="check" size={12} />
              {loggingIn ? '获取链接中…' : polling ? '等待浏览器完成…' : '立即授权'}
            </button>
          ) : (
            <button
              className="btn btn-secondary btn-sm"
              onClick={() => startLogin({ force: true })}
              disabled={loggingIn || polling}
              style={{ marginTop: 12 }}
              title="按当前最小权限集重新授权一次——补齐新增的权限（如发群消息 im:message）。会在浏览器打开飞书授权页，由你本人点同意。"
            >
              <Icon name="refresh" size={12} />
              {loggingIn ? '获取链接中…' : polling ? '等待浏览器完成…' : '重新授权（补齐权限）'}
            </button>
          )}
          {loginInfo?.verification_uri && (
            <div className="alert alert-info" style={{ marginTop: 12 }}>
              <Icon name="external" size={16} />
              <div>
                授权页已自动新开浏览器标签。完成后本页会自动变绿。<br/>
                若未打开，请手动访问：
                <a href={loginInfo.verification_uri} target="_blank" rel="noreferrer" style={{ wordBreak: 'break-all' }}>{loginInfo.verification_uri}</a>
                {loginInfo.user_code && <><br/>授权码：<code className="mono">{loginInfo.user_code}</code></>}
              </div>
            </div>
          )}
        </Section>

        <Section title="模型连通" tone={(data.llm.text?.ok && data.llm.vision?.ok) || data.llm.mock ? 'success' : 'error'}>
          <KV k="文本模型" v={
            <span>
              <span className="mono">{data.env.text_model}</span>{' '}
              <span className="badge badge-info">{data.env.text_provider}</span>{' '}
              <span className={`badge ${data.llm.text?.ok ? 'badge-success' : 'badge-error'}`}>
                {data.llm.text?.mock ? 'mock' : data.llm.text?.ok ? `${data.llm.text.latency_ms ?? 0} ms` : '失败'}
              </span>
            </span>
          } />
          {/* 快省档和最强档以前不显示。它们各有真实调用点（批量回填走 fast、
              HTML 页面生成走 best），配错了会静默用错模型，所以必须看得到。 */}
          <KV k="快省 / 最强档" v={
            <span>
              <span className="mono">{data.env.text_model_fast || '—'}</span>
              <span style={{ color: 'var(--text-tertiary)' }}> / </span>
              <span className="mono">{data.env.text_model_best || '—'}</span>
            </span>
          } />
          <KV k="文本端点" v={<span className="mono" style={{ fontSize: 12, color: 'var(--text-tertiary)', wordBreak: 'break-all' }}>{data.env.text_endpoint || '—'}</span>} />
          <KV k="视觉模型" v={
            <span>
              <span className="mono">{data.env.vision_model}</span>{' '}
              <span className="badge badge-info">{data.env.vision_provider}</span>{' '}
              <span className={`badge ${data.llm.vision?.ok ? 'badge-success' : 'badge-error'}`}>
                {data.llm.vision?.mock ? 'mock' : data.llm.vision?.ok ? `${data.llm.vision.latency_ms ?? 0} ms` : '失败'}
              </span>
            </span>
          } />
          {/* vision_last_model = 上一次真正服务了读图请求的模型。它和 vision_model
              不一致，就说明主档正在失败、现在是兜底在顶 —— 排查读图问题第一个该看这里。
              一致时不显示，免得平时多一行噪音。 */}
          <KV k="视觉兜底" v={
            <span>
              <span className="mono">{data.env.vision_fallback_model || '—'}</span>{' '}
              {data.env.vision_last_model && data.env.vision_last_model !== data.env.vision_model
                ? <span className="badge badge-warning" title="主档读图失败，当前由兜底模型顶着">
                    正在顶：{data.env.vision_last_model}
                  </span>
                : <span style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>未启用（主档正常）</span>}
            </span>
          } />
          <KV k="视觉端点" v={<span className="mono" style={{ fontSize: 12, color: 'var(--text-tertiary)', wordBreak: 'break-all' }}>{data.env.vision_endpoint || '—'}</span>} />
          {/* 生图和语音以前在整个界面里一处都看不到，想确认只能翻 .env。
              生图只报配置状态，**不做连通探测** —— images.generate 一调就真扣费。
              语音是 realtime WebSocket，不在 llm.ping 覆盖范围内，所以也只报配置。 */}
          <KV k="生图模型" v={
            <span>
              <span className="mono">{data.env.image_model || '—'}</span>{' '}
              {data.env.image_provider && <span className="badge badge-info">{data.env.image_provider}</span>}{' '}
              {data.llm.image
                ? <span className={`badge ${data.llm.image.mock ? 'badge-warning' : 'badge-success'}`}
                        title="生图不做连通探测（真调用会扣费），这里只报配置状态">
                    {data.llm.image.mock ? '未配置' : '已配置'}
                  </span>
                : null}
            </span>
          } />
          <KV k="语音模型" v={
            <span>
              <span className="mono">{data.env.audio_model || '—'}</span>{' '}
              <span className="badge" title="realtime WebSocket，不在模型连通探测的覆盖范围内">未探测</span>
            </span>
          } />
          <KV k="语音端点" v={<span className="mono" style={{ fontSize: 12, color: 'var(--text-tertiary)', wordBreak: 'break-all' }}>{data.env.audio_endpoint || '—'}</span>} />
          {(data.llm.text?.error || data.llm.vision?.error) && (
            <div className="alert alert-error" style={{ marginTop: 8 }}>
              <Icon name="warning" size={16} />
              <div>
                {data.llm.text?.error && <div>文本：{data.llm.text.error}</div>}
                {data.llm.vision?.error && <div>视觉：{data.llm.vision.error}</div>}
              </div>
            </div>
          )}
          {data.llm.mock && (
            <div className="alert alert-info" style={{ marginTop: 8 }}>
              <Icon name="warning" size={16} />
              <div>未配置 API Key 或 endpoint，文本模型走 mock 模式。请在 backend/.env 中填写后重启。</div>
            </div>
          )}
        </Section>

        <Section title="飞书索引" tone={(data.index?.total || 0) > 0 ? 'success' : 'info'}>
          <KV k="资产总数" v={<span className="mono" style={{ fontSize: 20, fontWeight: 600 }}>{data.index?.total || 0}</span>} />
          <KV k="上次刷新" v={<span className="mono">{data.index?.last_refreshed || '—'}</span>} />
          <KV k="分类" v={
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
              {Object.entries(data.index || {}).filter(([k]) => !['total', 'last_refreshed'].includes(k)).map(([k, v]) => (
                <span key={k} className="badge">{k}: {String(v)}</span>
              ))}
            </div>
          } />
        </Section>
      </div>

      <h3 style={{ marginTop: 32, marginBottom: 8 }}>最近审计</h3>
      <div className="card" style={{ padding: 0 }}>
        <table className="table">
          <thead>
            <tr><th>时间</th><th>Agent</th><th>动作</th><th>对象</th><th>结果</th></tr>
          </thead>
          <tbody>
            {(data.audit_recent || []).length === 0 && (
              <tr><td colSpan={5} style={{ padding: 24, textAlign: 'center', color: 'var(--text-tertiary)' }}>暂无审计记录</td></tr>
            )}
            {data.audit_recent.map((a: any) => (
              <tr key={a.id}>
                <td className="mono" style={{ fontSize: 12 }}>{a.ts}</td>
                <td>{a.agent_id || '—'}</td>
                <td><span className="badge">{a.action}</span></td>
                <td>{a.target || '—'}</td>
                <td>
                  <span className={`badge badge-${a.outcome === 'executed' || a.outcome === 'done' ? 'success' : a.outcome === 'failed' ? 'error' : 'info'}`}>
                    {a.outcome}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
};

const Section: React.FC<{ title: string; tone: string; children: React.ReactNode }> = ({ title, children }) => (
  <div className="card">
    <h3 style={{ margin: '0 0 12px', fontSize: 16, fontWeight: 600 }}>{title}</h3>
    {children}
  </div>
);

const KV: React.FC<{ k: string; v: React.ReactNode }> = ({ k, v }) => (
  <div style={{ display: 'grid', gridTemplateColumns: '110px 1fr', padding: '6px 0', gap: 12, alignItems: 'center' }}>
    <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, textTransform: 'uppercase', color: 'var(--text-tertiary)' }}>{k}</div>
    <div style={{ fontSize: 13 }}>{v}</div>
  </div>
);

export default DiagnosticsPage;
