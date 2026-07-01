import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import useSWR from 'swr';
import { api, fetcher, errMsg } from '../api';
import { Icon } from '../components/icons';
import { useToast } from '../components/Toast';

// ── 数据契约（对齐 backend/app/routes/org.py · build_department_tree） ──
type GNode = {
  id: string;
  label: string;
  type: 'dept';
  branch: string;
  depth: number;
  path: string;
  members: number;
  dept_id: string;
};
type GEdge = { source: string; target: string; kind: 'hierarchy' };
type GChangeItem = {
  dept_id: string;
  label: string;
  path: string;
  branch: string;
  members: number;       // 当前在册（含下级）
  prev: number;          // 上次在册（含下级）
  delta: number;         // 累计变化（含下级）
  own: number;           // 本级变化（= 自己 − 直接下级），把变动钉在最深的那一层
  has_children: boolean;
  kind: 'changed' | 'added' | 'removed';
};
type GChanges = {
  prev_at: string | null;
  items: GChangeItem[];
  total_delta: number;
  total_prev: number;
};
type Graph = {
  nodes: GNode[];
  edges: GEdge[];
  branches: string[];
  stats: { departments: number; members: number; branches: number };
  last_refreshed: string | null;
  changes?: GChanges;
};
type Member = { open_id: string; name: string; email: string };

type SimNode = GNode & { x: number; y: number; vx: number; vy: number; r: number; fixed: boolean };
type SimEdge = { a: SimNode; b: SimNode };

const ROOT = 'root';

// 一级部门配色（最多 16 个分支）
const PALETTE = [
  '#00AA4F', '#0095D4', '#F0A800', '#8B5CF6', '#E0567A', '#14B8A6',
  '#EC6B2D', '#6366F1', '#84CC16', '#0EA5E9', '#D946EF', '#F43F5E',
  '#22C55E', '#A855F7', '#EAB308', '#64748B',
];

function deptR(members: number, isRoot: boolean) {
  if (isRoot) return 30;
  return Math.max(5, Math.min(32, 5 + Math.sqrt(members || 0) * 1.4));
}

