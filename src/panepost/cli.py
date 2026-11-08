"""panepost (`ag`) — a tmux-native control desk for mixed AI coding agents.

  ag ls [filter] [-t] [--json]   list agents grouped by tmux session; -t = flat, most recent first
  ag send [--dry-run] <target> <message>
                                 deliver without interrupting: Codex via native `codex queue`,
                                 others via tmux (refused if the input box holds a draft)
  ag me [--pane %N] [--copy]     this pane's reply line:  ag send %12 "…"   # claude api @ tmux work:1.2
  ag cp <target> [send|id|pane|addr|resume|cwd|name]   copy to clipboard (default: send line)
  ag go <target>                 select that pane (and raise its terminal window when possible)
  ag new <dir> [--agent claude|codex|shell] [--session S] [--name N] [--go]
                                 start an agent in a directory (new tmux window / session)
  ag projects [--json] [-n N]    directories ranked by recent use (pinned first)
  ag pin [--rm] <dir>            pin a directory to the top of `ag projects`
  ag alias <name> <target>       stable alias (bound to the session id, survives resume)
  ag pick | ag pick-new          fzf pickers used by the tmux popups (prefix+A / prefix+N)
  ag scan [--quiet]              refresh the registry + periodic snapshot
  ag restore --plan [--from F] [--all] [--session S]
                                 print commands to rebuild sessions after a reboot (never executes)
  ag tmux-snippet | ag codex-hook-snippet   print config to add yourself
  ag hook codex <Event>          Codex hook entry: records pane <-> thread id (silent, never fails)
  ag --version

Targets: alias, %pane, session:window[.pane], session-id prefix (>=8), agent name, window name,
or a unique substring. A pasted `ag send X "…"  # …` line works as a target too.
Metadata only: never reads conversation content or credentials.
"""
import datetime, glob, hashlib, json, os, platform, re, shlex, shutil, sqlite3, subprocess, sys, time, unicodedata

from . import __version__

H = os.path.expanduser("~")
for _d in ("/opt/homebrew/bin", "/usr/local/bin", os.path.join(H, ".local/bin")):
    if _d not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = os.environ.get("PATH", "/usr/bin:/bin") + os.pathsep + _d


def _load_config():
    path = os.environ.get("PANEPOST_CONFIG") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.join(H, ".config"), "panepost", "config.toml")
    try:
        import tomllib
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (ImportError, OSError, ValueError):
        return {}


CFG = _load_config()
STATE = os.path.expanduser(os.environ.get("PANEPOST_STATE") or CFG.get("state_dir") or os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.join(H, ".local/state"), "panepost"))
REG = os.path.join(STATE, "registry.json")
SNAPS = os.path.join(STATE, "snapshots")
HOOKLOG = os.path.join(STATE, "codex-hooks.jsonl")
ALIASES = os.path.join(STATE, "aliases.json")
SENT = os.path.join(STATE, "sent.jsonl")
PINS = os.path.join(STATE, "projects.json")
CREATED = os.path.join(STATE, "created.jsonl")
CLAUDE_SESS = os.path.join(H, ".claude/sessions")
SNAP_EVERY_S, SNAP_KEEP_S = 15 * 60, 7 * 86400
BUSY_RE = re.compile(r"esc to interrupt|Working \(|Thinking…|Reviewing approval")
# approval / question dialogs that need the human (Claude permission + questions, Codex approvals, opencode)
WAIT_RE = re.compile(r"Do you want to (proceed|make this edit|create)|Would you like to (run|make|apply|allow)|"
                     r"Enter to select|Esc to cancel|Press enter to confirm|Yes, and don't ask again|"
                     r"Allow (once|always)|requires approval")
SEP = "\t"
CODEX_CFG = CFG.get("codex", {}) if isinstance(CFG.get("codex"), dict) else {}
AGENT_CMDS = {"claude": "claude", "codex": "codex"}
AGENT_CMDS.update(CFG.get("agents", {}) if isinstance(CFG.get("agents"), dict) else {})
TERMINAL = CFG.get("terminal", "auto")  # auto | ghostty | none


def codex_env():
    px = CODEX_CFG.get("proxy") or ""
    if not px:
        return None
    no = "localhost,127.0.0.1,::1"
    return dict(os.environ, HTTP_PROXY=px, HTTPS_PROXY=px, http_proxy=px, https_proxy=px, ALL_PROXY="",
                all_proxy="", NO_PROXY=no, no_proxy=no)


def sh(args, timeout=20, env=None):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 99, "", str(e)


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def append_jsonl(path, obj):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- discovery
def proc_table():
    rc, out, _ = sh(["ps", "-A", "-o", "pid=,ppid=,comm="])
    procs, kids = {}, {}
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        pid, ppid, comm = int(parts[0]), int(parts[1]), os.path.basename(parts[2])
        procs[pid] = (ppid, comm)
        kids.setdefault(ppid, []).append(pid)
    return procs, kids


def agent_kind(comm):
    if comm == "claude" or re.match(r"^\d+\.\d+\.\d+$", comm):  # Claude Code's native binary is named by version
        return "claude"
    if comm == "codex":
        return "codex"
    if comm.startswith("opencode"):
        return "opencode"
    if comm in ("agy", "antigravity"):
        return "agy"
    if comm == "cursor-agent":
        return "cursor"
    if comm == "gemini":
        return "gemini"
    return None


def find_agent(pane_pid, procs, kids):
    queue, seen = [pane_pid], set()
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        if pid in procs:
            k = agent_kind(procs[pid][1])
            if k:
                return k, pid
        queue.extend(sorted(kids.get(pid, [])))
    return None, None


