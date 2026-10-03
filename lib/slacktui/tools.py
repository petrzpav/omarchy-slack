"""Scriptable Slack commands (for shell scripts and AI agents): read, search and write without
opening the client. They share the client's database, so whatever they fetch or send shows up
there too. A conversation is named by its id, #channel, @person (a DM) or part of its name;
a message by its `ts`.
"""

import json
import sys
import time
from pathlib import Path

from .api import Slack, SlackError
from .config import Config
from .db import Db
from .mrkdwn import author, conv_name, fold, has_layout, plain, render_blocks, to_slack, user_name
from .sync import counts, listener_state, write_unread


class Ctx:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = Db()
        self._api = None
        self.users = self.db.users()
        self.convs = self.db.convs()
        me = self.db.get("me")
        if not me:
            me = self.api.auth()
            self.db.put("me", me)
        self.me = me["user_id"]

    @property
    def api(self) -> Slack:
        if self._api is None:
            self._api = Slack(self.cfg.user_token)
        return self._api

    def name(self, cid: str) -> str:
        c = self.convs.get(cid)
        return conv_name(c, self.users, self.me) if c else cid

    def text(self, m: dict) -> str:
        if has_layout(m.get("blocks")):
            t = render_blocks(m["blocks"], self.users, self.convs, self.me).plain.strip()
        else:
            t = plain(m.get("text") or "", self.users, self.convs, self.me)
        for f in m.get("files") or []:
            t += f"\n[file: {f.get('name') or f.get('title')} {f.get('url_private') or ''}]".rstrip()
        if not t.strip() and m.get("attachments"):
            a = m["attachments"][0]
            t = a.get("fallback") or a.get("text") or ""
        return t

    def row(self, cid: str, m: dict) -> dict:
        r = {"conv": cid, "ts": m["ts"], "time": _when(m["ts"]), "author": author(m, self.users),
             "text": self.text(m)}
        if m.get("reply_count") and m.get("thread_ts") == m["ts"]:
            r["replies"] = m["reply_count"]
        if m.get("thread_ts") and m["thread_ts"] != m["ts"]:
            r["thread"] = m["thread_ts"]
        if m.get("reactions"):
            r["reactions"] = {x["name"]: x.get("count", 1) for x in m["reactions"]}
        if m.get("edited"):
            r["edited"] = True
        return r


def _die(msg: str):
    sys.exit(f"slack: {msg}")


def _when(ts: str) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))


def _print(rows: list[dict], as_json: bool, conv_names: dict | None = None, indent_threads=False):
    if as_json:
        print(json.dumps(rows, ensure_ascii=False))
        return
    for r in rows:
        where = f"{conv_names[r['conv']]}  " if conv_names else ""
        extra = ""
        if r.get("replies"):
            extra += f"  [{r['replies']} replies]"
        if r.get("reactions"):
            extra += "  " + " ".join(f":{k}:{v if v > 1 else ''}" for k, v in r["reactions"].items())
        pad = "    " if indent_threads and r.get("thread") else ""
        text = r["text"].replace("\n", "\n" + pad + "    ")
        print(f"{pad}{where}{r['ts']}  {r['time']}  {r['author']}: {text}{extra}")
    if not rows:
        print("(nothing)")


def resolve(ctx: Ctx, what: str) -> str:
    """Conversation id from an id, #channel, @person or part of a name."""
    if what in ctx.convs:
        return what
    want = fold(what.lstrip("#@"))
    if what.startswith("@") or not what.startswith("#"):
        people = [u for u in ctx.users.values() if not u.get("deleted") and want in
                  {fold(n) for n in (u.get("name"), u.get("real_name"), u.get("profile", {}).get("display_name"),
                                     u.get("profile", {}).get("real_name")) if n}]
        if len(people) == 1:
            uid = people[0]["id"]
            cid = next((c for c, v in ctx.convs.items() if v.get("is_im") and v.get("user") == uid), None)
            return cid or ctx.api.open_im(uid)
    exact = [c for c, v in ctx.convs.items() if fold(v.get("name") or "") == want]
    if len(exact) == 1:
        return exact[0]
    loose = [c for c in ctx.convs if want in fold(ctx.name(c))]
    if len(loose) == 1:
        return loose[0]
    if loose:
        _die(f"{what!r} matches several: " + ", ".join(f"{ctx.name(c)} ({c})" for c in loose[:10]))
    _die(f"no conversation {what!r}; see `slack convs`")


# ------------------------------------------------------------ reading

