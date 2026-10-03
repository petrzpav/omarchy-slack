"""A made-up Slack for the demo: Mia's small web studio and the Alderbrew relaunch (the same
story as the Trello and Mail demos). Written into a fresh database, so the client opens it
as if the sync service had filled it."""

import time
from datetime import datetime, timedelta

ME = "U0MIA"
USERS = {
    "U0MIA": ("mia", "Mia Lind"),
    "U0JONAS": ("jonas", "Jonas Berg"),
    "U0CLARA": ("clara", "Clara Holm"),
    "U0TOM": ("tomas", "Tomas Ek"),
}
NAME = {k: v[0] for k, v in USERS.items()}
TAPROOM = {"id": "F0TAPROOM", "name": "taproom-at-night.jpg", "mimetype": "image/jpeg", "size": 2_481_337,
           "thumb_480": "demo://taproom", "thumb_480_w": 480, "thumb_480_h": 320,
           "url_private": "demo://taproom"}

# channel -> (topic, [(minutes ago, user, text, extras)]); extras: replies [(min ago, user, text)],
# reactions {name: [users]}, files, blocks, bot
NOW = time.time()


def at(minutes: float) -> str:
    return f"{NOW - minutes * 60:.6f}"


def yesterday(hour: int, minute: int) -> float:
    d = datetime.now() - timedelta(days=1)
    return (NOW - d.replace(hour=hour, minute=minute, second=0).timestamp()) / 60


CHANNELS = {
    "general": ("Lind Studio · small web studio, Brno", [
        (yesterday(9, 12), "U0TOM", "Morning! Coffee machine is fixed :coffee:", {"reactions": {"tada": ["U0MIA", "U0CLARA"]}}),
        (yesterday(9, 30), "U0MIA", "Thanks Tomas. Reminder: studio lunch on Friday, I'm buying :pizza:", {}),
        (yesterday(16, 40), "U0CLARA", "Out tomorrow morning, dentist. Back after lunch.", {}),
        (95, "U0MIA", "Alderbrew launch moves to *October 14*. Everything else stays as planned.",
         {"reactions": {"+1": ["U0TOM", "U0CLARA"]}}),
        (52, "U0TOM", "Noted. I'll move the deploy too.", {}),
    ]),
    "alderbrew": ("Relaunch of alderbrew.cz · launch October 14", [
        (yesterday(10, 5), "U0JONAS", "Can we move the launch from October 7 to October 14? Our new menu is not printed yet.", {}),
        (yesterday(10, 21), "U0MIA", "Sure, October 14 works. I'll tell the printer and update the press kit.", {}),
        (yesterday(15, 2), "U0MIA", "Homepage hero is up on staging: https://staging.alderbrew.cz", {
            "replies": [(yesterday(15, 30), "U0JONAS", "Looks great! Can the photo be a bit warmer?"),
                        (yesterday(15, 44), "U0CLARA", "I'll use the taproom one from the extra day, it's warmer."),
                        (yesterday(16, 10), "U0JONAS", "Perfect :raised_hands:")]}),
        (41, "U0JONAS", "Printer says the menus are ready on Thursday :tada:", {}),
        (38, "U0JONAS", "<@U0MIA> can the countdown on the homepage show the 14th already?", {}),
    ]),
    "design": ("Work in progress, feedback welcome", [
        (yesterday(11, 0), "U0CLARA", "New logo files are in the shared drive.", {"reactions": {"fire": ["U0MIA"]}}),
        (27, "U0CLARA", "Taproom at night, from the extra photo day. For the Alderbrew hero?",
         {"files": [TAPROOM], "reactions": {"heart_eyes": ["U0TOM"]}}),
    ]),
    "dev": ("Deploys and servers", [
        (yesterday(14, 0), "U0TOM", "Contact form now goes to Jonas and Clara both.", {}),
        (64, None, "Deployed alderbrew.cz to staging", {"bot": "Deploys", "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "Deployed to staging ✓"}},
            {"type": "section", "fields": [{"type": "mrkdwn", "text": "*Site*\nalderbrew.cz"},
                                           {"type": "mrkdwn", "text": "*By*\nTomas Ek"}]},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "a41f9c2 · Homepage hero, mobile crop · 48 s"}]},
            {"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Open staging"},
                                              "url": "https://staging.alderbrew.cz"}]}]}),
    ]),
}
DMS = {
    "U0CLARA": [
        (yesterday(17, 2), "U0MIA", "Thanks for the logo files!", {}),
        (19, "U0CLARA", "Can you look at the hero crop for mobile? The photo is in #design", {}),
    ],
    "U0TOM": [
        (yesterday(13, 15), "U0TOM", "The new laptop arrived, setting it up today.", {}),
        (yesterday(13, 20), "U0MIA", "Great, enjoy :rocket:", {}),
    ],
    "U0JONAS": [
        (yesterday(18, 0), "U0JONAS", "Invoice received, paying it this week. Thank you!", {}),
    ],
}
# what you've read up to: everything except these (minutes ago)
UNREAD_FROM = {"alderbrew": 45, "design": 30, "U0CLARA": 20}
VISITED = {"general": 5, "alderbrew": 4, "U0CLARA": 3, "design": 2, "dev": 1}


