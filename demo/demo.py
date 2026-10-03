"""Run the Slack client on a made-up workspace, offline.

    demo/slack-demo             the client, in this terminal
    demo/slack-demo record      render demo/out/slack-demo.mp4 (headless)

Everything lives in a temp dir and Slack is replaced by demo/fake.py: send, react and
search freely, nothing reaches slack.com and your own Slack copy is never opened.
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def setup():
    tmp = Path(tempfile.mkdtemp(prefix="slack-demo-"))
    for var, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_DATA_HOME", "data"),
                     ("XDG_CACHE_HOME", "cache")):
        os.environ[var] = str(tmp / sub)
    sys.path[:0] = [str(HERE.parent / "lib"), str(HERE)]

    import webbrowser
    webbrowser.open = lambda *a, **kw: True

    import sample
    from slacktui import config, thumbs
    from slacktui.db import Db
    from slacktui.sync import LISTENER_FILE

    sample.seed(Db(), thumbs.THUMBS)
    LISTENER_FILE.parent.mkdir(parents=True, exist_ok=True)       # "● live" in the top bar
    LISTENER_FILE.write_text(json.dumps({"state": "connected", "since": 0, "error": ""}))
    cfg = config.load()
    cfg.notify = False
    cfg.selection = "#3b4252"
    cfg.secrets = {"SLACK_USER_TOKEN": "demo", "SLACK_APP_TOKEN": "demo"}
    return cfg, tmp


def make_app(cfg):
    import fake
    from slacktui.app import SlackApp
    from slacktui.db import Db

    db = Db()
    return SlackApp(cfg, db=db, api=fake.FakeSlack(db), listen=False)


def main():
    args = sys.argv[1:]
    cfg, tmp = setup()
    try:
        if args[:1] == ["record"]:
            import record
            record.main(cfg, make_app)
        else:
            make_app(cfg).run()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
