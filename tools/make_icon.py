"""アプリのアイコン（icon.png 1024px）を作る。PyInstaller が .ico / .icns に変換する"""
from PIL import Image, ImageDraw, ImageFont
import sys

S = 1024
im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(im)
# 角丸の背景（紺→青のグラデ）
grad = Image.new("RGBA", (S, S))
gd = ImageDraw.Draw(grad)
for y in range(S):
    k = y / S
    gd.line([(0, y), (S, y)], fill=(int(20 + 30 * k), int(40 + 70 * k), int(90 + 120 * k), 255))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle((60, 60, S - 60, S - 60), radius=200, fill=255)
im.paste(grad, (0, 0), mask)
# 「CT」の文字（縁取り付き）
font = None
for name in ("Arial Bold.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
             "C:/Windows/Fonts/arialbd.ttf"):
    try:
        font = ImageFont.truetype(name, 470)
        break
    except OSError:
        pass
font = font or ImageFont.load_default()
d = ImageDraw.Draw(im)
box = d.textbbox((0, 0), "CT", font=font, stroke_width=24)
x = (S - (box[2] - box[0])) / 2 - box[0]
y = (S - (box[3] - box[1])) / 2 - box[1] - 20
d.text((x, y), "CT", font=font, fill=(255, 210, 60, 255), stroke_width=24, stroke_fill=(20, 20, 40, 255))
# 下のライン（テロップの帯）
d.rounded_rectangle((200, 790, S - 200, 840), radius=25, fill=(255, 255, 255, 230))
im.save(sys.argv[1] if len(sys.argv) > 1 else "icon.png")