def visible_undimmed(raw):
    """True if raw (with SGR escapes) contains visible non-space text outside dim mode.
    Placeholders and suggestions are drawn dim; a real draft is not."""
    dim, i, n = False, 0, len(raw)
    while i < n:
        if raw[i] == "\x1b":
            m = re.match(r"\x1b\[([0-9;]*)m", raw[i:])
            if m:
                codes = m.group(1).split(";") if m.group(1) else ["0"]
                j = 0
                while j < len(codes):
                    c = codes[j]
                    if c in ("38", "48") and j + 1 < len(codes):
                        j += 5 if codes[j + 1] == "2" else 3
                        continue
                    if c == "2":
                        dim = True
                    elif c in ("0", "22", ""):
                        dim = False
                    j += 1
                i += len(m.group(0))
                continue
            m = re.match(r"\x1b[\[\]][^a-zA-Z]*[a-zA-Z]", raw[i:])
            i += len(m.group(0)) if m else 1
            continue
        if not dim and not raw[i].isspace():
            return True
        i += 1
    return False


def parse_ui(raw, kind):
    """(busy, draft, footer, waiting) from the captured bottom of a pane (with escapes)."""
    lines = [l for l in raw.splitlines() if l.strip()][-14:]
    plain = [re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07", "", l) for l in lines]
    busy = bool(BUSY_RE.search("\n".join(plain)))
    waiting = bool(WAIT_RE.search("\n".join(plain[-10:])))
    prompt = "›" if kind == "codex" else "❯" if kind == "claude" else None
    draft = None
    if prompt:
        for raw_l, pl in reversed(list(zip(lines, plain))):
            if pl.lstrip().startswith(prompt):
                k = raw_l.find(prompt)
                draft = visible_undimmed(raw_l[k + len(prompt):])
                break
    footer = ""
    if kind == "codex":
        for pl in reversed(plain):
            if " · " in pl and ("~/" in pl or " /" in pl):
                footer = pl.strip()
                break
    return busy, draft, footer, waiting


def pane_ui(pane_id, kind):
    rc, raw, _ = sh(["tmux", "capture-pane", "-e", "-p", "-t", pane_id])
    return parse_ui(raw, kind)


def claude_sessions():
    out = {}
    try:
        names = os.listdir(CLAUDE_SESS)
    except OSError:
        return out
    for n in names:
        if not re.match(r"^\d+\.json$", n):  # only <pid>.json; never touch anything else
            continue
        d = load_json(os.path.join(CLAUDE_SESS, n), None)
        if isinstance(d, dict) and d.get("pid"):
            out[int(d["pid"])] = d
    return out


def codex_db():
    if CODEX_CFG.get("state_db"):
        return os.path.expanduser(CODEX_CFG["state_db"])
    dbs = glob.glob(os.path.join(H, ".codex", "state_*.sqlite"))
    num = lambda p: int(re.search(r"state_(\d+)", p).group(1)) if re.search(r"state_(\d+)", p) else -1
    return max(dbs, key=num) if dbs else ""


def codex_threads():
    out, db = {}, codex_db()
    if not db or not os.path.exists(db):
        return out
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=2)
        since = int((time.time() - 45 * 86400) * 1000)
        for row in con.execute("select id, name, title, cwd, updated_at_ms from threads "
                               "where archived = 0 and updated_at_ms > ?", (since,)):
            out[row[0]] = {"id": row[0], "name": row[1] or "", "title": row[2] or "", "cwd": row[3] or "",
                           "updated_ms": row[4] or 0}
        con.close()
    except sqlite3.Error:
        pass
    return out


def codex_hook_map(procs):
    """codex pid -> latest hook record (only pids still alive as codex)."""
    out = {}
    try:
        with open(HOOKLOG) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                pid = r.get("codex_pid")
                if pid and procs.get(pid, (0, ""))[1] == "codex" and r.get("session_id"):
                    out[pid] = r
    except OSError:
        pass
    return out


def match_codex_footer(footer, cwd, threads):
    """Codex footer: 'model · <cwd> · <thread name> · …' -> (thread, source)."""
    parts = [p.strip() for p in footer.split(" · ")]
    if len(parts) < 3:
        return None, "unknown"
    label = parts[2]
    path = parts[1].replace("~", H, 1) if parts[1].startswith("~") else parts[1]
    if label.endswith("…"):  # truncated: prefix match
        pre = label[:-1].strip()
        cands = [t for t in threads.values() if pre and (t["name"].startswith(pre) or t["title"].startswith(pre))]
    else:
        cands = [t for t in threads.values() if label and (t["name"] == label or t["title"] == label)]
    if "…" not in path:
        same = [t for t in cands if t["cwd"] == path or t["cwd"] == cwd]
        cands = same or cands
    if len(cands) == 1:
        return cands[0], "footer"
    if cands:
        return max(cands, key=lambda t: t["updated_ms"]), "ambiguous"
    return None, "unknown"


def state_of(p):
    if p.get("waiting"):
        return "wait"
    if p.get("draft"):
        return "draft"
    return "busy" if p.get("busy") else "idle"


