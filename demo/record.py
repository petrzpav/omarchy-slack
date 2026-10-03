"""Drive the app headless through a tour and render it to an MP4.

Every key goes through the real bindings (pilot.press); each step saves an SVG
screenshot with how long it stays on screen and a caption. rsvg-convert, ImageMagick
and ffmpeg turn those into 1920x1080 video.

    slack-demo record
"""

import asyncio
import base64
import hashlib
import html
import io
import re
import shutil
import subprocess
import time
from functools import lru_cache
from pathlib import Path

from rich.cells import cell_len

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
COLS, ROWS = 118, 34
FONT = "JetBrainsMono Nerd Font"      # what Omarchy's terminals use
EMOJI_FONT = "/usr/share/fonts/noto/NotoColorEmoji.ttf"
CAPTION_FONT = "iA-Writer-Duo-S-Bold"
BG = "#11131a"
PACE = 1.4              # every hold is this much longer (typing keeps its speed)
LINGER = 1.2            # extra seconds on the last frame of a step, before the next caption
END = ((88, "white", -70, "Slack for Omarchy"),
       (44, "#b8bcc8", 30, "Fast in the terminal, live over Socket Mode"),
       (36, "#7d8290", 110, "Ctrl+P to anything · threads · reactions · search"))
KEYS = {" ": "space", ",": "comma", ".": "full_stop", ":": "colon", "!": "exclamation_mark", "?": "question_mark",
        "'": "apostrophe", "@": "at", "#": "number_sign", "-": "minus"}


class Film:
    def __init__(self, app, pilot):
        self.app, self.pilot = app, pilot
        self.frames: list[tuple[str, float, str]] = []
        self.caption = ""

    async def snap(self, seconds: float, caption: str | None = None, settle: float = 0.08, pace=PACE):
        await self.pilot.pause(settle)
        if caption is not None:
            if caption != self.caption:
                print("·", caption or "-", flush=True)
                if self.frames and self.frames[-1][0]:        # let the step's result sink in
                    svg, held, cap = self.frames[-1]
                    self.frames[-1] = (svg, held + LINGER, cap)
            self.caption = caption
        self.frames.append((self.app.export_screenshot(title="Slack"), seconds * pace, self.caption))

    async def key(self, k: str, seconds=0.45, caption: str | None = None):
        await self.pilot.press(k)
        await self.snap(seconds, caption)

    async def type(self, s: str, caption: str | None = None):
        for ch in s:
            await self.pilot.press(KEYS.get(ch, ch))
            await self.snap(0.07, caption, settle=0.02, pace=1)

    async def until(self, cond, timeout=10.0):
        end = time.time() + timeout
        while not cond():
            if time.time() > end:
                raise TimeoutError("demo step never finished")
            await self.pilot.pause(0.05)


def arrives(cid: str, user: str, text: str):
    """A message from someone else, written the way the sync service writes it: on its own
    connection, so the client notices it like a real one."""
    from slacktui.db import Db
    Db().put_msgs(cid, [{"type": "message", "ts": f"{time.time():.6f}", "user": user, "text": text}])


async def story(app, film: Film):
    from slacktui.app import ChatScreen, MsgList, Picker

    def main():
        return app.main()

    def showing(cid):
        return lambda: main() and main().cid == cid and app.screen is main()

    await film.until(showing("C0GENERAL"))
    await film.snap(3.2, "Your Slack, in the terminal: one conversation, the whole screen")

    # -- next unread
    await film.key("ctrl+down", 3.0, "Ctrl+↓ goes to the next unread, mentions first")
    await film.until(showing("C0ALDERBREW"))

    # -- send, then a live reply
    await film.type("Yes, it says October 14 now :tada:", "Type and Enter: it's there at once, sent behind it")
    await film.key("enter", 0.25)
    await film.until(lambda: not main().pending, 5)
    await film.snap(1.6)
    await film.pilot.pause(0.6)
    arrives("C0ALDERBREW", "U0JONAS", "Perfect, thank you! :raised_hands:")
    await film.until(lambda: main().msgs[-1].get("user") == "U0JONAS", 5)
    await film.snap(2.8, "Replies arrive live over Socket Mode")

    # -- react
    await film.key("ctrl+r", 1.0, "Ctrl+R reacts")
    await film.type("heart")
    await film.snap(0.8)
    await film.key("enter", 2.4)

    # -- a thread
    ml = main().query_one(MsgList)
    await film.key("up", 0.5, "↑ selects a message, Enter opens its thread")
    root = next(m["ts"] for m in main().msgs if m.get("reply_count"))
    while ml.get_option_at_index(ml.highlighted).id != root:
        await film.key("up", 0.3)
    await film.snap(0.8)
    await film.key("enter", 0.3)
    await film.until(lambda: isinstance(app.screen, ChatScreen) and app.screen.thread == root)
    await film.snap(2.2)
    await film.type("The taproom photo is in, mobile crop too", "Reply in the thread, Esc goes back")
    await film.key("enter", 0.25)
    await film.until(lambda: not app.screen.pending, 5)
    await film.snap(2.0)
    await film.key("escape", 1.6)

    # -- Ctrl+P
    await film.key("ctrl+p", 1.0, "Ctrl+P jumps to any channel or person")
    await film.type("clara")
    await film.snap(1.2)
    await film.key("enter", 2.6)
    await film.until(showing("D0CLARA"))

    await film.key("ctrl+p", 0.6, "Images and videos show inline")
    await film.type("design")
    await film.snap(0.6)
    await film.key("enter", 0.4)
    await film.until(showing("C0DESIGN"))
    await film.snap(3.6)

    # -- search
    await film.key("ctrl+f", 0.8, "Ctrl+F searches every message, even offline")
    await film.type("launch")
    await film.snap(2.4)
    await film.key("down", 0.6)
    await film.key("enter", 2.8)
    await film.until(lambda: not isinstance(app.screen, Picker))

    # -- bots
    await film.key("ctrl+p", 0.5, "Bots' Block Kit messages read as text")
    await film.type("dev")
    await film.key("enter", 0.4)
    await film.until(showing("C0DEV"))
    await film.snap(3.2)

    # -- keys
    await film.key("f1", 3.6, "F1 lists every key")
    await film.key("escape", 0.6, "")
    film.frames.append(("", 4.0, ""))


