/**
 * 「任务场景」卡片点进去落到哪 —— 全站唯一一份。
 *
 * 之前工作台（Dashboard）和任务场景页（ScenesPage）各存了一份同样的映射，
 * 加「AIHot 内容和模型」时只改了后者，于是工作台上点那张卡片会 fallback 到
 * /scenes（看起来像"点了没反应，只是跳到场景列表"）。合成一份，杜绝再漂移。
 *
 * 新增场景时：在 backend/app/routes/scenes.py 加条目，然后在这里加一行落点。
 */
export const SCENE_TARGETS: Record<string, string> = {
  'content':       '/task/html-page',
  'knowledge-gov': '/task/document-map',      // 知识库治理：先建图，再切到治理
  'meeting':       '/task/meeting-minutes',   // 会议沉淀：妙记 / 会议记录整理
  'table':         '/task/base-analysis',     // 表格分析：多维表格分析
  'pdf':           '/task/pdf-recognition',   // PDF 识别：云盘 PDF AI 识别
  'dispatch':      '/task/collab-dispatch',   // 协作分发：群消息 + 任务草稿
  'auto-extract':  '/autoextract',            // 自动化提炼：按 Enter 留痕 + 定时提炼
  'aihot':         '/aihot?tab=board',        // AIHot：打开即看的数据页，默认模型榜
};

// 这里**没有** 'outlook' 和 'memo'，不是漏了：它们不在任务场景列表里（理由见
// backend/app/routes/scenes.py 末尾的注释），入口只在左侧导航。哪天把它们加回
// 场景卡，记得连这里的落点一起加。

/** 场景 id → 导航。没登记的场景退回场景列表页（而不是白屏）。 */
export function sceneTarget(sceneId: string, nav: (p: string) => void): void {
  nav(SCENE_TARGETS[sceneId] || '/scenes');
}
