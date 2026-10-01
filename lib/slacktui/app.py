"""The Textual client: one conversation on the whole screen, Ctrl+P to any other.

Everything is painted from the local database at once. The sync daemon (or the client
itself, when the daemon isn't running) writes what Slack sends; the client notices via
`PRAGMA data_version` and repaints in place. What you do is shown first and sent behind it.
"""

import itertools
import os
import subprocess
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

from rich.table import Table
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from . import sync, thumbs
from .api import Slack
from .config import CACHE_DIR, Config
from .db import Db, is_top
from .mrkdwn import (author, conv_name, emoji_char, emoji_names, fold, from_slack, has_layout, popular, render,
                     render_blocks, to_slack, user_name)

RULE = 72                 # width of the day and "new" rules
GROUP_GAP = 300            # same author within 5 minutes: no new name line
MARK_AFTER = 1.0           # seconds at the bottom of a conversation before it counts as read
NAME_COLORS = ["#e8a33d", "#6cb6ff", "#4bce97", "#f87168", "#9f8fef", "#e774bb", "#6cc3e0",
               "#94c748", "#fea362", "#f5cd47"]

_pending = itertools.count(1)


KEY_NAMES = {"pageup": "PgUp", "pagedown": "PgDn", "down": "↓", "up": "↑", "delete": "Del"}


def pretty(key: str) -> str:
    return "+".join(KEY_NAMES.get(p) or (p.capitalize() if len(p) > 1 else p.upper()) for p in key.split("+"))


def ts_time(ts: str) -> datetime:
    return datetime.fromtimestamp(float(ts))


def day_label(d: date) -> str:
    today = date.today()
    if d == today:
        return "Today"
    if d == today - timedelta(days=1):
        return "Yesterday"
    return d.strftime("%A %-d %B" + (" %Y" if d.year != today.year else ""))


def name_color(uid: str) -> str:
    return NAME_COLORS[zlib.crc32((uid or "").encode()) % len(NAME_COLORS)]


def size(n: int) -> str:
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def rank(items: list[dict], query: str, limit=80) -> list[dict]:
    """items: {"text", "boost"}. Every word must appear; word starts and earlier hits rank higher."""
    q = fold(query).split()
    if not q:
        return sorted(items, key=lambda it: -it.get("boost", 0))[:limit]
    scored = []
    for it in items:
        f = it.get("fold") or fold(it["text"])
        score = it.get("boost", 0) / 1000
        for w in q:
            i = f.find(w)
            if i < 0:
                break
            score += 10 - min(i, 9) * 0.5 + (5 if i == 0 or not f[i - 1].isalnum() else 0)
        else:
            scored.append((score, it))
    scored.sort(key=lambda x: -x[0])
    return [it for _, it in scored[:limit]]


def highlight(text: str, query: str, base="") -> Text:
    t = Text(text, style=base)
    f = fold(text)
    for w in fold(query).split():
        start = 0
        while (i := f.find(w, start)) >= 0:
            t.stylize("bold underline", i, i + len(w))
            start = i + len(w)
    return t


# ------------------------------------------------------------ modals

