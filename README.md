# panepost

**A tmux-native control desk for mixed AI coding agents.**
If you run Claude Code, Codex, opencode and others side by side in tmux, `ag` lists them all, shows which are **waiting for you**, lets them **message each other safely by name**, and helps you **restore everything after a reboot**. You keep using tmux exactly as before.

```
$ ag ls -t
  2m  wait  claude   3f1c9a20  api:1.2            api-refactor                   ~/code/api
  5m  busy  codex    01a1b5fe  web:2.1            Fix the login redirect         ~/code/web
  1h  idle  claude   9d0e4b11  web:1.1            web-c4                         ~/code/web
```

> Status: **0.0.1**, early. Tested on macOS + tmux 3.6 with Claude Code and Codex CLI. Linux should work for the CLI (untested).

## Why

- **Cross-vendor messaging.** Claude Code and Codex can't talk to each other natively. `ag send codex-web "tests pass, please deploy"` routes to Codex through its own `codex queue` (never touches the input box) and to other agents by typing into their tmux pane.
- **Safe by default.** Refuses to type into a pane whose input box holds your unsent draft; never sends Ctrl-C or Enter-spam to a busy agent; refuses ambiguous targets and lists candidates.
- **"Who's waiting for me?"** Detects permission prompts / question dialogs (Claude, Codex, opencode) as `wait`, besides `draft`, `busy`, `idle`.
- **Reply lines.** `prefix + a` copies `ag send %12 "…"   # claude api-refactor @ tmux api:1.2` — paste it into another agent and it knows how to report back.
- **Reboot recovery.** Snapshots tmux layout plus each agent's session id; `ag restore --plan` prints the `tmux` + `claude --resume` / `codex resume` commands (it never runs them for you).
- **No lock-in.** Plain tmux, a single stdlib-only Python file, metadata only (never reads conversation content or credentials).

## Install

```bash
pipx install git+https://github.com/jefftko/panepost   # or: pip install --user git+https://...
ag tmux-snippet >> ~/.tmux.conf && tmux source-file ~/.tmux.conf
```

Requirements: Python ≥ 3.9, tmux ≥ 3.2 (popups), [fzf](https://github.com/junegunn/fzf) for the pickers.

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
| Claude Code | process in the pane | `~/.claude/sessions/<pid>.json` | tmux typing (draft-safe) |
| Codex CLI | process in the pane | hook log, else footer name ↔ `~/.codex/state_*.sqlite` (read-only) | `codex queue --thread` |
| opencode / agy / cursor-agent / gemini | process in the pane | — | tmux typing |

Status comes from the bottom lines of `tmux capture-pane -e`: dim text after the prompt is a placeholder, normal text is a draft. These are **undocumented internals of each CLI** and may change with their releases — expect the occasional `?` (unconfirmed id) until fixed.

State lives in `$XDG_STATE_HOME/panepost` (`~/.local/state/panepost`): `registry.json`, `snapshots/`, `sent.jsonl` (time, from, to, SHA-256 — no message text), `aliases.json`, `projects.json`.

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
