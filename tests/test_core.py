import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["PANEPOST_STATE"] = tempfile.mkdtemp(prefix="panepost-test-")
os.environ["PANEPOST_CONFIG"] = os.path.join(os.environ["PANEPOST_STATE"], "none.toml")

from panepost import cli  # noqa: E402

DIM = "\x1b[2m"
RST = "\x1b[0m"
CODEX_IDLE = (
    "  Worked for 1m 9s\n"
    "\x1b[1m\x1b[38;2;252;183;81m›\x1b[0m\x1b[48;2;65;65;65m " + DIM + "Ask Codex to do anything" + RST + "\n"
    "  GPT-x max · ~/work/demo · Fix the login bug · Main [default]\n"
    "  ← for agents · ? for shortcuts\n"
)
CODEX_DRAFT = CODEX_IDLE.replace(DIM + "Ask Codex to do anything", "/status")
CLAUDE_SUGGESTION = "────\n\x1b[39m❯ " + DIM + "how is the cleanup going" + RST + "\n────\n  -- INSERT --\n"
CLAUDE_DRAFT = "────\n❯ please merge and delete the branch\n────\n  -- INSERT --\n"
CLAUDE_PERMISSION = "Bash command\n  rm -rf build\nDo you want to proceed?\n❯ 1. Yes\n  2. No\nEsc to cancel\n"
CODEX_BUSY = "• Working (2s • esc to interrupt)\n› " + DIM + "Ask Codex to do anything" + RST + "\n  GPT-x · ~/w · t · Main\n"


class ParseUI(unittest.TestCase):
    def test_codex_placeholder_is_not_draft(self):
        busy, draft, footer, waiting = cli.parse_ui(CODEX_IDLE, "codex")
        self.assertFalse(busy)
        self.assertFalse(draft)
        self.assertFalse(waiting)
        self.assertIn("Fix the login bug", footer)

    def test_codex_draft(self):
        self.assertTrue(cli.parse_ui(CODEX_DRAFT, "codex")[1])

    def test_codex_busy(self):
        self.assertTrue(cli.parse_ui(CODEX_BUSY, "codex")[0])

    def test_claude_dim_suggestion_is_not_draft(self):
        self.assertFalse(cli.parse_ui(CLAUDE_SUGGESTION, "claude")[1])

    def test_claude_draft(self):
        self.assertTrue(cli.parse_ui(CLAUDE_DRAFT, "claude")[1])

    def test_claude_permission_is_waiting(self):
        self.assertTrue(cli.parse_ui(CLAUDE_PERMISSION, "claude")[3])

    def test_opencode_allow_is_waiting(self):
        self.assertTrue(cli.parse_ui("  ┃   Allow once   Allow always   Reject\n", "opencode")[3])

    def test_truecolor_codes_do_not_toggle_dim(self):
        # 38;2;r;g;b contains a literal "2" that must not be read as the dim attribute
        self.assertTrue(cli.visible_undimmed("\x1b[38;2;2;2;2mtext"))
        self.assertFalse(cli.visible_undimmed("\x1b[2mtext"))


class Targets(unittest.TestCase):
    def test_pasted_reply_line(self):
        self.assertEqual(cli.normalize_target('ag send %75 "…"   # claude api @ tmux work:1.2'), "%75")
        self.assertEqual(cli.normalize_target('ag send 0abc1234 "…"   # codex x @ tmux w:1.1'), "0abc1234")
        self.assertEqual(cli.normalize_target("%12（claude api @ work:1.2）"), "%12")
        self.assertEqual(cli.normalize_target("web-c8"), "web-c8")

    def _reg(self):
        mk = lambda pid, sess, w, kind, name, cwd, sid=None: {
            "pane_id": pid, "session": sess, "window": w, "pane": 1, "window_name": "win%d" % w,
            "addr": "%s:%d.1" % (sess, w), "kind": kind, "name": name, "cwd": cwd, "id": sid,
            "id_source": "claude-sessions" if sid else "unknown"}
        return {"panes": [mk("%1", "web", 1, "claude", "web-a1", "/w/site", "aaaaaaaa-1111"),
                          mk("%2", "web", 2, "codex", "Fix login", "/w/site", "bbbbbbbb-2222"),
                          mk("%3", "api", 1, "claude", "api-c3", "/w/api", "cccccccc-3333")]}

    def test_resolve_kinds(self):
        reg = self._reg()
        self.assertEqual([p["pane_id"] for p in cli.resolve("%2", reg)], ["%2"])
        self.assertEqual([p["pane_id"] for p in cli.resolve("api:1.1", reg)], ["%3"])
        self.assertEqual([p["pane_id"] for p in cli.resolve("cccccccc", reg)], ["%3"])
        self.assertEqual([p["pane_id"] for p in cli.resolve("web-a1", reg)], ["%1"])
        self.assertEqual(len(cli.resolve("site", reg)), 2)  # ambiguous substring -> caller refuses
        self.assertEqual(cli.resolve("nope", reg), [])
        self.assertEqual([p["pane_id"] for p in cli.resolve('ag send %3 "…"  # x', reg)], ["%3"])


