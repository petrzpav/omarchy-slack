"""Load ~/.config/petrzpav-slack/{config.toml,secrets}."""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "petrzpav-slack"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "petrzpav-slack"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "petrzpav-slack"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "petrzpav-slack"
ERROR_LOG = STATE_DIR / "errors.log"

DEFAULT_KEYS = {
    "palette": "ctrl+p",
    "palette2": "ctrl+k",
    "next_unread": "ctrl+down",
    "search": "ctrl+f",
    "react": "ctrl+r",
    "edit": "f2",
    "delete": "delete",
    "open": "ctrl+o",
    "browser": "alt+o",
    "attach": "alt+a",
    "paste": "ctrl+v,ctrl+shift+v,shift+insert",
    "copy": "ctrl+c",
    "prev_thread": "ctrl+pageup",
    "next_thread": "ctrl+pagedown",
    "mark_unread": "alt+u",
    "refresh": "f5",
    "help": "f1",
}


@dataclass
class Config:
    notify: bool = True           # desktop notifications for DMs, mentions and replies to your threads
    muted: list[str] = field(default_factory=list)   # channel names that never count as unread
    notify_channels: list[str] = field(default_factory=list)   # channels where every message notifies
    history: int = 100            # messages fetched when a conversation is opened
    selection: str = ""           # background of the selected message; empty = the Omarchy theme's
    keys: dict[str, str] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)

    @property
    def user_token(self) -> str:
        return self.secrets.get("SLACK_USER_TOKEN", "")

    @property
    def app_token(self) -> str:
        return self.secrets.get("SLACK_APP_TOKEN", "")


def read_secrets(path: Path) -> dict[str, str]:
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip("'\"")
    return out


def load() -> Config:
    raw = {}
    path = CONFIG_DIR / "config.toml"
    if path.exists():
        raw = tomllib.loads(path.read_text())
    cfg = Config(
        notify=bool(raw.get("notify", True)),
        muted=[m.lstrip("#") for m in raw.get("muted", [])],
        notify_channels=[m.lstrip("#") for m in raw.get("notify_channels", [])],
        history=int(raw.get("history", 100)),
        selection=raw.get("selection", ""),
        keys={**DEFAULT_KEYS, **raw.get("keys", {})},
        secrets=read_secrets(CONFIG_DIR / "secrets"),
    )
    for k in ("SLACK_USER_TOKEN", "SLACK_APP_TOKEN"):
        if os.environ.get(k):
            cfg.secrets[k] = os.environ[k]
    return cfg


def save_secret(name: str, value: str):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = CONFIG_DIR / "secrets"
    lines = [l for l in (path.read_text().splitlines() if path.exists() else [])
             if not l.strip().startswith(name + "=")]
    lines.append(f"{name}={value}")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