def scan():
    fmt = SEP.join(["#{session_name}", "#{window_index}", "#{window_name}", "#{pane_index}", "#{pane_id}",
                    "#{pane_pid}", "#{pane_current_command}", "#{pane_current_path}", "#{window_layout}",
                    "#{pane_active}"])
    rc, out, err = sh(["tmux", "list-panes", "-a", "-F", fmt])
    if rc != 0:
        return {"ts": now_iso(), "tmux": False, "error": err.strip()[:200], "panes": []}
    procs, kids = proc_table()
    csess, threads, hooks = claude_sessions(), None, None
    panes = []
    for line in out.splitlines():
        f = line.split(SEP)
        if len(f) < 10:
            continue
        p = {"session": f[0], "window": int(f[1]), "window_name": f[2], "pane": int(f[3]), "pane_id": f[4],
             "pane_pid": int(f[5]), "command": f[6], "cwd": f[7], "layout": f[8], "active": f[9] == "1",
             "addr": "%s:%s.%s" % (f[0], f[1], f[3])}
        kind, apid = find_agent(p["pane_pid"], procs, kids)
        p["kind"], p["agent_pid"] = kind, apid
        footer = ""
        if kind:
            busy, draft, footer, waiting = pane_ui(p["pane_id"], kind)
            p.update(busy=busy, draft=draft, waiting=waiting)
        if kind == "claude":
            s = csess.get(apid) or {}
            p.update(id=s.get("sessionId"), name=s.get("name") or "", id_source="claude-sessions" if s else "unknown",
                     agent_cwd=s.get("cwd"), last_ms=s.get("updatedAt") or s.get("statusUpdatedAt"))
            if s.get("status") == "busy":
                p["busy"] = True
        elif kind == "codex":
            if threads is None:
                threads, hooks = codex_threads(), codex_hook_map(procs)
            h = hooks.get(apid)
            if h:
                t = threads.get(h["session_id"]) or {"id": h["session_id"], "name": "", "title": "",
                                                     "cwd": h.get("cwd", ""), "updated_ms": 0}
                src = "hook"
            else:
                t, src = match_codex_footer(footer, p["cwd"], threads)
            if t:
                p.update(id=t["id"], name=t["name"] or t["title"][:40], agent_cwd=t["cwd"], last_ms=t["updated_ms"])
            p["id_source"] = src
        if kind:
            p["state"] = state_of(p)
        panes.append(p)
    return {"ts": now_iso(), "tmux": True, "boot": boot_time(), "panes": panes}


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def boot_time():
    if platform.system() == "Darwin":
        rc, out, _ = sh(["sysctl", "-n", "kern.boottime"])
        m = re.search(r"sec = (\d+)", out)
        return int(m.group(1)) if m else 0
    try:
        for line in open("/proc/stat"):
            if line.startswith("btime"):
                return int(line.split()[1])
    except OSError:
        pass
    return 0


def write_registry(reg):
    save_json(REG, reg)
    if not reg.get("tmux") or not any(p.get("kind") for p in reg["panes"]):
        return None
    os.makedirs(SNAPS, mode=0o700, exist_ok=True)
    snaps = sorted(os.listdir(SNAPS))
    nowt = time.time()
    for s in snaps:
        fp = os.path.join(SNAPS, s)
        if nowt - os.path.getmtime(fp) > SNAP_KEEP_S:
            os.unlink(fp)
    last = os.path.join(SNAPS, snaps[-1]) if snaps else None
    if last and os.path.exists(last) and nowt - os.path.getmtime(last) < SNAP_EVERY_S:
        return None
    fp = os.path.join(SNAPS, time.strftime("%Y%m%dT%H%M%S") + ".json")
    save_json(fp, reg)
    return fp


# ---------------------------------------------------------------- targets
def agents(reg):
    return [p for p in reg["panes"] if p.get("kind")]


def normalize_target(target):
    """Accept bare targets and pasted reply lines like  ag send %12 "…"   # claude api @ tmux work:1.2"""
    target = re.sub(r"^\s*ag send\s+", "", target)
    m = re.match(r'^(\S+)\s+["“…#]', target)
    if m:
        target = m.group(1)
    m = re.match(r"^\s*(%\d+)", target)
    if m:
        target = m.group(1)
    return target.strip()


def resolve(target, reg, depth=0):
    ags = agents(reg)
    al = load_json(ALIASES, {})
    if target in al and depth == 0:
        return resolve(al[target], reg, 1)
    target = normalize_target(target)
    t = target.lower()
    tests = [
        lambda p: p["pane_id"] == target,
        lambda p: p["addr"] == target or "%s:%s" % (p["session"], p["window"]) == target,
        lambda p: p.get("id") and len(target) >= 8 and p["id"].startswith(target),
        lambda p: (p.get("name") or "").lower() == t,
        lambda p: p["window_name"].lower() == t,
        lambda p: t in (p.get("name") or "").lower() or t in p["window_name"].lower()
        or t in os.path.basename(p["cwd"]).lower(),
    ]
    for test in tests:
        hit = [p for p in ags if test(p)]
        if hit:
            return hit
    return []


def me(reg):
    """Identify the caller. Codex tool calls may run under a shared exec server whose TMUX_PANE / ancestry
    belongs to another pane, so prefer the per-call thread id and treat a Codex ancestor as unknown."""
    threads = {os.environ[k] for k in ("CODEX_THREAD_ID", "CODEX_SESSION_ID") if os.environ.get(k)}
    if threads:
        if len(threads) != 1:
            return None
        thread = next(iter(threads))
        hits = [p for p in reg["panes"] if p.get("kind") == "codex" and p.get("id") == thread]
        if len(hits) == 1 and hits[0].get("id_source") in ("hook", "footer"):
            return hits[0]
        return None
    procs, _ = proc_table()
    lineage, pid = set(), os.getpid()
    while pid and pid not in lineage and len(lineage) < 64:
        lineage.add(pid)
        parent, comm = procs.get(pid, (0, ""))
        if agent_kind(comm) == "codex":
            return None
        pid = parent
    hits = [p for p in reg["panes"] if p.get("pane_pid") in lineage]
    return hits[0] if len(hits) == 1 else None