def cmd_convs(cfg: Config, args):
    ctx = Ctx(cfg)
    unread = counts(ctx.db, cfg, ctx.me)
    latest = ctx.db.latest_all()
    rows = []
    for cid, c in ctx.convs.items():
        if c.get("is_im") and c.get("is_user_deleted"):
            continue
        n, mentions = unread.get(cid, (0, 0))
        if args.unread and not n:
            continue
        kind = "dm" if c.get("is_im") else "group" if c.get("is_mpim") else \
            "private" if c.get("is_private") else "channel"
        rows.append({"id": cid, "name": ctx.name(cid), "kind": kind, "unread": n, "mentions": mentions,
                     "last": _when(latest[cid]) if latest.get(cid) else ""})
    rows.sort(key=lambda r: r["last"], reverse=True)
    rows = rows[:args.limit] if args.limit else rows
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
        return
    for r in rows:
        badge = f"{r['unread']} unread" + (f", {r['mentions']} for you" if r["mentions"] else "") if r["unread"] else ""
        print(f"{r['id']}  {r['kind']:<7} {r['last']:<16}  {r['name']:<32} {badge}")


def cmd_inbox(cfg: Config, args):
    """Everything unread, grouped by conversation, DMs and mentions first."""
    ctx = Ctx(cfg)
    unread = counts(ctx.db, cfg, ctx.me)
    msgs = ctx.db.unread_msgs(ctx.me)
    order = sorted(unread, key=lambda c: (-unread[c][1], -unread[c][0]))
    out = []
    for cid in order:
        rows = [ctx.row(cid, m) for m in msgs.get(cid, [])][-args.per:]
        out.append({"conv": cid, "name": ctx.name(cid), "unread": unread[cid][0],
                    "mentions": unread[cid][1], "messages": rows})
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
        return
    if not out:
        print("nothing unread")
    for c in out:
        print(f"== {c['name']} ({c['conv']}) {c['unread']} unread"
              + (f", {c['mentions']} for you" if c["mentions"] else ""))
        _print(c["messages"], False)
        print()
    st = listener_state()
    if st.get("state") != "connected":
        print("(the sync daemon isn't connected, so this may be stale: `systemctl --user status slack-sync`)")


def cmd_read(cfg: Config, args):
    ctx = Ctx(cfg)
    cid = resolve(ctx, args.conv)
    if not args.cached:
        try:
            ctx.db.put_msgs(cid, ctx.api.history(cid, limit=max(args.limit, 20)), window=True)
        except SlackError as e:
            _die(str(e))
    rows = [ctx.row(cid, m) for m in ctx.db.msgs(cid, limit=args.limit)]
    if args.mark_read and rows:
        ctx.api.mark(cid, rows[-1]["ts"])
        ctx.db.set_last_read(cid, rows[-1]["ts"])
        write_unread(ctx.db, cfg, ctx.me)
    if not args.json:
        print(f"== {ctx.name(cid)} ({cid})")
    _print(rows, args.json)


def cmd_thread(cfg: Config, args):
    ctx = Ctx(cfg)
    cid = resolve(ctx, args.conv)
    if not args.cached:
        ctx.db.put_msgs(cid, ctx.api.replies(cid, args.ts), thread=args.ts)
    rows = [ctx.row(cid, m) for m in ctx.db.thread(cid, args.ts)]
    _print(rows, args.json, indent_threads=True)


def cmd_search(cfg: Config, args):
    ctx = Ctx(cfg)
    if args.remote:
        rows = []
        for m in ctx.api.search(args.query, count=args.limit):
            cid = (m.get("channel") or {}).get("id", "")
            r = ctx.row(cid, m)
            if cid not in ctx.convs:
                r["conv_name"] = "#" + (m.get("channel") or {}).get("name", cid)
            r["link"] = m.get("permalink", "")
            rows.append(r)
    else:
        rows = [ctx.row(cid, m) for cid, m in ctx.db.search(args.query.split(), limit=args.limit)]
    names = {r["conv"]: r.get("conv_name") or ctx.name(r["conv"]) for r in rows}
    _print(rows, args.json, conv_names=names)


def cmd_users(cfg: Config, args):
    ctx = Ctx(cfg)
    want = fold(args.query or "")
    rows = []
    for u in ctx.users.values():
        if u.get("deleted") or u.get("is_bot") or u["id"] == "USLACKBOT":
            continue
        p = u.get("profile", {})
        names = " ".join(n for n in (u.get("name"), p.get("real_name"), p.get("display_name")) if n)
        if want and want not in fold(names):
            continue
        rows.append({"id": u["id"], "name": user_name(u["id"], ctx.users), "handle": u.get("name"),
                     "title": p.get("title", ""), "email": p.get("email", "")})
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
        return
    for r in rows:
        print(f"{r['id']}  {r['name']:<28} @{r['handle']:<20} {r['email']}  {r['title']}")


def cmd_link(cfg: Config, args):
    ctx = Ctx(cfg)
    print(ctx.api.permalink(resolve(ctx, args.conv), args.ts))


def cmd_download(cfg: Config, args):
    ctx = Ctx(cfg)
    cid = resolve(ctx, args.conv)
    m = ctx.db.msg(cid, args.ts)
    if not m:
        _die("message not in the local copy; `slack read` or `slack thread` it first")
    out = Path(args.out).expanduser()
    for f in m.get("files") or []:
        if f.get("url_private_download") or f.get("url_private"):
            print(ctx.api.download(f.get("url_private_download") or f["url_private"], out / f["name"]))