class CodexFooter(unittest.TestCase):
    T = {"t1": {"id": "t1", "name": "Fix login", "title": "", "cwd": "/w/a", "updated_ms": 1},
         "t2": {"id": "t2", "name": "Fix login", "title": "", "cwd": "/w/b", "updated_ms": 2},
         "t3": {"id": "t3", "name": "Write docs for the API", "title": "", "cwd": "/w/c", "updated_ms": 3}}

    def test_unique_by_cwd(self):
        t, src = cli.match_codex_footer("GPT · /w/a · Fix login · Main", "/w/a", self.T)
        self.assertEqual((t["id"], src), ("t1", "footer"))

    def test_truncated_prefix(self):
        t, src = cli.match_codex_footer("GPT · /w/c · Write docs fo…", "/w/c", self.T)
        self.assertEqual((t["id"], src), ("t3", "footer"))

    def test_ambiguous(self):
        t, src = cli.match_codex_footer("GPT · /w/… · Fix login · Main", "/x", self.T)
        self.assertEqual(src, "ambiguous")


class Restore(unittest.TestCase):
    def test_plan(self):
        snap = {"panes": [
            {"session": "web", "window": 1, "pane": 1, "window_name": "site", "cwd": "/w/site", "layout": "L",
             "kind": "claude", "id": "aaaa", "id_source": "claude-sessions"},
            {"session": "web", "window": 1, "pane": 2, "window_name": "site", "cwd": "/w/site", "layout": "L",
             "kind": "codex", "id": "bbbb", "id_source": "ambiguous"}]}
        plan = "\n".join(cli.restore_plan(snap))
        self.assertIn("tmux new-session -d -s web", plan)
        self.assertIn("tmux split-window", plan)
        self.assertIn("claude --resume aaaa", plan)
        self.assertIn("codex resume bbbb", plan)
        self.assertIn("unconfirmed", plan)
        self.assertIn("skipped", "\n".join(cli.restore_plan(snap, live={"web"})))


class Misc(unittest.TestCase):
    def test_pad_cjk(self):
        self.assertEqual(len(cli.pad("修复", 6)), 4)  # 2 wide chars = 4 columns + 2 spaces
        self.assertEqual(cli.pad("abcdef", 3), "abc")

    def test_agent_kind(self):
        self.assertEqual(cli.agent_kind("2.1.318"), "claude")
        self.assertEqual(cli.agent_kind("codex"), "codex")
        self.assertEqual(cli.agent_kind("opencode.exe"), "opencode")
        self.assertIsNone(cli.agent_kind("zsh"))

    def test_copy_resume(self):
        p = {"pane_id": "%1", "addr": "w:1.1", "kind": "claude", "name": "n", "window_name": "w", "cwd": "/a b",
             "id": "abcdefgh-1", "id_source": "claude-sessions"}
        self.assertEqual(cli.copy_text(p, "resume"), "cd '/a b' && claude --resume abcdefgh-1")
        self.assertTrue(cli.copy_text(p, "send").startswith('ag send abcdefgh "…"'))


if __name__ == "__main__":
    unittest.main()