def label(p):
    if not p:
        return "unknown"
    return "%s %s %s(%s)" % (p.get("kind") or "shell", p.get("name") or p["window_name"], p["addr"], p["pane_id"])


def reply_line(p, tgt=None):
    return 'ag send %s "…"   # %s %s @ tmux %s' % (tgt or p["pane_id"], p.get("kind") or "shell",
                                                   p.get("name") or p["window_name"], p["addr"])


# ---------------------------------------------------------------- formatting
def age(ms):
    if not ms:
        return "-"
    s = max(0, time.time() - ms / 1000)
    return "%ds" % s if s < 60 else "%dm" % (s // 60) if s < 3600 else "%dh" % (s // 3600) if s < 86400 else "%dd" % (s // 86400)


def pad(text, width):
    """Pad/truncate by terminal display width (CJK = 2 columns)."""
    out, w = "", 0
    for ch in text:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w + cw > width:
            break
        out, w = out + ch, w + cw
    return out + " " * (width - w)


def short(path):
    return path.replace(H, "~", 1) if path else ""


def recency_key(p):
    rank = {"wait": 0, "draft": 1, "busy": 2}.get(state_of(p), 3)
    return (rank, -(p.get("last_ms") or 0))


def idtxt(p):
    return (p.get("id") or "")[:8] + ("?" if p.get("id_source") in ("ambiguous", "unknown") else "")


# ---------------------------------------------------------------- commands
def cmd_ls(args):
    reg = scan()
    write_registry(reg)
    flt = [a for a in args if not a.startswith("-")]
    ags = agents(reg)
    if flt:
        ags = [p for p in ags if any(x.lower() in json.dumps(p, ensure_ascii=False).lower() for x in flt)]
    if "--json" in args:
        print(json.dumps(ags, ensure_ascii=False, indent=1))
        return 0
    if not reg.get("tmux"):
        print("tmux not running: %s" % reg.get("error"))
        return 1
    mine = me(reg)
    flag = lambda p: " <- me" if mine and mine["pane_id"] == p["pane_id"] else ""
    if "-t" in args:
        for p in sorted(ags, key=recency_key):
            print("%4s  %-5s %-8s %-9s %-18s %s %s%s" % (age(p.get("last_ms")), state_of(p), p["kind"], idtxt(p),
                  p["addr"], pad(p.get("name") or p["window_name"], 34), short(p["cwd"]), flag(p)))
        return 0
    cnt = {}
    for p in ags:
        cnt[p["kind"]] = cnt.get(p["kind"], 0) + 1
    print("%d agents  (%s)   %s" % (len(ags), ", ".join("%s %d" % kv for kv in sorted(cnt.items())), reg["ts"]))
    cur = None
    for p in sorted(ags, key=lambda p: (p["session"], p["window"], p["pane"])):
        if p["session"] != cur:
            cur = p["session"]
            print("\n[%s]" % cur)
        print("  %-11s %-8s %-9s %-5s %s %4s  %s%s" % (
            "%s.%s %s" % (p["window"], p["pane"], p["pane_id"]), p["kind"], idtxt(p), state_of(p),
            pad(p.get("name") or p["window_name"], 34), age(p.get("last_ms")), short(p["cwd"]), flag(p)))
    return 0


def tmux_type(pane, msg):
    """Type a message into a TUI input box and make sure it is submitted."""
    probe = msg[:12]
    sh(["tmux", "send-keys", "-t", pane, "-l", msg])
    for i in range(4):
        time.sleep(0.8)
        sh(["tmux", "send-keys", "-t", pane, "Enter"])
        time.sleep(1.5)
        rc, out, _ = sh(["tmux", "capture-pane", "-p", "-t", pane])
        tail = "\n".join([l for l in out.splitlines() if l.strip()][-6:])
        if BUSY_RE.search(tail) or probe not in tail:
            return 0, "submitted (try %d)" % (i + 1)
    return 1, "message may still be in the input box"


def can_native_queue(p):
    return p["kind"] == "codex" and p.get("id") and p.get("id_source") in ("hook", "footer")


def cmd_send(args):
    dry = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]
    if len(args) < 2:
        print("usage: ag send [--dry-run] <target> <message>", file=sys.stderr)
        return 2
    target, msg = args[0], " ".join(args[1:])
    reg = scan()
    hit = resolve(target, reg)
    if len(hit) != 1:
        print("target %s: %s" % ("ambiguous" if hit else "not found", target), file=sys.stderr)
        for p in hit[:12]:
            print("  %s %s %s %s" % (p["pane_id"], p["kind"], p.get("name") or p["window_name"], short(p["cwd"])),
                  file=sys.stderr)
        return 3
    p, src = hit[0], me(reg)
    if src and src["pane_id"] == p["pane_id"]:
        print("refused: the target is yourself", file=sys.stderr)
        return 3
    body = "[ag from %s] %s" % (label(src), msg)
    rec = {"ts": now_iso(), "from": label(src), "to": label(p), "to_id": p.get("id"),
           "sha256": hashlib.sha256(body.encode()).hexdigest(), "chars": len(body)}
    if dry:
        how = "codex-queue" if can_native_queue(p) else "refused(draft)" if p.get("draft") else "tmux"
        print("dry-run from=%s -> %s  id=%s(%s) state=%s method=%s" % (
            label(src), label(p), p.get("id"), p.get("id_source"), state_of(p), how))
        return 0
    if can_native_queue(p):
        codex = CODEX_CFG.get("bin") or shutil.which("codex") or "codex"
        rc, out, err = sh([codex, "queue", "--thread", p["id"], "--message", body], timeout=60, env=codex_env())
        rec.update(method="codex-queue", rc=rc, detail=(out + err).strip().splitlines()[-1:])
    elif p.get("draft"):
        rec.update(method="refused", rc=4, detail=["input box holds a draft"])
    elif p["kind"] == "codex" and p.get("busy"):
        rec.update(method="refused", rc=4, detail=["codex thread unconfirmed (%s) and pane busy" % p.get("id_source")])
    else:
        rc, detail = tmux_type(p["pane_id"], re.sub(r"\s*\n\s*", " ⏎ ", body))
        rec.update(method="tmux", rc=rc, detail=[detail])
    append_jsonl(SENT, rec)
    ok = rec["rc"] == 0
    print("%s -> %s  [%s] %s" % ("delivered/queued" if ok else "NOT delivered", label(p), rec["method"],
                                 " ".join(rec.get("detail") or [])))
    return 0 if ok else 4


