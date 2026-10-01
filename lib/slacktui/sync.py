"""Keeping the local copy live: Socket Mode events into the database, catch-up after being
offline, desktop notifications and the unread count for the bar.

Slack hands each event to only one of an app's open sockets, so exactly one process
listens: whoever holds `listener.lock` (normally `slack-sync.service`; the client takes
over while the service isn't running).
"""

import fcntl
import json
import queue
import shutil
import subprocess
import threading
import time
import traceback

import httpx
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from .api import Slack, SlackError
from .config import ERROR_LOG, STATE_DIR, Config
from .db import QUIET, Db, is_top
from .mrkdwn import author, conv_name, plain

UNREAD_FILE = STATE_DIR / "unread"
LISTENER_FILE = STATE_DIR / "listener.json"
LOCK_FILE = STATE_DIR / "listener.lock"

CONVS_EVERY = 600      # re-list conversations (joins/leaves we might have missed)
READ_EVERY = 120       # re-check where you've read up to in unread conversations (phone, browser)
USERS_EVERY = 6 * 3600

CONV_EVENTS = {"channel_rename", "group_rename", "member_joined_channel", "member_left_channel",
               "channel_left", "group_left", "channel_archive", "group_archive", "channel_unarchive",
               "group_unarchive", "im_created", "channel_created", "channel_deleted", "group_deleted"}


def log_error(where: str):
    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ERROR_LOG.open("a") as f:
            f.write(f"--- {time.strftime('%F %T')} {where}\n{traceback.format_exc()}\n")
    except OSError:
        pass


