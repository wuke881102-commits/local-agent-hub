/**
 * 个人提炼 —— 原「个人摘记」+「自动化提炼」合成一页。
 *
 * ## 为什么合
 *
 * 两个都是**常驻视图**：打开即在、自己按频率跑、没有「运行一次出一份产物」的概念。
 * 而且它们本来就是一条链的两端 —— 自动化提炼把工作留痕熬成 digest，个人摘记再把
 * digest（连同邮件、语音）汇总发到你飞书。分成两个入口时，你得先想清楚「我要看的
 * 是产出还是投递」才知道点哪个，这个问题本身就不该由用户回答。
 *
 * 顺序是**摘记在上、提炼在下**：上面是「发出去的东西」，下面是「发出去的东西是哪来的」。
 *
 * ## 两半仍然各自独立
 *
 * 合并只动了外壳。两个 section 各有自己的 start/stop、自己的后端状态、自己的轮询，
 * 谁停了另一个照跑。这一点必须保持：把两个开关连成一个「总开关」会让「只想要留痕、
 * 不想往飞书发」这种很常见的用法没法表达。
 *
 * ## 段落标题降了一级
 *
 * 「个人摘记」「自动化提炼」原本各是页面 h2，合并后让位给页面标题「个人提炼」，
 * 降成 h3。除此之外两个 section 的内容一个字没改。
 */
import React from 'react';
import MemoSection from '../components/distill/MemoSection';
import AutoExtractSection from '../components/distill/AutoExtractSection';

const PersonalDistillPage: React.FC = () => (
  <div style={{ padding: 'var(--space-8)', maxWidth: 1080, margin: '0 auto' }}>
    <div style={{ marginBottom: 'var(--space-6)' }}>
      <h2 style={{ margin: 0, fontSize: 20, fontWeight: 600 }}>个人提炼</h2>
      <div className="eyebrow" style={{ marginTop: 4 }}>
        上半页把各处进展按频率汇总、发到你自己的飞书；下半页把工作留痕截图按频率提炼成记录 ·
        两半各自开关，互不影响
      </div>
    </div>

    <MemoSection />

    <div style={{
      height: 1, background: 'var(--border-subtle)',
      margin: 'var(--space-8) 0',
    }} />

    <AutoExtractSection />
  </div>
);

export default PersonalDistillPage;
