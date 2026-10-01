"""The local copy of Slack: one SQLite file shared by the sync daemon and the client.

The daemon writes what arrives over Socket Mode; the client paints from here and watches
`PRAGMA data_version` to notice the daemon's writes. Timestamps are Slack's `ts` strings
("1727000000.123456"), which sort correctly as text.
"""

import json
import sqlite3
import threading

from .config import DATA_DIR

DB_FILE = DATA_DIR / "slack.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS convs (id TEXT PRIMARY KEY, data TEXT, last_read TEXT DEFAULT '0',
                                  visited REAL DEFAULT 0, gone INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS msgs (conv TEXT, ts TEXT, thread_ts TEXT, top INTEGER, user TEXT, data TEXT,
                                 PRIMARY KEY (conv, ts));
CREATE INDEX IF NOT EXISTS msgs_thread ON msgs (conv, thread_ts);
CREATE INDEX IF NOT EXISTS msgs_top ON msgs (conv, top, ts);
"""

# Not something anybody said: these never make a conversation unread.
QUIET = {"channel_join", "channel_leave", "group_join", "group_leave", "channel_purpose",
         "channel_topic", "channel_name", "bot_add", "bot_remove"}


def is_top(m: dict) -> bool:
    """Shown in the conversation itself (not only inside a thread)."""
    t = m.get("thread_ts")
    return not t or t == m["ts"] or m.get("subtype") == "thread_broadcast"


class Db:
    def __init__(self, path=DB_FILE):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.c = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10)
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.execute("PRAGMA busy_timeout=10000")
        self.c.executescript(SCHEMA)
        self.lock = threading.RLock()

    def q(self, sql, *args):
        with self.lock:
            return self.c.execute(sql, args).fetchall()

    def tx(self, fn):
        with self.lock:
            self.c.execute("BEGIN IMMEDIATE")
            try:
                out = fn()
                self.c.execute("COMMIT")
                return out
            except BaseException:
                self.c.execute("ROLLBACK")
                raise

    def data_version(self) -> int:
        return self.q("PRAGMA data_version")[0][0]

    # -- kv

    def get(self, k, default=None):
        r = self.q("SELECT v FROM kv WHERE k=?", k)
        return json.loads(r[0][0]) if r else default

    def put(self, k, v):
        self.q("INSERT OR REPLACE INTO kv VALUES (?,?)", k, json.dumps(v, ensure_ascii=False))

    # -- users

    def put_users(self, users: list[dict]):
        self.tx(lambda: self.c.executemany("INSERT OR REPLACE INTO users VALUES (?,?)",
                                           [(u["id"], json.dumps(u, ensure_ascii=False)) for u in users]))

    def users(self) -> dict[str, dict]:
        return {i: json.loads(d) for i, d in self.q("SELECT id, data FROM users")}

    # -- conversations

    def put_convs(self, convs: list[dict], complete=True):
        """Store the conversations you're in; with `complete`, the others are marked gone."""
        def run():
            for c in convs:
                self.c.execute("INSERT INTO convs (id, data, gone) VALUES (?,?,0) ON CONFLICT(id) "
                               "DO UPDATE SET data=excluded.data, gone=0",
                               (c["id"], json.dumps(c, ensure_ascii=False)))
            if complete:
                ids = [c["id"] for c in convs]
                self.c.execute(f"UPDATE convs SET gone=1 WHERE id NOT IN ({','.join('?' * len(ids))})", ids)
        self.tx(run)

    def convs(self) -> dict[str, dict]:
        out = {}
        for i, d, lr, vis in self.q("SELECT id, data, last_read, visited FROM convs WHERE gone=0"):
            c = json.loads(d)
            c["_last_read"], c["_visited"] = lr or "0", vis or 0
            out[i] = c
        return out

    def last_read(self, cid: str) -> str:
        r = self.q("SELECT last_read FROM convs WHERE id=?", cid)
        return (r[0][0] or "0") if r else "0"

    def set_last_read(self, cid: str, ts: str, only_forward=False):
        if only_forward:
            self.q("UPDATE convs SET last_read=? WHERE id=? AND last_read < ?", ts, cid, ts)
        else:
            self.q("UPDATE convs SET last_read=? WHERE id=?", ts, cid)

    def visited(self, cid: str, when: float):
        self.q("UPDATE convs SET visited=? WHERE id=?", when, cid)

    # -- messages

    def _put(self, cid: str, m: dict):
        self.c.execute("INSERT OR REPLACE INTO msgs VALUES (?,?,?,?,?,?)",
                       (cid, m["ts"], m.get("thread_ts"), int(is_top(m)), m.get("user") or m.get("bot_id"),
                        json.dumps(m, ensure_ascii=False)))

    def put_msgs(self, cid: str, msgs: list[dict], window=False, thread: str | None = None):
        """Store messages. `window`: they are the newest top-level messages, so cached top-level
        ones in their time span that are missing were deleted elsewhere. `thread`: the same for
        a whole thread."""
        def run():
            if msgs and window:
                lo = min(m["ts"] for m in msgs)
                keep = [m["ts"] for m in msgs]
                self.c.execute(f"DELETE FROM msgs WHERE conv=? AND top=1 AND ts>=? AND (thread_ts IS NULL "
                               f"OR thread_ts=ts) AND ts NOT IN ({','.join('?' * len(keep))})",
                               (cid, lo, *keep))
            if thread:
                keep = [m["ts"] for m in msgs]
                self.c.execute(f"DELETE FROM msgs WHERE conv=? AND thread_ts=? AND ts NOT IN "
                               f"({','.join('?' * len(keep))})", (cid, thread, *keep))
            for m in msgs:
                old = self.c.execute("SELECT data FROM msgs WHERE conv=? AND ts=?", (cid, m["ts"])).fetchone()
                if old and thread is None and window is False:
                    # an event doesn't carry the thread summary; keep what we knew
                    o = json.loads(old[0])
                    for k in ("reply_count", "reply_users", "latest_reply", "reactions"):
                        if k in o and k not in m:
                            m[k] = o[k]
                self._put(cid, m)
        self.tx(run)

    def msg(self, cid: str, ts: str) -> dict | None:
        r = self.q("SELECT data FROM msgs WHERE conv=? AND ts=?", cid, ts)
        return json.loads(r[0][0]) if r else None

    def update_msg(self, cid: str, ts: str, fn) -> dict | None:
        """Change a cached message in place (fn mutates it); None when we don't have it."""
        def run():
            r = self.c.execute("SELECT data FROM msgs WHERE conv=? AND ts=?", (cid, ts)).fetchone()
            if not r:
                return None
            m = json.loads(r[0])
            fn(m)
            self._put(cid, m)
            return m
        return self.tx(run)

    def delete_msg(self, cid: str, ts: str):
        self.q("DELETE FROM msgs WHERE conv=? AND ts=?", cid, ts)

    def msgs(self, cid: str, limit=300) -> list[dict]:
        rows = self.q("SELECT data FROM msgs WHERE conv=? AND top=1 ORDER BY ts DESC LIMIT ?", cid, limit)
        return [json.loads(d) for (d,) in reversed(rows)]

    def thread(self, cid: str, ts: str) -> list[dict]:
        rows = self.q("SELECT data FROM msgs WHERE conv=? AND (ts=? OR thread_ts=?) ORDER BY ts", cid, ts, ts)
        return [json.loads(d) for (d,) in rows]

    def newest(self, cid: str) -> str | None:
        r = self.q("SELECT max(ts) FROM msgs WHERE conv=? AND top=1", cid)
        return r[0][0] if r else None

    def latest_all(self) -> dict[str, str]:
        return dict(self.q("SELECT conv, max(ts) FROM msgs WHERE top=1 GROUP BY conv"))

    def unread_msgs(self, me: str) -> dict[str, list[dict]]:
        """Top-level messages newer than what you read, per conversation, not your own."""
        out: dict[str, list[dict]] = {}
        for conv, d in self.q("SELECT m.conv, m.data FROM msgs m JOIN convs c ON c.id=m.conv "
                              "WHERE c.gone=0 AND m.top=1 AND m.ts > c.last_read AND "
                              "coalesce(m.user,'') != ? ORDER BY m.ts", me):
            m = json.loads(d)
            if m.get("subtype") not in QUIET:
                out.setdefault(conv, []).append(m)
        return out

    def search(self, words: list[str], limit=60) -> list[tuple[str, dict]]:
        """Local full-text-ish search: every word somewhere in the text (case-insensitive)."""
        if not words:
            return []
        where = " AND ".join("lower(json_extract(data,'$.text')) LIKE ?" for _ in words)
        rows = self.q(f"SELECT conv, data FROM msgs WHERE {where} ORDER BY ts DESC LIMIT ?",
                      *[f"%{w.lower()}%" for w in words], limit)
        return [(c, json.loads(d)) for c, d in rows]