def take_lock(wait: bool):
    """The listener lock as an open file (keep it open to keep the lock), or None."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = LOCK_FILE.open("w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
    except BlockingIOError:
        f.close()
        return None
    return f


def listener_state() -> dict:
    try:
        return json.loads(LISTENER_FILE.read_text())
    except (OSError, ValueError):
        return {}


def counts(db: Db, cfg: Config, me: str) -> dict:
    """{conv id: (unread, mentions)} for conversations with something unread."""
    convs = db.convs()
    out = {}
    for cid, msgs in db.unread_msgs(me).items():
        c = convs.get(cid)
        if not c:
            continue
        direct = c.get("is_im") or c.get("is_mpim")
        if c.get("is_im") and c.get("user") == me:
            continue
        mentions = sum(1 for m in msgs if direct or mentions_me(m, me))
        if c.get("name") in cfg.muted and not mentions:
            continue
        out[cid] = (len(msgs), mentions)
    return out


def mentions_me(m: dict, me: str) -> bool:
    t = m.get("text") or ""
    return f"<@{me}" in t or "<!here" in t or "<!channel" in t or "<!everyone" in t


def write_unread(db: Db, cfg: Config, me: str):
    try:
        c = counts(db, cfg, me)
        mentions = sum(n for _, n in c.values())
        UNREAD_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = UNREAD_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"mentions": mentions, "unread": len(c)}) + "\n")
        tmp.replace(UNREAD_FILE)
    except Exception:  # noqa: BLE001 - the bar count is best effort
        log_error("unread")


class Sync:
    def __init__(self, cfg: Config, db: Db | None = None, api: Slack | None = None, report=print):
        self.cfg = cfg
        self.db = db or Db()
        self.api = api or Slack(cfg.user_token)
        self.report = report
        self.events: queue.Queue = queue.Queue()
        self.last = {"convs": 0.0, "read": 0.0, "users": 0.0}

    # -- who and what

    @property
    def me(self) -> str:
        me = self.db.get("me")
        if not me:
            me = self.api.auth()
            self.db.put("me", me)
        return me["user_id"]

    def refresh_users(self):
        self.db.put_users(self.api.users())
        try:
            self.db.put("emoji", self.api.emoji())
        except SlackError:
            pass
        self.last["users"] = time.time()

    def refresh_convs(self):
        convs = self.api.conversations()
        known = self.db.convs()
        self.db.put_convs(convs)
        for c in convs:
            if c["id"] not in known:
                self.refresh_read([c["id"]])
        self.last["convs"] = time.time()
        return convs

    def refresh_read(self, cids):
        """Where you've read up to, as Slack knows it (you may have read on your phone)."""
        for cid in cids:
            try:
                info = self.api.info(cid)
            except SlackError as e:
                if e.error in ("channel_not_found", "missing_scope"):
                    continue
                raise
            if info.get("last_read"):
                self.db.set_last_read(cid, info["last_read"])
        self.last["read"] = time.time()

    def catch_up(self):
        """Everything said while nobody was listening: new messages in every conversation,
        most recently active first."""
        me = self.me
        if time.time() - self.last["users"] > USERS_EVERY or not self.db.users():
            self.refresh_users()
        convs = self.refresh_convs()
        newest = self.db.latest_all()
        convs.sort(key=lambda c: -(c.get("updated") or 0))
        for c in convs:
            if c.get("is_im") and c.get("is_user_deleted"):
                continue
            try:
                have = newest.get(c["id"])
                if have:
                    msgs = self.api.history(c["id"], limit=200, oldest=have)
                    if msgs:
                        self.db.put_msgs(c["id"], msgs)
                else:
                    self.db.put_msgs(c["id"], self.api.history(c["id"], limit=50), window=True)
            except SlackError as e:
                if e.error not in ("channel_not_found", "not_in_channel"):
                    raise
        self.refresh_read([c for c in counts(self.db, self.cfg, me)])
        write_unread(self.db, self.cfg, me)

    def housekeeping(self):
        now = time.time()
        if now - self.last["users"] > USERS_EVERY:
            self.refresh_users()
        if now - self.last["convs"] > CONVS_EVERY:
            self.refresh_convs()
        if now - self.last["read"] > READ_EVERY:
            self.refresh_read(list(counts(self.db, self.cfg, self.me)))
            write_unread(self.db, self.cfg, self.me)

    # -- events

    def handle(self, ev: dict):
        t = ev.get("type")
        me = self.me
        if t == "message":
            self.on_message(ev, me)
        elif t in ("reaction_added", "reaction_removed"):
            item = ev.get("item") or {}
            if item.get("type") == "message":
                self.db.update_msg(item["channel"], item["ts"],
                                   lambda m: react(m, ev["reaction"], ev["user"], t == "reaction_added"))
        elif t in CONV_EVENTS:
            self.refresh_convs()
            if t in ("member_joined_channel", "im_created") and ev.get("channel"):
                cid = ev["channel"] if isinstance(ev["channel"], str) else ev["channel"]["id"]
                self.db.put_msgs(cid, self.api.history(cid, limit=50), window=True)
                self.refresh_read([cid])
        elif t in ("user_change", "team_join"):
            self.db.put_users([ev["user"]])
        elif t == "emoji_changed":
            self.db.put("emoji", self.api.emoji())
        elif t in ("channel_marked", "group_marked", "im_marked", "mpim_marked"):
            self.db.set_last_read(ev["channel"], ev["ts"])
        write_unread(self.db, self.cfg, me)

    def on_message(self, ev: dict, me: str):
        cid, sub = ev.get("channel"), ev.get("subtype")
        if sub == "message_deleted":
            old = self.db.msg(cid, ev["deleted_ts"])
            self.db.delete_msg(cid, ev["deleted_ts"])
            if old and not is_top(old):
                self.db.update_msg(cid, old["thread_ts"], lambda p: p.update(
                    reply_count=max(0, p.get("reply_count", 1) - 1)))
            return
        if sub in ("message_changed", "message_replied"):
            m = dict(ev["message"])
            if (m.get("subtype") == "tombstone" or (ev.get("previous_message") or {}).get("subtype") == "tombstone") \
                    and not m.get("reply_count"):
                self.db.delete_msg(cid, m["ts"])
                return
            self.db.put_msgs(cid, [m])
            return
        m = {k: v for k, v in ev.items() if k not in ("channel", "event_ts", "channel_type")}
        if "ts" not in m:
            return
        known = self.db.msg(cid, m["ts"]) is not None
        self.db.put_msgs(cid, [m])
        if m.get("thread_ts") and m["thread_ts"] != m["ts"]:
            self.db.update_msg(cid, m["thread_ts"], lambda p: bump(p, m))
        if m.get("user") == me:
            if is_top(m):
                self.db.set_last_read(cid, m["ts"], only_forward=True)
        elif not known and sub not in QUIET:
            self.maybe_notify(cid, m, me)

    def maybe_notify(self, cid: str, m: dict, me: str):
        if not self.cfg.notify or not shutil.which("notify-send"):
            return
        c = self.db.convs().get(cid)
        if not c:
            return
        direct = c.get("is_im") or c.get("is_mpim")
        thread = None if is_top(m) and m.get("subtype") != "thread_broadcast" else m.get("thread_ts")
        mine_thread = False
        if thread:
            parent = self.db.msg(cid, thread) or {}
            mine_thread = parent.get("user") == me or me in parent.get("reply_users", [])
        if not (direct or mentions_me(m, me) or mine_thread):
            return
        if c.get("name") in self.cfg.muted and not mentions_me(m, me):
            return
        viewing = self.db.get("viewing") or {}
        if viewing.get("focused") and viewing.get("cid") == cid and viewing.get("thread") == thread \
                and time.time() - viewing.get("at", 0) < 30:
            return
        users, convs = self.db.users(), self.db.convs()
        who = author(m, users)
        where = conv_name(c, users, me)
        title = who if c.get("is_im") else f"{who} in {where}"
        body = plain(m.get("text") or "📎 file", users, convs, me)[:300]
        threading.Thread(target=notify, args=(self.db, title, body, cid, m["ts"], thread), daemon=True).start()

    # -- socket

    def worker(self):
        while True:
            ev = self.events.get()
            try:
                if ev == "catch_up":
                    self.catch_up()
                elif ev == "tick":
                    self.housekeeping()
                else:
                    self.handle(ev)
            except Exception as e:  # noqa: BLE001 - keep listening; logged
                log_error(f"event {ev if isinstance(ev, str) else ev.get('type')}")
                self.report(f"  ! {type(e).__name__}: {e}")

    def run(self):
        """Listen forever (reconnecting); events are handled in order on a worker thread."""
        if not self.cfg.app_token:
            raise SystemExit("SLACK_APP_TOKEN missing: run `slack auth`")
        threading.Thread(target=self.worker, daemon=True).start()

        def ticker():
            while True:
                time.sleep(60)
                self.events.put("tick")
        threading.Thread(target=ticker, daemon=True).start()
        backoff = 1
        while True:
            try:
                r = httpx.post("https://slack.com/api/apps.connections.open", timeout=30,
                               headers={"Authorization": f"Bearer {self.cfg.app_token}"}).json()
                if not r.get("ok"):
                    raise SlackError("apps.connections.open", r.get("error", "?"))
                with connect(r["url"], open_timeout=30, ping_interval=30, max_size=None) as ws:
                    self.report("connected to Slack")
                    state("connected")
                    self.events.put("catch_up")
                    backoff = 1
                    for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("envelope_id"):
                            ws.send(json.dumps({"envelope_id": msg["envelope_id"]}))
                        if msg.get("type") == "disconnect":
                            break
                        ev = (msg.get("payload") or {}).get("event")
                        if ev:
                            self.events.put(ev)
            except (OSError, WebSocketException, SlackError, httpx.HTTPError, ValueError) as e:
                self.report(f"  ! Slack connection: {type(e).__name__}: {e}")
                state("down", f"{type(e).__name__}: {e}")
                if isinstance(e, SlackError) and e.error in ("invalid_auth", "not_allowed_token_type"):
                    backoff = 300
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)


