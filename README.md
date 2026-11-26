# panepost

**A tmux-native control desk for mixed AI coding agents.**
If you run Claude Code, Codex, opencode and others side by side in tmux, `ag` lists them all, shows which are **waiting for you**, lets them **message each other safely by name**, and helps you **restore everything after a reboot**. You keep using tmux exactly as before.

```
$ ag ls -t
  2m  wait  claude   3f1c9a20  api:1.2            api-refactor                   ~/code/api
  5m  busy  codex    01a1b5fe  web:2.1            Fix the login redirect         ~/code/web
  1h  idle  claude   9d0e4b11  web:1.1            web-c4                         ~/code/web
```

> Status: **0.0.2**, early. Tested on macOS + tmux 3.6 with Claude Code and Codex CLI.

Use `ag where <target>` to print a readable location and a copyable `ag go` command without moving focus.

## Why

- **Cross-vendor messaging.** `ag send codex-web "tests pass"` uses `codex queue` for a verified Codex thread. When the receiving CLI has a recognizable idle input box, PanePost can also submit the message through tmux.
- **Draft protection.** The tmux route stops at drafts, busy panes, choice menus and unknown input layouts. It checks the whole message in the input box before submitting once.
- **"Who's waiting for me?"** Detects known permission prompts and question dialogs as `wait`, alongside `draft`, `busy` and `idle`.
- **Readable reply lines.** `prefix + a` copies `ag send %12 "…"   # claude work:ops(1)-1(%12) · API review`. The window and pane are visible in the message header, the send receipt and the reply line.
- **Reboot recovery.** Snapshots tmux layout plus each agent's session id; `ag restore --plan` prints the `tmux` + `claude --resume` / `codex resume` commands (it never runs them for you).
- **No lock-in.** Plain tmux and a stdlib-only Python CLI. PanePost uses the visible terminal layout and local session metadata to route messages.

## Install

```bash
pipx install git+https://github.com/jefftko/panepost   # or: pip install --user git+https://...
ag tmux-snippet >> ~/.tmux.conf && tmux source-file ~/.tmux.conf
```

Requirements: Python ≥ 3.10, tmux ≥ 3.2 (popups), [fzf](https://github.com/junegunn/fzf) for the pickers.

Optional — let Codex sessions register their thread id (more reliable than reading the footer):
`ag codex-hook-snippet` prints a `~/.codex/hooks.json` fragment to merge by hand.

## tmux keys (from `ag tmux-snippet`)

| Key | Action |
|---|---|
| `prefix A` | list all agents (waiting/draft/busy first). Enter jumps. `^Y` reply line · `^O` session id · `^R` resume command · `^X` dir · `^P` pane id |
| `prefix N` | pick a directory (pinned, then most recently used — or type a path) → claude / codex / shell → new window |
| `prefix a` | copy this pane's reply line |

> Using an IME (Chinese/Japanese)? Switch to half-width/ASCII before pressing the keys.

## Commands

```bash
ag ls [filter] [-t] [--json]     # grouped by tmux session; -t = flat, most recent first
ag send [--dry-run] <target> <message>
ag me [--copy]                   # your reply line
ag cp <target> [send|id|pane|addr|resume|cwd|name]
ag where <target>                # show the pane and a pasteable jump command
ag go <target>                   # select pane (raises the Ghostty window on macOS)
ag new <dir> [--agent claude|codex|shell] [--session S] [--name N] [--go]
ag projects [-n N] / ag pin [--rm] <dir>
ag alias <name> <target>         # bound to the session id, survives resume
ag restore --plan [--session S] [--all] [--from snapshot.json]
ag scan [--quiet]                # refresh registry + snapshot (run it from cron/launchd if you like)
```

**Targets:** alias · `%12` · `session:window[.pane]` · session-id prefix (≥ 8 chars) · agent name · window name · unique substring of name/dir · or a pasted reply line.

## How it works

| Agent | Detected by | Session id from | Delivery |
|---|---|---|---|
| Claude Code | process in the pane | `~/.claude/sessions/<pid>.json` | verified tmux input box |
| Codex CLI | process in the pane | hook log, else footer name and directory ↔ `~/.codex/state_*.sqlite` (read-only) | `codex queue --thread`, or verified tmux input box |
| agy / Antigravity | process in the pane | — | verified tmux input box |
| opencode / cursor-agent / gemini | process in the pane | — | listing and navigation |

The status parser uses the visible input box and footer in `tmux capture-pane -e`. These layouts can change between CLI releases. If delivery is uncertain, check the pane before retrying. PanePost may have left text in the input box.

Use `ag send --dry-run %12 "example"` to see the source, destination and planned route. In `work:ops(1)-1(%12)`, `work` is the session, `ops` is window 1, and the final `1` is the pane index. You can address this pane as `%12`, `work:1.1` or `'work:ops(1)-1'` in a shell.

State lives in `$XDG_STATE_HOME/panepost` (`~/.local/state/panepost`). The bounded `sent.jsonl` stores time, source, destination, SHA-256 and delivery status. Other files hold the registry, snapshots, aliases and projects.

## Configuration (optional)

`~/.config/panepost/config.toml` (Python ≥ 3.11 reads it):

```toml
state_dir = "~/.local/state/panepost"
terminal = "auto"            # auto | ghostty | none — how `ag go` raises windows

[agents]                     # commands typed by `ag new` / restore
claude = "claude"
codex = "codex"

[codex]
bin = "/opt/homebrew/bin/codex"
proxy = ""                   # e.g. "http://127.0.0.1:8888" if your codex needs one
state_db = ""                # override ~/.codex/state_*.sqlite
```

## AgentBar (macOS menu bar, optional)

`macos/AgentBar` — a small SwiftUI menu-bar app on top of `ag ls --json`: counts in the menu bar, notifications when an agent starts waiting or finishes, click-to-copy reply lines, new/restore tabs. Build locally: `cd macos/AgentBar && ./build.sh --install` (ad-hoc signed; Xcode command line tools required).

## Development

```bash
python3 -m unittest discover -s tests -v
```

Captured-screen fixtures live in the tests; when a CLI changes its UI, add a fixture first.

## License

MIT © jefftko
