"""Inline image and video thumbnails, drawn with half blocks (two pixels per cell), so they
work in any terminal and sit inside a message like text. The space a thumbnail takes is
known before it's downloaded, so loading one never moves the messages around it."""

from functools import lru_cache

from rich.text import Text

from .config import CACHE_DIR

THUMBS = CACHE_DIR / "thumbs"
MAX_W, MAX_H = 48, 14          # cells


def source(f: dict) -> tuple[str, int, int] | None:
    """(url, width px, height px) of the thumbnail Slack made for a file, if any."""
    for k in ("thumb_480", "thumb_360", "thumb_video", "thumb_160"):
        if f.get(k):
            w = f.get(f"{k}_w") or f.get("original_w") or 4
            h = f.get(f"{k}_h") or f.get("original_h") or 3
            return f[k], int(w), int(h)
    return None


def cells(w: int, h: int, max_w=MAX_W) -> tuple[int, int]:
    cols = min(max_w, MAX_W, max(8, w // 8))
    rows = max(2, round(cols * h / w / 2))
    if rows > MAX_H:
        rows = MAX_H
        cols = max(8, round(rows * 2 * w / h))
    return cols, rows


def path(f: dict):
    return THUMBS / f"{f['id']}.img"


def placeholder(cols: int, rows: int, label: str = "") -> Text:
    t = Text()
    for r in range(rows):
        line = label.center(cols)[:cols] if r == rows // 2 else " " * cols
        t.append(line, "#5c6370 on #23272e")
        if r < rows - 1:
            t.append("\n")
    return t


@lru_cache(256)
def picture(file: str, cols: int, rows: int, mtime: float) -> Text:
    from PIL import Image
    with Image.open(file) as im:
        im = im.convert("RGB").resize((cols, rows * 2), Image.Resampling.LANCZOS)
        px = im.load()
    t = Text()
    for r in range(rows):
        for c in range(cols):
            top, bot = px[c, r * 2], px[c, r * 2 + 1]
            t.append("▀", f"#{top[0]:02x}{top[1]:02x}{top[2]:02x} on #{bot[0]:02x}{bot[1]:02x}{bot[2]:02x}")
        if r < rows - 1:
            t.append("\n")
    return t


def render(f: dict, max_w: int) -> Text | None:
    """The thumbnail, or a box of the same size while it isn't downloaded yet; None if the file has none."""
    src = source(f)
    if not src:
        return None
    cols, rows = cells(src[1], src[2], max_w)
    p = path(f)
    if p.exists():
        try:
            return picture(str(p), cols, rows, p.stat().st_mtime)
        except Exception:  # noqa: BLE001 - a broken image is shown as a box
            return placeholder(cols, rows, "image")
    return placeholder(cols, rows, "loading…")