// 按填充色亮度选黑/白文字，保证圈内人数可读（黄色系用深色字）
function textOn(hex: string): string {
  const h = hex.replace('#', '');
  const r = parseInt(h.slice(0, 2), 16), g = parseInt(h.slice(2, 4), 16), b = parseInt(h.slice(4, 6), 16);
  return (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.62 ? '#1F2328' : '#FFFFFF';
}

function fmtTime(s: string | null): string {
  if (!s) return '—';
  return s.replace('T', ' ').slice(0, 16);
}

// 人数增减徽标：增绿减红，零灰
const DeltaBadge: React.FC<{ delta: number; label?: string }> = ({ delta, label }) => {
  const up = delta > 0, down = delta < 0;
  const bg = up ? 'rgba(0,170,79,0.12)' : down ? 'rgba(224,86,122,0.14)' : 'var(--surface-subtle)';
  const fg = up ? '#0A8F43' : down ? '#C23A5E' : 'var(--text-tertiary)';
  return (
    <span className="tnum" style={{
      display: 'inline-flex', alignItems: 'center', gap: 2, flexShrink: 0,
      background: bg, color: fg, fontSize: 11.5, fontWeight: 700,
      padding: '1px 6px', borderRadius: 6, lineHeight: 1.6,
    }}>
      {label && <span style={{ fontWeight: 600, opacity: 0.8, marginRight: 1 }}>{label}</span>}
      {up ? '▲' : down ? '▼' : '—'}{delta !== 0 ? Math.abs(delta) : ''}
    </span>
  );
};

const OrgGraphPage: React.FC = () => {
  const nav = useNavigate();
  const toast = useToast();
  const { data, error, isLoading, mutate } = useSWR<Graph>('/api/org/graph', fetcher, {
    revalidateOnFocus: false,
  });

  const [selected, setSelected] = useState<GNode | null>(null);
  const [showAllLabels, setShowAllLabels] = useState(false);
  const [focusBranch, setFocusBranch] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  // 钻取展开：expanded 集合里的部门会展示其直接下级。null = 用默认（展开到二级）。
  const [expanded, setExpanded] = useState<Set<string> | null>(null);

  // 选中部门的直属成员（按需懒加载）
  const [members, setMembers] = useState<{ deptId: string; loading: boolean; items: Member[] } | null>(null);

  const containerRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);

  const nodesRef = useRef<SimNode[]>([]);
  const edgesRef = useRef<SimEdge[]>([]);
  const adjRef = useRef<Map<string, Set<string>>>(new Map());
  const posCacheRef = useRef<Map<string, { x: number; y: number }>>(new Map());
  const colorRef = useRef<Map<string, string>>(new Map());

  const viewRef = useRef({ scale: 1, tx: 0, ty: 0, inited: false });
  const alphaRef = useRef(1);
  const hoverRef = useRef<SimNode | null>(null);
  const selectedIdRef = useRef<string | null>(null);
  const focusBranchRef = useRef<string | null>(focusBranch);
  const showLabelsRef = useRef(showAllLabels);
  const rafRef = useRef<number>(0);

  useEffect(() => { showLabelsRef.current = showAllLabels; }, [showAllLabels]);
  useEffect(() => { focusBranchRef.current = focusBranch; }, [focusBranch]);
  useEffect(() => { selectedIdRef.current = selected?.id ?? null; }, [selected]);

  const branchColor = useMemo(() => {
    const m = new Map<string, string>();
    (data?.branches || []).forEach((b, i) => m.set(b, PALETTE[i % PALETTE.length]));
    m.set('全员', '#1F2328');
    return m;
  }, [data?.branches]);

  // 拓扑：父部门 / 直接下级数 / 子部门列表
  const topo = useMemo(() => {
    const parentOf = new Map<string, string>();
    const childCount = new Map<string, number>();
    const childrenOf = new Map<string, string[]>();
    for (const e of (data?.edges || [])) {
      parentOf.set(e.target, e.source);
      childCount.set(e.source, (childCount.get(e.source) || 0) + 1);
      if (!childrenOf.has(e.source)) childrenOf.set(e.source, []);
      childrenOf.get(e.source)!.push(e.target);
    }
    return { parentOf, childCount, childrenOf };
  }, [data?.edges]);

  // 默认展开集合：根 + 所有一级部门（即默认显示到二级）
  const defaultExpanded = useMemo(() => {
    const s = new Set<string>([ROOT]);
    (data?.nodes || []).forEach((n) => { if (n.depth === 1) s.add(n.id); });
    return s;
  }, [data?.nodes]);

  const effExpanded = expanded ?? defaultExpanded;

  function toggleExpand(id: string) {
    setExpanded((prev) => {
      const s = new Set(prev ?? defaultExpanded);
      if (s.has(id)) s.delete(id); else s.add(id);
      return s;
    });
  }

  // 递归展开整棵子树（双击）
  function expandSubtree(id: string) {
    setExpanded((prev) => {
      const s = new Set(prev ?? defaultExpanded);
      const stack = [id];
      while (stack.length) {
        const cur = stack.pop()!;
        const kids = topo.childrenOf.get(cur) || [];
        if (kids.length) s.add(cur);
        for (const k of kids) stack.push(k);
      }
      return s;
    });
  }

  // 当前可见部门数（用于头部统计）
  const visibleCount = useMemo(() => {
    if (!data) return 0;
    const { parentOf } = topo;
    const vis = (n: GNode) => { let p = parentOf.get(n.id); while (p) { if (!effExpanded.has(p)) return false; p = parentOf.get(p); } return true; };
    return data.nodes.filter(vis).length;
  }, [data, topo, effExpanded]);

  // 供 canvas 闭包读取最新值的 ref（避免重建 canvas effect）
  const childCountRef = useRef(topo.childCount);
  const expandedRef = useRef(effExpanded);
  const nodeClickRef = useRef<(n: GNode) => void>(() => {});
  const nodeDblClickRef = useRef<(n: GNode) => void>(() => {});
  useEffect(() => { childCountRef.current = topo.childCount; });
  useEffect(() => { expandedRef.current = effExpanded; });
  useEffect(() => {
    nodeClickRef.current = (n: GNode) => {
      setSelected(n);
      if ((topo.childCount.get(n.id) || 0) > 0) toggleExpand(n.id);
    };
    nodeDblClickRef.current = (n: GNode) => { setSelected(n); expandSubtree(n.id); };
  });

  // 选中部门 → 拉直属成员
  useEffect(() => {
    if (!selected || !selected.dept_id) { setMembers(null); return; }
    let cancelled = false;
    setMembers({ deptId: selected.dept_id, loading: true, items: [] });
    api.get<{ members: Member[] }>(`/api/org/members?dept_id=${encodeURIComponent(selected.dept_id)}`)
      .then((r) => { if (!cancelled) setMembers({ deptId: selected.dept_id, loading: false, items: r.members || [] }); })
      .catch(() => { if (!cancelled) setMembers({ deptId: selected.dept_id, loading: false, items: [] }); });
    return () => { cancelled = true; };
  }, [selected]);

  // ── 构建仿真图（按展开状态过滤可见节点） ───────────────────────────
  useEffect(() => {
    if (!data) return;
    colorRef.current = branchColor;
    const cache = posCacheRef.current;
    const hadNodes = nodesRef.current.length > 0;
    // 快照当前活动布局，使展开/收起时已有节点位置稳定（收起的节点位置也保留）
    for (const n of nodesRef.current) cache.set(n.id, { x: n.x, y: n.y });

    const { parentOf } = topo;
    const visible = (n: GNode) => {
      let p = parentOf.get(n.id);
      while (p) { if (!effExpanded.has(p)) return false; p = parentOf.get(p); }
      return true;
    };
    const vis = data.nodes.filter(visible);

    const branchIndex = new Map<string, number>();
    (data.branches || []).forEach((b, i) => branchIndex.set(b, i));
    const nBranch = Math.max(1, (data.branches || []).length);

    const nodes: SimNode[] = vis.map((n) => {
      const isRoot = n.id === ROOT;
      let x: number, y: number;
      const cached = cache.get(n.id);
      if (cached) { x = cached.x; y = cached.y; }
      else {
        // 新展开出来的子部门：从父节点位置"长出来"
        const pc = cache.get(parentOf.get(n.id) || '');
        if (pc) { x = pc.x + (Math.random() - 0.5) * 50; y = pc.y + (Math.random() - 0.5) * 50; }
        else {
          const bi = branchIndex.get(n.branch) ?? 0;
          const ang = (bi / nBranch) * Math.PI * 2;
          x = isRoot ? 0 : Math.cos(ang) * 300 + (Math.random() - 0.5) * 220;
          y = isRoot ? 0 : Math.sin(ang) * 300 + (Math.random() - 0.5) * 220;
        }
      }
      return { ...n, x, y, vx: 0, vy: 0, r: deptR(n.members, isRoot), fixed: false };
    });
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const adj = new Map<string, Set<string>>();
    const edges: SimEdge[] = [];
    for (const e of data.edges) {
      const a = byId.get(e.source);
      const b = byId.get(e.target);
      if (!a || !b) continue;
      edges.push({ a, b });
      if (!adj.has(a.id)) adj.set(a.id, new Set());
      if (!adj.has(b.id)) adj.set(b.id, new Set());
      adj.get(a.id)!.add(b.id);
      adj.get(b.id)!.add(a.id);
    }
    nodesRef.current = nodes;
    edgesRef.current = edges;
    adjRef.current = adj;
    alphaRef.current = hadNodes ? 0.7 : 1; // 展开/收起轻微加热，首次满加热
  }, [data, branchColor, topo, effExpanded]);

  // ── 力学仿真 + Canvas 渲染 + 交互 ───────────────────────────────────
  useEffect(() => {
    const canvas = canvasRef.current;
    const container = containerRef.current;
    if (!canvas || !container) return;
    const ctx = canvas.getContext('2d')!;
    let W = 0, H = 0, dpr = Math.max(1, window.devicePixelRatio || 1);

    const resize = () => {
      const rect = container.getBoundingClientRect();
      W = rect.width; H = rect.height;
      dpr = Math.max(1, window.devicePixelRatio || 1);
      canvas.width = Math.round(W * dpr);
      canvas.height = Math.round(H * dpr);
      canvas.style.width = W + 'px';
      canvas.style.height = H + 'px';
      if (!viewRef.current.inited && W > 0) {
        viewRef.current.tx = W / 2; viewRef.current.ty = H / 2; viewRef.current.inited = true;
      }
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(container);

    const proj = (x: number, y: number): [number, number] => {
      const v = viewRef.current; return [x * v.scale + v.tx, y * v.scale + v.ty];
    };
    const unproj = (sx: number, sy: number): [number, number] => {
      const v = viewRef.current; return [(sx - v.tx) / v.scale, (sy - v.ty) / v.scale];
    };

    const tick = () => {
      const nodes = nodesRef.current;
      const edges = edgesRef.current;
      const alpha = alphaRef.current;
      if (alpha > 0.02 && nodes.length) {
        for (let i = 0; i < nodes.length; i++) {
          const a = nodes[i];
          for (let j = i + 1; j < nodes.length; j++) {
            const b = nodes[j];
            let dx = a.x - b.x, dy = a.y - b.y;
            let d2 = dx * dx + dy * dy;
            if (d2 < 1) { d2 = 1; dx = Math.random() - 0.5; dy = Math.random() - 0.5; }
            const d = Math.sqrt(d2);
            const f = (2600 / d2) * alpha;
            const fx = (dx / d) * f, fy = (dy / d) * f;
            a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
          }
        }
        for (const e of edges) {
          const dx = e.b.x - e.a.x, dy = e.b.y - e.a.y;
          const d = Math.sqrt(dx * dx + dy * dy) || 1;
          const f = (d - 58) * 0.025 * alpha;
          const fx = (dx / d) * f, fy = (dy / d) * f;
          e.a.vx += fx; e.a.vy += fy; e.b.vx -= fx; e.b.vy -= fy;
        }
        for (const n of nodes) {
          if (n.fixed) { n.vx = 0; n.vy = 0; continue; }
          n.vx += -n.x * 0.01 * alpha;
          n.vy += -n.y * 0.01 * alpha;
          n.vx *= 0.86; n.vy *= 0.86;
          n.x += n.vx; n.y += n.vy;
        }
        alphaRef.current = alpha * 0.985;
      }
    };

    const draw = () => {
      const nodes = nodesRef.current;
      const edges = edgesRef.current;
      const colors = colorRef.current;
      const adj = adjRef.current;
      const hover = hoverRef.current;
      const selId = selectedIdRef.current;
      const focus = focusBranchRef.current;
      const showLab = showLabelsRef.current;
      const v = viewRef.current;

      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);

      const neighbors = hover ? adj.get(hover.id) : null;
      const dimNode = (n: SimNode): number => {
        if (focus && n.branch !== focus && n.id !== ROOT) return 0.07;
        if (hover) {
          if (n.id === hover.id) return 1;
          return neighbors?.has(n.id) ? 1 : 0.1;
        }
        return 1;
      };

      // 边
      ctx.lineCap = 'round';
      for (const e of edges) {
        const [ax, ay] = proj(e.a.x, e.a.y);
        const [bx, by] = proj(e.b.x, e.b.y);
        let alpha = 0.4;
        let lw = 1;
        const color = colors.get(e.b.branch) || '#9AA5B1';
        if (hover) {
          const touch = e.a.id === hover.id || e.b.id === hover.id;
          alpha = touch ? 0.85 : 0.06;
          if (touch) lw = 1.6;
        } else if (focus) {
          if (e.a.branch !== focus && e.b.branch !== focus) alpha = 0.05;
        }
        ctx.globalAlpha = alpha;
        ctx.strokeStyle = color;
        ctx.lineWidth = lw;
        ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
      }
      ctx.globalAlpha = 1;

      // 节点
      const labels: { x: number; y: number; text: string; bold: boolean; alpha: number }[] = [];
      for (const n of nodes) {
        const [sx, sy] = proj(n.x, n.y);
        const r = Math.max(1.5, n.r * v.scale);
        const op = dimNode(n);
        const isRoot = n.id === ROOT;
        const color = isRoot ? '#1F2328' : (colors.get(n.branch) || '#94A3B8');
        ctx.globalAlpha = op;
        ctx.beginPath();
        ctx.arc(sx, sy, r, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();
        ctx.lineWidth = isRoot ? 2.5 : 1.5;
        ctx.strokeStyle = '#FFFFFF';
        ctx.stroke();
        // 有未展开下级 → 虚线外圈提示"可点击展开"
        if ((childCountRef.current.get(n.id) || 0) > 0 && !expandedRef.current.has(n.id)) {
          ctx.globalAlpha = op * 0.9;
          ctx.setLineDash([2, 2]);
          ctx.beginPath(); ctx.arc(sx, sy, r + 3, 0, Math.PI * 2);
          ctx.lineWidth = 1.4; ctx.strokeStyle = color; ctx.stroke();
          ctx.setLineDash([]);
        }
        if (n.id === selId) {
          ctx.globalAlpha = 1;
          ctx.beginPath(); ctx.arc(sx, sy, r + 5, 0, Math.PI * 2);
          ctx.lineWidth = 2.5; ctx.strokeStyle = '#1F2328'; ctx.stroke();
        }
        // 圈内显示在册人数（圈够大才画，缩小时自动隐藏，避免糊成一团）
        const fs = r * 0.8;
        if (fs >= 6) {
          ctx.globalAlpha = op;
          ctx.font = `600 ${Math.min(fs, 15)}px Inter, "Microsoft YaHei", sans-serif`;
          ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
          ctx.fillStyle = textOn(color);
          ctx.fillText(String(n.members), sx, sy);
        }
        const isHot = hover && (n.id === hover.id || neighbors?.has(n.id));
        const wantLabel = isRoot || n.depth <= 1 || (n.members >= 100) || showLab || isHot || n.id === selId;
        if (wantLabel && op > 0.2) {
          labels.push({ x: sx, y: sy + r + 11, text: n.label, bold: isRoot || n.depth <= 1, alpha: op });
        }
      }
      ctx.globalAlpha = 1;

      // 标签
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      for (const l of labels) {
        ctx.font = `${l.bold ? '600 ' : ''}${l.bold ? 12.5 : 11}px Inter, "Microsoft YaHei", sans-serif`;
        ctx.globalAlpha = Math.min(1, l.alpha);
        ctx.lineWidth = 3;
        ctx.strokeStyle = 'rgba(255,255,255,0.92)';
        ctx.strokeText(l.text, l.x, l.y);
        ctx.fillStyle = l.bold ? '#1F2328' : '#555B61';
        ctx.fillText(l.text, l.x, l.y);
      }
      ctx.globalAlpha = 1;
    };

    const loop = () => { tick(); draw(); rafRef.current = requestAnimationFrame(loop); };
    rafRef.current = requestAnimationFrame(loop);

    const hit = (sx: number, sy: number): SimNode | null => {
      const [wx, wy] = unproj(sx, sy);
      const nodes = nodesRef.current;
      let best: SimNode | null = null;
      let bestD = Infinity;
      for (const n of nodes) {
        const dx = n.x - wx, dy = n.y - wy;
        const d = Math.sqrt(dx * dx + dy * dy);
        const rad = n.r + 6 / viewRef.current.scale;
        if (d <= rad && d < bestD) { bestD = d; best = n; }
      }
      return best;
    };

    let dragging: SimNode | null = null;
    let panning = false;
    let lastX = 0, lastY = 0, downX = 0, downY = 0, moved = false;
    let clickTimer: number | null = null;
    const getXY = (e: PointerEvent): [number, number] => {
      const rect = canvas.getBoundingClientRect();
      return [e.clientX - rect.left, e.clientY - rect.top];
    };
    const onDown = (e: PointerEvent) => {
      const [sx, sy] = getXY(e);
      downX = sx; downY = sy; moved = false;
      const n = hit(sx, sy);
      canvas.setPointerCapture(e.pointerId);
      if (n) { dragging = n; n.fixed = true; alphaRef.current = Math.max(alphaRef.current, 0.4); }
      else { panning = true; }
      lastX = sx; lastY = sy;
    };
    const onMove = (e: PointerEvent) => {
      const [sx, sy] = getXY(e);
      if (Math.abs(sx - downX) + Math.abs(sy - downY) > 4) moved = true;
      if (dragging) {
        const [wx, wy] = unproj(sx, sy);
        dragging.x = wx; dragging.y = wy; dragging.vx = 0; dragging.vy = 0;
        alphaRef.current = Math.max(alphaRef.current, 0.3);
      } else if (panning) {
        viewRef.current.tx += sx - lastX; viewRef.current.ty += sy - lastY;
      } else {
        const n = hit(sx, sy);
        hoverRef.current = n;
        canvas.style.cursor = n ? 'pointer' : 'grab';
      }
      lastX = sx; lastY = sy;
    };
    const onUp = (e: PointerEvent) => {
      const [sx, sy] = getXY(e);
      if (dragging) dragging.fixed = false;
      if (!moved) {
        const n = hit(sx, sy);
        if (!n) {
          setSelected(null);
        } else if (clickTimer !== null) {
          // 第二次点击落在 250ms 内 → 双击：递归展开子树
          clearTimeout(clickTimer); clickTimer = null;
          nodeDblClickRef.current(n as GNode);
        } else {
          const nn = n;
          clickTimer = window.setTimeout(() => { clickTimer = null; nodeClickRef.current(nn as GNode); }, 240);
        }
      }
      dragging = null; panning = false;
      try { canvas.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
    };
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = canvas.getBoundingClientRect();
      const mx = e.clientX - rect.left, my = e.clientY - rect.top;
      const v = viewRef.current;
      const wx = (mx - v.tx) / v.scale, wy = (my - v.ty) / v.scale;
      const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
      v.scale = Math.max(0.15, Math.min(5, v.scale * factor));
      v.tx = mx - wx * v.scale; v.ty = my - wy * v.scale;
    };

    canvas.addEventListener('pointerdown', onDown);
    canvas.addEventListener('pointermove', onMove);
    canvas.addEventListener('pointerup', onUp);
    canvas.addEventListener('wheel', onWheel, { passive: false });

    return () => {
      cancelAnimationFrame(rafRef.current);
      if (clickTimer !== null) clearTimeout(clickTimer);
      ro.disconnect();
      canvas.removeEventListener('pointerdown', onDown);
      canvas.removeEventListener('pointermove', onMove);
      canvas.removeEventListener('pointerup', onUp);
      canvas.removeEventListener('wheel', onWheel);
      const cache = posCacheRef.current;
      for (const n of nodesRef.current) cache.set(n.id, { x: n.x, y: n.y });
    };
  }, [data]);

  function locate(term: string) {
    const t = term.trim().toLowerCase();
    if (!t) return;
    // 已可见 → 直接居中
    const node = nodesRef.current.find((n) => n.label.toLowerCase().includes(t));
    if (node) {
      const c = containerRef.current?.getBoundingClientRect();
      const v = viewRef.current;
      if (c) { v.scale = Math.max(v.scale, 1.2); v.tx = c.width / 2 - node.x * v.scale; v.ty = c.height / 2 - node.y * v.scale; }
      setSelected(node as GNode);
      hoverRef.current = node;
      return;
    }
    // 折叠中的部门：展开其祖先链使其可见并选中
    const target = data?.nodes.find((n) => n.label.toLowerCase().includes(t));
    if (!target) return;
    const anc = new Set<string>(effExpanded);
    let p = topo.parentOf.get(target.id);
    while (p) { anc.add(p); p = topo.parentOf.get(p); }
    setExpanded(anc);
    setSelected(target);
  }

  // 精确定位某个部门（按 dept_id，而非按名称——同名部门可能分属不同分支）：
  // 展开其祖先链使其可见，居中并选中，确保详情面板显示的就是这一个部门。
  function focusDept(deptId: string) {
    const target = data?.nodes.find((n) => n.id === deptId || n.dept_id === deptId);
    if (!target) return;
    const anc = new Set<string>(effExpanded);
    let p = topo.parentOf.get(target.id);
    while (p) { anc.add(p); p = topo.parentOf.get(p); }
    setExpanded(anc);
    // 已在场上 → 居中；尚未展开出来的，退而求其次居中到父节点位置
    const node = nodesRef.current.find((n) => n.id === target.id);
    const at = node ?? nodesRef.current.find((n) => n.id === topo.parentOf.get(target.id));
    if (at) {
      const c = containerRef.current?.getBoundingClientRect();
      const v = viewRef.current;
      if (c) { v.scale = Math.max(v.scale, 1.2); v.tx = c.width / 2 - at.x * v.scale; v.ty = c.height / 2 - at.y * v.scale; }
      if (node) hoverRef.current = node;
    }
    setSelected(target);
  }

  function resetView() {
    const c = containerRef.current?.getBoundingClientRect();
    const v = viewRef.current;
    v.scale = 1; v.tx = (c?.width || 0) / 2; v.ty = (c?.height || 0) / 2;
    alphaRef.current = Math.max(alphaRef.current, 0.3);
  }

  async function hardRefresh() {
    setRefreshing(true);
    try {
      await mutate(fetcher<Graph>('/api/org/graph?refresh=true'), { revalidate: false });
      toast.success('组织架构已重新拉取');
    } catch (err) {
      toast.error('重新拉取组织架构失败', { detail: errMsg(err) });
    } finally { setRefreshing(false); }
  }

  const crumbs = useMemo(() => (selected?.path || '').split('/').filter(Boolean), [selected]);
  const stats = data?.stats;

  // 人数变化：按 dept_id 建表，供详情面板查单个部门的 Δ
  const changeMap = useMemo(() => {
    const m = new Map<string, GChangeItem>();
    for (const it of data?.changes?.items || []) m.set(it.dept_id, it);
    return m;
  }, [data?.changes]);
  const changes = data?.changes;
  const selChange = selected?.dept_id ? changeMap.get(selected.dept_id) : undefined;
  // 卡片只列「本级」有变动的部门：把每笔增减钉在最深的那一层，不在各级祖先重复
  const changeRows = useMemo(
    () => (data?.changes?.items || [])
      .filter((it) => it.own !== 0)
      .sort((a, b) => Math.abs(b.own) - Math.abs(a.own)),
    [data?.changes],
  );

  return (
    <div style={{ position: 'relative', height: '100%', minHeight: 520, overflow: 'hidden' }}>
      <div ref={containerRef} style={{ position: 'absolute', inset: 0 }}>
        <canvas ref={canvasRef} style={{ display: 'block', touchAction: 'none' }} />
      </div>

      {/* 顶部 */}
      <div style={S.topbar}>
        <div>
          <span className="page-title" style={{ fontSize: 18 }}>组织架构图谱</span>
          <div className="eyebrow" style={{ marginTop: 3, letterSpacing: '0.04em' }}>
            {stats ? `部门 ${stats.departments}（显示 ${visibleCount}） · 全员 ${stats.members} · 一级部门 ${stats.branches}` : isLoading ? '加载中…' : '—'}
          </div>
        </div>
        <div style={{ flex: 1 }} />
        <div style={S.searchWrap}>
          <Icon name="search" size={14} style={{ color: 'var(--text-tertiary)' }} />
          <input value={search} onChange={(e) => setSearch(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') locate(search); }}
            placeholder="搜索部门…" style={S.searchInput} />
        </div>
        <button className="btn btn-secondary btn-sm" style={{ pointerEvents: 'auto' }} onClick={resetView}><Icon name="refresh" size={14} /> 复位</button>
        <button className="btn btn-secondary btn-sm" style={{ pointerEvents: 'auto' }} onClick={hardRefresh} disabled={refreshing}>
          <Icon name="refresh" size={14} className={refreshing ? 'spin' : ''} /> {refreshing ? '刷新中' : '重新拉取'}
        </button>
      </div>

      {/* 图例 */}
      {data && (
        <div style={S.legend} className="scroll">
          <div className="eyebrow" style={{ marginBottom: 6 }}>一级部门</div>
          {data.branches.map((b) => {
            const active = focusBranch === b;
            return (
              <div key={b} onClick={() => setFocusBranch(active ? null : b)}
                style={{ ...S.legendRow, background: active ? 'var(--surface-hover)' : 'transparent' }}>
                <span style={{ width: 10, height: 10, borderRadius: 3, background: branchColor.get(b), flexShrink: 0 }} />
                <span style={{ fontSize: 12, color: 'var(--text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{b}</span>
              </div>
            );
          })}
          <div style={{ height: 1, background: 'var(--border-subtle)', margin: '8px 0' }} />
          <label style={S.toggleRow}>
            <input type="checkbox" checked={showAllLabels} onChange={(e) => setShowAllLabels(e.target.checked)} /> 显示全部部门名
          </label>
          <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
            <button className="btn btn-ghost btn-sm" style={{ flex: 1, padding: '4px 6px' }}
              onClick={() => { const s = new Set<string>(); topo.childCount.forEach((c, id) => { if (c > 0) s.add(id); }); setExpanded(s); }}>展开全部</button>
            <button className="btn btn-ghost btn-sm" style={{ flex: 1, padding: '4px 6px' }}
              onClick={() => setExpanded(null)}>收起到二级</button>
          </div>
        </div>
      )}

      {/* 人数变化（较上次拉取）— 选中部门时让位给右侧详情面板 */}
      {data && !selected && (
        <div style={S.changes} className="scroll fade-in">
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 8 }}>
            <span className="eyebrow">人数变化</span>
            <span style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>
              {changes?.prev_at ? `较 ${fmtTime(changes.prev_at)}` : ''}
            </span>
            {!!changes && changes.total_delta !== 0 && (
              <span style={{ marginLeft: 'auto' }}><DeltaBadge delta={changes.total_delta} label="全员" /></span>
            )}
          </div>
          {!changes || !changes.prev_at ? (
            <div style={{ fontSize: 12, color: 'var(--text-tertiary)', lineHeight: 1.5 }}>
              首次拉取，已记录基准。下次「重新拉取」后这里会列出各部门的人数增减。
            </div>
          ) : changeRows.length === 0 ? (
            <div style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>较上次无人数变化。</div>
          ) : (
            changeRows.map((it) => {
              const parentPath = it.path.split('/').slice(0, -1).join(' / ');
              return (
                <div key={it.dept_id} style={S.changeRow}
                  title={`${it.path}　本级 ${it.own > 0 ? '+' : ''}${it.own}　含下级 ${it.prev} → ${it.members}`}
                  onClick={() => focusDept(it.dept_id)}>
                  <span style={{ width: 8, height: 8, borderRadius: 2, background: branchColor.get(it.branch) || '#94A3B8', flexShrink: 0 }} />
                  <span style={{ display: 'flex', flexDirection: 'column', minWidth: 0, flex: 1 }}>
                    <span style={{ fontSize: 12.5, color: 'var(--text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {it.label}
                      {it.kind === 'added' && <span style={S.tag}>新增</span>}
                      {it.kind === 'removed' && <span style={S.tag}>撤销</span>}
                      {it.kind === 'changed' && it.has_children && <span style={S.tag}>本级</span>}
                    </span>
                    <span style={{ fontSize: 10.5, color: 'var(--text-tertiary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {parentPath || '一级部门'}
                    </span>
                  </span>
                  <DeltaBadge delta={it.own} />
                </div>
              );
            })
          )}
        </div>
      )}

      {error && <div style={{ ...S.detail, color: 'var(--error)' }}>加载失败：{String(error).slice(0, 200)}</div>}

      {/* 部门详情 */}
      {selected && (
        <div style={S.detail} className="scroll fade-in">
          <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10 }}>
            <span style={{ width: 12, height: 12, borderRadius: 3, background: branchColor.get(selected.branch), marginTop: 5, flexShrink: 0 }} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 16, fontWeight: 700, color: 'var(--text-primary)', wordBreak: 'break-word' }}>{selected.label}</div>
              <div className="eyebrow" style={{ marginTop: 2 }}>{selected.id === ROOT ? '全公司' : `部门 · 第 ${selected.depth} 级`}</div>
            </div>
            <button className="btn btn-ghost btn-icon" onClick={() => setSelected(null)}><Icon name="x" size={15} /></button>
          </div>

          {crumbs.length > 1 && (
            <div style={{ marginTop: 12, display: 'flex', flexWrap: 'wrap', gap: 4, alignItems: 'center' }}>
              {crumbs.map((c, i) => (
                <React.Fragment key={i}>
                  {i > 0 && <Icon name="chevron-right" size={11} style={{ color: 'var(--text-tertiary)' }} />}
                  <span style={{ fontSize: 12, color: i === crumbs.length - 1 ? 'var(--text-primary)' : 'var(--text-tertiary)' }}>{c}</span>
                </React.Fragment>
              ))}
            </div>
          )}

          <div style={{ marginTop: 14 }}>
            <div style={S.kv}><span style={S.k}>在册</span><span style={{ fontSize: 13 }} className="tnum">{selected.members} 人{selected.id === ROOT ? '' : '（含下级）'}</span></div>
            {selChange && (selChange.delta !== 0 || selChange.own !== 0) && (
              <>
                <div style={S.kv}>
                  <span style={S.k}>较上次</span>
                  <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                    <DeltaBadge delta={selChange.delta} />
                    <span style={{ fontSize: 12, color: 'var(--text-tertiary)' }} className="tnum">
                      {selChange.prev} → {selChange.members}{selected.id !== ROOT ? '（含下级）' : ''}
                    </span>
                  </span>
                </div>
                {selChange.has_children && selChange.own !== selChange.delta && (
                  <div style={S.kv}>
                    <span style={S.k}>本级</span>
                    <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <DeltaBadge delta={selChange.own} />
                      <span style={{ fontSize: 11.5, color: 'var(--text-tertiary)' }}>不含下级，仅本部门直属</span>
                    </span>
                  </div>
                )}
              </>
            )}
            {(topo.childCount.get(selected.id) || 0) > 0 && (
              <button className="btn btn-secondary btn-sm" style={{ marginTop: 6, width: '100%' }}
                onClick={() => toggleExpand(selected.id)}>
                {effExpanded.has(selected.id) ? '收起下级' : `展开下级（${topo.childCount.get(selected.id)} 个部门）`}
              </button>
            )}
            {selected.id !== ROOT && (
              <button className="btn btn-tonal btn-sm" style={{ marginTop: 6, width: '100%' }}
                onClick={() => setFocusBranch(focusBranch === selected.branch ? null : selected.branch)}>
                {focusBranch === selected.branch ? '取消聚焦分支' : '聚焦所属分支'}
              </button>
            )}
          </div>

          {/* 直属成员 */}
          {selected.id !== ROOT && (
            <div style={{ marginTop: 16 }}>
              <div className="eyebrow" style={{ marginBottom: 6 }}>
                直属成员{members && !members.loading ? ` · ${members.items.length}` : ''}
              </div>
              {members && !members.loading && selected.members > members.items.length && (
                <div style={{ fontSize: 11, color: 'var(--text-tertiary)', lineHeight: 1.5, marginBottom: 8, background: 'var(--surface-subtle)', borderRadius: 6, padding: '6px 8px' }}>
                  「直属成员」只含主部门在本部门的人；「在册 {selected.members}（含下级）」还含下级部门成员、兼岗及部门负责人，故多 {selected.members - members.items.length} 人（如负责人主部门挂在别处）。
                </div>
              )}
              {members?.loading && <div style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>加载中…</div>}
              {members && !members.loading && members.items.length === 0 && (
                <div style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>无直属成员（人员都在下级部门）</div>
              )}
              {members && !members.loading && members.items.map((m) => (
                <div key={m.open_id} style={S.memberRow}
                  title={m.email || m.name}
                  onClick={() => nav(`/assets?owner_id=${encodeURIComponent(m.open_id)}&owner_name=${encodeURIComponent(m.name)}`)}>
                  <span style={S.memberAvatar}>{(m.name || '·').slice(0, 1)}</span>
                  <span style={{ fontSize: 13, color: 'var(--text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{m.name || m.open_id}</span>
                  <Icon name="external" size={12} style={{ color: 'var(--text-tertiary)', marginLeft: 'auto' }} />
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      <div style={S.footnote}>
        真实组织架构 · 共 {stats?.departments ?? '—'} 个部门 / {stats?.members ?? '—'} 人 · 更新于 {fmtTime(data?.last_refreshed ?? null)}
      </div>
    </div>
  );
};

const S: Record<string, React.CSSProperties> = {
  topbar: { position: 'absolute', top: 14, left: 16, right: 16, display: 'flex', alignItems: 'center', gap: 8, pointerEvents: 'none' },
  searchWrap: { display: 'flex', alignItems: 'center', gap: 6, pointerEvents: 'auto', background: 'var(--surface-elevated)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '6px 10px', boxShadow: 'var(--shadow-sm)' },
  searchInput: { border: 'none', outline: 'none', background: 'transparent', fontSize: 13, width: 130, fontFamily: 'inherit', color: 'var(--text-primary)' },
  legend: { position: 'absolute', left: 16, bottom: 44, width: 190, maxHeight: '52%', overflowY: 'auto', background: 'var(--surface-elevated)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-xl)', padding: '12px 12px 10px', boxShadow: 'var(--shadow-md)' },
  legendRow: { display: 'flex', alignItems: 'center', gap: 8, padding: '4px 6px', borderRadius: 6, cursor: 'pointer' },
  toggleRow: { display: 'flex', alignItems: 'center', gap: 7, fontSize: 12.5, color: 'var(--text-secondary)', padding: '3px 6px', cursor: 'pointer' },
  detail: { position: 'absolute', top: 70, right: 16, width: 272, maxHeight: 'calc(100% - 130px)', overflowY: 'auto', background: 'var(--surface-elevated)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-2xl)', padding: 'var(--space-5)', boxShadow: 'var(--shadow-lg)' },
  kv: { display: 'flex', gap: 10, padding: '5px 0', alignItems: 'baseline' },
  k: { fontSize: 11, fontFamily: 'var(--font-mono)', textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--text-tertiary)', width: 46, flexShrink: 0 },
  memberRow: { display: 'flex', alignItems: 'center', gap: 8, padding: '5px 6px', borderRadius: 6, cursor: 'pointer' },
  changes: { position: 'absolute', right: 16, bottom: 44, width: 234, maxHeight: '46%', overflowY: 'auto', background: 'var(--surface-elevated)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-xl)', padding: '12px 12px 10px', boxShadow: 'var(--shadow-md)', pointerEvents: 'auto' },
  changeRow: { display: 'flex', alignItems: 'center', gap: 8, padding: '5px 6px', borderRadius: 6, cursor: 'pointer' },
  tag: { fontSize: 9.5, fontWeight: 700, color: 'var(--text-tertiary)', background: 'var(--surface-subtle)', borderRadius: 4, padding: '0 4px', marginLeft: 5, verticalAlign: 'middle' },
  memberAvatar: { width: 22, height: 22, borderRadius: '50%', background: 'var(--surface-subtle)', color: 'var(--text-secondary)', fontSize: 11, fontWeight: 600, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 },
  footnote: { position: 'absolute', bottom: 12, left: 16, fontSize: 11, color: 'var(--text-tertiary)', pointerEvents: 'none', maxWidth: '60%' },
};

export default OrgGraphPage;
