// Inline-SVG 图标集 — 一套与原型 icons.jsx 对齐的常用 24×24 stroke 图标。
import React from 'react';

type IconProps = { name: string; size?: number; style?: React.CSSProperties; className?: string };

const PATHS: Record<string, React.ReactNode> = {
  logo: <path d="M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z" fill="currentColor" />,
  home: <path d="M3 12L12 4l9 8M5 10v10h14V10" stroke="currentColor" strokeWidth="1.6" fill="none" strokeLinecap="round" strokeLinejoin="round" />,
  sparkle: <path d="M12 3l1.6 4.8L18 9l-4.4 1.6L12 15l-1.6-4.4L6 9l4.4-1.2zM18 14l.8 2.2 2.2.8-2.2.8L18 20l-.8-2.2L15 17l2.2-.8z" stroke="currentColor" strokeWidth="1.4" fill="none" />,
  list: <path d="M4 6h16M4 12h16M4 18h16" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />,
  sidebar: <><rect x="3" y="4" width="18" height="16" rx="2" stroke="currentColor" strokeWidth="1.6" fill="none" /><path d="M9 4v16" stroke="currentColor" strokeWidth="1.6" /></>,
  folder: <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" stroke="currentColor" strokeWidth="1.6" fill="none" />,
  desktop: <><rect x="3" y="4" width="18" height="13" rx="2" stroke="currentColor" strokeWidth="1.6" fill="none" /><path d="M9 21h6M12 17v4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /></>,
  cloud: <path d="M7 18a4 4 0 0 1-.5-7.97A5 5 0 0 1 16 9.5a3.5 3.5 0 0 1 1 6.86" stroke="currentColor" strokeWidth="1.6" fill="none" strokeLinecap="round" strokeLinejoin="round" />,
  camera: <><path d="M4 8a2 2 0 0 1 2-2h1.5l1-1.5h7L17.5 6H19a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" stroke="currentColor" strokeWidth="1.6" fill="none" strokeLinejoin="round" /><circle cx="12" cy="12.5" r="3.2" stroke="currentColor" strokeWidth="1.6" fill="none" /></>,
  gear: <><circle cx="12" cy="12" r="3" stroke="currentColor" strokeWidth="1.6" fill="none" /><path d="M19.4 13a7 7 0 0 0 .1-1 7 7 0 0 0-.1-1l2-1.6-2-3.4-2.4.9a7 7 0 0 0-1.7-1L14.8 3h-3.6l-.5 2.5a7 7 0 0 0-1.7 1l-2.4-.9-2 3.4 2 1.6a7 7 0 0 0 0 2l-2 1.6 2 3.4 2.4-.9a7 7 0 0 0 1.7 1l.5 2.5h3.6l.5-2.5a7 7 0 0 0 1.7-1l2.4.9 2-3.4z" stroke="currentColor" strokeWidth="1.4" fill="none" /></>,
  search: <><circle cx="11" cy="11" r="7" stroke="currentColor" strokeWidth="1.6" fill="none" /><path d="M20 20l-3.5-3.5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /></>,
  bell: <path d="M6 16V11a6 6 0 1 1 12 0v5l1.6 2H4.4zM10 20a2 2 0 0 0 4 0" stroke="currentColor" strokeWidth="1.6" fill="none" strokeLinejoin="round" />,
  'chevron-right': <path d="M9 6l6 6-6 6" stroke="currentColor" strokeWidth="1.8" fill="none" strokeLinecap="round" strokeLinejoin="round" />,
  page: <path d="M6 3h9l5 5v13a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1zM14 3v5h5" stroke="currentColor" strokeWidth="1.6" fill="none" strokeLinejoin="round" />,
  shield: <path d="M12 3l8 3v6c0 5-3.5 8.5-8 9-4.5-.5-8-4-8-9V6z" stroke="currentColor" strokeWidth="1.6" fill="none" strokeLinejoin="round" />,
  mic: <><rect x="9" y="3" width="6" height="11" rx="3" stroke="currentColor" strokeWidth="1.6" fill="none" /><path d="M5 11a7 7 0 0 0 14 0M12 18v3" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /></>,
  table: <><rect x="3" y="4" width="18" height="16" rx="2" stroke="currentColor" strokeWidth="1.6" fill="none" /><path d="M3 9h18M9 4v16" stroke="currentColor" strokeWidth="1.4" /></>,
  send: <path d="M4 20l16-8L4 4l3 8z" stroke="currentColor" strokeWidth="1.6" fill="none" strokeLinejoin="round" />,
  scan: <><path d="M4 8V6a2 2 0 0 1 2-2h2M16 4h2a2 2 0 0 1 2 2v2M20 16v2a2 2 0 0 1-2 2h-2M8 20H6a2 2 0 0 1-2-2v-2" stroke="currentColor" strokeWidth="1.6" fill="none" strokeLinecap="round" /><path d="M4 12h16" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /></>,
  map: <path d="M3 6l6-2 6 2 6-2v14l-6 2-6-2-6 2zM9 4v16M15 6v16" stroke="currentColor" strokeWidth="1.4" fill="none" strokeLinejoin="round" />,
  funnel: <path d="M4 5h16l-6 7.5V19l-4 2v-8.5z" stroke="currentColor" strokeWidth="1.6" fill="none" strokeLinejoin="round" />,
  spark: <path d="M12 2v8M12 14v8M2 12h8M14 12h8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />,
  refresh: <path d="M3 12a9 9 0 0 1 15.5-6.4L21 8M21 4v4h-4M21 12a9 9 0 0 1-15.5 6.4L3 16M3 20v-4h4" stroke="currentColor" strokeWidth="1.6" fill="none" strokeLinecap="round" strokeLinejoin="round" />,
  check: <path d="M5 12l5 5L20 7" stroke="currentColor" strokeWidth="1.8" fill="none" strokeLinecap="round" strokeLinejoin="round" />,
  x: <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />,
  trash: <path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13M10 11v6M14 11v6" stroke="currentColor" strokeWidth="1.6" fill="none" strokeLinecap="round" strokeLinejoin="round" />,
  external: <path d="M14 4h6v6M20 4l-9 9M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5" stroke="currentColor" strokeWidth="1.6" fill="none" strokeLinecap="round" strokeLinejoin="round" />,
  warning: <><path d="M12 4l10 17H2z" stroke="currentColor" strokeWidth="1.6" fill="none" strokeLinejoin="round" /><path d="M12 10v5M12 18v.5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /></>,
  calendar: <><rect x="3" y="5" width="18" height="16" rx="2" stroke="currentColor" strokeWidth="1.6" fill="none" /><path d="M3 9h18M8 3v4M16 3v4M7 13h3M7 17h3" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /></>,
  graph: <><path d="M7 7.5l5 2M13.6 9l3-2.2M11.5 11.6l-2.7 5.2M14 11.4l3.2 4.6" stroke="currentColor" strokeWidth="1.4" /><circle cx="6" cy="6" r="2.3" stroke="currentColor" strokeWidth="1.6" fill="none" /><circle cx="18" cy="6" r="2.1" stroke="currentColor" strokeWidth="1.6" fill="none" /><circle cx="12.5" cy="10.5" r="2" stroke="currentColor" strokeWidth="1.6" fill="none" /><circle cx="8" cy="19" r="2.1" stroke="currentColor" strokeWidth="1.6" fill="none" /><circle cx="18" cy="18" r="2.3" stroke="currentColor" strokeWidth="1.6" fill="none" /></>,
};

export const Icon: React.FC<IconProps> = ({ name, size = 16, style, className }) => {
  const inner = PATHS[name] || <circle cx="12" cy="12" r="4" stroke="currentColor" strokeWidth="1.6" fill="none" />;
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" style={style} className={className} aria-hidden>
      {inner}
    </svg>
  );
};

export default Icon;
