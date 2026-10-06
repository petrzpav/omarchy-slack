"""Slack's mrkdwn and ids to terminal text: <@U1> → @Jana, *bold*, :emoji: → 😀, ``` blocks."""

import html
import re
import unicodedata
from functools import lru_cache

import emoji as emojilib
from rich.style import Style
from rich.text import Text

MENTION_STYLE = "bold #e8a33d"
ME_STYLE = "bold black on #e8a33d"
LINK_STYLE = "underline #6cb6ff"
CODE_STYLE = "#e6a3ff"


def fold(s: str) -> str:
    """Lowercase, accents stripped, same length (so match offsets still fit the original)."""
    return "".join(unicodedata.normalize("NFD", ch)[0] for ch in (s or "")).lower()


# ------------------------------------------------------------ emoji

@lru_cache(1)
def emoji_names() -> dict[str, str]:
    """Slack-style short name → character (Slack uses mostly GitHub's names)."""
    out = {}
    for ch, d in emojilib.EMOJI_DATA.items():
        if d.get("status", 2) > 2:
            continue
        for n in [d["en"], *d.get("alias", [])]:
            out.setdefault(n.strip(":"), ch)
    out.update({"simple_smile": "🙂", "thumbsup": "👍", "thumbsdown": "👎", "+1": "👍", "-1": "👎",
                "white_check_mark": "✅", "heavy_check_mark": "✔️", "slightly_smiling_face": "🙂",
                "upside_down_face": "🙃", "face_with_rolling_eyes": "🙄", "hugging_face": "🤗"})
    return out


@lru_cache(1)
def popular() -> list[str]:
    return ["+1", "white_check_mark", "eyes", "pray", "joy", "heart", "tada", "raised_hands", "fire",
            "100", "thinking_face", "ok_hand", "slightly_smiling_face", "rocket", "muscle", "clap",
            "x", "-1", "sob", "wave"]


def emoji_char(name: str, custom: dict | None = None) -> str:
    name = name.split("::")[0]                       # :+1::skin-tone-3:
    names = emoji_names()
    ch = names.get(name) or (name.startswith("large_") and names.get(name[6:]))   # Slack's :large_green_circle:
    if ch:
        return ch
    if custom and name in custom:
        alias = custom[name]
        if alias.startswith("alias:"):
            return emoji_char(alias[6:], custom)
    return f":{name}:"


def emojize(s: str, custom: dict | None = None) -> str:
    def sub(m):
        ch = emoji_char(m.group(1), custom)
        return m.group(0) if ch == f":{m.group(1)}:" else ch
    return re.sub(r":([a-z0-9_+\-']+)(?:::skin-tone-\d)?:", sub, s)


# ------------------------------------------------------------ names

def user_name(uid: str | None, users: dict, short=False) -> str:
    u = users.get(uid or "")
    if not u:
        return uid or "?"
    p = u.get("profile", {})
    name = p.get("display_name") or p.get("real_name") or u.get("real_name") or u.get("name") or uid
    return name.split()[0] if short and " " in name and not p.get("display_name") else name


def author(m: dict, users: dict) -> str:
    if m.get("user") and (m["user"] in users or not (m.get("bot_profile") or m.get("username"))):
        return user_name(m["user"], users)
    return m.get("username") or (m.get("bot_profile") or {}).get("name") or "bot"


def conv_name(c: dict, users: dict, me: str = "") -> str:
    if c.get("is_im"):
        return user_name(c.get("user"), users) + (" (you)" if c.get("user") == me else "")
    if c.get("is_mpim"):
        # "mpdm-jana--petr--me-1" → names; the purpose has them too but changes with language
        handles = re.sub(r"^mpdm-|-\d+$", "", c.get("name", "")).split("--")
        by_handle = {u.get("name"): u["id"] for u in users.values()}
        names = [user_name(by_handle.get(h, h), users, short=True) for h in handles if by_handle.get(h) != me]
        return ", ".join(names)
    return "#" + c.get("name", c["id"])


# ------------------------------------------------------------ mrkdwn

TOKEN = re.compile(r"<([^<>]+)>")
INLINE = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)\*(?![\w*])|(?<![\w_])_(?!\s)([^_\n]+?)_(?![\w_])"
                    r"|(?<![\w~])~(?!\s)([^~\n]+?)~(?![\w~])|`([^`\n]+)`")