def bump(parent: dict, reply: dict):
    """Count a new reply in its thread's parent, once (the client and the daemon both may)."""
    if reply["ts"] <= parent.get("latest_reply", "0"):
        return
    parent["reply_count"] = parent.get("reply_count", 0) + 1
    parent["latest_reply"] = reply["ts"]
    u = reply.get("user")
    if u and u not in parent.setdefault("reply_users", []):
        parent["reply_users"].append(u)


def react(m: dict, name: str, user: str, add: bool):
    rs = m.setdefault("reactions", [])
    r = next((r for r in rs if r["name"] == name), None)
    if add:
        if not r:
            r = {"name": name, "users": [], "count": 0}
            rs.append(r)
        if user not in r["users"]:
            r["users"].append(user)
            r["count"] = len(r["users"])
    elif r and user in r["users"]:
        r["users"].remove(user)
        r["count"] = len(r["users"])
        if not r["users"]:
            rs.remove(r)


def state(s: str, error: str = ""):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        LISTENER_FILE.write_text(json.dumps({"state": s, "since": time.time(), "error": error}) + "\n")
    except OSError:
        pass


def notify(db: Db, title: str, body: str, cid: str, ts: str, thread: str | None):
    """A desktop notification; clicking it opens the conversation in the client."""
    try:
        r = subprocess.run(["notify-send", "--app-name=Slack", "--icon=slack", "--wait",
                            "--action=default=Open", title, body],
                           capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return
    if r.stdout.strip() == "default":
        db.put("goto", {"cid": cid, "ts": ts, "thread": thread, "at": time.time()})
        window = shutil.which("slack-window")
        if window:
            subprocess.Popen([window], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
