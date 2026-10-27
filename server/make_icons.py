"""Draws the lostOS app icon (a glass orb with an L on black) into web/icons/. Run: python make_icons.py"""
import os

from PIL import Image, ImageChops, ImageDraw, ImageFilter

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "icons")
S = 1024
FILLET = 30      # radius of the rounding of the inner corner of the L, on the 1024 grid


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def draw(scale: float = 1.0) -> Image.Image:
    img = Image.new("RGB", (S, S), (0, 0, 0))
    # background: a soft vertical gradient and a red glow at the bottom
    px = ImageDraw.Draw(img)
    for y in range(S):
        px.line([(0, y), (S, y)], fill=lerp((24, 24, 31), (2, 2, 4), y / S))
    glow = Image.new("RGB", (S, S), (0, 0, 0))
    ImageDraw.Draw(glow).ellipse([S * 0.18, S * 0.55, S * 0.82, S * 1.15], fill=(139, 92, 246))
    glow = glow.filter(ImageFilter.GaussianBlur(120))
    img = ImageChops.add(img, Image.eval(glow, lambda v: int(v * 0.55)))

    c = S // 2
    r = int(300 * scale)
    # the orb: a circle with a diagonal gradient (a mask carries the round shape)
    orb = Image.new("RGB", (S, S))
    od = ImageDraw.Draw(orb)
    for i in range(2 * S):
        t = i / (2 * S)
        od.line([(i, 0), (0, i)], fill=lerp((190, 160, 255), (70, 30, 160), t), width=2)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).ellipse([c - r, c - r, c + r, c + r], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(1.2))
    shadow = Image.new("L", (S, S), 0)
    ImageDraw.Draw(shadow).ellipse([c - r, c - r + int(40 * scale), c + r, c + r + int(40 * scale)], fill=170)
    shadow = shadow.filter(ImageFilter.GaussianBlur(46))
    img = Image.composite(Image.new("RGB", (S, S), (0, 0, 0)), img, shadow)
    img = Image.composite(orb, img, mask)

    # a specular rim and the glassy highlight on the upper left
    rim = Image.new("L", (S, S), 0)
    rd = ImageDraw.Draw(rim)
    rd.ellipse([c - r, c - r, c + r, c + r], outline=255, width=int(7 * scale))
    rim = rim.filter(ImageFilter.GaussianBlur(1.5))
    grad = Image.new("L", (S, S))
    gd = ImageDraw.Draw(grad)
    for i in range(2 * S):
        t = abs((i / (2 * S)) - 0.3) * 2.2
        gd.line([(i, 0), (0, i)], fill=int(max(0, 255 - 255 * t)), width=2)
    img = Image.composite(Image.new("RGB", (S, S), (255, 255, 255)), img, ImageChops.multiply(rim, grad).point(lambda v: int(v * 0.9)))
    hi = Image.new("L", (S, S), 0)
    ImageDraw.Draw(hi).ellipse([c - r * 0.78, c - r * 0.9, c + r * 0.25, c - r * 0.05], fill=120)
    hi = ImageChops.multiply(hi.filter(ImageFilter.GaussianBlur(40)), mask)
    img = Image.composite(Image.new("RGB", (S, S), (255, 255, 255)), img, hi.point(lambda v: int(v * 0.55)))

    # the letter L: two bars with round ends, and the inner corner softly rounded (a fillet) instead of a sharp notch.
    # It is drawn as a mask four times larger and scaled down, which gives smooth edges.
    k = 4
    m = Image.new("L", (S * k, S * k), 0)
    md = ImageDraw.Draw(m)
    w = int(78 * scale) * k
    x0, y0, y1, x1 = (c - int(110 * scale)) * k, (c - int(170 * scale)) * k, (c + int(130 * scale)) * k, (c + int(160 * scale)) * k
    md.rounded_rectangle([x0, y0, x0 + w, y1], radius=w // 2, fill=255)
    md.rounded_rectangle([x0, y1 - w, x1, y1], radius=w // 2, fill=255)
    r = int(FILLET * scale) * k                                  # the fillet: a square in the corner minus the circle that rounds it
    cx, cy = x0 + w + r, y1 - w - r                              # centre of the rounding circle: in the empty quarter next to the corner
    md.rectangle([x0 + w, cy, cx, y1 - w], fill=255)
    md.ellipse([cx - r, cy - r, cx + r, cy + r], fill=0)
    m = m.resize((S, S), Image.LANCZOS)
    img.paste((255, 255, 255), (0, 0), m)
    return img


def save(img: Image.Image, name: str, size: int):
    img.resize((size, size), Image.LANCZOS).save(os.path.join(OUT, name), optimize=True)


def main():
    os.makedirs(OUT, exist_ok=True)
    full = draw(1.0)
    save(full, "icon-512.png", 512)
    save(full, "icon-192.png", 192)
    save(full, "apple-touch-icon.png", 180)
    save(full, "favicon-32.png", 32)
    save(draw(0.72), "icon-maskable-512.png", 512)    # the safe zone of a maskable icon is the inner 80%
    print("icons written to", OUT)


main()