def clients():
    rc, out, _ = sh(["tmux", "list-clients", "-F", "#{client_name}\t#{client_session}\t#{client_activity}"])
    return [l.split("\t") for l in out.splitlines() if l.count("\t") == 2]


def use_ghostty():
    if TERMINAL == "ghostty":
        return True
    return TERMINAL == "auto" and platform.system() == "Darwin" and os.path.isdir("/Applications/Ghostty.app")


def raise_ghostty(session):
    """Bring the Ghostty window that shows this tmux session to front (matched by window title)."""
    scr = ('tell application "Ghostty" to activate\n'
           'tell application "System Events" to tell process "ghostty"\n'
           '  repeat with w in windows\n'
           '    set t to name of w\n'
           '    if t is "tmux:%s" or t ends with "-t %s" or t ends with "-s %s" then\n'
           '      perform action "AXRaise" of w\n'
           '      return "raised"\n'
           '    end if\n'
           '  end repeat\n'
           'end tell\nreturn "activated"') % (session, session, session)
    rc, out, _ = sh(["osascript", "-e", scr], timeout=10)
    return out.strip() or "activate-failed"


def go_pane(p):
    sh(["tmux", "select-window", "-t", p["pane_id"]])
    sh(["tmux", "select-pane", "-t", p["pane_id"]])
    cl = [c for c in clients() if c[1] == p["session"]]
    if os.environ.get("TMUX"):  # inside tmux: don't hijack this window if another client already shows the target
        rc, cur, _ = sh(["tmux", "display", "-p", "#S"])
        if cur.strip() == p["session"]:
            return "selected"
        if not cl or not use_ghostty():
            sh(["tmux", "switch-client", "-t", p["pane_id"]])
            return "switched"
    if not use_ghostty():
        return "selected"
    if not cl:
        sh(["open", "-na", "Ghostty", "--args", "-e", "tmux", "attach", "-t", p["session"]])
        return "opened-new-window"
    return raise_ghostty(p["session"])


def cmd_go(args):
    reg = scan()
    hit = resolve(args[0], reg) if args else []
    if len(hit) != 1:
        print("target %s" % ("ambiguous" if hit else "not found"), file=sys.stderr)
        return 3
    print(go_pane(hit[0]))
    return 0


SAFE_NAME = re.compile(r"[^A-Za-z0-9_\-一-鿿]+")


def cmd_new(args):
    """ag new <dir> [--agent claude|codex|shell] [--session S] [--name N] [--go]"""
    opts, pos, i = {}, [], 0
    while i < len(args):
        if args[i] in ("--agent", "--session", "--name"):
            opts[args[i][2:]] = args[i + 1] if i + 1 < len(args) else ""
            i += 2
        elif args[i] == "--go":
            opts["go"] = True
            i += 1
        else:
            pos.append(args[i])
            i += 1
    if not pos:
        print(cmd_new.__doc__, file=sys.stderr)
        return 2
    d = os.path.realpath(os.path.expanduser(pos[0]))
    if not os.path.isdir(d):
        print("no such directory: %s" % d, file=sys.stderr)
        return 2
    agent = opts.get("agent", "claude")
    if agent != "shell" and agent not in AGENT_CMDS:
        print("--agent must be shell or one of: %s" % ", ".join(sorted(AGENT_CMDS)), file=sys.stderr)
        return 2
    reg = scan()
    live = sorted({p["session"] for p in reg["panes"]})
    sess = opts.get("session") or ""
    if not sess:  # reuse the session that already hosts agents in this directory
        same = [p["session"] for p in reg["panes"] if p.get("kind") and os.path.realpath(p["cwd"]) == d]
        sess = same[0] if same else SAFE_NAME.sub("-", os.path.basename(d)).strip("-") or "work"
    sess = SAFE_NAME.sub("-", sess).strip("-")
    name = SAFE_NAME.sub("-", opts.get("name") or os.path.basename(d)).strip("-") or "win"
    if sess in live:
        rc, out, err = sh(["tmux", "new-window", "-t", sess + ":", "-n", name, "-c", d, "-P", "-F", "#{pane_id}"])
    else:
        rc, out, err = sh(["tmux", "new-session", "-d", "-s", sess, "-n", name, "-c", d, "-P", "-F", "#{pane_id}"])
    if rc != 0:
        print("tmux failed: %s" % err.strip(), file=sys.stderr)
        return 1
    pane = out.strip()
    if agent != "shell":  # typed into the interactive shell, so shell aliases/functions apply
        sh(["tmux", "send-keys", "-t", pane, AGENT_CMDS[agent], "Enter"])
    rec = {"ts": now_iso(), "dir": d, "agent": agent, "session": sess, "window": name, "pane_id": pane,
           "new_session": sess not in live}
    append_jsonl(CREATED, rec)
    print(json.dumps(rec, ensure_ascii=False))
    if opts.get("go"):
        go_pane({"pane_id": pane, "session": sess})
    return 0


