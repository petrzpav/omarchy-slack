"""The Slack Web API, called with your user token (you read and write as yourself)."""

import time
from pathlib import Path

import httpx

BASE = "https://slack.com/api/"
# Methods that change something take JSON; the rest are plain GETs.
POST = ("chat.", "reactions.add", "reactions.remove", "conversations.mark", "conversations.open",
        "files.completeUploadExternal")


class SlackError(Exception):
    def __init__(self, method: str, error: str):
        super().__init__(f"{method}: {error}")
        self.error = error


class Slack:
    def __init__(self, token: str):
        self.token = token
        self.c = httpx.Client(timeout=30, headers={"Authorization": f"Bearer {token}"})

    def call(self, method: str, **params) -> dict:
        """One call; waits out rate limits (Slack says how long in Retry-After)."""
        for _ in range(8):
            if method.startswith(POST):
                r = self.c.post(BASE + method, json=params)
            else:
                r = self.c.get(BASE + method, params={k: v for k, v in params.items() if v is not None})
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "5")) + 0.5)
                continue
            data = r.json()
            if not data.get("ok"):
                raise SlackError(method, data.get("error", "?"))
            return data
        raise SlackError(method, "rate limited")

    def pages(self, method: str, key: str, **params) -> list:
        out, cursor = [], None
        while True:
            data = self.call(method, cursor=cursor, **params)
            out += data.get(key, [])
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return out

    # -- reading

    def auth(self) -> dict:
        return self.call("auth.test")

    def users(self) -> list[dict]:
        return self.pages("users.list", "members", limit=500)

    def conversations(self) -> list[dict]:
        return self.pages("users.conversations", "channels", limit=500, exclude_archived=True,
                          types="public_channel,private_channel,mpim,im")

    def info(self, cid: str) -> dict:
        return self.call("conversations.info", channel=cid)["channel"]

    def history(self, cid: str, limit=100, oldest=None, latest=None, inclusive=None) -> list[dict]:
        return self.call("conversations.history", channel=cid, limit=limit, oldest=oldest,
                         latest=latest, inclusive=inclusive)["messages"]

    def replies(self, cid: str, ts: str) -> list[dict]:
        return self.pages("conversations.replies", "messages", channel=cid, ts=ts, limit=200)

    def search(self, query: str, count=40) -> list[dict]:
        return self.call("search.messages", query=query, count=count, sort="timestamp")["messages"]["matches"]

    def emoji(self) -> dict[str, str]:
        return self.call("emoji.list")["emoji"]

    def permalink(self, cid: str, ts: str) -> str:
        return self.call("chat.getPermalink", channel=cid, message_ts=ts)["permalink"]

    def download(self, url: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.c.stream("GET", url, follow_redirects=True) as r:
            r.raise_for_status()
            with dest.open("wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        return dest

    # -- writing

    def post(self, cid: str, text: str, thread_ts=None, broadcast=False) -> dict:
        params = {"channel": cid, "text": text, "link_names": True, "unfurl_links": False, "unfurl_media": False}
        if thread_ts:
            params["thread_ts"] = thread_ts
            if broadcast:
                params["reply_broadcast"] = True
        return self.call("chat.postMessage", **params)["message"]

    def edit(self, cid: str, ts: str, text: str):
        self.call("chat.update", channel=cid, ts=ts, text=text, link_names=True)

    def delete(self, cid: str, ts: str):
        self.call("chat.delete", channel=cid, ts=ts)

    def react(self, cid: str, ts: str, name: str, add=True):
        try:
            self.call("reactions.add" if add else "reactions.remove", channel=cid, timestamp=ts, name=name)
        except SlackError as e:
            if e.error not in ("already_reacted", "no_reaction"):
                raise

    def mark(self, cid: str, ts: str):
        self.call("conversations.mark", channel=cid, ts=ts)

    def open_im(self, user: str) -> str:
        return self.call("conversations.open", users=user)["channel"]["id"]

    def upload(self, cid: str, path: Path, thread_ts=None, comment="") -> None:
        size = path.stat().st_size
        up = self.call("files.getUploadURLExternal", filename=path.name, length=size)
        with path.open("rb") as f:
            r = httpx.post(up["upload_url"], content=f.read(), timeout=300)
        r.raise_for_status()
        params = {"files": [{"id": up["file_id"], "title": path.name}], "channel_id": cid}
        if thread_ts:
            params["thread_ts"] = thread_ts
        if comment:
            params["initial_comment"] = comment
        self.call("files.completeUploadExternal", **params)