def _ref(tok: str, users: dict, convs: dict, me: str) -> tuple[str, str, str | None]:
    """A <...> token → (shown text, style, link)."""
    target, _, label = tok.partition("|")
    if target.startswith("@"):
        uid = target[1:]
        return "@" + (user_name(uid, users) if uid in users else label or uid), \
            ME_STYLE if uid == me else MENTION_STYLE, None
    if target.startswith("#"):
        c = convs.get(target[1:])
        return "#" + (label or (c or {}).get("name") or target[1:]), MENTION_STYLE, None
    if target.startswith("!subteam^"):
        return label or "@team", MENTION_STYLE, None
    if target.startswith("!date^"):
        return label or "", "", None
    if target.startswith("!"):
        return "@" + target[1:].split("^")[0], ME_STYLE, None
    if target.startswith("mailto:"):
        return label or target[7:], LINK_STYLE, target
    return label or target, LINK_STYLE, target


def _inline(t: Text, s: str, base: str, custom: dict | None):
    pos = 0
    for m in INLINE.finditer(s):
        t.append(emojize(s[pos:m.start()], custom), base)
        if m.group(1):
            t.append(emojize(m.group(1), custom), f"{base} bold".strip())
        elif m.group(2):
            t.append(emojize(m.group(2), custom), f"{base} italic".strip())
        elif m.group(3):
            t.append(emojize(m.group(3), custom), f"{base} strike".strip())
        else:
            t.append(m.group(4), CODE_STYLE)
        pos = m.end()
    t.append(emojize(s[pos:], custom), base)


def _append_ref(out: Text, shown: str, style: str, link: str | None, links: list | None):
    """A link carries its URL in the style's meta, so a click on it knows where to go."""
    if not link:
        return out.append(shown, style)
    out.append(shown, Style.parse(style) + Style.from_meta({"url": link}))
    if links is not None:
        links.append(link)


def render(text: str, users: dict, convs: dict, me: str, custom: dict | None = None,
           links: list | None = None) -> Text:
    """mrkdwn → Rich Text. Links found are appended to `links`."""
    out = Text()
    parts = re.split(r"```", text or "")
    for i, part in enumerate(parts):
        if i % 2:                                     # code block: links stay links, nothing else is styled
            out.append("\n" if out.plain and not out.plain.endswith("\n") else "")
            for line in part.strip("\n").split("\n"):
                out.append("  ", CODE_STYLE)
                pos = 0
                for m in TOKEN.finditer(line):
                    out.append(html.unescape(line[pos:m.start()]), CODE_STYLE)
                    shown, style, link = _ref(html.unescape(m.group(1)), users, convs, me)
                    _append_ref(out, shown, f"{CODE_STYLE} underline" if link else CODE_STYLE, link, links)
                    pos = m.end()
                out.append(html.unescape(line[pos:]) + "\n", CODE_STYLE)
            continue
        if i and part.startswith("\n"):             # right after a code block, which ended the line
            part = part[1:]
        lines = part.split("\n")
        for j, line in enumerate(lines):
            quote = line.startswith("&gt;")
            if quote:
                line = line[4:].lstrip()
                out.append("▎ ", "dim")
            base = "italic dim" if quote else ""
            pos = 0
            for m in TOKEN.finditer(line):
                _inline(out, html.unescape(line[pos:m.start()]), base, custom)
                shown, style, link = _ref(html.unescape(m.group(1)), users, convs, me)
                _append_ref(out, shown, style, link, links)
                pos = m.end()
            _inline(out, html.unescape(line[pos:]), base, custom)
            if j < len(lines) - 1:
                out.append("\n")
    return out.rstrip() or out


def plain(text: str, users: dict, convs: dict, me: str = "") -> str:
    return render(text, users, convs, me).plain


def _link(m) -> str:
    """A URL as an explicit link; a closing bracket it didn't open is the text's: "(see https://x)"."""
    url, rest = m.group(0), ""
    while url[-1] in ")]" and url.count({")": "(", "]": "["}[url[-1]]) < url.count(url[-1]):
        url, rest = url[:-1], url[-1] + rest
    return f"<{url}>{rest}"