# ------------------------------------------------------------ writing

def _text_arg(args) -> str:
    if args.text == "-":
        return sys.stdin.read().strip()
    return args.text


def cmd_post(cfg: Config, args):
    ctx = Ctx(cfg)
    cid = resolve(ctx, args.conv)
    text = to_slack(_text_arg(args), ctx.users, ctx.convs)
    if args.file:
        ctx.api.upload(cid, Path(args.file).expanduser(), thread_ts=args.thread, comment=text)
        print(f"uploaded {Path(args.file).name} to {ctx.name(cid)}")
        return
    m = ctx.api.post(cid, text, thread_ts=args.thread, broadcast=args.broadcast)
    ctx.db.put_msgs(cid, [m])
    print(f"posted to {ctx.name(cid)}" + (" (thread)" if args.thread else "") + f", ts {m['ts']}")


def cmd_edit(cfg: Config, args):
    ctx = Ctx(cfg)
    cid = resolve(ctx, args.conv)
    ctx.api.edit(cid, args.ts, to_slack(_text_arg(args), ctx.users, ctx.convs))
    print("edited")


def cmd_delete(cfg: Config, args):
    ctx = Ctx(cfg)
    cid = resolve(ctx, args.conv)
    ctx.api.delete(cid, args.ts)
    ctx.db.delete_msg(cid, args.ts)
    print("deleted")


def cmd_react(cfg: Config, args):
    ctx = Ctx(cfg)
    cid = resolve(ctx, args.conv)
    ctx.api.react(cid, args.ts, args.emoji.strip(":"), add=not args.remove)
    print(("removed :" if args.remove else "reacted :") + args.emoji.strip(":") + ":")


def cmd_mark(cfg: Config, args):
    ctx = Ctx(cfg)
    cids = [resolve(ctx, c) for c in args.convs] if args.convs else list(counts(ctx.db, cfg, ctx.me))
    for cid in cids:
        ts = args.ts or ctx.db.newest(cid)
        if ts:
            ctx.api.mark(cid, ts)
            ctx.db.set_last_read(cid, ts)
    write_unread(ctx.db, cfg, ctx.me)
    print(f"marked read: {', '.join(ctx.name(c) for c in cids) or 'nothing unread'}")


# ------------------------------------------------------------ argparse

def add_parsers(sub):
    p = sub.add_parser("convs", help="conversations, most recent first")
    p.add_argument("--unread", action="store_true")
    p.add_argument("-n", "--limit", type=int, default=0)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_convs)

    p = sub.add_parser("inbox", help="every unread message, DMs and mentions first")
    p.add_argument("--per", type=int, default=15, help="newest N per conversation")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_inbox)

    p = sub.add_parser("read", help="the newest messages of a conversation")
    p.add_argument("conv")
    p.add_argument("-n", "--limit", type=int, default=30)
    p.add_argument("--cached", action="store_true", help="local copy only, no API call")
    p.add_argument("--mark-read", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("thread", help="a thread: its first message and every reply")
    p.add_argument("conv")
    p.add_argument("ts")
    p.add_argument("--cached", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_thread)

    p = sub.add_parser("search", help="search messages (local copy; --remote uses Slack's search syntax)")
    p.add_argument("query")
    p.add_argument("--remote", action="store_true")
    p.add_argument("-n", "--limit", type=int, default=30)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("users", help="people in the workspace")
    p.add_argument("query", nargs="?")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_users)

    p = sub.add_parser("link", help="permalink of a message")
    p.add_argument("conv")
    p.add_argument("ts")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("download", help="save a message's files")
    p.add_argument("conv")
    p.add_argument("ts")
    p.add_argument("-o", "--out", default=".")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("post", help="send a message as you (@Name and #channel are linked)")
    p.add_argument("conv")
    p.add_argument("text", help="the message, - for stdin")
    p.add_argument("--thread", metavar="TS", help="reply in this thread")
    p.add_argument("--broadcast", action="store_true", help="thread reply also sent to the channel")
    p.add_argument("--file", help="upload this file with the text as its comment")
    p.set_defaults(func=cmd_post)

    p = sub.add_parser("edit", help="change your message")
    p.add_argument("conv")
    p.add_argument("ts")
    p.add_argument("text")
    p.set_defaults(func=cmd_edit)

    p = sub.add_parser("delete", help="delete your message")
    p.add_argument("conv")
    p.add_argument("ts")
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("react", help="add (or --remove) an emoji reaction")
    p.add_argument("conv")
    p.add_argument("ts")
    p.add_argument("emoji")
    p.add_argument("--remove", action="store_true")
    p.set_defaults(func=cmd_react)

    p = sub.add_parser("mark", help="mark conversations read (all unread ones when none given)")
    p.add_argument("convs", nargs="*")
    p.add_argument("--ts", help="read up to this message")
    p.set_defaults(func=cmd_mark)
