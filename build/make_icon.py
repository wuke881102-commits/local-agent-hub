"""Generate icon.ico (multi-size) and icon.png (64) from scratch.

图标 = 侧栏那个品牌方块：圆角绿底 + 四个白方块（2×2）。
和 frontend/public/favicon.svg 是同一套几何和同一组颜色 —— 任务栏、开始菜单、
托盘、浏览器标签页必须是同一个标识，不然同一个应用在四个地方长四个样。

几何（以 64 为基准，等比缩放到各尺寸）：
  · 圆角矩形 radius 18/64 ≈ 29%（= 前端 brandMark 的 11/38）
  · 四个方块 13×13、间距 6，整体居中（16..29 / 35..48）
  · 渐变 135°：brand-500 #00AA4F → brand-700 #006845

方块比前端侧栏那版略大、缝略宽是**刻意的**：任务栏和标签页只有 16px，
按侧栏 38px 的比例缩下去缝不到 1px，四块会糊成一坨。这套比例在 16px 下
中缝仍有整像素的绿色。

原来这里画的是白色字母「F」。换掉是因为它和界面里的四方块对不上号。
"""
from pathlib import Path

from PIL import Image, ImageDraw

BRAND_A = (0, 170, 79)      # --brand-500
BRAND_B = (0, 104, 69)      # --brand-700
OUT_DIR = Path(__file__).parent

# 64 基准下的四个方块（x, y），边长 13、圆角 2
SQUARES = ((16, 16), (35, 16), (16, 35), (35, 35))
SQ, SQ_R, TILE_R = 13, 2, 18


def _draw(size: int) -> Image.Image:
    """一张图标。内部 4 倍超采样再缩小 —— 16px 直接画圆角和小方块全是锯齿。"""
    ss = size * 4
    img = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))

    # 135° 线性渐变：沿 x+y 方向插值，逐条对角线画
    grad = Image.new("RGB", (ss, ss))
    gd = ImageDraw.Draw(grad)
    for i in range(2 * ss):
        t = i / (2 * ss - 1)
        gd.line([(i, 0), (0, i)],
                fill=tuple(int(a + (b - a) * t) for a, b in zip(BRAND_A, BRAND_B)))

    k = ss / 64.0
    mask = Image.new("L", (ss, ss), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, ss - 1, ss - 1),
                                           radius=int(TILE_R * k), fill=255)
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img)
    for x, y in SQUARES:
        d.rounded_rectangle((x * k, y * k, (x + SQ) * k - 1, (y + SQ) * k - 1),
                            radius=max(1, int(SQ_R * k)), fill=(255, 255, 255, 255))
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [_draw(s) for s in sizes]
    # 64x64 PNG for tray
    images[sizes.index(64)].save(OUT_DIR / "icon.png", format="PNG")

    # 多尺寸 ICO。**必须从最大那张存**：Pillow 的 ICO 只会把源图往下缩，不会放大，
    # 所以从 16×16 那张存的话，sizes 里比它大的全被丢掉 —— 结果是一个只有 16×16
    # 的 .ico，Windows 在任务栏/桌面/Alt-Tab 上再放大，糊。
    # （原来这里就是从 images[0] 存的，老 icon.ico 只有 222 字节、一个 16×16。）
    # append_images 对 ICO 无效，sizes 才是那个起作用的参数。
    images[-1].save(OUT_DIR / "icon.ico", format="ICO", sizes=[(s, s) for s in sizes])

    from PIL import Image as _I
    got = sorted(_I.open(OUT_DIR / "icon.ico").ico.sizes())
    assert got == sorted((s, s) for s in sizes), "ICO 尺寸不全：%s" % (got,)
    print(f"Wrote {OUT_DIR / 'icon.png'} and {OUT_DIR / 'icon.ico'}  sizes={got}")


if __name__ == "__main__":
    main()
