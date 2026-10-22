import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("AGENT_BRIDGE_MCP_SYNC", "off")  # tests never write the operator's real configs
import backends  # noqa: E402
import gates  # noqa: E402
import inbox  # noqa: E402
import policy_hook  # noqa: E402
import projects  # noqa: E402
import runner  # noqa: E402
import server  # noqa: E402
from store import Store  # noqa: E402

FAKE = os.path.join(HERE, "fake_claude.py")


def make_app(scenario, argv_log=None):
    os.environ["FAKE_SCENARIO"] = scenario
    if argv_log:
        os.environ["FAKE_ARGV_LOG"] = argv_log
    store = Store(":memory:")
    mgr = runner.RunManager(store, claude=FAKE)
    return server.App(store=store, manager=mgr), mgr


def drive(it, on_gate=None):
    """Collect events; call on_gate(event) when a gate event shows up (from the stream thread)."""
    evs = []
    for ev in it:
        evs.append(ev)
        if ev.get("type") == "gate" and on_gate:
            on_gate(ev)
    return evs


def text(evs):
    return "".join(e.get("text", "") for e in evs if e.get("type") == "delta")


class FileEvents(unittest.TestCase):
    """#1331: a file the run wrote is offered to the chat, only when it is safe and confirmed."""

    def _run(self, *paths):
        os.environ["FAKE_WRITE_PATHS"] = ",".join(paths)
        app, mgr = make_app("write")
        mgr.claude = FAKE
        return [e for e in drive(app.chat("cf", "general", "make a file")) if e.get("type") == "file"]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_a_written_png_becomes_a_file_event_with_its_bytes(self):
        p = os.path.join(self.tmp, "chart.png")
        evs = self._run(p)
        self.assertEqual(len(evs), 1)
        self.assertEqual((evs[0]["name"], evs[0]["mime"]), ("chart.png", "image/png"))
        import base64
        self.assertEqual(base64.b64decode(evs[0]["b64"]), open(p, "rb").read())

    def test_unlisted_types_and_missing_files_are_not_offered(self):
        evs = self._run(os.path.join(self.tmp, "run.sh"), os.path.join(self.tmp, ".env"),
                        "!" + os.path.join(self.tmp, "gone.png"))
        self.assertEqual(evs, [])

    def test_a_failed_write_is_not_offered(self):
        p = os.path.join(self.tmp, "x.png.fail")      # tool_result is_error, wrong extension too
        self.assertEqual(self._run(p), [])

    def test_oversize_and_relative_paths_are_refused(self):
        big = os.path.join(self.tmp, "big.png")
        with open(big, "wb") as f:
            f.write(b"0" * 100)
        import events
        with mock.patch.object(events, "FILE_MAX_BYTES", 50):
            self.assertIsNone(events.file_event(big))
        self.assertIsNone(events.file_event("big.png"))
        self.assertIsNotNone(events.file_event(big))

    def test_at_most_ten_files_per_run(self):
        paths = [os.path.join(self.tmp, f"f{i}.csv") for i in range(14)]
        self.assertEqual(len(self._run(*paths)), 10)