# ------------------------------------------------------------ rendering

@lru_cache(64)
def emoji_png(glyph: str) -> str:
    """A color emoji as a data: URI (rsvg would draw it in one flat color)."""
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype(EMOJI_FONT, 109)            # the one size the bitmap font has
    im = Image.new("RGBA", (160, 160))
    ImageDraw.Draw(im).text((8, 8), glyph, font=font, embedded_color=True)
    im = im.crop(im.getbbox() or (0, 0, 1, 1))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def fix_svg(svg: str) -> str:
    """Make rsvg draw Textual's SVG the way a terminal does: every character in its own cell
    (rsvg stretches runs unevenly and drops nbsp), emoji in color."""
    svg = re.sub(r'font-family:\s*"?Fira Code"?(, monospace)?', f'font-family: "{FONT}", monospace', svg)
    svg = svg.replace("font-family: arial", f'font-family: "{FONT}"')

    def run(m):
        attrs, chars = m.group(1), html.unescape(m.group(2))
        if not chars:
            return m.group(0)
        cw = float(re.search(r'textLength="([\d.]+)"', attrs).group(1)) / max(1, cell_len(chars))
        x = float(re.search(r' x="([\d.]+)"', attrs).group(1))
        y = float(re.search(r' y="([\d.]+)"', attrs).group(1))
        clip = re.search(r'clip-path="[^"]*"', attrs)
        base = re.sub(r' (x|y|textLength)="[^"]*"', "", attrs)
        out, i = [], 0
        while i < len(chars):
            g = chars[i]
            i += 1
            while i < len(chars) and (chars[i] in "\ufe0f\u200d" or chars[i - 1] == "\u200d"):
                g += chars[i]
                i += 1
            w = cell_len(g)
            if ord(g[0]) >= 0x2300 and (w == 2 or "️" in g):
                size = min(w * cw, 21)
                out.append(f'<image x="{x + (w * cw - size) / 2:.1f}" y="{y - 17.5:.1f}" width="{size:.1f}" '
                           f'height="{size:.1f}" {clip.group(0) if clip else ""} href="{emoji_png(g)}"/>')
            elif g.strip("\xa0 "):
                out.append(f'<text{base} x="{x:.1f}" y="{y:.1f}">{html.escape(g, quote=False)}</text>')
            x += w * cw
        return "".join(out)
    return re.sub(r"<text([^>]*textLength[^>]*)>([^<]*)</text>", run, svg)


def render(frames, out: Path, work: Path, fps=30):
    """SVG screenshots -> captioned 1920x1080 PNGs -> one image per video frame -> MP4."""
    work.mkdir(parents=True, exist_ok=True)
    pngs: dict[str, Path] = {}
    seq = work / "seq"
    seq.mkdir()
    n, t = 0, 0.0
    for svg, seconds, caption in frames:
        key = hashlib.sha1((svg + caption).encode()).hexdigest()[:16]
        if key not in pngs:
            pngs[key] = card(work / key, caption, fix_svg(svg)) if svg else card(work / key, "", lines=END)
        t += seconds
        while n < round(t * fps):                    # cumulative, so short frames never drift
            (seq / f"{n:05d}.png").hardlink_to(pngs[key])
            n += 1
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps), "-i", seq / "%05d.png",
                    "-vf", "format=yuv420p", "-c:v", "libx264", "-preset", "slow", "-crf", "18",
                    "-movflags", "+faststart", out], check=True)


def card(base: Path, caption: str, svg: str = "", lines: tuple = ()) -> Path:
    """One 1920x1080 frame: the screenshot on top with the caption under it, or a title card."""
    frame = base.with_suffix(".png")
    cmd = ["magick", "-size", "1920x1080", f"xc:{BG}"]
    if svg:
        src, shot = base.with_suffix(".svg"), base.with_name(base.name + "-shot.png")
        src.write_text(svg)
        subprocess.run(["rsvg-convert", "-w", "1560", "-o", shot, src], check=True)
        cmd += [shot, "-gravity", "north", "-geometry", "+0+18", "-composite"]
    if caption:
        cmd += ["-font", CAPTION_FONT, "-pointsize", "48", "-fill", "white", "-gravity", "south",
                "-annotate", "+0+34", caption]
    for size, colour, dy, text in lines:
        cmd += ["-font", CAPTION_FONT, "-pointsize", str(size), "-fill", colour, "-gravity", "center",
                "-annotate", f"+0{dy:+d}", text]
    subprocess.run(cmd + [frame], check=True)
    return frame


def main(cfg, make_app):
    app = make_app(cfg)
    film = None

    async def run():
        nonlocal film
        async with app.run_test(size=(COLS, ROWS)) as pilot:
            film = Film(app, pilot)
            await pilot.pause(0.5)
            await story(app, film)

    asyncio.run(run())
    OUT.mkdir(exist_ok=True)
    work = OUT / "frames"
    shutil.rmtree(work, ignore_errors=True)
    out = OUT / "slack-demo.mp4"
    render(film.frames, out, work)
    shutil.rmtree(work)
    total = sum(s for _, s, _ in film.frames)
    print(f"{out}  {len(film.frames)} frames, {total:.1f}s")
