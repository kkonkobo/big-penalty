"""Draw packaging/icon.png (1024 px) from the design of assets/icon.svg. Run on macOS (uses the system font).

uv run --group package python packaging/make_icon.py
PyInstaller converts the PNG to .icns / .ico at build time (needs Pillow, in the `package` group).
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SIZE = 1024
MARGIN = 100  # macOS icon grid: the rounded square sits inside a transparent margin
OUT = Path(__file__).with_name("icon.png")

img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
box = (MARGIN, MARGIN, SIZE - MARGIN, SIZE - MARGIN)
side = SIZE - 2 * MARGIN
d.rounded_rectangle(box, radius=round(side * 14 / 64), fill="#ea580c")
font = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", round(side * 26 / 64))
font.set_variation_by_axes([100, 28, 400, 600])  # width, optical size, grade, weight 600 as in the SVG
d.text((SIZE / 2, SIZE / 2), "BP", font=font, fill="#ffffff", anchor="mm")
img.save(OUT)
print(OUT)
