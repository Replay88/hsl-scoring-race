"""Link-preview image (1200x630): the points leader in every division."""
import os

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = os.path.join(HERE, "fonts")


def font(name, size):
    return ImageFont.truetype(os.path.join(FONTS, name), size)


def render(index, path, site="replay88.github.io/hsl-scoring-race"):
    W, H = 1200, 630
    bg, fg, muted, accent, line = (14, 18, 24), (232, 236, 242), (152, 162, 179), (255, 77, 99), (42, 50, 64)
    im = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 14, H], fill=accent)
    d.text((60, 40), "HSL STATISTICS", font=font("Oswald-SemiBold.ttf", 60), fill=fg)
    d.text((62, 112), "Points leaders, standings, goalies and schedules for every Hockey Super League division",
           font=font("Barlow-Medium.ttf", 24), fill=muted)
    d.line([60, 156, W - 60, 156], fill=line, width=2)

    divs = [x for x in index.get("divisions", []) if x.get("leader")]
    cols, rows = 2, 6
    colw, rowh, top = (W - 120) // cols, 68, 176
    for i, x in enumerate(divs[:cols * rows]):
        cx, cy = 60 + (i // rows) * colw, top + (i % rows) * rowh
        d.text((cx, cy + 2), x["name"].upper(), font=font("Oswald-SemiBold.ttf", 26), fill=accent)
        name, team, pts = x["leader"]
        d.text((cx + 150, cy), name[:22], font=font("Barlow-Medium.ttf", 27), fill=fg)
        d.text((cx + 150, cy + 32), team[:30], font=font("Barlow-Medium.ttf", 19), fill=muted)
        p = str(pts)
        tw = d.textlength(p, font=font("Oswald-SemiBold.ttf", 40))
        d.text((cx + colw - 40 - tw, cy - 2), p, font=font("Oswald-SemiBold.ttf", 40), fill=fg)
    d.text((60, H - 48), site, font=font("Barlow-Medium.ttf", 22), fill=muted)
    im.save(path, optimize=True)
