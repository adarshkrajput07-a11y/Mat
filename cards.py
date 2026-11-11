"""Image helpers: live lot cards (used by the bot) and banner/GIF generators
(used by make_media.py). Requires Pillow."""
import io
import math
import os
import random

from PIL import Image, ImageChops, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_PATH = os.path.join(HERE, "media", "font.ttf")
GOLD, WHITE, RED = (255, 196, 46), (255, 255, 255), (214, 40, 57)
GREEN_TOP, GREEN_BOT = (10, 92, 54), (4, 32, 28)


def font(size):
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except OSError:
        return ImageFont.load_default(size)


def gradient(w, h, top=GREEN_TOP, bottom=GREEN_BOT):
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / (h - 1)
        d.line([(0, y), (w, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return img


def ball(img, cx, cy, r):
    s = r * 2
    layer = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse((0, 0, s - 1, s - 1), fill=(190, 30, 38, 255))
    d.ellipse((s * 0.18, s * 0.12, s * 0.5, s * 0.4), fill=(225, 80, 85, 255))
    w = max(2, r // 10)
    d.arc((-s * 0.55, -s * 0.2, s * 0.35, s * 1.2), -62, 62, fill=(255, 255, 255, 255), width=w)
    d.arc((s * 0.65, -s * 0.2, s * 1.55, s * 1.2), 118, 242, fill=(255, 255, 255, 255), width=w)
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, s - 1, s - 1), fill=255)
    layer.putalpha(ImageChops.multiply(layer.getchannel("A"), mask))
    img.paste(layer, (int(cx - r), int(cy - r)), layer)


def stumps(d, x, y, h, color=(240, 230, 200)):
    for i in range(3):
        d.rounded_rectangle((x + i * 14, y, x + i * 14 + 6, y + h), radius=3, fill=color)
    for i in range(2):
        d.rounded_rectangle((x + i * 14 + 5, y - 6, x + i * 14 + 15, y - 1), radius=2, fill=GOLD)


def fit_text(d, text, max_w, start, min_size=24):
    size = start
    while size > min_size and d.textlength(text, font=font(size)) > max_w:
        size -= 2
    return font(size)


def centered(d, text, y, w, fnt, fill=WHITE, shadow=True):
    x = (w - d.textlength(text, font=fnt)) / 2
    if shadow:
        d.text((x + 3, y + 3), text, font=fnt, fill=(0, 0, 0))
    d.text((x, y), text, font=fnt, fill=fill)


def frame_border(img, color=GOLD, pad=10, width=4):
    ImageDraw.Draw(img).rounded_rectangle(
        (pad, pad, img.width - pad, img.height - pad), radius=18, outline=color, width=width)


def to_png(img):
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    buf.seek(0)
    buf.name = "card.png"
    return buf


# ---------- live card for each lot (called by the bot) ----------
def lot_card(name, role, base, set_name, lot_no, total, username=""):
    w, h = 800, 450
    img = gradient(w, h)
    d = ImageDraw.Draw(img)
    stumps(d, 40, 330, 90)
    ball(img, 660, 150, 95)
    d.text((40, 34), f"{set_name.upper()}  -  LOT {lot_no}/{total}", font=font(26), fill=GOLD)
    nf = fit_text(d, name.upper(), 520, 64)
    d.text((43, 103), name.upper(), font=nf, fill=(0, 0, 0))
    d.text((40, 100), name.upper(), font=nf, fill=WHITE)
    pill = role.upper()
    pf = font(28)
    pw = int(d.textlength(pill, font=pf)) + 40
    d.rounded_rectangle((40, 195, 40 + pw, 245), radius=25, fill=RED)
    d.text((60, 202), pill, font=pf, fill=WHITE)
    if username:
        d.text((40 + pw + 20, 204), f"@{username}", font=fit_text(d, f"@{username}", 330, 30), fill=(200, 230, 210))
    d.text((40, 290), "BASE PRICE", font=font(24), fill=(190, 220, 200))
    d.text((235, 270), f"{base}", font=font(84), fill=GOLD)
    d.text((560, 380), "GOING ONCE...", font=font(26), fill=(190, 220, 200))
    frame_border(img)
    return to_png(img)


# ---------- static banners ----------
def banner(title, subtitle, path, accent=GOLD):
    w, h = 800, 450
    img = gradient(w, h)
    d = ImageDraw.Draw(img)
    stumps(d, 70, 300, 100)
    stumps(d, 690, 300, 100)
    ball(img, 400, 110, 70)
    tf = fit_text(d, title, 700, 70)
    centered(d, title, 205, w, tf, accent)
    centered(d, subtitle, 295, w, fit_text(d, subtitle, 640, 30), (200, 230, 210), shadow=False)
    frame_border(img)
    img.save(path, "PNG", optimize=True)


# ---------- animated GIFs ----------
def save_gif(frames, path, ms=70):
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=ms,
                   loop=0, optimize=True, disposal=2)


def confetti_frames(n, w, h, seed):
    rnd = random.Random(seed)
    bits = [(rnd.random() * w, rnd.random() * h, rnd.choice([GOLD, RED, WHITE, (80, 200, 255)]),
             rnd.randint(4, 9), rnd.uniform(6, 14)) for _ in range(60)]
    return [[(x, (y + f * sp) % h, c, s) for x, y, c, s, sp in bits] for f in range(n)]


def sold_gif(path):
    w, h, n = 480, 270, 16
    conf = confetti_frames(n, w, h, 7)
    frames = []
    for f in range(n):
        img = gradient(w, h, (30, 20, 70), (8, 6, 24))
        d = ImageDraw.Draw(img)
        for x, y, c, s in conf[f]:
            d.rectangle((x, y, x + s, y + s * 0.6), fill=c)
        size = int(86 + 14 * math.sin(f / n * 2 * math.pi * 2))
        centered(d, "SOLD!", h / 2 - size / 1.5, w, font(size), GOLD if f % 4 < 2 else WHITE)
        centered(d, "GOING, GOING, GONE", h - 52, w, font(20), (200, 200, 230), shadow=False)
        frames.append(img)
    save_gif(frames, path, 70)


def unsold_gif(path):
    w, h, n = 480, 270, 12
    frames = []
    for f in range(n):
        img = gradient(w, h, (60, 64, 72), (22, 24, 30))
        d = ImageDraw.Draw(img)
        dx = int(8 * math.sin(f / n * 2 * math.pi * 3))
        ball(img, 240 + dx, 70, 34)
        tf = font(70)
        x = (w - d.textlength("UNSOLD", font=tf)) / 2 + dx
        d.text((x + 3, 130 + 3), "UNSOLD", font=tf, fill=(0, 0, 0))
        d.text((x, 130), "UNSOLD", font=tf, fill=(220, 90, 90))
        centered(d, "NO TAKERS THIS TIME", h - 48, w, font(20), (170, 175, 190), shadow=False)
        frames.append(img)
    save_gif(frames, path, 80)


def live_gif(path):
    w, h, n = 480, 270, 20
    frames = []
    for f in range(n):
        img = gradient(w, h)
        d = ImageDraw.Draw(img)
        stumps(d, 40, 160, 80)
        stumps(d, 400, 160, 80)
        bx = 90 + (300 * (f / (n - 1)))
        by = 190 - abs(math.sin(f / (n - 1) * math.pi * 2)) * 90
        ball(img, bx, by, 18)
        if f % 10 < 6:
            d.ellipse((40, 30, 66, 56), fill=RED)
        d.text((78, 26), "LIVE", font=font(34), fill=WHITE)
        centered(d, "MATRIX AUCTION", 70, w, font(46), GOLD)
        centered(d, "BIDS ARE OPEN", 215, w, font(26), (200, 230, 210), shadow=False)
        frames.append(img)
    save_gif(frames, path, 70)


def trophy_gif(path):
    w, h, n = 480, 270, 14
    rnd = random.Random(3)
    sparks = [(rnd.randint(30, w - 30), rnd.randint(20, h - 60), rnd.randint(0, n)) for _ in range(14)]
    frames = []
    for f in range(n):
        img = gradient(w, h, (60, 40, 10), (14, 10, 4))
        d = ImageDraw.Draw(img)
        cx = w // 2
        d.polygon([(cx - 55, 60), (cx + 55, 60), (cx + 40, 135), (cx - 40, 135)], fill=GOLD)
        d.arc((cx - 85, 70, cx - 40, 120), 90, 270, fill=GOLD, width=8)
        d.arc((cx + 40, 70, cx + 85, 120), 270, 90, fill=GOLD, width=8)
        d.rectangle((cx - 9, 135, cx + 9, 170), fill=GOLD)
        d.rounded_rectangle((cx - 45, 170, cx + 45, 190), radius=6, fill=(200, 140, 30))
        for x, y, off in sparks:
            r = 2 + int(5 * abs(math.sin((f + off) / n * math.pi)))
            d.line([(x - r, y), (x + r, y)], fill=WHITE, width=2)
            d.line([(x, y - r), (x, y + r)], fill=WHITE, width=2)
        centered(d, "AUCTION COMPLETE", 205, w, font(34), GOLD)
        frames.append(img)
    save_gif(frames, path, 90)
