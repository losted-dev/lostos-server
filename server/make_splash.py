"""Draws the pictures an iPhone shows while the panel starts from the home screen (apple-touch-startup-image): the orb of lostOS and the name on black.
Run: python make_splash.py   (needs Pillow; the fonts are the Segoe UI of Windows, the panel itself draws its name with the font of the phone)"""
import os

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "web", "icons")
FONTS = r"C:\Windows\Fonts"
SIZES = [(1170, 2532), (1179, 2556), (1284, 2778), (1290, 2796), (1206, 2622), (1320, 2868)]      # the iPhones of the last years, see the links in index.html
VIOLET = (139, 92, 246)


def make(w: int, h: int):
    img = Image.new("RGB", (w, h), (0, 0, 0))
    orb = Image.open(os.path.join(OUT, "icon-512.png")).convert("RGB")
    side = int(w * 0.30)
    orb = orb.resize((side, side), Image.LANCZOS)
    top = int(h * 0.5) - side // 2 - int(w * 0.05)
    mask = Image.new("L", (side * 4, side * 4), 0)                      # rounded like the icon on the home screen
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, side * 4 - 1, side * 4 - 1], radius=int(side * 4 * 0.225), fill=255)
    img.paste(orb, ((w - side) // 2, top), mask.resize((side, side), Image.LANCZOS))
    size = int(w * 0.105)
    light, bold = ImageFont.truetype(os.path.join(FONTS, "segoeuil.ttf"), size), ImageFont.truetype(os.path.join(FONTS, "seguibl.ttf"), size)
    d = ImageDraw.Draw(img)
    a, b = d.textlength("lost", font=light), d.textlength("OS", font=bold)
    x, y = (w - a - b) / 2, top + side + int(w * 0.03)
    d.text((x, y), "lost", font=light, fill=(255, 255, 255))
    d.text((x + a, y), "OS", font=bold, fill=VIOLET)
    img.save(os.path.join(OUT, f"splash-{w}x{h}.png"), optimize=True)


for w, h in SIZES:
    make(w, h)
    print("splash", w, h)
