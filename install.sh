#!/bin/bash
#
# install.sh - put `slack` on your PATH, link the sync service and start a
# config from examples/. Safe to run again: it never replaces a file it didn't create,
# and never touches an existing config.
#
#   install.sh              install
#   install.sh --skill      install, and also let Claude Code use `slack` (~/.claude/skills/slack)
#   install.sh --remove     undo all of it (your config and local copy stay)

set -euo pipefail
root=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
bin="$HOME/.local/bin"
conf="${XDG_CONFIG_HOME:-$HOME/.config}/petrzpav-slack"
unit="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/slack-sync.service"

mine_link() { [[ -L $1 && $(readlink -f "$1") == "$root"/* ]]; }

link() {
  local target="$bin/$(basename "$1")"
  if [[ -e $target || -L $target ]] && ! mine_link "$target"; then
    echo "skipped $target: it already exists and isn't from this plugin"
    return
  fi
  ln -sfn "$1" "$target"
}

if [[ ${1:-} == --remove ]]; then
  # Only stop, disable and unlink the unit if it's ours; a foreign slack-sync.service stays untouched.
  if mine_link "$unit"; then
    systemctl --user disable --now slack-sync.service >/dev/null 2>&1 || true
    rm -f "$unit"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
  fi
  for f in "$bin/slack" "$bin/slack-window" "$HOME/.claude/skills/slack"; do
    mine_link "$f" && rm -f "$f"
  done
  echo "Removed. Your config ($conf) and local copy (~/.local/share/petrzpav-slack) are left in place."
  exit 0
fi

mkdir -p "$bin"
link "$root/bin/slack"
link "$root/bin/slack-window"
if [[ ${1:-} == --skill ]]; then   # only when asked: a skill is visible to Claude Code in every project
  skill="$HOME/.claude/skills/slack"
  if [[ -e $skill || -L $skill ]] && ! mine_link "$skill"; then
    echo "skipped $skill: it already exists and isn't from this plugin"
  else
    mkdir -p "$HOME/.claude/skills" && ln -sfn "$root/skill" "$skill"
  fi
fi

if [[ ! -e $conf ]]; then
  mkdir -p "$conf"
  cp "$root/examples/config.toml" "$root/examples/secrets" "$conf/"
  chmod 600 "$conf/secrets"
fi

if [[ -e $unit || -L $unit ]] && ! mine_link "$unit"; then
  echo "skipped $unit: it already exists and isn't from this plugin"
else
  systemctl --user link "$root/systemd/slack-sync.service" >/dev/null
fi

cat <<MSG
Installed. Next:
  1. slack auth                                          # create the Slack app, paste its two tokens
  2. systemctl --user enable --now slack-sync.service    # live updates + notifications, also when closed
  3. slack-window                                        # open it
MSG
