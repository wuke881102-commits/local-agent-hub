// 图表渲染组件：ECharts（数据图，数字由后端精确聚合）、Mermaid（甘特）、生图（GPT-Image-1）。
// 每张图一张卡，带「下载」。数据图下载 PNG，甘特下载 SVG，生图直接下载原 PNG。
import React, { useEffect, useRef, useState } from 'react';
import * as echarts from 'echarts';
import mermaid from 'mermaid';

function triggerDownload(url: string, filename: string) {
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function safeName(s: string): string {
  return (s || 'chart').replace(/[\\/:*?"<>|\n]/g, '_').slice(0, 40);
}

// ── ECharts 封装 ─────────────────────────────────────────────────────
export const EChart: React.FC<{
  option: any;
  height?: number;
  onReady?: (inst: echarts.ECharts) => void;
}> = ({ option, height = 340, onReady }) => {
  const ref = useRef<HTMLDivElement>(null);
  const instRef = useRef<echarts.ECharts | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    const inst = echarts.init(ref.current, undefined, { renderer: 'canvas' });
    instRef.current = inst;
    inst.setOption(option || {});
    onReady?.(inst);
    const ro = new ResizeObserver(() => inst.resize());
    ro.observe(ref.current);
    return () => { ro.disconnect(); inst.dispose(); instRef.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => { instRef.current?.setOption(option || {}, true); }, [option]);

  return <div ref={ref} style={{ width: '100%', height }} />;
};

// ── Mermaid 封装（甘特 / 流程） ───────────────────────────────────────
let _mermaidInit = false;
function ensureMermaid() {
  if (_mermaidInit) return;
  mermaid.initialize({ startOnLoad: false, theme: 'default', securityLevel: 'strict' });
  _mermaidInit = true;
}

export const MermaidChart: React.FC<{
  code: string;
  id: string;
  onSvg?: (svg: string) => void;
}> = ({ code, id, onSvg }) => {
  const [svg, setSvg] = useState('');
  const [err, setErr] = useState('');

  useEffect(() => {
    ensureMermaid();
    let alive = true;
    const domId = `mmd-${id}-${Math.random().toString(36).slice(2, 8)}`;
    mermaid.render(domId, code)
      .then(({ svg }) => { if (alive) { setSvg(svg); onSvg?.(svg); } })
      .catch((e) => { if (alive) setErr(String(e?.message || e)); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [code, id]);

  if (err) {
    return <div style={{ color: 'var(--text-tertiary)', fontSize: 13 }}>甘特图渲染失败：{err}</div>;
  }
  return <div style={{ overflowX: 'auto' }} dangerouslySetInnerHTML={{ __html: svg }} />;
};

// ── 一张图的卡片（按 engine 分发 + 下载） ─────────────────────────────
const ENGINE_BADGE: Record<string, string> = { echarts: '数据图', mermaid: '甘特图', image: 'AI 生图' };

export const ChartCard: React.FC<{ chart: any; idx: number }> = ({ chart, idx }) => {
  const instRef = useRef<echarts.ECharts | null>(null);
  const svgRef = useRef<string>('');
  const engine: string = chart.engine || 'echarts';
  const title: string = chart.title || '图表';

  function downloadPng() {
    const inst = instRef.current;
    if (!inst) return;
    const url = inst.getDataURL({ type: 'png', pixelRatio: 2, backgroundColor: '#fff' });
    triggerDownload(url, `${safeName(title)}.png`);
  }
  function downloadSvg() {
    if (!svgRef.current) return;
    const blob = new Blob([svgRef.current], { type: 'image/svg+xml;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    triggerDownload(url, `${safeName(title)}.svg`);
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  return (
    <div className="card" style={{ padding: 16, display: 'flex', flexDirection: 'column' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
        <strong style={{ fontSize: 14 }}>{title}</strong>
        <span className="badge">{ENGINE_BADGE[engine] || engine}</span>
        <div style={{ marginLeft: 'auto' }}>
          {engine === 'echarts' && (
            <button className="btn btn-ghost btn-sm" onClick={downloadPng}>下载 PNG</button>
          )}
          {engine === 'mermaid' && (
            <button className="btn btn-ghost btn-sm" onClick={downloadSvg}>下载 SVG</button>
          )}
          {engine === 'image' && chart.image_url && (
            <a className="btn btn-ghost btn-sm" href={chart.image_url} download={`${safeName(title)}.png`}>下载</a>
          )}
        </div>
      </div>
      {chart.rationale && (
        <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 8, lineHeight: 1.5 }}>{chart.rationale}</div>
      )}

      {engine === 'echarts' && chart.option && (
        <EChart option={chart.option} onReady={(i) => { instRef.current = i; }} />
      )}

      {engine === 'mermaid' && chart.mermaid && (
        <MermaidChart code={chart.mermaid} id={String(idx)} onSvg={(s) => { svgRef.current = s; }} />
      )}

      {engine === 'image' && (
        chart.placeholder || !chart.image_url ? (
          <div style={{
            border: '1px dashed var(--border-strong)', borderRadius: 8, padding: '28px 16px',
            textAlign: 'center', color: 'var(--text-tertiary)', fontSize: 13, background: 'var(--surface-subtle)',
          }}>
            🖼 架构 / 关系图未生成
            <div style={{ marginTop: 6, fontSize: 12 }}>{chart.note || '未配置 GPT-Image-1。'}</div>
          </div>
        ) : (
          <img src={chart.image_url} alt={title} style={{ maxWidth: '100%', borderRadius: 8, border: '1px solid var(--border-subtle)' }} />
        )
      )}
    </div>
  );
};
