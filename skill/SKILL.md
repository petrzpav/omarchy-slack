---
name: slack
description: >
  Read and write the user's Slack workspace as the user, through the `slack` CLI of the
  petrzpav.slack Omarchy plugin. Use whenever the user asks about Slack: "what's unread on Slack",
  "what did X write", "catch me up on #channel", "find the message about …", "reply in that thread",
  "send X a message", "react with …", "mark it read", or a file someone sent. Prefer it over the
  Slack web app or raw API calls.
---

# Slack (`slack` CLI)

`slack` with no arguments opens the user's TUI client, so never run it bare. Always give it a subcommand.

## How it works

- Everything runs as the user, with the user's own token. Posts appear under their name.
- A local SQLite copy (`~/.local/share/petrzpav-slack/slack.db`) is kept live by `slack-sync.service`.
- Reads from the copy are instant. `read` and `thread` also refresh from the API.
- Anything these commands fetch or send also shows up in the TUI.

## Naming things

- **Conversation:** its id (`C…`, `D…`, `G…`), `#channel`, `@person` (their DM, opened if needed), or a unique part of the name. Accents and case don't matter.
- **Message:** its `ts`, the first column of every listing, e.g. `1790943325.431569`. A thread is named by its parent's ts.

## Reading

```
slack inbox [--per 15]             # every unread message, DMs and @mentions first — start here for "catch me up"
slack convs [--unread] [-n 20]     # conversations, most recent first, with unread/mention counts
slack read CONV [-n 30] [--cached] [--mark-read]
slack thread CONV TS               # parent + all replies
slack search 'words' [-n 30]       # local copy, every word must match
slack search 'QUERY' --remote      # Slack's own search: from:@x in:#y before:2026-10-01 has:link
slack users [name]                 # ids, handles, e-mails
slack link CONV TS                 # permalink
slack download CONV TS -o DIR      # files attached to a message
```

- Lines read `ts  time  author: text  [N replies]  :reaction:N`.
- Messages with `[N replies]` have a thread; open it with `slack thread`.
- Every read command takes `--json`.
- If `inbox` says the sync daemon isn't connected, the unread state may be stale. `slack read` the conversation to refresh it.

## Writing

```
slack post CONV 'text' [--thread TS] [--broadcast] [--file PATH]   # text '-' = stdin
slack edit CONV TS 'new text'      slack delete CONV TS   # only the user's own messages
slack react CONV TS emoji [--remove]
slack mark [CONV…] [--ts TS]       # mark read; with no CONV it marks EVERYTHING unread as read
```

- `@Full Name`, `@handle`, `#channel`, `@here` in text become real mentions.
- Slack formatting applies: `*bold*`, `_italic_`, `` `code` ``, ``` blocks, `> quote`.
- **A post, edit, delete or reaction is visible to other people at once.**
  - Show the exact text and target and get an explicit OK first, unless the user dictated the message and the target.
  - A reaction the user asked for needs no extra confirmation.
- Reply in the thread (`--thread TS`) when answering a threaded message. Don't post to the channel.
- Write in the language of the conversation, usually Czech. Keep it short and informal, like the user's own messages.
- Only `mark` read when the user asks, or after summarizing for them if they asked to "catch up and clear".

## Notes

- The #poptavky channel belongs to the Poptávky bot. Decide demands with the `poptavky` skill, not by reacting here.
- Config: `~/.config/petrzpav-slack/config.toml`. Errors: `~/.local/state/petrzpav-slack/errors.log`. Never print `secrets`.