def claude_project_usage():
    """dir -> last-used ms for Claude Code projects (~/.claude.json keys; newest transcript mtime)."""
    out = {}
    d = load_json(os.path.join(H, ".claude.json"), {})
    base = os.path.join(H, ".claude/projects")
    for path in (d.get("projects") or {}):
        folder = os.path.join(base, re.sub(r"[^A-Za-z0-9]", "-", path))
        try:
            ms = max(os.path.getmtime(os.path.join(folder, f)) for f in os.listdir(folder) if f.endswith(".jsonl")) * 1000
        except (OSError, ValueError):
            continue
        out[path] = ms
    return out


def project_list(reg=None):
    """Pinned dirs first (pin order), then by most recent use: live panes, Codex threads, Claude, `ag new`."""
    use = {}

    def bump(path, src, ms):
        if not path:
            return
        r = use.setdefault(path, {"dir": path, "last_ms": 0, "sources": set()})
        r["last_ms"] = max(r["last_ms"], ms or 0)
        r["sources"].add(src)
    reg = reg or scan()
    for p in reg["panes"]:
        if p.get("kind"):
            bump(p["cwd"], "live", p.get("last_ms") or 0)
    for t in codex_threads().values():
        bump(t["cwd"], "codex", t["updated_ms"])
    for path, ms in claude_project_usage().items():
        bump(path, "claude", ms)
    try:
        for line in open(CREATED):
            r = json.loads(line)
            bump(r.get("dir"), "ag-new", datetime.datetime.fromisoformat(r["ts"]).timestamp() * 1000)
    except (OSError, ValueError, KeyError):
        pass
    pins = [os.path.expanduser(d) for d in load_json(PINS, [])]
    rows = [dict(use.get(d, {"dir": d, "last_ms": 0, "sources": set()}), pinned=True) for d in pins]
    rows += [dict(r, pinned=False) for r in sorted((r for d, r in use.items() if d not in pins),
                                                    key=lambda r: -r["last_ms"])]
    tmp = ("/private/tmp", "/tmp", "/private/var", "/var/folders")
    return [{"dir": r["dir"], "short": short(r["dir"]), "pinned": r["pinned"], "last_ms": r["last_ms"],
             "source": "pinned" if r["pinned"] else "+".join(sorted(r["sources"]))}
            for r in rows if os.path.isdir(r["dir"]) and not r["dir"].startswith(tmp)]


def cmd_projects(args):
    n = int(args[args.index("-n") + 1]) if "-n" in args else 60
    out = project_list()[:n]
    if "--json" in args:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        for r in out:
            print("%s %4s  %-18s %s" % ("*" if r["pinned"] else " ", age(r["last_ms"]), r["source"][:18], r["short"]))
    return 0


def cmd_pin(args):
    pins = load_json(PINS, [])
    if args:
        rm = "--rm" in args
        d = os.path.realpath(os.path.expanduser([a for a in args if a != "--rm"][0]))
        pins = [x for x in pins if os.path.realpath(os.path.expanduser(x)) != d]
        if not rm:
            if not os.path.isdir(d):
                print("no such directory: %s" % d, file=sys.stderr)
                return 2
            pins.insert(0, d)
        save_json(PINS, pins)
    for x in pins:
        print("* " + short(x))
    return 0


COPY_FIELDS = ("send", "id", "pane", "addr", "resume", "cwd", "name")


def copy_text(p, what):
    tgt = p["id"][:8] if p.get("id") and p.get("id_source") in ("hook", "footer", "claude-sessions") else p["pane_id"]
    if what == "send":
        return reply_line(p, tgt)
    if what == "id":
        return p.get("id") or ""
    if what == "pane":
        return p["pane_id"]
    if what == "addr":
        return p["addr"]
    if what == "cwd":
        return p["cwd"]
    if what == "name":
        return p.get("name") or p["window_name"]
    if what == "resume" and p.get("id"):
        cmd = "claude --resume" if p["kind"] == "claude" else "codex resume" if p["kind"] == "codex" else None
        return "cd %s && %s %s" % (shlex.quote(p["cwd"]), cmd, p["id"]) if cmd else ""
    return ""


def clipboard(text):
    for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, input=text, text=True)
            break
    if os.environ.get("TMUX"):
        sh(["tmux", "set-buffer", "--", text])


def cmd_cp(args):
    if not args:
        print("usage: ag cp <target> [%s]" % "|".join(COPY_FIELDS), file=sys.stderr)
        return 2
    what = args[1] if len(args) > 1 else "send"
    if what not in COPY_FIELDS:
        print("can copy: %s" % " / ".join(COPY_FIELDS), file=sys.stderr)
        return 2
    hit = resolve(args[0], scan())
    if len(hit) != 1:
        print("target %s" % ("ambiguous" if hit else "not found"), file=sys.stderr)
        return 3
    text = copy_text(hit[0], what)
    if not text:
        print("nothing to copy for %s" % what, file=sys.stderr)
        return 4
    clipboard(text)
    print("copied: " + text)
    return 0