def build():
    users = [{"id": uid, "name": n, "real_name": real, "profile": {"real_name": real, "display_name": ""}}
             for uid, (n, real) in USERS.items()]
    convs, msgs = [], {}
    for name, (topic, rows) in CHANNELS.items():
        cid = "C0" + name.upper()
        convs.append({"id": cid, "name": name, "is_channel": True, "is_private": False,
                      "is_general": name == "general", "topic": {"value": topic}, "_key": name})
        msgs[cid] = rows
    for uid, rows in DMS.items():
        cid = "D0" + uid[2:]
        convs.append({"id": cid, "is_im": True, "user": uid, "_key": uid})
        msgs[cid] = rows
    return users, convs, msgs


def message(cid, minutes, user, text, extra) -> list[dict]:
    m = {"type": "message", "ts": at(minutes), "text": text}
    if user:
        m["user"] = user
    if extra.get("bot"):
        m["bot_id"] = "B0DEPLOY"
        m["bot_profile"] = {"name": extra["bot"]}
        m["username"] = extra["bot"]
    for k in ("files", "blocks"):
        if extra.get(k):
            m[k] = extra[k]
    if extra.get("reactions"):
        m["reactions"] = [{"name": n, "users": u, "count": len(u)} for n, u in extra["reactions"].items()]
    out = [m]
    replies = extra.get("replies") or []
    if replies:
        m["thread_ts"] = m["ts"]
        m["reply_count"] = len(replies)
        m["reply_users"] = list(dict.fromkeys(u for _, u, _ in replies))
        m["latest_reply"] = at(replies[-1][0])
        out += [{"type": "message", "ts": at(mm), "thread_ts": m["ts"], "user": u, "text": t}
                for mm, u, t in replies]
    return out


def seed(db, thumbs_dir):
    """Fill `db` with the made-up workspace and draw the photo's thumbnail into `thumbs_dir`."""
    users, convs, msgs = build()
    db.put("me", {"user_id": ME, "user": "mia", "team": "Lind Studio"})
    db.put_users(users)
    keys = {c["id"]: c.pop("_key") for c in convs}
    db.put_convs(convs)
    for cid, rows in msgs.items():
        db.put_msgs(cid, [x for row in rows for x in message(cid, *row)], window=True)
        key = keys[cid]
        db.set_last_read(cid, at(UNREAD_FROM[key]) if key in UNREAD_FROM else at(0))
        db.visited(cid, NOW - 3600 + VISITED.get(key, 0))
    db.put("last_conv", "C0GENERAL")
    db.put("recent_emoji", ["+1", "heart", "tada", "raised_hands", "eyes", "white_check_mark"])
    draw_taproom(thumbs_dir / f"{TAPROOM['id']}.img")


def draw_taproom(path):
    """A small night scene: a lit taproom under string lights."""
    from PIL import Image, ImageDraw
    w, h = 480, 320
    im = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(im)
    for y in range(h):                                   # night sky to a warm street
        f = y / h
        d.line([(0, y), (w, y)], fill=(int(14 + 40 * f * f), int(18 + 22 * f), int(48 - 10 * f)))
    d.ellipse([388, 34, 428, 74], fill=(238, 232, 200))                     # moon
    d.rectangle([60, 120, 420, 300], fill=(52, 34, 30))                     # building
    d.polygon([(40, 124), (240, 64), (440, 124)], fill=(70, 44, 38))        # roof
    for x0 in (90, 190, 290):                                               # warm windows
        d.rectangle([x0, 160, x0 + 72, 236], fill=(250, 178, 74))
        d.rectangle([x0 + 34, 160, x0 + 38, 236], fill=(120, 72, 40))
    d.rectangle([380, 200, 408, 300], fill=(210, 120, 50))                  # door
    for i in range(14):                                                     # string lights
        x = 30 + i * 32
        y = 108 + int(10 * ((i % 4) - 1.5) ** 2 / 2)
        d.ellipse([x - 5, y - 5, x + 5, y + 5], fill=(255, 214, 120))
    d.rectangle([0, 300, w, h], fill=(36, 30, 34))                          # street
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, "PNG")
