"""Generate icon.ico (multi-size) and icon.png (64) from scratch.

Brand: Lumen-light green #00AA4F, white "F" letterform.
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

BRAND = (0, 170, 79, 255)
OUT_DIR = Path(__file__).parent


def _draw(size: int) -> Image.Image:
    """Rounded-square icon with the letter 'F' centered."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    radius = max(6, size // 6)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=BRAND)

    font = None
    try:
        font_size = int(size * 0.7)
        for fname in ("arialbd.ttf", "arial.ttf", "segoeuib.ttf", "segoeui.ttf"):
            try:
                font = ImageFont.truetype(fname, font_size)
                break
            except OSError:
                continue
    except OSError:
        font = None
    if font is None:
        font = ImageFont.load_default()

    text = "F"
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (size - tw) / 2 - bbox[0]
    y = (size - th) / 2 - bbox[1] - size * 0.05
    d.text((x, y), text, fill=(255, 255, 255, 255), font=font)
    return img


def main() -> None:
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [_draw(s) for s in sizes]
    # 64x64 PNG for tray
    images[4].save(OUT_DIR / "icon.png", format="PNG")
    # Multi-size ICO for Windows app icon
    images[0].save(
        OUT_DIR / "icon.ico",
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=images[1:],
    )
    print(f"Wrote {OUT_DIR / 'icon.png'} and {OUT_DIR / 'icon.ico'}")


if __name__ == "__main__":
    main()
