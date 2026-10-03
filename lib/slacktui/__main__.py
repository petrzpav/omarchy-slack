import argparse
import json
import sys
import webbrowser
from pathlib import Path

from . import config

MANIFEST = Path(__file__).resolve().parents[2] / "examples" / "slack-app-manifest.json"


def cmd_auth(args):
    print(f"""1. Create the app: https://api.slack.com/apps?new_app=1 → From a manifest → your workspace
   → paste the JSON from {MANIFEST} → Create → Install to Workspace → Allow.
2. OAuth & Permissions → "User OAuth Token" (xoxp-…).
3. Basic Information → App-Level Tokens → Generate → scope connections:write (xapp-…).""")
    webbrowser.open("https://api.slack.com/apps?new_app=1")
    user = input("User OAuth Token (xoxp-…): ").strip()
    app = input("App-Level Token (xapp-…): ").strip()
    from .api import Slack
    me = Slack(user).auth()
    config.save_secret("SLACK_USER_TOKEN", user)
    config.save_secret("SLACK_APP_TOKEN", app)
    print(f"Signed in as {me['user']} in {me['team']}.")
    print("Next: systemctl --user enable --now slack-sync.service   (live updates and notifications)")


def cmd_unread(cfg, args):
    from .sync import UNREAD_FILE, write_unread
    if not args.cached or not UNREAD_FILE.exists():
        from .db import Db
        db = Db()
        write_unread(db, cfg, (db.get("me") or {}).get("user_id", ""))
    try:
        data = json.loads(UNREAD_FILE.read_text())
    except (OSError, ValueError):
        data = {"mentions": 0, "unread": 0}
    print(json.dumps(data) if args.json else data["mentions"])


def cmd_daemon(cfg, args):
    from .sync import Sync, take_lock
    lock = take_lock(wait=False)
    if not lock:
        print("the client is listening right now; waiting for it to close", flush=True)
        lock = take_lock(wait=True)
    Sync(cfg, report=lambda s: print(s, flush=True)).run()


def main():
    p = argparse.ArgumentParser(prog="slack", description="Fast terminal Slack")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("auth", help="store your Slack tokens")
    sub.add_parser("daemon", help="keep the local copy live and notify (run by slack-sync.service)")
    u = sub.add_parser("unread", help="print the number of unread mentions and DMs")
    u.add_argument("--cached", action="store_true")
    u.add_argument("--json", action="store_true")
    g = sub.add_parser("open", help="open the client on a conversation")
    g.add_argument("channel", help="conversation id")
    g2 = sub.add_parser("goto", help="show a message in the client (opening it if needed)")
    g2.add_argument("channel")
    g2.add_argument("ts", nargs="?")
    g2.add_argument("thread", nargs="?")
    from . import tools
    tools.add_parsers(sub)
    args = p.parse_args()

    if args.cmd == "auth":
        return cmd_auth(args)
    cfg = config.load()
    if args.cmd == "unread":
        return cmd_unread(cfg, args)
    if not cfg.user_token:
        sys.exit("No Slack token yet: run `slack auth`")
    if getattr(args, "func", None):
        return args.func(cfg, args)
    if args.cmd == "goto":
        from .sync import goto
        return goto(args.channel, args.ts, args.thread)
    if args.cmd == "daemon":
        cmd_daemon(cfg, args)
    else:
        from .app import SlackApp
        app = SlackApp(cfg)
        if args.cmd == "open":
            import time
            app.db.put("goto", {"cid": args.channel, "at": time.time()})
        app.run()


if __name__ == "__main__":
    main()