def cmd_me(args):
    reg = scan()
    if "--pane" in args:
        i = args.index("--pane") + 1
        pane = args[i] if i < len(args) else None
        p = next((x for x in reg["panes"] if x["pane_id"] == pane), None)
    else:
        p = me(reg)
    if not p:
        print("caller identity unknown; use --pane %N", file=sys.stderr)
        return 3
    text = reply_line(p)
    if "--copy" in args:
        clipboard(text)
        sh(["tmux", "display-message", "-d", "2500", "copied: " + text])
    print(text)
    return 0


def self_cmd():
    return "%s -m panepost" % shlex.quote(sys.executable)


def cmd_pick(args):
    reg = scan()
    write_registry(reg)
    rows = ["%s\t%-18s %-8s %-5s %s %4s  %s" % (p["pane_id"], p["addr"], p["kind"], state_of(p),
                                              pad(p.get("name") or p["window_name"], 34), age(p.get("last_ms")),
                                              short(p["cwd"])) for p in sorted(agents(reg), key=recency_key)]

    def cp(what, lbl):
        return "execute-silent(%s cp {1} %s)+change-header(copied %s · Enter jump · Esc close)" % (self_cmd(), what, lbl)
    r = subprocess.run(["fzf", "--with-nth=2..", "--delimiter=\t", "--reverse", "--prompt=agent> ",
                        "--header=Enter jump · ^Y reply line · ^O session id · ^R resume cmd · ^X dir · ^P pane · Esc close",
                        "--bind", "ctrl-y:" + cp("send", "reply line"), "--bind", "ctrl-o:" + cp("id", "session id"),
                        "--bind", "ctrl-r:" + cp("resume", "resume command"), "--bind", "ctrl-x:" + cp("cwd", "dir"),
                        "--bind", "ctrl-p:" + cp("pane", "pane id")],
                       input="\n".join(rows), stdout=subprocess.PIPE, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return 0
    return cmd_go([r.stdout.split("\t")[0].strip()])


def cmd_pick_new(args):
    rows = ["%s\t%s %4s  %s" % (r["dir"], "*" if r["pinned"] else " ", age(r["last_ms"]), r["short"])
            for r in project_list()]
    r = subprocess.run(["fzf", "--with-nth=2..", "--delimiter=\t", "--reverse", "--print-query", "--prompt=dir> ",
                        "--header=most recent first (* pinned) · Enter select · type a full path to use it · Esc cancel"],
                       input="\n".join(rows), stdout=subprocess.PIPE, text=True)
    lines = r.stdout.splitlines()
    if r.returncode not in (0, 1) or not lines:
        return 0
    d = lines[1].split("\t")[0] if len(lines) > 1 else os.path.expanduser(lines[0].strip())
    if not d or not os.path.isdir(d):
        return 0
    r = subprocess.run(["fzf", "--reverse", "--prompt=start> ", "--header=%s" % short(d)],
                       input="\n".join(sorted(AGENT_CMDS) + ["shell"]), stdout=subprocess.PIPE, text=True)
    agent = r.stdout.strip()
    if r.returncode != 0 or not agent:
        return 0
    return cmd_new([d, "--agent", agent, "--go"])


def cmd_scan(args):
    reg = scan()
    snap = write_registry(reg)
    if "--quiet" not in args:
        print(json.dumps({"agents": len(agents(reg)), "registry": REG, "snapshot": snap}, ensure_ascii=False))
    return 0 if reg.get("tmux") or "--quiet" in args else 1


def cmd_alias(args):
    al = load_json(ALIASES, {})
    if not args:
        print(json.dumps(al, ensure_ascii=False, indent=1))
        return 0
    if len(args) == 2 and args[1] == "--rm":
        al.pop(args[0], None)
    else:
        hit = resolve(args[1], scan()) if len(args) == 2 else []
        if len(hit) != 1:
            print("target %s" % ("ambiguous" if hit else "not found"), file=sys.stderr)
            return 3
        al[args[0]] = hit[0].get("id") or hit[0]["pane_id"]
    save_json(ALIASES, al)
    print(json.dumps(al, ensure_ascii=False))
    return 0


def pick_snapshot(path=None):
    if path:
        return load_json(path, None), path
    bt = boot_time()
    try:
        snaps = sorted(os.listdir(SNAPS), reverse=True)
    except OSError:
        snaps = []
    first = None
    for s in snaps:
        fp = os.path.join(SNAPS, s)
        d = load_json(fp, None)
        if d and d.get("boot") and d["boot"] < bt - 60:
            return d, fp  # newest snapshot from before the current boot
        first = first or (d, fp)
    return first or (load_json(REG, None), REG)


def restore_plan(snap, live=(), only=None):
    q = shlex.quote
    out = []
    by = {}
    for p in snap["panes"]:
        by.setdefault(p["session"], {}).setdefault(p["window"], []).append(p)
    for s, wins in sorted(by.items()):
        if only and s != only:
            continue
        out.append("\n# ---- %s%s" % (s, " (already running, skipped)" if s in live else ""))
        if s in live:
            continue
        first = True
        for w, ps in sorted(wins.items()):
            ps.sort(key=lambda p: p["pane"])
            p0 = ps[0]
            if first:
                out.append("tmux new-session -d -s %s -n %s -c %s" % (q(s), q(p0["window_name"]), q(p0["cwd"])))
                out.append("tmux move-window -s %s -t %s 2>/dev/null || true" % (q(s + ":^"), q("%s:%d" % (s, w))))
                first = False
            else:
                out.append("tmux new-window -d -t %s -n %s -c %s" % (q("%s:%d" % (s, w)), q(p0["window_name"]), q(p0["cwd"])))
            for p in ps[1:]:
                out.append("tmux split-window -d -t %s -c %s" % (q("%s:%d" % (s, w)), q(p["cwd"])))
            out.append("tmux select-layout -t %s %s" % (q("%s:%d" % (s, w)), q(p0["layout"])))
            for p in ps:
                tgt = q("%s:%d.%d" % (s, w, p["pane"]))
                if p.get("kind") == "claude" and p.get("id"):
                    out.append("tmux send-keys -t %s %s Enter" % (tgt, q("%s --resume %s" % (AGENT_CMDS["claude"], p["id"]))))
                elif p.get("kind") == "codex" and p.get("id"):
                    note = "" if p.get("id_source") in ("hook", "footer") else "   # unconfirmed (%s): check first" % p.get("id_source")
                    out.append("tmux send-keys -t %s %s Enter%s" % (tgt, q("%s resume %s" % (AGENT_CMDS["codex"], p["id"])), note))
                elif p.get("kind"):
                    out.append("# %s %s: no session id recorded, start it manually" % (tgt, p["kind"]))
    return out


def cmd_restore(args):
    if "--plan" not in args:
        print("only `ag restore --plan` is supported (prints commands, never executes)", file=sys.stderr)
        return 2
    src = args[args.index("--from") + 1] if "--from" in args else None
    snap, fp = pick_snapshot(src)
    if not snap:
        print("no snapshot available", file=sys.stderr)
        return 1
    live = set() if "--all" in args else {p["session"] for p in scan()["panes"]}
    only = args[args.index("--session") + 1] if "--session" in args else None
    print("# snapshot: %s  (%s)\n# plan only: review, then run section by section; running sessions are skipped." % (
        fp, snap.get("ts")))
    print("\n".join(restore_plan(snap, live, only)))
    return 0


def cmd_hook(args):
    """Codex hook: stdin JSON carries session_id; record pane / codex pid. Silent, always exits 0."""
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        d = json.loads(raw) if raw.strip() else {}
        sid = d.get("session_id") or d.get("sessionId") or d.get("thread_id")
        if not sid:
            return 0
        procs, _ = proc_table()
        pid, cpid = os.getpid(), None
        for _ in range(12):
            pid = procs.get(pid, (0, ""))[0]
            if not pid:
                break
            if procs.get(pid, (0, ""))[1] == "codex":
                cpid = pid
                break
        append_jsonl(HOOKLOG, {"ts": now_iso(), "event": args[1] if len(args) > 1 else "", "session_id": sid,
                               "pane": os.environ.get("TMUX_PANE"), "cwd": d.get("cwd") or os.getcwd(),
                               "codex_pid": cpid})
        if os.path.getsize(HOOKLOG) > 4 * 1024 * 1024:
            with open(HOOKLOG) as f:
                lines = f.readlines()[-3000:]
            tmp = HOOKLOG + ".tmp"
            with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
                f.writelines(lines)
            os.replace(tmp, HOOKLOG)
    except Exception:
        pass
    return 0


def cmd_tmux_snippet(args):
    ag = shutil.which("ag") or "ag"
    print('''# panepost — add to ~/.tmux.conf, then: tmux source-file ~/.tmux.conf
# prefix + A : list agents (Enter jump; ^Y copy reply line)   prefix + N : start an agent in a directory
# prefix + a : copy this pane's reply line
bind-key A display-popup -E -w 90%% -h 75%% "%(ag)s pick || { echo; echo 'ag pick failed, press Enter'; read -r _; }"
bind-key N display-popup -E -w 80%% -h 70%% "%(ag)s pick-new || { echo; echo 'ag pick-new failed, press Enter'; read -r _; }"
bind-key a run-shell -b "%(ag)s me --pane '#{pane_id}' --copy >/dev/null"''' % {"ag": ag})
    return 0


def cmd_codex_hook_snippet(args):
    ag = shutil.which("ag") or "ag"
    hook = lambda ev: {"hooks": [{"type": "command", "command": "%s hook codex %s" % (ag, ev), "timeout": 2}]}
    print("# Merge into ~/.codex/hooks.json (append to existing arrays; keep your other hooks):")
    print(json.dumps({"hooks": {"SessionStart": [hook("SessionStart")], "UserPromptSubmit": [hook("UserPromptSubmit")]}},
                     indent=2))
    return 0


COMMANDS = {"ls": cmd_ls, "send": cmd_send, "go": cmd_go, "pick": cmd_pick, "scan": cmd_scan,
            "restore": cmd_restore, "alias": cmd_alias, "hook": cmd_hook, "new": cmd_new,
            "projects": cmd_projects, "pick-new": cmd_pick_new, "pin": cmd_pin, "cp": cmd_cp, "me": cmd_me,
            "tmux-snippet": cmd_tmux_snippet, "codex-hook-snippet": cmd_codex_hook_snippet}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if argv[0] in ("-V", "--version", "version"):
        print("panepost %s" % __version__)
        return 0
    if argv[0] not in COMMANDS:
        print("unknown command %s\n%s" % (argv[0], __doc__), file=sys.stderr)
        return 2
    return COMMANDS[argv[0]](argv[1:])
