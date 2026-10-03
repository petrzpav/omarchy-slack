"""A Slack that answers from the demo's own database, so the client behaves as it does
online (a message you send comes back as sent) and nothing ever reaches slack.com."""

import time

from slacktui.api import SlackError


class FakeSlack:
    def __init__(self, db):
        self.db = db

    # -- reading

    def auth(self) -> dict:
        return self.db.get("me")

    def users(self) -> list[dict]:
        return list(self.db.users().values())

    def conversations(self) -> list[dict]:
        return [{k: v for k, v in c.items() if not k.startswith("_")} for c in self.db.convs().values()]

    def info(self, cid: str) -> dict:
        return {"id": cid, "last_read": self.db.last_read(cid)}

    def history(self, cid: str, limit=100, oldest=None, latest=None, inclusive=None) -> list[dict]:
        msgs = [m for m in self.db.msgs(cid, 100000)
                if (not oldest or m["ts"] > oldest) and (not latest or m["ts"] < latest or inclusive and m["ts"] == latest)]
        return list(reversed(msgs[-limit:]))

    def replies(self, cid: str, ts: str) -> list[dict]:
        return self.db.thread(cid, ts)

    def search(self, query: str, count=40) -> list[dict]:
        return []

    def emoji(self) -> dict[str, str]:
        return {}

    def permalink(self, cid: str, ts: str) -> str:
        return f"https://lind-studio.slack.com/archives/{cid}/p{ts.replace('.', '')}"

    def download(self, url, dest):
        raise SlackError("files.download", "demo")

    # -- writing

    def post(self, cid: str, text: str, thread_ts=None, broadcast=False) -> dict:
        time.sleep(0.25)                                   # the round trip, so "sending…" shows
        m = {"type": "message", "ts": f"{time.time():.6f}", "user": self.db.get("me")["user_id"], "text": text}
        if thread_ts:
            m["thread_ts"] = thread_ts
        return m

    def edit(self, cid, ts, text):
        pass

    def delete(self, cid, ts):
        pass

    def react(self, cid, ts, name, add=True):
        pass

    def mark(self, cid, ts):
        pass

    def open_im(self, user: str) -> str:
        return next(c["id"] for c in self.db.convs().values() if c.get("user") == user)

    def upload(self, cid, path, thread_ts=None, comment=""):
        pass
