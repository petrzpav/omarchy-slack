# Change Log
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/)
and this project adheres to [Semantic Versioning](http://semver.org/).

## [0.2.1] - 2026-10-06

### Added

- Instructions for AI agents: the repository follows Flow (ig-flow and ig-changelog skills).

## [0.2.0] - 2026-10-06

### Added

- Scriptable commands (`inbox`, `convs`, `read`, `thread`, `search`, `users`, `post`, `react`, `edit`, `delete`, `mark`) on the shared local copy, and a Claude Code skill.
- Offline demo on a made-up workspace, and a headless recorder for the demo video.
- Select any text in the messages with the mouse; `Ctrl+C` copies it.
- `Ctrl+click` opens the link under the mouse; links in code blocks show as plain URLs and open too.
- Trello card and board links open in the Trello client (`trello open`), or in the browser when it doesn't know them.

### Changed

- F1 help in sections, one key per row, scrollable.
- Bigger inline thumbnails.
- Link previews are not shown, and messages are sent without them.

### Fixed

- URLs in a code block are sent whole; Slack no longer leaves out trailing dots and brackets.

## [0.1.0] - 2026-10-02

### Added

- Fast terminal Slack client: one conversation on the screen, `Ctrl+P` to any other, live over Socket Mode.
- Block Kit messages, inline image and video thumbnails, pasting images and screenshots (also in Ghostty and foot), thread cycling, Omarchy notifications, copying messages.
- Bar widget with unread mentions and DMs.
- `install.sh` with `--remove`; it leaves a `slack-sync.service` that isn't this plugin's alone.

[0.2.1]: https://https://github.com/petrzpav/omarchy-slack/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/petrzpav/omarchy-slack/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/petrzpav/omarchy-slack/releases/tag/v0.1.0
