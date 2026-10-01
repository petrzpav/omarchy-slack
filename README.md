# Slack for Omarchy

A fast terminal Slack client for [Omarchy](https://omarchy.org). One conversation fills the screen,
with no sidebar: `Ctrl+P` jumps to any channel or person, `Ctrl+↓` to the next unread one. Arrow
keys and Ctrl shortcuts, no vim.

## How it works

It uses the official Slack API with a **user token**, so you read and write as yourself. That means
an internal app in your workspace, created in a minute from the manifest in `examples/`. Internal
apps keep Slack's full API rate limits.

Everything is painted from a local SQLite copy (`~/.local/share/petrzpav-slack/slack.db`) at once.
`slack-sync.service` keeps it live over Socket Mode, also while the client is closed. It sends
desktop notifications (DMs, mentions, replies in your threads) and keeps the unread count for the
bar. Clicking a notification opens the conversation. While the service isn't running, the client
listens itself. What you send shows at once and goes to Slack behind it.

## Keys

| Key | |
|---|---|
| `Ctrl+P` / `Ctrl+K` | go to a channel or person (unread and mentions first, then recent) |
| `Ctrl+↓` | next unread conversation, mentions first |
| `Ctrl+F` | search messages: local copy as you type, first line asks Slack |
| `Enter`, `Shift+Enter` | send, new line (`Ctrl+J` also) |
| `Tab` | complete `@name`, `#channel`, `:emoji:` |
| `↑` in an empty box | select messages; `↑ ↓` move, typing goes back to the box |
| `Enter` on a message | open its thread (reply there; `Esc` back) |
| `Ctrl+R` | react (toggles) |
| `F2`, `Delete` | edit, delete your message |
| `Ctrl+O`, `Alt+O` | open a file or link of the message, open it in the browser |
| `Alt+U` | mark unread from the selected message |
| `Alt+A` | send a file (the text in the box goes with it) |
| `F5`, `F1`, `Ctrl+Q` | refresh, help, quit |

Every key can be changed in `~/.config/petrzpav-slack/config.toml` under `[keys]`.

## Install

```
omarchy plugin add <this repo> --enable
~/.config/omarchy/plugins/petrzpav.slack/install.sh
slack auth
systemctl --user enable --now slack-sync.service
slack-window
```

`slack auth` walks you through it: create an app **from a manifest** at
<https://api.slack.com/apps?new_app=1>, paste `examples/slack-app-manifest.json`, install it to your
workspace. Then paste its *User OAuth Token* (`xoxp-…`) and an *App-Level Token* with
`connections:write` (`xapp-…`).

## Not here

Huddles and calls, canvases, lists, workflows and Block Kit buttons. Use `Alt+O` to open the
message in the browser for those. Slash commands can't be sent through the API.

## License

MIT
