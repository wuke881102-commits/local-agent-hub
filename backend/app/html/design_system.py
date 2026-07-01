"""内置「Lumen-light」设计系统规范 —— 供 html-page「自由版式」模式喂给模型。

为什么是 Python 常量而不是 .md 资源文件：PyInstaller 打包时 .py 一定随包，
免去额外把数据文件塞进 spec datas 的麻烦；内容也不大。
套模板模式不用它（走 templates/*.html）；仅自由版式（AI 直出整页 HTML）引用。
"""
from __future__ import annotations

# 完整可直接粘进 <style> 的设计令牌。给模型现成的 :root，既保证配色/圆角/阴影忠实，
# 又省下它自己推导 token 的篇幅，把输出预算留给内容与版式。
LUMEN_LIGHT_ROOT_CSS = """:root{
  /* 品牌绿 10 级 */
  --brand-50:#E3F5EA;--brand-100:#C0E6D0;--brand-200:#90D4A8;--brand-300:#60C080;--brand-400:#30AA60;
  --brand-500:#00AA4F;--brand-600:#008E43;--brand-700:#006845;--brand-800:#005A3C;--brand-900:#004A30;
  /* 辅助色 */
  --accent-lime:#82BC00;--accent-emerald:#00D47B;--accent-forest:#228B22;--accent-mint:#38A169;
  /* 语义色 + 浅底 */
  --success:#10B050;--success-bg:rgba(16,176,80,.12);
  --warning:#F0A800;--warning-bg:rgba(240,168,0,.12);
  --error:#C83A3A;--error-bg:rgba(200,58,58,.12);
  --info:#0095D4;--info-bg:rgba(42,157,143,.12);
  /* 表面 / 边框 / 文字 */
  --surface-page:#F9FAFC;--surface-elevated:#FFFFFF;--surface-container:#FFFFFF;
  --surface-subtle:#F0F1F3;--surface-hover:#EDEEF0;--surface-active:#E5E7EB;
  --border-default:#DDE3EA;--border-subtle:#E8ECF0;--border-strong:#C5CCD3;--border-focus:#00AA4F;
  --text-primary:#42464A;--text-secondary:#555B61;--text-tertiary:#737A82;
  --text-disabled:#A6ADB5;--text-placeholder:#9CA3AF;--text-inverse:#FFFFFF;--text-brand:#00AA4F;
  /* 图表 10 色 */
  --chart-1:#00AA4F;--chart-2:#00D47B;--chart-3:#82BC00;--chart-4:#F0A800;--chart-5:#2A9D8F;
  --chart-6:#9070F0;--chart-7:#30C0C0;--chart-8:#C83A3A;--chart-9:#70D080;--chart-10:#F0F0A0;
  /* 圆角 / 间距(4px 基) / 阴影 / 字体 */
  --radius-xs:2px;--radius-sm:4px;--radius-md:6px;--radius-lg:8px;--radius-xl:12px;--radius-2xl:16px;--radius-full:9999px;
  --space-1:4px;--space-2:8px;--space-3:12px;--space-4:16px;--space-5:20px;--space-6:24px;
  --space-8:32px;--space-10:40px;--space-12:48px;--space-16:64px;--space-20:80px;--space-24:96px;
  --shadow-sm:0 1px 3px rgba(0,0,0,.08),0 0 0 1px rgba(0,0,0,.03);
  --shadow-md:0 4px 12px rgba(0,0,0,.1),0 0 0 1px rgba(0,0,0,.04);
  --shadow-lg:0 8px 24px rgba(0,0,0,.12),0 0 0 1px rgba(0,0,0,.05);
  --font-sans:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;
  --font-mono:'SF Mono','Menlo','Monaco','Consolas','JetBrains Mono',monospace;
}"""

# 组件速查（精炼版，给模型“怎么搭”的依据；完整规范见仓库 Lumen-light.md）。
LUMEN_LIGHT_COMPONENTS = """### 设计原则
- 扁平化、绿色品牌(#00AA4F)、有机圆角、所有交互元素带 hover/focus 微动效(150–300ms，translateY(-1~2px)+阴影加深)。
- 颜色/圆角/间距/阴影一律用上面 :root 的 var()，不要硬编码新色值。
- 正文最大宽度 1200px，水平内边距 32px，页面底色 var(--surface-page)。

### 关键组件配方
- 头部 Hero：background:linear-gradient(135deg,var(--brand-500),var(--brand-600),var(--brand-700))；白字；可加半透明圆形装饰；标题用 44–60px 粗体。
- 卡片 Card：白底，border-radius:var(--radius-2xl)，padding:var(--space-6)，box-shadow:var(--shadow-sm)，hover 抬升。
- 指标卡 Metric：大号数字(var(--brand-600)) + 小标签；常用 2–4 列网格。
- 徽章 Badge：小号、mono、大写、圆角 md；语义色用对应 --success/-bg 等；状态/等级首选。
- 提示 Alert：flex + 图标 + 标题 + 描述，左边框或浅底用语义色，border-radius:var(--radius-lg)。
- 表格 Table：外层圆角 xl；表头 var(--surface-subtle)+mono+大写+字距；行 hover 高亮；**原文有并列/对照/参数数据时优先用表格**。
- 标签页 Tabs / 仪表盘 Dashboard / 错误页：见需要再用。
- 分区：每个主题一个 section，给小标题(带品牌色竖条或图标)，再选最贴合的组件承载，避免通篇纯段落。"""


def lumen_light_spec() -> str:
    """拼出喂给模型的完整设计系统文本。"""
    return (
        "<<LUMEN-LIGHT 设计系统 · 开始>>\n"
        "【设计令牌（请原样放进 <style> 的 :root）】\n"
        f"{LUMEN_LIGHT_ROOT_CSS}\n\n"
        f"{LUMEN_LIGHT_COMPONENTS}\n"
        "<<LUMEN-LIGHT 设计系统 · 结束>>"
    )