def to_slack(text: str, users: dict, convs: dict) -> str:
    """What you typed → what Slack wants: @Name / #channel as ids, &<> escaped."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # a line starting with "> " is a quote; Slack expects the raw ">"
    text = re.sub(r"(?m)^&gt; ", "> ", text)
    # in a code block Slack would link a URL itself and leave out trailing dots, brackets…
    # (expires:..2026-08-31.. loses its ".."); an explicit <url> keeps every character
    parts = text.split("```")
    for i in range(1, len(parts), 2):
        parts[i] = re.sub(r"(?<![\w<|])https?://(?:[^\s&]|&(?!gt;|lt;))+", _link, parts[i])
    text = "```".join(parts)
    names = {}
    for u in users.values():
        if u.get("deleted"):
            continue
        p = u.get("profile", {})
        for n in (p.get("display_name"), p.get("real_name"), u.get("name")):
            if n:
                names.setdefault(fold(n), u["id"])
    # longest names first so "@Jana Nováková" wins over "@Jana"
    for n in sorted(names, key=len, reverse=True):
        pat = re.compile(r"(?<![\w@])@" + re.escape(n) + r"(?![\w])", re.I)
        f = fold(text)
        hits = [m.span() for m in pat.finditer(f)]
        for a, b in reversed(hits):
            text = text[:a] + f"<@{names[n]}>" + text[b:]
    for c in convs.values():
        if c.get("name") and not c.get("is_im") and not c.get("is_mpim"):
            text = re.sub(r"(?<![\w#<])#" + re.escape(c["name"]) + r"(?![\w-])", f"<#{c['id']}>", text)
    for n in ("here", "channel", "everyone"):
        text = re.sub(rf"(?<![\w@])@{n}\b", f"<!{n}>", text)
    return text


def from_slack(text: str, users: dict, convs: dict) -> str:
    """Slack's text → what you'd type (for editing a message)."""
    def sub(m):
        target, _, label = m.group(1).partition("|")
        if target.startswith("@"):
            return "@" + user_name(target[1:], users)
        if target.startswith("#"):
            return "#" + (label or (convs.get(target[1:]) or {}).get("name", target[1:]))
        if target.startswith("!"):
            return label or "@" + target[1:].split("^")[0]
        return target if not label or label == target else f"{label} ({target})"
    return html.unescape(TOKEN.sub(sub, text or ""))


# ------------------------------------------------------------ Block Kit

RICH_ONLY = {"rich_text"}


def has_layout(blocks: list | None) -> bool:
    """Blocks that say more than the message text (bots' headers, sections, buttons…)."""
    return any(b.get("type") not in RICH_ONLY for b in blocks or [])


def _btext(o: dict | None, users, convs, me, custom, links) -> Text:
    if not o:
        return Text()
    if o.get("type") == "plain_text":
        return Text(emojize(o.get("text", ""), custom))
    return render(o.get("text", ""), users, convs, me, custom, links)


def render_blocks(blocks: list, users: dict, convs: dict, me: str, custom: dict | None = None,
                  links: list | None = None) -> Text:
    """Header, section (text + fields), context, divider, image, actions → terminal text."""
    out = Text()

    def sep():
        if out.plain:
            out.append("\n")
    for b in blocks:
        t = b.get("type")
        if t == "header":
            sep()
            h = _btext(b.get("text"), users, convs, me, custom, links)
            h.stylize("bold")
            out.append_text(h)
        elif t == "section":
            if b.get("text"):
                sep()
                out.append_text(_btext(b["text"], users, convs, me, custom, links))
            for f in b.get("fields") or []:
                sep()
                out.append_text(_btext(f, users, convs, me, custom, links))
            acc = b.get("accessory") or {}
            if acc.get("type") == "button":
                out.append("  ")
                out.append(f"[ {(acc.get('text') or {}).get('text', '')} ]", "bold #6cb6ff")
                if acc.get("url") and links is not None:
                    links.append(acc["url"])
        elif t == "context":
            sep()
            for i, e in enumerate(b.get("elements") or []):
                if i:
                    out.append("  ")
                if e.get("type") in ("mrkdwn", "plain_text"):
                    x = _btext(e, users, convs, me, custom, links)
                    x.stylize("dim")
                    out.append_text(x)
        elif t == "divider":
            sep()
            out.append("─" * 40, "dim")
        elif t == "image":
            sep()
            out.append(f"🖼 {(b.get('title') or {}).get('text') or b.get('alt_text') or 'image'}", "#6cb6ff")
            if b.get("image_url") and links is not None:
                links.append(b["image_url"])
        elif t == "actions":
            sep()
            for e in b.get("elements") or []:
                label = (e.get("text") or {}).get("text") or e.get("placeholder", {}).get("text") or e.get("type")
                out.append(f"[ {emojize(label, custom)} ]", "bold #6cb6ff")
                out.append(" ")
                if e.get("url") and links is not None:
                    links.append(e["url"])
        elif t == "rich_text":
            pass                                     # the message text says the same
    return out
