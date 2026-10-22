"""一次性调研：样本图片里有多少张是规则网格结构。

不是产品代码，是实施计划 Task 0 的证据来源 —— 几何等分切块赌的是「展板基本是
规则网格」，先把这个赌注量化，再决定要不要给非网格的图片加一条路。

用法：
    backend\\.venv\\Scripts\\python.exe tools/survey_slides.py <图片目录>
"""
import asyncio
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.llm.client import get_llm  # noqa: E402

PROMPT = """这是一张会议展板/幻灯片的照片。只回答结构，不要转写文字。输出 JSON：
{"is_slide": true/false,
 "grid": true/false,
 "bands": [{"role": "flow|columns", "n_columns": 4}],
 "why": "一句话说明为什么 grid 是 true 或 false"}

grid=true 表示：内容能用「横向带 × 纵向栏」的网格描述完。
grid=false 表示：自由摆放、放射状、不规则拼贴等，网格描述不下来。
只输出 JSON。"""


async def main(folder: str) -> int:
    llm = get_llm()
    files = sorted(Path(folder).glob("*.jpg"))
    files = [f for f in files if not f.name.startswith(("s_", "m_", "crop_"))]
    grid = non_grid = not_slide = bad = 0
    for f in files:
        url = "data:image/jpeg;base64," + base64.b64encode(f.read_bytes()).decode()
        raw = (await llm.vision_describe(url, PROMPT, max_tokens=400) or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            bad += 1
            print(f"{f.name:12} JSON 解析失败：{raw[:80]}")
            continue
        bands = d.get("bands") or []
        shape = " + ".join(f"{b.get('role')}x{b.get('n_columns')}" for b in bands)
        if not d.get("is_slide"):
            not_slide += 1
        elif d.get("grid"):
            grid += 1
        else:
            non_grid += 1
        print(f"{f.name:12} slide={d.get('is_slide')!s:5} grid={d.get('grid')!s:5} "
              f"{shape:22} {str(d.get('why', ''))[:42]}")
    print(f"\n合计 {len(files)} 张：网格 {grid} · 非网格 {non_grid} · "
          f"非幻灯片 {not_slide} · 解析失败 {bad}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1])))
