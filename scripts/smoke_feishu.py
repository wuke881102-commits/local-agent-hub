"""发版前的飞书集成冒烟测试 —— 真打 lark-cli，不打模型。

## 为什么有这个东西

飞书的所有功能都踩在 lark-cli 上，而它是个**动作很快的外部依赖**：
1.0.43 删掉 ``docs +export-markdown``、1.0.49 删掉 docs v1 API、某版把错误信封从
``type`` 拆成 ``type``+``subtype``、1.0.89 给 ``docs +media-download`` 加了一道
和下载无关的 scope 预检。每一次的表象都不是「CLI 变了」，而是

    「未能从该文件抽取到任何正文」 / 「不支持的会议来源类型」 / 「LarkCLIError」

—— 全在怪用户的文档。抽正文那条路因此从 2026-06 一路坏到 8.0 没人发现：
功能没人用，用的人看到的又是一句听起来很合理的假话。

这个脚本就是那道本该存在的闸：**真的跑一遍**，任何一环拿不到东西就红。
在 2026-08-24 那次把捆绑 CLI 从 ~1.0.44 跳到 1.0.89 时，它会当场拦下来。

## 跑法

    backend\\.venv\\Scripts\\python.exe scripts\\smoke_feishu.py
    backend\\.venv\\Scripts\\python.exe scripts\\smoke_feishu.py --doc <token>

不传 ``--doc`` 就从本地索引里挑一篇最近更新的 docx。
需要一份有效的飞书授权；没有授权时会明说，并以 exit 2 退出（区别于「真的坏了」）。

**故意不接进 build_installer.cmd**：那会让打包依赖构建机上有飞书授权。
发版前手动跑一次就行。

退出码：0 全绿 / 1 有检查失败 / 2 前置条件不满足（没授权、没 CLI、索引里没文档）。
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.feishu.cli import LarkCLI, LarkCLIError, explain_error  # noqa: E402
from app.services import doc_images, index_service  # noqa: E402

PASS, FAIL, SKIP = "✓", "✗", "·"
_results: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    _results.append((ok, label, detail))
    print(f"  {PASS if ok else FAIL} {label}" + (f"   {detail}" if detail else ""))
    return ok


class Bail(SystemExit):
    """前置条件不满足 —— 不是产品坏了，别和真失败混在一起。"""

    def __init__(self, msg: str):
        print(f"\n  前置条件不满足：{msg}")
        super().__init__(2)


async def pick_doc(explicit: str | None) -> tuple[str, str]:
    """选一篇用来测的文档。

    故意**不把 token 写死在源码里**：文档会被删、会改权限，而且那是公司内部资料，
    不该固化进仓库。默认从本地索引里挑最近更新的一篇 docx。
    """
    if explicit:
        return explicit, "(命令行指定)"
    rows = await index_service.list_assets(asset_type="docx", limit=20)
    for r in rows:
        if r.get("asset_id"):
            return r["asset_id"], r.get("title") or "(无标题)"
    raise Bail("本地索引里没有 docx 资产，先在「飞书文档」页跑一次索引，或用 --doc 指定一篇")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", help="用哪篇文档测；缺省从本地索引里挑一篇 docx")
    args = ap.parse_args()

    lark = LarkCLI()
    print(f"\nlark-cli: {lark.bin}")

    # ── 0. 前置：CLI 在不在、授权好不好 ──────────────────────────────
    if not await lark.ping():
        raise Bail("lark-cli 不可用（ping 失败）")
    print(f"版本: {lark.version}")
    st = await lark.auth_status()
    if not (st.get("user_usable") or st.get("authenticated")):
        raise Bail(f"飞书用户授权不可用（user_status={st.get('user_status')}），先在工作台完成授权")

    token, title = await pick_doc(args.doc)
    print(f"测试文档: {title}   {token}\n")

    # ── 1. 抽正文 ────────────────────────────────────────────────────
    print("[1/5] 抽取文档正文")
    try:
        md = await lark.docs_export_markdown(token)
    except LarkCLIError as e:
        check(False, "docs_export_markdown", explain_error(e)[:160])
        md = ""
    if md:
        lines = md.split("\n")
        heads = sum(1 for x in lines if x.startswith("#"))
        pipes = sum(1 for x in lines if x.startswith("|"))
        check(len(md) > 200, "正文非空", f"{len(md)} 字")
        # 形状检查：拿到 DocxXML 而不是 Markdown 时长度照样很大，只看非空会漏掉。
        check(heads > 0, "是 Markdown（有标题行）", f"{heads} 行")
        check("<grid" not in md and "<figure" not in md,
              "没有 DocxXML 残留", "grid/figure 均未出现")
        check(pipes > 0 or "|" not in md, "表格是管道表", f"{pipes} 行")

    # ── 2. 元数据 ────────────────────────────────────────────────────
    print("[2/5] 读文档元数据")
    try:
        meta = await lark.docs_get(token)
        check(bool(meta.get("doc_token")), "docs_get 返回 doc_token", str(meta.get("doc_token"))[:12] + "…")
    except LarkCLIError as e:
        check(False, "docs_get", explain_error(e)[:160])

    # ── 3. 图片标记认不认得出 ────────────────────────────────────────
    print("[3/5] 识别内嵌图片标记")
    toks = [doc_images._img_token(m) for m in doc_images.IMG_TAG_RE.finditer(md or "")]
    toks = [t for t in toks if t]
    raw_imgs = (md or "").count("![") + len(re.findall(r"<img\b|<image\b", md or ""))
    if raw_imgs == 0:
        print(f"  {SKIP} 这篇文档没有图片，跳过图片相关检查")
    else:
        # 只数「有几处图片写法」对不上「认出几个 token」——8.1 就是在这里漏了
        # 56 张里的 13 张（im-markdown 混用 URL 形和裸 token 形），而日志上毫无异样。
        check(len(toks) > 0, "认出图片 token", f"{len(toks)}/{raw_imgs} 处")
        check(len(toks) >= raw_imgs * 0.9, "绝大多数图片标记都认得出",
              f"认出 {len(toks)}，正文里共 {raw_imgs} 处")

        # ── 4. 真下一张图 ───────────────────────────────────────────
        print("[4/5] 真的下载一张图片")
        tmp = ROOT / "backend" / "data" / "_smoke"
        try:
            info = await lark.docs_download_media(toks[0], tmp, "smoke0")
            size = Path(info["path"]).stat().st_size
            check(size > 0, "media-download 拿到文件", f"{size} 字节 {info.get('content_type')}")
        except Exception as e:  # noqa: BLE001
            check(False, "media-download", explain_error(e)[:160])
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    # ── 5. 报错还翻不翻得动 ──────────────────────────────────────────
    # CLI 换过错误信封的形状（type → type+subtype），换完之后 explain_error 的所有
    # 分支都落空、一路吐英文原文，而**功能本身是好的**，所以任何功能测试都发现不了。
    print("[5/5] 错误信息还能翻成中文")
    try:
        await lark.run("docs", "+no-such-subcommand-smoke", "--format", "json", timeout=20)
        check(False, "构造的错误应当失败", "它居然成功了")
    except LarkCLIError as e:
        msg = explain_error(e)
        check(msg and str(e) not in msg, "explain_error 给出了比退出码更多的信息", msg[:80])
        check(any("一" <= c <= "鿿" for c in msg) or "unknown subcommand" in msg,
              "报错能被识别（中文提示或原文透出）")

    upd = await lark.update_status()
    if upd:
        n = upd.get("behind")
        print(f"\nlark-cli 版本：{upd['current']} → 最新 {upd['latest']}"
              + (f"（落后 {n} 个）" if n else ""))
        if n and n >= 10:
            print("  ! 落后较多。一次性跨过去会同时吞下多处破坏性改动，升级后把这个脚本再跑一遍。")

    bad = [r for r in _results if not r[0]]
    print(f"\n{len(_results) - len(bad)}/{len(_results)} 通过")
    for _, label, detail in bad:
        print(f"  {FAIL} {label}   {detail}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