class Picker(ModalScreen):
    """Type to filter, ↑↓ to choose, Enter to pick."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Close"),
                Binding("up", "move(-1)", show=False, priority=True),
                Binding("down", "move(1)", show=False, priority=True),
                Binding("pageup", "move(-10)", show=False, priority=True),
                Binding("pagedown", "move(10)", show=False, priority=True)]

    def __init__(self, title: str, source, placeholder="Type to search…", query=""):
        super().__init__()
        self.title_text, self.source, self.placeholder, self.query_text = title, source, placeholder, query
        self.items: list[dict] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog picker"):
            yield Static(self.title_text, classes="dialog-title")
            yield Input(self.query_text, placeholder=self.placeholder)
            yield OptionList()

    def on_mount(self):
        self.refill(self.query_text)

    def refill(self, query: str):
        ol = self.query_one(OptionList)
        self.items = self.source(query)
        opts = []
        for i, it in enumerate(self.items):
            t = it.get("prefix", Text()) + (it["label"] if "label" in it else highlight(it["text"], query))
            if it.get("detail"):
                t += Text("  ") + (it["detail"] if isinstance(it["detail"], Text) else Text(it["detail"], "dim"))
            opts.append(Option(t, id=str(i)))
        ol.set_options(opts)
        if opts:
            ol.highlighted = 0

    @on(Input.Changed)
    def changed(self, ev: Input.Changed):
        self.refill(ev.value)

    def action_move(self, d: int):
        ol = self.query_one(OptionList)
        if ol.option_count:
            ol.highlighted = max(0, min(ol.option_count - 1, (ol.highlighted or 0) + d))

    @on(Input.Submitted)
    def submitted(self):
        ol = self.query_one(OptionList)
        if ol.highlighted is not None and self.items:
            self.dismiss(self.items[ol.highlighted])

    @on(OptionList.OptionSelected)
    def selected(self, ev: OptionList.OptionSelected):
        self.dismiss(self.items[int(ev.option.id)])


class Prompt(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, title: str, value="", note=""):
        super().__init__()
        self.title_text, self.value, self.note = title, value, note

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.title_text, classes="dialog-title")
            yield Input(self.value)
            if self.note:
                yield Static(self.note, classes="note")

    @on(Input.Submitted)
    def submitted(self, ev: Input.Submitted):
        self.dismiss(ev.value)


class Confirm(ModalScreen):
    BINDINGS = [Binding("escape,n", "dismiss(False)", "No"), Binding("enter,y", "dismiss(True)", "Yes")]

    def __init__(self, question: str):
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.question)
            yield Static("Enter yes  ·  Esc no", classes="note")


class Help(ModalScreen):
    BINDINGS = [Binding("escape,f1,enter", "dismiss", "Close")]

    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def compose(self) -> ComposeResult:
        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold", no_wrap=True)
        t.add_column()
        for k, d in self.rows:
            t.add_row(k, d)
        with Vertical(classes="dialog"):
            yield Static("Keys", classes="dialog-title")
            yield Static(t)


# ------------------------------------------------------------ chat

class MsgList(OptionList):
    """The messages; ↑↓ select one, typing goes to the composer."""

    BINDINGS = [Binding("enter", "select", show=False)]

    async def _on_click(self, event: events.Click):
        """One click selects a message, a double click opens its thread."""
        event.stop()
        event.prevent_default()                     # not OptionList's: it opens on one click
        i = event.style.meta.get("option")
        if i is None:
            return
        self.focus()
        self.highlighted = i
        if event.chain >= 2:
            self.action_select()

    def action_cursor_up(self):
        if (self.highlighted or 0) == 0:
            self.screen.load_older()
        super().action_cursor_up()

    def action_cursor_down(self):
        if self.highlighted is None or self.highlighted >= self.option_count - 1:
            self.screen.query_one(Composer).focus()
            return
        super().action_cursor_down()

    async def _on_key(self, event: events.Key):
        if event.is_printable and event.character:
            event.stop()
            event.prevent_default()
            c = self.screen.query_one(Composer)
            c.focus()
            c.insert(event.character)
        elif event.key == "escape":
            event.stop()
            if isinstance(self.screen, ChatScreen) and self.screen.thread:
                self.app.pop_screen()
            else:
                self.screen.query_one(Composer).focus()


class Composer(TextArea):
    """Enter sends, Shift+Enter (or Ctrl+J) is a new line, Tab completes @name #channel :emoji:."""

    async def _on_key(self, event: events.Key):
        key = event.key
        if key == "enter":
            event.stop()
            event.prevent_default()
            self.screen.send()
            return
        if key == "ctrl+a":
            event.stop()
            event.prevent_default()
            self.select_all()
            return
        if key in ("shift+enter", "ctrl+j"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if key == "tab":
            event.stop()
            event.prevent_default()
            self.screen.complete()
            return
        if key == "up" and self.cursor_location[0] == 0 and not self.text.strip():
            event.stop()
            event.prevent_default()
            ml = self.screen.query_one(MsgList)
            if ml.option_count:
                ml.focus()
                ml.highlighted = ml.option_count - 1
            return
        if key == "escape":
            event.stop()
            event.prevent_default()
            s = self.screen
            if s.editing:
                s.stop_editing()
            elif s.thread:
                self.app.pop_screen()
            return
        await super()._on_key(event)


class ChatScreen(Screen):
    """A conversation (thread=None) or one thread in it."""

    def __init__(self, cid: str | None, thread: str | None = None):
        super().__init__()
        self.cid, self.thread = cid, thread
        self.limit = 300
        self.msgs: list[dict] = []
        self.pending: list[dict] = []
        self.shown: tuple = (None, [], [])  # (conversation/thread, ids, prompts) on screen
        self.since: str | None = None     # oldest message shown; newer ones never push it out
        self.editing: dict | None = None
        self.new_from = "9"               # first unread when the conversation was opened
        self.hold_read = False            # after "mark unread": don't mark read again until you leave
        self.links: dict[str, list[str]] = {}
        self._mark_timer = None

    def compose(self) -> ComposeResult:
        yield Static(id="top")
        yield MsgList(id="msgs")
        yield Static(id="editing")
        yield Composer(id="composer", soft_wrap=True, show_line_numbers=False, tab_behavior="indent",
                       highlight_cursor_line=False)
        yield Static(id="status")

    def on_mount(self):
        self.query_one("#editing").display = False
        if self.cid:
            self.open(self.cid)
        self.query_one(Composer).focus()

    # -- data

    def open(self, cid: str, select_ts: str | None = None):
        app: SlackApp = self.app
        self.cid = cid
        self.limit = 300
        self.pending, self.hold_read = [], False
        self.shown, self.since = (None, [], []), None
        self.stop_editing()
        if not self.thread:
            self.new_from = self.first_unread()
            app.db.visited(cid, time.time())
            app.db.put("last_conv", cid)
        self.paint(select_ts=select_ts)
        self.query_one(Composer).placeholder = (
            "Reply…" if self.thread else f"Message {app.name_of(cid)}")
        if self.thread:
            app.submit("bg", lambda: app.db.put_msgs(cid, app.api.replies(cid, self.thread), thread=self.thread),
                       lambda _: self.paint())
        else:
            app.submit("bg", lambda: app.db.put_msgs(cid, app.api.history(cid, limit=app.cfg.history), window=True),
                       lambda _: self.paint())
        app.heartbeat(force=True)

    def first_unread(self) -> str:
        lr = self.app.db.last_read(self.cid)
        me = self.app.me
        for m in self.app.db.msgs(self.cid, 300):
            if m["ts"] > lr and m.get("user") != me:
                return m["ts"]
        return "9"

    def load_older(self):
        if self.thread or not self.msgs:
            return
        app: SlackApp = self.app
        oldest, cid = self.msgs[0]["ts"], self.cid

        def fetch():
            msgs = app.api.history(cid, limit=100, latest=oldest)
            app.db.put_msgs(cid, msgs)
            return min((m["ts"] for m in msgs), default=None)

        def done(first):
            if first and self.cid == cid:
                self.since = first
                self.paint(select_ts=oldest)
        self.notify("Loading older messages…", timeout=2)
        app.submit("bg", fetch, done)

    # -- painting

    def paint(self, select_ts: str | None = None):
        """Show what the database has. Only what changed is touched: messages appended,
        edited or removed at the end are updated in place, so the view never jumps."""
        if not self.cid or not self.is_mounted:
            return
        app: SlackApp = self.app
        db = app.db
        if self.thread:
            self.msgs = db.thread(self.cid, self.thread)
        elif self.since:
            self.msgs = db.msgs_from(self.cid, self.since)
        else:
            self.msgs = db.msgs(self.cid, self.limit)
            self.since = self.msgs[0]["ts"] if self.msgs else None
        rows = self.msgs + [p for p in self.pending if p["_cid"] == self.cid]
        self.paint_top()
        self.links = {}
        prompts, prev = [], None
        for m in rows:
            prompts.append(self.render_msg(m, prev))
            prev = m
        ids = [m["ts"] for m in rows]
        where, old_ids, old_prompts = self.shown
        self.shown = ((self.cid, self.thread), ids, prompts)
        ml = self.query_one(MsgList)
        at_end = ml.scroll_y >= ml.max_scroll_y - 1 or not ml.option_count
        k = 0
        while k < min(len(ids), len(old_ids)) and ids[k] == old_ids[k]:
            k += 1
        full = (select_ts or where != (self.cid, self.thread) or k == 0
                or (self.thread and (len(old_ids) > 1) != (len(ids) > 1)))
        if not full:
            if ids == old_ids and prompts == old_prompts:
                self.schedule_mark()
                return
            with app.batch_update():
                for i in range(k):
                    if prompts[i] != old_prompts[i]:
                        ml.replace_option_prompt_at_index(i, prompts[i])
                for i in range(len(old_ids) - 1, k - 1, -1):
                    ml.remove_option_at_index(i)
                if len(ids) > k:
                    ml.add_options([Option(prompts[i], id=ids[i]) for i in range(k, len(ids))])
            if at_end:
                self.call_after_refresh(ml.scroll_end, animate=False)
            self.schedule_mark()
            return
        keep = select_ts
        if not keep and ml.highlighted is not None and ml.option_count:
            keep = ml.get_option_at_index(ml.highlighted).id
        scroll = ml.scroll_y
        opts = [Option(p, id=i) for p, i in zip(prompts, ids)]
        if self.thread and len(rows) > 1:
            opts.insert(1, None)
        ml.set_options(opts)
        if keep in ids:
            ml.highlighted = ids.index(keep)        # separators don't count in the index
        if select_ts and keep in ids:
            self.call_after_refresh(ml.scroll_to_highlight, top=True)
        elif at_end or where != (self.cid, self.thread):
            ml.scroll_end(animate=False, immediate=True)
            self.call_after_refresh(ml.scroll_end, animate=False)
        else:
            self.call_after_refresh(ml.scroll_to, y=scroll, animate=False)
        self.schedule_mark()

    def render_msg(self, m: dict, prev: dict | None) -> Text:
        app: SlackApp = self.app
        users, me = app.users, app.me
        t = Text()
        when = ts_time(m["ts"]) if not m.get("_pending") else datetime.now()
        new_day = not prev or prev.get("_pending") or ts_time(prev["ts"]).date() != when.date()
        rule = bool(self.thread and prev and prev["ts"] == self.thread)    # under the thread's separator
        if new_day and not m.get("_pending"):
            label = f"── {day_label(when.date())} "
            t.append(label, "bold dim")
            t.append("─" * (RULE - len(label)) + "\n", "dim")
            rule = True
        if m["ts"] == self.new_from and not self.thread:
            t.append("── new " + "─" * (RULE - 7) + "\n", "#f87168")
            rule = True
        same = (prev and not rule and prev.get("user") == m.get("user")
                and author(prev, users) == author(m, users) and not prev.get("_pending")
                and float(m["ts"]) - float(prev["ts"]) < GROUP_GAP and m.get("subtype") != "thread_broadcast")
        if not same:
            if prev and not rule:
                t.append("\n")
            name = author(m, users)
            t.append(name, f"bold {name_color(m.get('user') or name)}")
            t.append("  " + when.strftime("%H:%M"), "dim")
            if m.get("subtype") == "thread_broadcast" and not self.thread:
                t.append("  replied to a thread", "dim italic")
            t.append("\n")
        links: list[str] = []
        sub = m.get("subtype")
        if sub in ("channel_join", "channel_leave", "group_join", "group_leave", "channel_topic",
                   "channel_purpose", "channel_name"):
            body = render(m.get("text", ""), users, app.convs, me, app.custom_emoji, links)
            body.stylize("dim italic")
            t.append_text(body)
        elif has_layout(m.get("blocks")):
            if any(b.get("type") == "rich_text" for b in m["blocks"]):
                t.append_text(render(m.get("text", ""), users, app.convs, me, app.custom_emoji, links))
                t.append("\n")
            t.append_text(render_blocks(m["blocks"], users, app.convs, me, app.custom_emoji, links))
        else:
            body = render(m.get("text", ""), users, app.convs, me, app.custom_emoji, links)
            if m.get("_pending"):
                body.stylize("dim")
            t.append_text(body)
        if m.get("edited"):
            t.append(" (edited)", "dim")
        for a in m.get("attachments", []) or []:
            title = a.get("title") or a.get("fallback") or ""
            if a.get("title_link"):
                links.append(a["title_link"])
            text = a.get("text") or ""
            if title or text:
                t.append("\n")
                if title:
                    t.append("▎ ", a.get("color") and f"#{a['color'].lstrip('#')}" or "dim")
                    t.append(title, "bold")
                if text:
                    for line in render(text, users, app.convs, me, app.custom_emoji, links).split("\n"):
                        t.append("\n▎ ", "dim")
                        t.append_text(line)
        for f in m.get("files", []) or []:
            if f.get("mode") == "tombstone":
                continue
            mime = f.get("mimetype") or ""
            pic = thumbs.render(f, max(16, self.size.width - 8))
            if pic is not None:
                t.append("\n")
                t.append_text(pic)
                app.fetch_thumb(f)
            icon = "▶ " if mime.startswith("video/") else "🖼 " if mime.startswith("image/") else "📎 "
            t.append(f"\n{icon}{f.get('name') or f.get('title') or 'file'}", "#6cb6ff")
            if f.get("size"):
                t.append(f"  {size(f['size'])}", "dim")
            if pic is not None:
                t.append(f"  {pretty(app.keys['open'])} open", "dim")
        if m.get("_pending"):
            t.append("  sending…" if not m.get("_failed") else "  not sent", "dim" if not m.get("_failed") else "red")
        if m.get("reactions"):
            t.append("\n")
            for r in m["reactions"]:
                mine = me in r.get("users", [])
                t.append(f" {emoji_char(r['name'], app.custom_emoji)} {r.get('count', len(r.get('users', [])))} ",
                         "bold black on #6cb6ff" if mine else "on #333a45")
                t.append(" ")
        if not self.thread and m.get("reply_count") and is_top(m) and m.get("subtype") != "thread_broadcast":
            n = m["reply_count"]
            who = ", ".join(user_name(u, users, short=True) for u in (m.get("reply_users") or [])[:3])
            t.append(f"\n💬 {n} {'reply' if n == 1 else 'replies'}", "bold #6cb6ff")
            last = m.get("latest_reply")
            t.append(f"  {who}" + (f" · last {self.short_when(last)}" if last else ""), "dim")
        self.links[m["ts"]] = links
        return t

    @staticmethod
    def short_when(ts: str) -> str:
        d = ts_time(ts)
        return d.strftime("%H:%M") if d.date() == date.today() else d.strftime("%-d %b %H:%M")

    def paint_top(self):
        app: SlackApp = self.app
        c = app.convs.get(self.cid or "", {})
        name = app.name_of(self.cid) if self.cid else "Slack"
        t = Text(" ")
        if self.thread:
            t.append("Thread", "bold")
            t.append(f" in {name}", "dim")
        else:
            t.append(name, "bold")
            topic = ((c.get("topic") or {}).get("value") or "").replace("\n", " ")
            if topic:
                t.append("  " + topic[:80], "dim")
        others = {k: v for k, v in app.counts.items() if k != self.cid}
        mentions = sum(m for _, m in others.values())
        right = Text()
        if mentions:
            right.append(f" @{mentions} ", "bold black on #e8a33d")
            right.append(" ")
        if others:
            right.append(f"{len(others)} unread", "bold")
            right.append(f" · {pretty(app.keys['next_unread'])}  ", "dim")
        right.append(app.status_text(), "dim")
        pad = max(1, self.size.width - t.cell_len - right.cell_len - 1)
        self.query_one("#top", Static).update(t + Text(" " * pad) + right)

    # -- reading

    def schedule_mark(self):
        if self._mark_timer:
            self._mark_timer.stop()
        self._mark_timer = self.set_timer(MARK_AFTER, self.mark_read)

    def mark_read(self):
        app: SlackApp = self.app
        if self.thread or self.hold_read or not self.cid or not self.msgs or not app.app_focused:
            return
        if app.screen is not self:
            return
        ml = self.query_one(MsgList)
        if ml.scroll_y < ml.max_scroll_y - 1:
            return
        newest = self.msgs[-1]["ts"]
        if newest <= app.db.last_read(self.cid):
            return
        cid = self.cid
        app.db.set_last_read(cid, newest)
        app.refresh_counts()
        self.paint_top()
        app.submit("fg", lambda: app.api.mark(cid, newest), error=lambda e: None)

    def on_resize(self):
        self.paint_top()

    def on_descendant_focus(self, _):
        self.hints()

    def hints(self):
        k = {n: pretty(v) for n, v in self.app.keys.items()}
        if self.query_one(MsgList).has_focus:
            bits = ["Enter/double click " + ("reply" if self.thread else "thread"), f"{k['react']} react",
                    f"{k['copy']} copy", f"{k['edit']} edit",
                    f"{k['delete']} delete", f"{k['open']} open", f"{k['mark_unread']} unread", "Esc type"]
        else:
            bits = [f"{k['palette']} go to", f"{k['next_unread']} next unread", "↑ messages",
                    f"{k['react']} react", f"{k['paste']} paste image"]
        bits.insert(0, f"{k['help']} keys")
        if self.thread:
            bits.insert(0, "Esc back")
        bits.append(f"{k['prev_thread']}/{k['next_thread'].split('+')[-1]} threads")
        bits = [b for b in bits if b]
        self.query_one("#status", Static).update(Text(" · ".join(bits), "dim"))

    # -- the selected message

    def selected(self) -> dict | None:
        ml = self.query_one(MsgList)
        if ml.highlighted is None or not ml.option_count or not ml.has_focus:
            return None
        oid = ml.get_option_at_index(ml.highlighted).id
        return next((m for m in self.msgs if m["ts"] == oid), None)

    @on(OptionList.OptionSelected, "#msgs")
    def open_thread(self, ev: OptionList.OptionSelected):
        m = next((m for m in self.msgs if m["ts"] == ev.option.id), None)
        if not m or self.thread:
            self.query_one(Composer).focus()
            return
        root = m.get("thread_ts") if m.get("subtype") == "thread_broadcast" else m["ts"]
        self.app.push_screen(ChatScreen(self.cid, thread=root))

    def action_react(self):
        m = self.selected() or (self.msgs[-1] if self.msgs else None)
        if not m:
            return
        app: SlackApp = self.app
        cid = self.cid

        def source(q):
            mine = {r["name"] for r in m.get("reactions", []) if app.me in r.get("users", [])}
            names = list(dict.fromkeys(app.db.get("recent_emoji", []) + popular()))
            if q:
                allnames = list(emoji_names()) + list(app.custom_emoji)
                names = [n for n in allnames if fold(q.strip(":")) in n][:200]
                names.sort(key=lambda n: (not n.startswith(fold(q.strip(":"))), len(n)))
            return [{"text": n, "label": Text(f"{emoji_char(n, app.custom_emoji)}  :{n}:"),
                     "detail": "✓ yours" if n in mine else "", "name": n} for n in names[:80]]

        def chosen(it):
            if not it:
                return
            name = it["name"]
            add = not any(r["name"] == name and app.me in r.get("users", []) for r in m.get("reactions", []))
            app.db.update_msg(cid, m["ts"], lambda x: sync.react(x, name, app.me, add))
            recent = [name] + [n for n in app.db.get("recent_emoji", []) if n != name]
            app.db.put("recent_emoji", recent[:12])
            self.paint()
            app.submit("fg", lambda: app.api.react(cid, m["ts"], name, add),
                       error=app.fail("Reaction", lambda: self.reload()))
        what = render(m.get("text", ""), app.users, app.convs, app.me).plain.replace("\n", " ")[:50]
        self.app.push_screen(Picker(f"React to {author(m, app.users)}: {what}", source, "Emoji name…"), chosen)

    def action_edit(self):
        m = self.selected()
        if not m:
            return
        if m.get("user") != self.app.me:
            return self.notify("You can only edit your own messages", timeout=3)
        self.editing = m
        c = self.query_one(Composer)
        c.text = from_slack(m.get("text", ""), self.app.users, self.app.convs)
        c.move_cursor(c.document.end)
        bar = self.query_one("#editing", Static)
        bar.update(Text(" Editing message · Enter save · Esc cancel", "bold black on #e8a33d"))
        bar.display = True
        c.focus()

    def stop_editing(self):
        if self.editing:
            self.editing = None
            self.query_one(Composer).text = ""
        self.query_one("#editing").display = False

    def action_delete(self):
        m = self.selected()
        if not m:
            return
        if m.get("user") != self.app.me:
            return self.notify("You can only delete your own messages", timeout=3)
        app: SlackApp = self.app
        cid = self.cid

        def go(yes):
            if not yes:
                return
            app.db.delete_msg(cid, m["ts"])
            self.paint()
            app.submit("fg", lambda: app.api.delete(cid, m["ts"]), error=app.fail("Delete", self.reload))
        app.push_screen(Confirm("Delete this message?"), go)

    def action_open(self):
        m = self.selected() or (self.msgs[-1] if self.msgs else None)
        if not m:
            return
        app: SlackApp = self.app
        items = [{"text": f.get("name") or "file", "file": f, "prefix": Text("📎 ")}
                 for f in m.get("files", []) or [] if f.get("url_private")]
        items += [{"text": u, "url": u, "prefix": Text("🔗 ")} for u in self.links.get(m["ts"], [])]
        if not items:
            return self.notify("Nothing to open in this message", timeout=3)

        def go(it):
            if not it:
                return
            if "url" in it:
                return app.open_url(it["url"])
            f = it["file"]
            dest = CACHE_DIR / "files" / f"{f['id']}-{f.get('name') or 'file'}"
            if dest.exists():
                return app.open_url(str(dest))
            self.notify(f"Downloading {f.get('name')}…", timeout=3)
            app.submit("bg", lambda: app.api.download(f["url_private_download"] if f.get("url_private_download")
                                                      else f["url_private"], dest),
                       lambda p: app.open_url(str(p)), app.fail("Download"))
        if len(items) == 1:
            go(items[0])
        else:
            app.push_screen(Picker("Open", lambda q: rank(items, q)), go)

    def action_browser(self):
        m = self.selected() or (self.msgs[-1] if self.msgs else None)
        if not m:
            return
        app: SlackApp = self.app
        app.submit("bg", lambda: app.api.permalink(self.cid, m["ts"]), app.open_url, app.fail("Link"))

    def action_copy(self):
        """Copy the selected text in the box, or the selected message."""
        app: SlackApp = self.app
        c = self.query_one(Composer)
        if c.has_focus:
            text = c.selected_text
            if not text:
                return
        else:
            m = self.selected()
            if not m:
                return
            text = render(m.get("text", ""), app.users, app.convs, app.me).plain
            files = [f.get("name") for f in m.get("files", []) or [] if f.get("name")]
            text = "\n".join([text] + files).strip()
        try:
            subprocess.run(["wl-copy"], input=text, text=True, timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            app.copy_to_clipboard(text)
        self.notify("Copied", timeout=1.5)

    def action_mark_unread(self):
        m = self.selected()
        if not m or self.thread:
            return self.notify("Select the first message that should be unread", timeout=3)
        app: SlackApp = self.app
        i = self.msgs.index(m)
        before = self.msgs[i - 1]["ts"] if i else f"{float(m['ts']) - 0.000001:.6f}"
        cid = self.cid
        app.db.set_last_read(cid, before)
        self.hold_read = True
        self.new_from = m["ts"]
        self.shown = (None, [], [])
        app.refresh_counts()
        self.paint()
        app.submit("fg", lambda: app.api.mark(cid, before), error=app.fail("Mark unread"))

    def reload(self):
        self.open(self.cid)

    def action_thread(self, d: int):
        """In a channel: select the previous/next message with a thread. In a thread: go to that thread."""
        app: SlackApp = self.app
        main = app.main() if self.thread else self
        if not main:
            return
        roots = [m["ts"] for m in main.msgs if m.get("reply_count") and is_top(m)
                 and m.get("subtype") != "thread_broadcast"]
        ml = main.query_one(MsgList)
        if self.thread:
            here = self.thread
        elif ml.highlighted is not None and ml.has_focus and ml.option_count:
            here = ml.get_option_at_index(ml.highlighted).id
        else:
            here = "9"                                  # from the end
        target = next((r for r in (reversed(roots) if d < 0 else roots) if (r < here if d < 0 else r > here)), None)
        if not target:
            return self.notify("No more threads" if roots else "No threads here", timeout=2)
        ids = [m["ts"] for m in main.msgs]
        if self.thread:
            app.pop_screen()
            ml.highlighted = ids.index(target) if target in ids else ml.highlighted
            app.push_screen(ChatScreen(self.cid, thread=target))
        else:
            ml.focus()
            ml.highlighted = ids.index(target)
            ml.scroll_to_highlight()

    # -- writing

    def send(self):
        c = self.query_one(Composer)
        raw = c.text.strip()
        if not raw or not self.cid:
            return
        app: SlackApp = self.app
        text = to_slack(raw, app.users, app.convs)
        cid, thread = self.cid, self.thread
        if self.editing:
            m = self.editing
            self.stop_editing()
            c.text = ""
            app.db.update_msg(cid, m["ts"], lambda x: x.update(text=text, edited={"user": app.me}))
            self.paint()
            app.submit("fg", lambda: app.api.edit(cid, m["ts"], text), error=app.fail("Edit", self.reload))
            return
        if raw.startswith("/") and not raw.startswith("//"):
            return self.notify("Slash commands aren't available outside the Slack app", severity="warning")
        c.text = ""
        p = {"ts": f"9{next(_pending):015d}", "_cid": cid, "_pending": True, "user": app.me, "text": text}
        if thread:
            p["thread_ts"] = thread
        self.pending.append(p)
        self.paint()
        self.call_after_refresh(self.query_one(MsgList).scroll_end, animate=False)

        def post():
            m = app.api.post(cid, text, thread_ts=thread)
            app.db.put_msgs(cid, [m])
            if thread:
                app.db.update_msg(cid, thread, lambda parent: sync.bump(parent, m))
            else:
                app.db.set_last_read(cid, m["ts"], only_forward=True)
            return m

        def done(_):
            self.pending.remove(p)
            self.paint()

        def failed(e):
            p["_failed"] = True
            self.paint()
            self.notify(f"Not sent: {e}", severity="error", timeout=8)
            if not c.text:
                self.pending.remove(p)
                c.text = raw
                self.paint()
        app.submit("fg", post, done, failed)

    def complete(self):
        """Tab: finish the @name, #channel or :emoji: before the cursor."""
        app: SlackApp = self.app
        c = self.query_one(Composer)
        row, col = c.cursor_location
        line = c.document.get_line(row)[:col]
        word = line.split(" ")[-1] if line else ""
        if not word or word[0] not in "@#:" or len(word) < 2:
            return
        q, kind = word[1:], word[0]
        if kind == "@":
            items = [{"text": user_name(u["id"], app.users), "insert": "@" + user_name(u["id"], app.users) + " ",
                      "detail": (u.get("profile") or {}).get("real_name", "")}
                     for u in app.users.values() if not u.get("deleted") and not u.get("is_bot")
                     and u["id"] != "USLACKBOT"]
            items += [{"text": n, "insert": f"@{n} "} for n in ("here", "channel")]
        elif kind == "#":
            items = [{"text": c2["name"], "insert": f"#{c2['name']} "} for c2 in app.convs.values()
                     if c2.get("name") and not c2.get("is_im") and not c2.get("is_mpim")]
        else:
            fav = {n: i for i, n in enumerate(app.db.get("recent_emoji", []) + popular())}
            names = [n for n in list(emoji_names()) + list(app.custom_emoji) if n.startswith(fold(q))]
            names.sort(key=lambda n: (fav.get(n, 999), len(n)))
            items = [{"text": n, "insert": f":{n}: ", "label": Text(f"{emoji_char(n, app.custom_emoji)}  :{n}:")}
                     for n in names[:80]]
        hits = rank(items, q) if kind != ":" else items

        def put(it):
            if it:
                c.replace(it["insert"], (row, col - len(word)), (row, col))
            c.focus()
        if len(hits) == 1:
            put(hits[0])
        elif hits:
            self.app.push_screen(Picker("Complete", lambda qq: rank(items, qq) if kind != ":" else
                                        [i for i in items if fold(qq) in i["text"]], query=q), put)


# ------------------------------------------------------------ app

class SlackApp(App):
    CSS = """
    Screen { background: ansi_default; }
    #top { height: 1; background: ansi_default; }
    #msgs { height: 1fr; border: none; background: ansi_default; padding: 0 1;
            border-top: solid $panel-lighten-2; scrollbar-size-vertical: 1; }
    #msgs:focus { border: none; border-top: solid $panel-lighten-2; }
    #msgs > .option-list--option { padding: 0 1; }
    #msgs > .option-list--option-highlighted { background: ansi_default; text-style: none; }
    #msgs:focus > .option-list--option-highlighted { background: #262c36; color: $foreground; text-style: none; }
    #msgs > .option-list--separator { color: $panel-lighten-2; }
    #editing { height: 1; }
    #composer { height: auto; min-height: 3; max-height: 12; border: round $panel-lighten-2;
                background: ansi_default; padding: 0 1; }
    #composer:focus { border: round $accent; }
    #composer > .text-area--selection { background: ansi_blue; color: ansi_black; }
    #status { height: 1; color: $text-muted; padding: 0 1; }
    .dialog { width: 80; max-width: 95%; height: auto; max-height: 85%; border: round $accent;
              background: $surface; padding: 1 2; }
    .dialog OptionList { height: auto; max-height: 24; border: none; background: $surface; }
    .dialog OptionList > .option-list--option-highlighted { background: ansi_blue; color: ansi_black; text-style: none; }
    Input > .input--selection { background: ansi_blue; color: ansi_black; }
    .dialog Input { background: $surface; border: tall $panel; }
    .dialog-title { text-style: bold; padding-bottom: 1; }
    .note { color: $text-muted; padding-top: 1; }
    ModalScreen { align: center middle; }
    """
    ENABLE_COMMAND_PALETTE = False

    def __init__(self, cfg: Config, db: Db | None = None, api: Slack | None = None, listen=True):
        super().__init__()
        self.cfg, self.keys = cfg, cfg.keys
        self.db = db or Db()
        self.api = api or Slack(cfg.user_token)
        self.listen = listen
        self._pools = {"fg": ThreadPoolExecutor(1, "slack-fg"), "bg": ThreadPoolExecutor(1, "slack-bg"),
                       "img": ThreadPoolExecutor(2, "slack-img")}
        self._thumbs: set[str] = set()
        self.inflight = 0
        self.app_focused = True
        self.error = ""
        self.lock = None
        self.version = -1
        self.users, self.convs, self.custom_emoji = {}, {}, {}
        self.counts: dict[str, tuple[int, int]] = {}
        self._beat = (None, 0.0)
        self.load_names()

    @property
    def me(self) -> str:
        return (self.db.get("me") or {}).get("user_id", "")

    def load_names(self):
        self.users, self.convs = self.db.users(), self.db.convs()
        self.custom_emoji = self.db.get("emoji", {})

    def name_of(self, cid: str) -> str:
        c = self.convs.get(cid)
        return conv_name(c, self.users, self.me) if c else cid

    def refresh_counts(self):
        self.counts = sync.counts(self.db, self.cfg, self.me)
        sync.write_unread(self.db, self.cfg, self.me)

    # -- lanes

    def submit(self, lane: str, fn, done=None, error=None):
        if lane == "fg":
            self.inflight += 1
        error = error or self.fail()

        def job():
            try:
                res = fn()
            except Exception as e:  # noqa: BLE001 - shown in the UI, always logged
                sync.log_error(lane)
                self.call_from_thread(error, e)
                return
            finally:
                if lane == "fg":
                    self.inflight -= 1
            self.error = ""
            if done:
                self.call_from_thread(done, res)
        self._pools[lane].submit(job)

    def fail(self, what="Slack", then=None):
        def report(e):
            self.error = str(e)
            self.notify(f"{what}: {e}", severity="error", timeout=8)
            if then:
                then()
        return report

    def status_text(self) -> str:
        bits = []
        if self.inflight:
            bits.append(f"↑{self.inflight}")
        st = sync.listener_state()
        if self.error:
            bits.append("! " + self.error[:30])
        elif st.get("state") == "connected":
            bits.append("● live")
        else:
            bits.append("○ offline")
        return " ".join(bits)

    def fetch_thumb(self, f: dict):
        """Download a file's thumbnail once; the message repaints in place when it's here."""
        p = thumbs.path(f)
        if p.exists() or f["id"] in self._thumbs:
            return
        self._thumbs.add(f["id"])
        src = thumbs.source(f)
        self.submit("img", lambda: self.api.download(src[0], p), lambda _: self.repaint(), lambda e: None)

    def open_url(self, url):
        if url:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)

    # -- lifecycle

    def on_mount(self):
        self.theme = "ansi-dark"
        k = self.keys
        for key, action, desc in [
            (k["palette"], "palette", "Go to"), (k["palette2"], "palette", "Go to"),
            (k["next_unread"], "next_unread", "Next unread"), (k["search"], "search", "Search"),
            (k["attach"], "attach", "Attach"), (k["refresh"], "refresh", "Refresh"), (k["help"], "help", "Help"),
            (k["react"], "msg('react')", "React"), (k["edit"], "msg('edit')", "Edit"),
            (k["open"], "msg('open')", "Open"), (k["browser"], "msg('browser')", "Browser"),
            (k["mark_unread"], "msg('mark_unread')", "Mark unread"), (k["paste"], "paste", "Paste"),
            (k["copy"], "msg('copy')", "Copy"),
            (k["prev_thread"], "msg('thread(-1)')", "Previous thread"),
            (k["next_thread"], "msg('thread(1)')", "Next thread"),
        ]:
            self._bindings.bind(key, action, desc, show=False, priority=True)
        self._bindings.bind(k["delete"], "msg('delete')", "Delete", show=False)
        if self.listen:
            self.lock = sync.take_lock(wait=False)
            if self.lock:
                import threading
                s = sync.Sync(self.cfg, report=lambda *_: None)
                threading.Thread(target=s.run, daemon=True).start()
        cid = self.db.get("last_conv")
        if cid not in self.convs:
            cid = next(iter(sorted(self.convs, key=lambda c: -self.convs[c]["_visited"])), None)
        self.refresh_counts()
        self.push_screen(ChatScreen(cid))
        if not self.me:
            self.submit("bg", lambda: self.db.put("me", self.api.auth()), lambda _: self.repaint(names=True))
        if not self.users or not self.convs:
            self.notify("First start: fetching your Slack…", timeout=5)
            self.submit("bg", self.first_fetch, lambda _: self.first_done())
        self.set_interval(0.3, self.watch_db)
        self.set_interval(10, self.heartbeat)
        self.set_interval(5, lambda: self.screen.paint_top() if isinstance(self.screen, ChatScreen) else None)

    def first_fetch(self):
        s = sync.Sync(self.cfg, db=self.db, api=self.api)
        s.refresh_users()
        s.refresh_convs()

    def first_done(self):
        self.repaint(names=True)
        s = self.main()
        if s and not s.cid and self.convs:
            general = next((c for c, v in self.convs.items() if v.get("is_general")), next(iter(self.convs)))
            s.open(general)

    def main(self) -> ChatScreen | None:
        return next((s for s in self.screen_stack if isinstance(s, ChatScreen) and not s.thread), None)

    def watch_db(self):
        v = self.db.data_version()
        if v == self.version:
            return
        self.version = v
        goto = self.db.get("goto")
        if goto and time.time() - goto.get("at", 0) < 60:
            self.db.put("goto", None)
            self.goto(goto["cid"], goto.get("ts"), goto.get("thread"))
        self.repaint(names=True)

    def repaint(self, names=False):
        if names:
            self.load_names()
        self.refresh_counts()
        for s in self.screen_stack:
            if isinstance(s, ChatScreen) and s.is_mounted:
                s.paint()

    def heartbeat(self, force=False):
        """Tell the daemon what you're looking at, so it doesn't notify you about it."""
        s = self.screen if isinstance(self.screen, ChatScreen) else None
        now = (s.cid if s else None, s.thread if s else None, self.app_focused)
        if force or now != self._beat[0] or time.time() - self._beat[1] > 20:
            self._beat = (now, time.time())
            self.db.put("viewing", {"cid": now[0], "thread": now[1], "focused": now[2], "at": time.time()})

    def on_app_focus(self, _):
        self.app_focused = True
        self.heartbeat(force=True)
        if isinstance(self.screen, ChatScreen):
            self.screen.schedule_mark()

    def on_app_blur(self, _):
        self.app_focused = False
        self.heartbeat(force=True)

    # -- navigation

    def goto(self, cid: str, ts: str | None = None, thread: str | None = None):
        if cid not in self.convs:
            self.load_names()
        while len(self.screen_stack) > 2:
            self.pop_screen()
        s = self.main()
        if not s:
            return
        if ts and not thread and not any(m["ts"] == ts for m in self.db.msgs(cid, 2000)):
            def fetch():
                self.db.put_msgs(cid, self.api.history(cid, latest=ts, inclusive=True, limit=30))
                return len(self.db.msgs(cid, 100000))
            s.open(cid)
            self.submit("bg", fetch, lambda n: (setattr(s, "limit", n), setattr(s, "since", None),
                                                s.paint(select_ts=ts)))
            return
        s.open(cid, select_ts=ts)
        if ts:
            s.query_one(MsgList).focus()
        if thread:
            self.push_screen(ChatScreen(cid, thread=thread))

    def conv_items(self) -> list[dict]:
        me = self.main()
        current = me.cid if me else None
        items = []
        for cid, c in self.convs.items():
            if c.get("is_im") and (self.users.get(c.get("user")) or {}).get("deleted"):
                continue
            unread, mentions = self.counts.get(cid, (0, 0))
            prefix = Text("🔒 " if c.get("is_private") and not c.get("is_mpim") else
                          "👥 " if c.get("is_mpim") else "👤 " if c.get("is_im") else "   ")
            detail = Text()
            if mentions:
                detail.append(f" @{mentions} ", "bold black on #e8a33d")
            elif unread:
                detail.append(f"{unread} new", "bold")
            boost = (3e9 if mentions else 2e9 if unread else 0) + c["_visited"]
            if cid == current:
                boost = -1
            name = self.name_of(cid)
            items.append({"text": name, "cid": cid, "prefix": prefix, "detail": detail, "boost": boost})
        have = {c.get("user") for c in self.convs.values() if c.get("is_im")}
        for u in self.users.values():
            if u["id"] in have or u["id"] == self.me or u.get("deleted") or u.get("is_bot") or u["id"] == "USLACKBOT":
                continue
            items.append({"text": user_name(u["id"], self.users), "user": u["id"], "prefix": Text("👤 "),
                          "detail": "new DM", "boost": -2})
        return items

    def action_palette(self):
        items = self.conv_items()

        def go(it):
            if not it:
                return
            if "user" in it:
                self.submit("bg", lambda: self.api.open_im(it["user"]),
                            lambda cid: self.submit("bg", lambda: sync.Sync(self.cfg, self.db, self.api).refresh_convs(),
                                                    lambda _: (self.load_names(), self.goto(cid))))
            else:
                self.goto(it["cid"])
        self.push_screen(Picker("Go to", lambda q: rank(items, q), "Channel or person…"), go)

    def action_next_unread(self):
        cur = self.main().cid if self.main() else None
        cands = [(m, n, self.convs[c]["_visited"], c) for c, (n, m) in self.counts.items()
                 if c != cur and c in self.convs]
        if not cands:
            return self.notify("All read ✓", timeout=2)
        cands.sort(key=lambda x: (-(x[0] > 0), -x[2]))
        self.goto(cands[0][3])

    def action_search(self):
        def source(q):
            words = q.split()
            items = []
            if q.strip():
                items.append({"text": f"Search all of Slack for “{q.strip()}”", "server": q.strip(),
                              "prefix": Text("🔎 ")})
            for cid, m in self.db.search(words, 60):
                if cid not in self.convs:
                    continue
                txt = render(m.get("text", ""), self.users, self.convs, self.me).plain.replace("\n", " ")
                items.append({"text": txt[:120], "cid": cid, "ts": m["ts"],
                              "thread": None if is_top(m) else m.get("thread_ts"),
                              "detail": f"{self.name_of(cid)} · {author(m, self.users)} · "
                                        f"{ChatScreen.short_when(m['ts'])}"})
            return items

        def go(it):
            if not it:
                return
            if "server" in it:
                self.submit("bg", lambda: self.api.search(it["server"]), lambda r: self.server_results(it["server"], r))
            else:
                self.goto(it["cid"], it["ts"], it["thread"])
        self.push_screen(Picker("Search messages", source, "Words…"), go)

    def server_results(self, q: str, matches: list[dict]):
        if not matches:
            return self.notify(f"Nothing found for “{q}”", timeout=3)
        items = []
        for m in matches:
            cid = (m.get("channel") or {}).get("id")
            thread = None
            if "thread_ts=" in (m.get("permalink") or ""):
                thread = m["permalink"].split("thread_ts=")[1].split("&")[0]
            txt = render(m.get("text", ""), self.users, self.convs, self.me).plain.replace("\n", " ")
            items.append({"text": txt[:120], "cid": cid, "ts": m["ts"], "thread": thread if thread != m["ts"] else None,
                          "detail": f"{self.name_of(cid)} · {m.get('username', '')} · {ChatScreen.short_when(m['ts'])}"})
        self.push_screen(Picker(f"Slack search: {q}", lambda qq: rank(items, qq)),
                         lambda it: it and self.goto(it["cid"], it["ts"], it["thread"]))

    def action_attach(self):
        s = self.screen
        if not isinstance(s, ChatScreen) or not s.cid:
            return

        def go(path):
            if not path:
                return
            p = Path(os.path.expanduser(path.strip().strip("'\"")))
            if not p.is_file():
                return self.notify(f"No such file: {p}", severity="error")
            comp = s.query_one(Composer)
            comment = to_slack(comp.text.strip(), self.users, self.convs)
            comp.text = ""
            self.notify(f"Uploading {p.name}…", timeout=3)
            self.submit("fg", lambda: self.api.upload(s.cid, p, thread_ts=s.thread, comment=comment),
                        lambda _: self.notify(f"Sent {p.name}", timeout=2), self.fail("Upload"))
        self.push_screen(Prompt("Attach a file", str(Path.home()) + "/",
                                "Path to the file. What's in the message box goes with it as a comment."), go)

    def action_msg(self, what: str):
        s = self.screen
        if isinstance(s, ChatScreen):
            name, _, arg = what.partition("(")
            fn = getattr(s, "action_" + name)
            fn(int(arg.rstrip(")"))) if arg else fn()

    def action_paste(self):
        """Ctrl+V: an image on the clipboard is sent (after asking); text is pasted into the box."""
        s = self.screen
        if not isinstance(s, ChatScreen) or not s.cid:
            if isinstance(self.focused, Input):
                try:
                    text = subprocess.run(["wl-paste", "--no-newline"], capture_output=True, text=True,
                                          timeout=3).stdout
                except (OSError, subprocess.TimeoutExpired):
                    text = ""
                self.focused.insert_text_at_cursor(text.replace("\n", " "))
            return
        comp = s.query_one(Composer)
        try:
            types = subprocess.run(["wl-paste", "--list-types"], capture_output=True, text=True, timeout=3).stdout.split()
        except (OSError, subprocess.TimeoutExpired):
            types = []
        image = next((t for t in ("image/png", "image/jpeg", "image/gif", "image/webp") if t in types), None)
        if not image:
            try:
                text = subprocess.run(["wl-paste", "--no-newline"], capture_output=True, text=True, timeout=3).stdout
            except (OSError, subprocess.TimeoutExpired):
                text = ""
            if text:
                comp.focus()
                comp.replace(text, *comp.selection)
            return
        data = subprocess.run(["wl-paste", "--type", image], capture_output=True, timeout=10).stdout
        dest = CACHE_DIR / "pasted" / f"pasted-{time.strftime('%Y%m%d-%H%M%S')}.{image.split('/')[1]}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        dims = ""
        try:
            from PIL import Image
            with Image.open(dest) as im:
                dims = f" ({im.width}×{im.height})"
        except Exception:  # noqa: BLE001 - only for the question
            pass
        cid, thread = s.cid, s.thread

        def go(yes):
            if not yes:
                return
            comment = to_slack(comp.text.strip(), self.users, self.convs)
            comp.text = ""
            self.notify("Sending image…", timeout=2)
            self.submit("fg", lambda: self.api.upload(cid, dest, thread_ts=thread, comment=comment),
                        lambda _: self.notify("Image sent", timeout=2), self.fail("Upload"))
        where = "this thread" if thread else self.name_of(cid)
        self.push_screen(Confirm(f"Send the image from the clipboard{dims} to {where}?"
                                 + ("\nThe text in the box goes with it." if comp.text.strip() else "")), go)

    def action_refresh(self):
        s = self.screen
        self.load_names()
        if isinstance(s, ChatScreen) and s.cid:
            s.reload()
        self.submit("bg", lambda: sync.Sync(self.cfg, self.db, self.api).refresh_read(list(self.convs)),
                    lambda _: self.repaint(names=True))

    def action_help(self):
        k = self.keys
        self.push_screen(Help([
            (f"{pretty(k['palette'])} / {pretty(k['palette2'])}", "Go to a channel or person"),
            (pretty(k["next_unread"]), "Next unread conversation (mentions first)"),
            (pretty(k["search"]), "Search messages (local, Enter on the first line asks Slack)"),
            ("Enter", "Send · on a message: open its thread (or double click it)"),
            (pretty(k["copy"]), "Copy the selected message (or the text selected in the box)"),
            ("Shift+drag", "Select any text on screen with the mouse (the terminal's own selection)"),
            ("Shift+Enter / Ctrl+J", "New line"),
            ("Tab", "Complete @name, #channel, :emoji:"),
            ("↑ (empty box)", "Select messages; ↑↓ move, typing returns to the box"),
            (pretty(k["react"]), "React to the selected message"),
            (pretty(k["edit"]), "Edit your message"),
            (pretty(k["delete"]), "Delete your message"),
            (pretty(k["open"]), "Open a file or link of the message"),
            (pretty(k["browser"]), "Open the message in the browser"),
            (pretty(k["mark_unread"]), "Mark unread from the selected message"),
            (pretty(k["attach"]), "Send a file"),
            (pretty(k["paste"]), "Paste: an image on the clipboard is sent, text goes in the box"),
            (f"{pretty(k['prev_thread'])} / {pretty(k['next_thread'])}", "Previous / next thread"),
            ("Ctrl+A", "Select all in the box"),
            ("Esc", "Back from a thread · cancel editing"),
            (pretty(k["refresh"]), "Refresh"),
            ("Ctrl+Q", "Quit"),
        ]))