class FileEventsAnyTool(unittest.TestCase):
    """#1355/#1400/#1401: files made by Bash or MCP tools show; what can't be shown says so."""

    def setUp(self):
        import events
        self.events = events
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.r = events.Renderer()
        self.r.watch(self.tmp)
        self.n = 0

    def tool(self, name, tin, result="ok", make=None):
        """Feed one tool_use + tool_result; `make` runs between them (the tool doing its work)."""
        self.n += 1
        i = f"t{self.n}"
        self.r.feed({"type": "assistant", "message": {"id": i, "content": [
            {"type": "tool_use", "id": i, "name": name, "input": tin}]}})
        if make:
            make()
        out = self.r.feed({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": i, "content": result}]}})
        return [e for e in out if e.get("type") == "file"]

    def put(self, name, data=b"x" * 10, sub=""):
        d = os.path.join(self.tmp, sub)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_image_from_an_mcp_tool_outside_the_cwd_is_found_in_the_result_text(self):
        out_dir = os.path.realpath(tempfile.mkdtemp())
        p = os.path.join(out_dir, "vase.png")
        evs = self.tool("mcp__ads__generate_image", {"prompt": "vase"}, result=f"saved to {p}",
                        make=lambda: open(p, "wb").write(b"\x89PNG"))
        self.assertEqual([(e["name"], e["mime"], e["source"]) for e in evs],
                         [("vase.png", "image/png", "path")])

    def test_files_named_only_inside_a_written_script_show_when_it_runs(self):
        # live test 2026-10-16: Write make_files.py (paths inside), Bash runs it, prints nothing
        out_dir = os.path.realpath(tempfile.mkdtemp())
        png, pdf = os.path.join(out_dir, "e.png"), os.path.join(out_dir, "f.pdf")
        script = os.path.join(self.tmp, "make_files.py")
        src = f"open({png!r},'wb').write(b'x')\nopen({pdf!r},'wb').write(b'%PDF')\n"
        self.assertEqual(self.tool("Write", {"file_path": script, "content": src},
                                   make=lambda: open(script, "w").write(src)), [])
        evs = self.tool("Bash", {"command": f"python3 {script}"}, result="",
                        make=lambda: [open(png, "wb").write(b"\x89PNG"), open(pdf, "wb").write(b"%PDF-1.4")])
        self.assertEqual(sorted((e["name"], e["source"]) for e in evs), [("e.png", "script"), ("f.pdf", "script")])
        self.assertEqual(self.tool("Bash", {"command": "true"}), [])        # once

    def test_files_the_agent_names_in_its_reply_show(self):
        out_dir = os.path.realpath(tempfile.mkdtemp())
        new, old = os.path.join(out_dir, "h.mp4"), os.path.join(out_dir, "old.png")
        for p in (new, old):
            open(p, "wb").write(b"x" * 10)
        os.utime(old, (1, 1))
        out = self.r.feed({"type": "assistant", "message": {"id": "m1", "content": [
            {"type": "text", "text": f"Done: {new} and {old}."}]}})
        self.assertEqual([(e["name"], e["source"]) for e in out if e.get("type") == "file"], [("h.mp4", "reply")])

    def test_pdf_from_a_bash_command_is_found_by_the_cwd_scan(self):
        evs = self.tool("Bash", {"command": "pandoc a.md -o report.pdf"},
                        make=lambda: self.put("report.pdf", b"%PDF-1.4"))
        self.assertEqual([(e["name"], e["mime"], e["source"]) for e in evs],
                         [("report.pdf", "application/pdf", "scan")])

    def test_old_files_and_ignored_dirs_are_skipped_and_new_ones_dedupe(self):
        self.put("old.png")
        self.put("junk.png", sub="node_modules")
        self.put("junk.png", sub=".git")
        self.put("junk.png", sub="venv/lib")
        self.put("pyvenv.cfg", sub="venv")
        self.r.watch(self.tmp)                              # baseline includes old.png
        self.put("junk2.png", sub="node_modules")
        self.assertEqual(self.tool("Bash", {"command": "make"}), [])
        self.assertEqual(len(self.tool("Bash", {"command": "a"}, make=lambda: self.put("new.csv"))), 1)
        self.assertEqual(self.tool("Bash", {"command": "b"}), [])         # same file: once
        self.assertEqual(self.tool("Bash", {"command": f"cat {self.tmp}/new.csv"}), [])

    def test_a_path_from_before_the_run_or_in_a_dot_dir_is_not_offered(self):
        old = self.put("old.png")
        os.utime(old, (1, 1))
        self.r.watch(self.tmp)
        self.assertEqual(self.tool("Bash", {"command": f"cp {old} /dev/null"}), [])
        dot = self.put("k.pdf", sub=".ssh")
        self.assertEqual(self.tool("Bash", {"command": f"echo {dot}"}), [])

    def test_zip_is_offered_as_download_only_with_its_bytes(self):
        evs = self.tool("Bash", {"command": "zip out.zip a"},
                        make=lambda: self.put("out.zip", b"PK\x03\x04"))
        self.assertEqual(len(evs), 1)
        self.assertTrue(evs[0]["download_only"])
        self.assertEqual(evs[0]["mime"], "application/zip")
        self.assertTrue(evs[0]["b64"])
        self.assertNotIn("too_large", evs[0])

    def test_oversize_file_is_offered_as_too_large_without_bytes(self):
        with mock.patch.object(self.events, "FILE_MAX_BYTES", 50):
            evs = self.tool("Bash", {"command": "ffmpeg"},
                            make=lambda: self.put("v.mp4", b"0" * 100))
        self.assertEqual(len(evs), 1)
        e = evs[0]
        self.assertTrue(e["too_large"] and e["download_only"])
        self.assertEqual((e["b64"], e["size"], e["mime"]), ("", 100, "video/mp4"))
        self.assertTrue(e["path"].endswith("v.mp4") and e["reason"])

    def test_write_of_an_unlisted_type_is_dropped_but_logged_with_the_reason(self):
        p = self.put("run.sh")
        with self.assertLogs("agent-bridge.files", level="INFO") as cm:
            evs = self.tool("Write", {"file_path": p})
        self.assertEqual(evs, [])
        self.assertTrue(any("file dropped" in m and "unlisted type .sh" in m for m in cm.output))

    def test_every_file_event_is_logged_at_info(self):
        with self.assertLogs("agent-bridge.files", level="INFO") as cm:
            self.tool("Bash", {"command": "x"}, make=lambda: self.put("a.csv"))
        self.assertTrue(any("file event" in m and "a.csv" in m and "source=scan" in m
                            for m in cm.output))

    def test_scan_is_bounded(self):
        for i in range(5):
            self.put(f"f{i}.png")
        with mock.patch.object(self.events, "SCAN_MAX_FILES", 3):
            self.assertEqual(len(self.events._scan(self.tmp)), 3)


class Inbox(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self._p = mock.patch.multiple(inbox, ROOT=self.root, MAX_BYTES=100)
        self._p.start()
        self.addCleanup(self._p.stop)

    @staticmethod
    def item(name, data):
        import base64
        return {"name": name, "b64": base64.b64encode(data).decode()}

    def test_saves_under_a_private_chat_dir_and_names_the_paths(self):
        saved, skipped = inbox.save("chat/1", [self.item("notes.txt", b"hi")])
        self.assertEqual(skipped, [])
        self.assertTrue(saved[0].startswith(self.root) and saved[0].endswith("notes.txt"))
        self.assertEqual(open(saved[0], "rb").read(), b"hi")
        self.assertEqual(os.stat(os.path.dirname(saved[0])).st_mode & 0o777, 0o700)
        self.assertNotIn("chat/1", os.path.relpath(saved[0], self.root).split(os.sep)[0])

    def test_names_cannot_escape_and_duplicates_do_not_overwrite(self):
        saved, _ = inbox.save("c", [self.item("../../etc/x.txt", b"a"), self.item("x.txt", b"b")])
        self.assertTrue(all(os.path.dirname(p) == os.path.dirname(saved[0]) for p in saved))
        self.assertEqual(len(set(saved)), 2)
        self.assertTrue(all(p.startswith(self.root) for p in saved))

    def test_the_same_file_id_is_stored_once_across_turns(self):
        it = dict(self.item("a.txt", b"hello"), id="abc12345-xyz")
        first, _ = inbox.save("c", [it])
        again, _ = inbox.save("c", [it])
        self.assertEqual(first, again)
        self.assertEqual(len(os.listdir(os.path.dirname(first[0]))), 1)

    def test_oversize_empty_and_corrupt_are_skipped_with_a_reason(self):
        saved, skipped = inbox.save("c", [self.item("big.bin", b"x" * 101), self.item("e.txt", b""),
                                          {"name": "bad.txt", "b64": "!!"}])
        self.assertEqual(saved, [])
        self.assertEqual(len(skipped), 3)

    def test_at_most_ten_files_a_message(self):
        saved, skipped = inbox.save("c", [self.item(f"f{i}.txt", b"a") for i in range(12)])
        self.assertEqual(len(saved), 10)
        self.assertIn("2 more", skipped[-1])

    def test_sweep_removes_only_old_files(self):
        saved, _ = inbox.save("c", [self.item("old.txt", b"a"), self.item("new.txt", b"b")])
        os.utime(saved[0], (1, 1))
        self.assertEqual(inbox.sweep(), 1)
        self.assertFalse(os.path.exists(saved[0]))
        self.assertTrue(os.path.exists(saved[1]))

    def test_chat_tells_the_agent_where_the_files_are(self):
        app, mgr = make_app("plain")
        seen = []
        real = mgr.start

        def spy(chat_id, cwd, pid, text, **kw):
            seen.append(text)
            return real(chat_id, cwd, pid, text, **kw)
        mgr.start = spy
        drive(app.chat("c1", "general", "summarise this", attachments=[self.item("a.txt", b"hello")]))
        self.assertIn("summarise this", seen[0])
        self.assertIn("a.txt", seen[0])
        self.assertIn(self.root, seen[0])


class Bridge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        runner.CLAUDE = FAKE
        # fake claude is a script: run it through the interpreter
        self._orig = runner.RunManager.start

    def _cmd_patch(self):
        pass

    def test_approve_resumes_the_run_and_reports_the_result(self):
        app, mgr = make_app("ask")
        mgr.claude = FAKE
        evs = drive(app.chat("c1", "general", "push it"),
                    on_gate=lambda g: self.assertTrue(
                        mgr.gates.decide(g["run_id"], "approve", chat_id="c1")["ok"]))
        t = text(evs)
        self.assertIn("approved", t)
        self.assertIn("✓ done", t)                  # tool_result-backed
        self.assertIn("1 ok, 0 failed", t)
        self.assertEqual(evs[-1]["status"], "done")

    def test_deny_reports_a_failed_tool_not_a_push(self):
        app, mgr = make_app("ask")
        evs = drive(app.chat("c1", "general", "push it"),
                    on_gate=lambda g: mgr.gates.decide(g["run_id"], "deny", chat_id="c1"))
        t = text(evs)
        self.assertIn("denied", t)
        self.assertIn("0 ok, 1 failed", t)
        self.assertNotIn("pushed", t)

    def test_approve_with_nothing_pending_says_so(self):
        app, _ = make_app("plain")
        t = text(list(app.chat("c1", "general", "approve")))
        self.assertIn("nothing is pending", t)
        self.assertIn("nothing was approved", t)

    def test_approve_for_wrong_run_id_acts_on_nothing(self):
        g = gates.Gates()
        r = g.decide("deadbeef", "approve")
        self.assertFalse(r["ok"])

    def test_question_answer_maps_number_to_option(self):
        app, mgr = make_app("question")
        pend = []

        def gate(ev):
            pend.append(ev)
            r = mgr.gates.decide(ev["run_id"], "approve",
                                 server.answers_for(ev["questions"], "2"), "c1")
            self.assertTrue(r["ok"])
        evs = drive(app.chat("c1", "general", "pick"), on_gate=gate)
        self.assertEqual(pend[0]["kind"], "question")
        self.assertIn("feat/x", text(evs))

    def test_session_is_resumed_on_the_next_turn(self):
        log = os.path.join(self.tmp, "argv.log")
        app, _ = make_app("plain", argv_log=log)
        drive(app.chat("c1", "general", "one"))
        drive(app.chat("c1", "general", "two"))
        calls = [json.loads(l) for l in open(log)]
        self.assertIn("--session-id", calls[0])
        self.assertIn("--resume", calls[1])
        self.assertEqual(calls[0][calls[0].index("--session-id") + 1],
                         calls[1][calls[1].index("--resume") + 1])
        for c in calls:
            self.assertIn("--permission-prompt-tool", c)

    def test_api_key_source_is_refused(self):
        app, _ = make_app("apikey")
        evs = drive(app.chat("c1", "general", "hi"))
        self.assertEqual(evs[-1]["status"], "failed")
        self.assertIn("not the local Max login", text(evs))

    def test_stop_kills_a_running_turn(self):
        import threading
        import time
        app, mgr = make_app("hang")
        run = mgr.start("c1", self.tmp, "general", "x")
        time.sleep(0.5)
        self.assertTrue(mgr.stop(chat_id="c1")["ok"])
        for _ in run.stream():
            pass
        self.assertEqual(run.status, "stopped")

    def test_second_turn_while_running_is_refused(self):
        app, mgr = make_app("hang")
        run = mgr.start("c1", self.tmp, "general", "x")
        with self.assertRaises(runner.Refused):
            mgr.start("c1", self.tmp, "general", "y")
        mgr.stop(chat_id="c1")
        for _ in run.stream():
            pass

    def test_pending_footer_lists_operator_and_agent_items(self):
        r = __import__("events").Renderer()
        r.todos = [{"content": "write tests", "status": "pending"}, {"content": "x", "status": "completed"}]
        f = r.footer("abc12345", ["Approve run abc12345"], r.open_todos(), "waiting")
        self.assertIn("Operator:\n1. Approve run abc12345", f)
        self.assertIn("Agent:\n2. write tests", f)

    def test_background_flag_returns_at_once(self):
        app, mgr = make_app("plain")
        evs = list(app.chat("c1", "general", "!bg do it"))
        self.assertEqual(evs[-1]["status"], "background")
        run = mgr.get(evs[-1]["run_id"])
        for _ in run.stream():
            pass
        out = text(list(app.chat("c1", "general", "next")))
        self.assertIn("background run", out)


class Policy(unittest.TestCase):
    def d(self, cmd, cwd="/tmp"):
        return policy_hook.decide("Bash", {"command": cmd}, cwd)[0]

    def test_stash_denied_list_allowed(self):
        self.assertEqual(self.d("git stash"), "deny")
        self.assertEqual(self.d("git -C ~/nomad stash push -u"), "deny")
        self.assertEqual(self.d("git stash list"), "allow")

    def test_commit_in_shared_checkout_needs_only(self):
        hub = policy_hook.SHARED
        self.assertEqual(self.d("git commit -m x", hub), "deny")
        self.assertEqual(self.d("git commit --only a.py -m x", hub), "allow")
        self.assertEqual(self.d("git commit -m x", "/tmp"), "allow")

    def test_the_cockpit_token_and_guardrail_save_are_denied(self):
        self.assertEqual(self.d("cat ~/.config/nomad/cockpit-token"), "deny")
        self.assertEqual(self.d("curl -X POST localhost:8099/api/policies/guardrail-save"), "deny")
        tok = {"file_path": os.path.expanduser("~/.config/nomad/cockpit-token")}
        self.assertEqual(policy_hook.decide("Read", tok, "/tmp")[0], "deny")
        self.assertEqual(policy_hook.decide("WebFetch", {"url": "http://localhost:8099/api/policies/guardrail-save"},
                                            "/tmp")[0], "deny")
        self.assertEqual(policy_hook.decide("Edit", {"file_path": "v2/static/p.js",
                                                     "new_string": "/api/policies/guardrail-save"}, "/tmp")[0], "allow")
        self.assertEqual(policy_hook.decide("Read", {"file_path": "/tmp/a.py"}, "/tmp")[0], "allow")

    def test_push_asks_force_denies_delete_asks(self):
        self.assertEqual(self.d("git push origin feat/x"), "ask")
        self.assertEqual(self.d("git push --force origin x"), "deny")
        self.assertEqual(self.d("rm -rf build"), "ask")
        self.assertEqual(self.d("docker compose up -d --force-recreate svc"), "ask")
        self.assertEqual(self.d("ls -la"), "allow")


class Projects(unittest.TestCase):
    rows = [{"id": "civis", "path": "/tmp", "local_only": True},
            {"id": "shift", "path": "/tmp", "aliases": ["sh"]},
            {"id": "gone", "path": "/nonexistent-xyz"}]

    def test_general_is_the_hub(self):
        self.assertEqual(projects.resolve("", self.rows), ("general", projects.HUB))
        self.assertEqual(projects.resolve("p-shift", self.rows)[0], "shift")

    def test_refusals(self):
        for p in ("civis", "nope", "gone"):
            with self.assertRaises(ValueError):
                projects.resolve(p, self.rows)


class Fallback(unittest.TestCase):
    def setUp(self):
        import backends
        self.be = backends
        self._codex = backends.CODEX
        backends.CODEX = os.path.join(HERE, "fake_codex.py")
        self.log = tempfile.mktemp()
        os.environ["FAKE_ARGV_LOG"] = self.log

    def tearDown(self):
        self.be.CODEX = self._codex

    def _app(self, scenario):
        app, mgr = make_app(scenario, self.log)
        mgr.be.available = (lambda orig: lambda n, **kw: (False, "test off") if n == "local"
                            else orig(n, **kw))(mgr.be.available)
        return app, mgr

    def test_label_names_the_agent_and_model(self):
        app, mgr = self._app("plain")
        evs = drive(app.chat("f0", "general", "edit the file"))
        t = text(evs)
        self.assertIn("Claude Code · Max plan", t)
        self.assertEqual(evs[-1]["backend"], "claude")
        self.assertIn("Claude Code · Max plan", evs[-1]["agent"])

    def test_quota_falls_back_to_codex_and_says_so(self):
        app, mgr = self._app("quota")
        evs = drive(app.chat("f1", "general", "edit the file"))
        t = text(evs)
        self.assertIn("continuing on **Codex CLI", t)
        self.assertIn("codex did it", t)
        self.assertIn("Codex CLI · ChatGPT plan", evs[-1]["agent"])
        self.assertEqual(evs[-1]["status"], "done")
        # claude now cools down: the next turn goes straight to codex, no failed attempt
        evs2 = drive(app.chat("f1", "general", "again"))
        self.assertNotIn("usage limit", text(evs2))
        self.assertEqual(evs2[-1]["backend"], "codex")
        with open(self.log) as f:
            codex_calls = [json.loads(l) for l in f if l.startswith('{"argv"')]
        self.assertIn("resume", codex_calls[-1]["argv"])           # same thread continued
        self.assertIn("th-123", codex_calls[-1]["argv"])

    def test_forced_backend_does_not_fall_back(self):
        app, mgr = self._app("quota")
        evs = drive(app.chat("f2", "general", "!claude edit the file"))
        self.assertEqual(evs[-1]["status"], "failed")
        self.assertNotIn("continuing on", text(evs))

    def test_codex_can_be_forced_and_sees_handoff_and_preamble(self):
        app, mgr = self._app("plain")
        drive(app.chat("f3", "general", "first thing"))
        evs = drive(app.chat("f3", "general", "!codex second thing"))
        self.assertEqual(evs[-1]["backend"], "codex")
        with open(self.log) as f:
            call = [json.loads(l) for l in f if l.startswith('{"argv"')][-1]
        self.assertIn("stand-in while Claude Code is unavailable", call["prompt"])
        self.assertIn("all done", call["prompt"])                  # last result handed over
        self.assertIn("second thing", call["prompt"])
        self.assertIn("✓ done", text(evs))                         # Bash result is a real tool_result

    def test_no_backend_available_says_why(self):
        app, mgr = self._app("plain")
        mgr.be.available = lambda n, **kw: (False, "test off")
        t = text(drive(app.chat("f4", "general", "edit the file")))
        self.assertIn("No agent backend is available", t)

    def test_no_fallback_once_a_tool_has_run(self):
        self.assertFalse(self.be.classify("usage limit reached", tools_ran=1))
        self.assertTrue(self.be.classify("usage limit reached", tools_ran=0))
        self.assertFalse(self.be.classify("tests failed: 3 errors", tools_ran=0))


class DurableAsks(unittest.TestCase):
    def _open(self, g, chat="c1", run="r1", kind="permission"):
        tin = {"questions": [{"question": "Which?", "header": "W", "multiSelect": False,
                              "options": [{"label": "A", "description": "a"}]}]} if kind == "question" \
            else {"command": "git push"}
        p = gates.Pending(run, chat, "req", "AskUserQuestion" if kind == "question" else "Bash", tin, kind)
        g.open(p)
        return p

    def test_ask_has_id_and_is_listed_with_payload(self):
        g = gates.Gates(Store(":memory:"))
        p = self._open(g)
        q = self._open(g, run="r2", kind="question")
        rows = {r["ask_id"]: r for r in g.list_asks("c1")}
        self.assertTrue(p.ask_id.startswith("ask-"))
        self.assertEqual(rows[p.ask_id]["state"], "pending")
        self.assertEqual(rows[p.ask_id]["tool"], "Bash")
        self.assertEqual(rows[q.ask_id]["questions"][0]["options"][0]["label"], "A")
        self.assertEqual(g.list_asks("other"), [])

    def test_decide_by_ask_id_and_first_answer_wins(self):
        g = gates.Gates(Store(":memory:"))
        p = self._open(g)
        r = g.decide("", "approve", ask_id=p.ask_id)
        self.assertTrue(r["ok"])
        self.assertEqual(p.decision["decision"], "approve")
        late = g.decide("", "deny", ask_id=p.ask_id)
        self.assertEqual(late, {"ok": False, "error": "already answered"})
        self.assertEqual(g.decide("r1", "deny"), {"ok": False, "error": "already answered"})
        self.assertEqual(p.decision["decision"], "approve")          # the second changed nothing
        self.assertEqual(g.list_asks()[0]["decision"], "approve")

    def test_concurrent_answers_apply_exactly_one(self):
        import threading
        g = gates.Gates(Store(":memory:"))
        p = self._open(g)
        res = []
        ts = [threading.Thread(target=lambda d=d: res.append(g.decide("", d, ask_id=p.ask_id)))
              for d in ("approve", "deny") * 4]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sum(1 for r in res if r["ok"]), 1)

    def test_restart_keeps_the_ask_listed_as_expired_and_applies_nothing(self):
        path = os.path.join(tempfile.mkdtemp(), "state.db")
        g1 = gates.Gates(Store(path))
        p = self._open(g1)
        g2 = gates.Gates(Store(path))                                # the bridge restarted
        self.assertEqual(g2.expired_at_start, 1)
        row = g2.list_asks("c1")[0]
        self.assertEqual((row["ask_id"], row["state"]), (p.ask_id, "expired"))
        r = g2.decide("", "approve", ask_id=p.ask_id)
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"], "expired")
        self.assertIn("nothing was applied", r["error"])
        self.assertFalse(g2.decide("r1", "approve")["ok"])           # by run id too
        self.assertEqual(g2.pending("c1"), [])

    def test_timeout_denies_and_a_late_answer_applies_nothing(self):
        g = gates.Gates(Store(":memory:"))
        p = self._open(g)
        d = g.wait(p, timeout=0.05)
        self.assertEqual(d["decision"], "deny")
        r = g.decide("", "approve", ask_id=p.ask_id)
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"], "timeout")
        self.assertEqual(g.list_asks()[0]["state"], "timeout")

    def test_unknown_ask_id_applies_nothing(self):
        g = gates.Gates(Store(":memory:"))
        self.assertFalse(g.decide("", "approve", ask_id="ask-deadbeef")["ok"])

    def test_parse_answer_forms(self):
        a = gates.parse_answer('answer ask-1a2b {"decision":"deny","note":"no"}')
        self.assertEqual((a["ok"], a["ask_id"], a["decision"], a["note"]), (True, "ask-1a2b", "deny", "no"))
        a = gates.parse_answer('answer ask-1a2b {"answers":[{"question":"Q1","answer":"A"},'
                               '{"question":"Q2","answer":["x","y"]}]}')
        self.assertEqual(a["answers"], {"Q1": "A", "Q2": "x, y"})
        self.assertEqual(a["decision"], "approve")
        for bad in ("answer ask-1a2b nope", 'answer ask-1a2b {"x":1}', 'answer ask-1a2b {"answers":[]}',
                    'answer ask-1a2b {"answers":[{"question":"Q"}]}', 'answer ask-1a2b {"decision":"maybe"}',
                    "answer 3 {}", "hello"):
            self.assertFalse(gates.parse_answer(bad)["ok"], bad)

    def test_answer_text_wrong_chat_and_kind_mismatch(self):
        g = gates.Gates(Store(":memory:"))
        p = self._open(g)
        self.assertFalse(g.answer_text('answer %s {"decision":"approve"}' % p.ask_id, "other")["ok"])
        self.assertFalse(g.answer_text('answer %s {"answers":[{"question":"Q","answer":"A"}]}' % p.ask_id,
                                       "c1")["ok"])
        self.assertEqual(p.state, "pending")                         # neither applied
        self.assertTrue(g.answer_text('answer %s {"decision":"deny","note":"nope"}' % p.ask_id, "c1")["ok"])
        self.assertEqual(p.decision["note"], "nope")

    def test_answer_message_in_chat_resolves_a_live_question(self):
        app, mgr = make_app("question")

        def gate(ev):
            msg = 'answer %s {"answers":[{"question":"%s","answer":"%s"}]}' % (
                ev["ask_id"], ev["questions"][0]["question"], ev["questions"][0]["options"][1]["label"])
            gate.out = text(list(app.chat("c1", "general", msg)))
        evs = drive(app.chat("c1", "general", "pick"), on_gate=gate)
        self.assertIn("feat/x", text(evs) + gate.out)

    def test_the_answer_says_where_it_came_from(self):
        g = gates.Gates(Store(":memory:"))
        p, q = self._open(g), self._open(g, run="r2")
        self.assertTrue(g.decide("", "approve", ask_id=p.ask_id)["ok"])             # the cockpit
        self.assertTrue(g.answer_text('answer %s {"decision":"deny"}' % q.ask_id, "c1", via="chat")["ok"])
        rows = {r["ask_id"]: r for r in g.list_asks("c1")}
        self.assertEqual((rows[p.ask_id]["via"], rows[q.ask_id]["via"]), ("api", "chat"))

    def test_a_chat_answer_is_marked_chat(self):
        app, mgr = make_app("question")

        def gate(ev):
            msg = 'answer %s {"answers":[{"question":"%s","answer":"B"}]}' % (ev["ask_id"], ev["questions"][0]["question"])
            list(app.chat("c1", "general", msg))
        drive(app.chat("c1", "general", "pick"), on_gate=gate)
        self.assertEqual(mgr.gates.list_asks("c1")[0]["via"], "chat")

    def test_after_a_cockpit_answer_attach_from_delivered_streams_only_the_rest(self):
        """The inline card ends the chat turn at the gate; the pipe's watcher attaches with
        cursor=delivered once the cockpit answers, and gets the continuation, not a replay (#1406)."""
        app, mgr = make_app("question")
        first = []
        for ev in app.chat("c1", "general", "pick"):
            first.append(ev)
            if ev.get("type") == "gate":
                break                                                   # the turn ends at the card
        gate = first[-1]
        run = mgr.get(gate["run_id"])
        label = gate["questions"][0]["options"][1]["label"]
        self.assertTrue(mgr.gates.decide("", "approve", {gate["questions"][0]["question"]: label},
                                         ask_id=gate["ask_id"])["ok"])
        rest = list(app._follow(run))
        self.assertIn("feat/x", text(rest))
        self.assertFalse(any(e.get("type") == "gate" for e in rest))     # nothing the chat already showed
        self.assertEqual(rest[-1]["type"], "done")

    def test_unanswered_run_stop_never_approves(self):
        app, mgr = make_app("ask")
        seen = []

        def gate(ev):
            seen.append(ev["ask_id"])
            mgr.stop(ev["run_id"], "c1")
        evs = drive(app.chat("c1", "general", "push it"), on_gate=gate)
        self.assertNotIn("approved", text(evs))
        self.assertEqual(mgr.gates.list_asks("c1")[0]["state"], "cancelled")
        self.assertEqual(mgr.gates.decide("", "approve", ask_id=seen[0])["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()


class LocalOnDemand(unittest.TestCase):
    def test_local_is_available_when_startable_and_started_by_ensure(self):
        import tempfile, stat
        d = tempfile.mkdtemp()
        flag = os.path.join(d, "up")
        script = os.path.join(d, "llama.sh")
        open(script, "w").write(f"#!/bin/sh\n[ \"$1\" = start ] && touch {flag}\n")
        os.chmod(script, os.stat(script).st_mode | stat.S_IEXEC)
        key = os.path.join(d, "key"); open(key, "w").write("k")
        be = backends.Backends("/bin/true")
        with mock.patch.object(backends, "LLAMA_SCRIPT", script), \
             mock.patch.object(backends, "LITELLM_KEY_FILE", key), \
             mock.patch.object(backends.Backends, "local_up", side_effect=lambda: os.path.exists(flag)):
            ok, why = be.available("local")
            self.assertTrue(ok); self.assertIn("on demand", why)
            self.assertEqual(be.ensure_local(), (True, ""))
            self.assertTrue(os.path.exists(flag))

    def _script(self, d, body):
        import stat
        script = os.path.join(d, "llama.sh")
        open(script, "w").write("#!/bin/sh\n" + body)
        os.chmod(script, os.stat(script).st_mode | stat.S_IEXEC)
        return script

    def test_exit_75_from_the_script_means_gpu_busy_not_a_failed_start(self):    # #1462
        d = tempfile.mkdtemp()
        script = self._script(d, "echo 'GPU busy: another GPU workload holds admission' >&2; exit 75\n")
        be = backends.Backends("/bin/true")
        with mock.patch.object(backends, "LLAMA_SCRIPT", script), \
             mock.patch.object(backends.Backends, "local_up", return_value=False):
            ok, why = be.ensure_local()
        self.assertFalse(ok)
        self.assertIn("GPU is busy", why)
        self.assertNotIn("did not start", why)
        self.assertFalse(getattr(be, "started_local", False))
        # any other non-zero exit is still a failed start
        script = self._script(d, "exit 1\n")
        with mock.patch.object(backends, "LLAMA_SCRIPT", script), \
             mock.patch.object(backends.Backends, "local_up", return_value=False):
            ok, why = be.ensure_local()
        self.assertFalse(ok)
        self.assertIn("did not start", why)

    def test_idle_stop_defers_on_gpu_busy_and_retries(self):    # #1462
        d = tempfile.mkdtemp()
        log = os.path.join(d, "calls")
        script = self._script(d, f'echo "$1" >> {log}\n[ $(wc -l < {log}) -ge 2 ] && exit 0\nexit 75\n')
        be = backends.Backends("/bin/true")
        be.started_local, be._idle_thread, be.last_local = True, object(), 0
        with mock.patch.object(backends, "LLAMA_SCRIPT", script), \
             mock.patch.object(backends.time, "sleep"), \
             mock.patch.object(be, "clock", return_value=10**9):
            be._idle_stop()
        self.assertEqual(open(log).read().split(), ["stop", "stop"])   # busy once, then stopped
        self.assertFalse(be.started_local)
        self.assertIsNone(be._idle_thread)


class AgentState(unittest.TestCase):
    def test_state_reports_whether_the_local_model_is_up(self):    # #1438
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        app, _ = make_app("plain")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            for up in (True, False):
                with mock.patch.object(server, "read_token", return_value="t"), \
                     mock.patch.object(backends.Backends, "local_up", return_value=up):
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{httpd.server_port}/agent/state?chat_id=c1&project=general",
                        headers={"X-Nomad-Token": "t"})
                    st = json.loads(urllib.request.urlopen(req, timeout=5).read())
                self.assertIs(st["local_up"], up)
                self.assertTrue(st["eligible"])
        finally:
            httpd.shutdown()


class McpRegistry(unittest.TestCase):
    def _fake(self, d):
        src = os.path.join(d, "mcp.json"); out = os.path.join(d, "all.json"); codex = os.path.join(d, "c.toml")
        open(src, "w").write(json.dumps({"mcpServers": {"nomad-pm": {"command": "/py"}}}))
        mod = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "mcp-sync.py")
        return src, out, codex, mod

    def test_local_gets_only_the_projects_servers_and_refuses_too_many(self):
        d = tempfile.mkdtemp()
        src, out, codex, mod = self._fake(d)
        grants = os.path.join(d, "g.json")
        json.dump({"default": ["nomad-pm"], "projects": {"p": ["nomad-pm"]}}, open(grants, "w"))
        env = {"MCP_SYNC_PROJECT": src, "MCP_SYNC_USER": os.path.join(d, "none.json"),
               "MCP_SYNC_CODEX": codex, "MCP_SYNC_LOCAL": out, "MCP_SYNC_BACKENDS": os.path.join(d, "b.json")}
        with mock.patch.dict(os.environ, env), mock.patch.object(backends, "MCP_SYNC", mod), \
                mock.patch.object(backends, "MCP_GRANTS", grants):
            be = backends.Backends("/bin/true")
            be.mcp_sync()
            p = be.mcp_config("p")
            self.assertNotEqual(p, out)
            self.assertEqual(list(json.load(open(p))["mcpServers"]), ["nomad-pm"])
            with mock.patch.object(backends, "LOCAL_MAX_SERVERS", 0):
                with self.assertRaises(ValueError):
                    be.mcp_config("p")

    def test_sync_runs_once_per_change_and_local_gets_the_file_and_tool_search(self):
        d = tempfile.mkdtemp()
        src, out, codex, mod = self._fake(d)
        env = {"MCP_SYNC_PROJECT": src, "MCP_SYNC_USER": os.path.join(d, "none.json"),
               "MCP_SYNC_CODEX": codex, "MCP_SYNC_LOCAL": out, "MCP_SYNC_BACKENDS": os.path.join(d, "b.json")}
        with mock.patch.dict(os.environ, env), mock.patch.object(backends, "MCP_SYNC", mod):
            be = backends.Backends("/bin/true")
            self.assertEqual(be.mcp_sync(), "")
            self.assertEqual(be.mcp_config(), out)
            self.assertIn("nomad-pm", json.load(open(out))["mcpServers"])
            self.assertIn("nomad-pm", open(codex).read())
            self.assertEqual(be.env("local")["ENABLE_TOOL_SEARCH"], "true")
            for k in backends.LOCAL_MODEL_VARS:     # background calls too, never a claude-* name
                self.assertEqual(be.env("local")[k], backends.LOCAL_MODEL)
            rep = be.mcp_report()
            self.assertIn("codex: 1 servers", rep); self.assertIn("local: 1 servers", rep)
