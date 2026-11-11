"""Regenerate the banners, GIFs and intro video in ./media (needs Pillow;
the video also needs ffmpeg). Not needed to run the bot."""
import math
import os
import shutil
import subprocess
import tempfile

import cards
from PIL import ImageDraw

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media")
os.makedirs(OUT, exist_ok=True)
p = lambda n: os.path.join(OUT, n)

cards.banner("MATRIX AUCTION", "Matrix Auction Bot", p("welcome.png"))
cards.banner("PLAYER REGISTRATION", "Sign up - get picked - get sold", p("register.png"))
cards.banner("SETS ARE READY", "Registration closed - let's go", p("sets.png"))
cards.sold_gif(p("sold.gif"))
cards.unsold_gif(p("unsold.gif"))
cards.live_gif(p("live.gif"))
cards.trophy_gif(p("trophy.gif"))


def intro_video(path, fps=15):
    if not shutil.which("ffmpeg"):
        print("ffmpeg not found - skipping intro.mp4")
        return
    scenes = [("MATRIX AUCTION", "The bidding war begins"),
              ("MANAGERS ASSEMBLE", "Build your dream squad"),
              ("LET THE BIDDING BEGIN", "Budget wisely - every point counts")]
    w, h, per = 800, 450, fps * 2
    with tempfile.TemporaryDirectory() as tmp:
        n = 0
        for title, sub in scenes:
            for f in range(per):
                img = cards.gradient(w, h)
                d = ImageDraw.Draw(img)
                cards.stumps(d, 70, 300, 100)
                cards.stumps(d, 690, 300, 100)
                cards.ball(img, int(120 + 560 * f / per), int(110 - 40 * math.sin(f / per * math.pi)), 55)
                a = min(1, f / (fps * 0.5))  # fade-in
                col = tuple(int(c * a + g * (1 - a)) for c, g in zip(cards.GOLD, cards.GREEN_TOP))
                cards.centered(d, title, 205, w, cards.fit_text(d, title, 720, 64), col)
                cards.centered(d, sub, 295, w, cards.font(28), (200, 230, 210), shadow=False)
                cards.frame_border(img)
                img.save(os.path.join(tmp, f"f{n:04d}.png"))
                n += 1
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps),
                        "-i", os.path.join(tmp, "f%04d.png"), "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-movflags", "+faststart", path], check=True)


intro_video(p("intro.mp4"))
print("done:", sorted(os.listdir(OUT)))
