"""Link-preview image (1200x630) showing the top three in each division."""
import os

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = os.path.join(HERE, "fonts")


def font(name, size):
    return ImageFont.truetype(os.path.join(FONTS, name), size)


def render(data, path):
    W, H = 1200, 630
    bg, fg, muted, accent, line = (14, 18, 24), (232, 236, 242), (152, 162, 179), (255, 77, 99), (42, 50, 64)
    im = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 14, H], fill=accent)
    d.text((60, 44), "HSL MAJOR SCORING RACE", font=font("Oswald-SemiBold.ttf", 64), fill=fg)
    d.text((62, 122), "Points leaders · 2017 Major and 2018 Major · updates itself after every game",
           font=font("Barlow-Medium.ttf", 26), fill=muted)
    d.line([60, 172, W - 60, 172], fill=line, width=2)

    cols = [(60, "2018 Major"), (630, "2017 Major")]
    for x, name in cols:
        div = data.get(name) or {}
        d.text((x, 196), name.upper(), font=font("Oswald-SemiBold.ttf", 34), fill=accent)
        d.text((x + 230, 206), f"{div.get('games', 0)} games through {div.get('thru', '-')}",
               font=font("Barlow-Medium.ttf", 22), fill=muted)
        y = 262
        for i, r in enumerate(div.get("rows", [])[:3]):
            d.text((x, y), f"{i + 1}", font=font("Oswald-SemiBold.ttf", 40), fill=muted)
            d.text((x + 44, y + 2), r[0][:24], font=font("Barlow-Medium.ttf", 34), fill=fg)
            d.text((x + 44, y + 44), f"{r[1][:30]}  ·  {r[4]} G  {r[5]} A", font=font("Barlow-Medium.ttf", 22), fill=muted)
            pts = f"{r[6]}"
            tw = d.textlength(pts, font=font("Oswald-SemiBold.ttf", 56))
            d.text((x + 500 - tw, y - 6), pts, font=font("Oswald-SemiBold.ttf", 56), fill=fg)
            y += 104
    d.text((60, H - 60), "replay88.github.io/hsl-scoring-race", font=font("Barlow-Medium.ttf", 24), fill=muted)
    im.save(path, optimize=True)
