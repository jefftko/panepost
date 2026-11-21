# Changelog

## Unreleased

- Show the tmux session, window name, indexes and pane ID alongside the agent name in send headers, send receipts and reply lines.
- Identify named Codex threads from wrapped footers when the title and directory match.
- Check for an idle empty input box before typing through tmux. Confirm the full input before submitting once.
- Bound the send receipt log to 8 MiB and retain up to 20,000 recent records on rotation.

## 0.0.1 — 2026-11-08

First public release.

- `ag ls` / `ag ls -t`: agents across all tmux sessions with state (wait / draft / busy / idle), session id and last activity.
- `ag send`: cross-agent messaging — Codex via native `codex queue`, others via draft-safe tmux typing; refuses ambiguous targets.
- `ag me`, `ag cp`: reply lines and quick copies; pasted reply lines are accepted as targets.
- `ag new`, `ag projects`, `ag pin`: start agents in a directory ranked by recent use.
- `ag restore --plan`: rebuild tmux sessions and resume commands from snapshots taken before the last reboot.
- tmux popups (`prefix A / N / a`) via `ag tmux-snippet`; Codex hook via `ag codex-hook-snippet`.
- AgentBar: optional macOS menu-bar app.
