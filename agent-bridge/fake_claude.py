#!/usr/bin/env python3
"""A stand-in for `claude -p --input-format stream-json` for the tests. FAKE_SCENARIO picks the script:
ask (one Bash permission request), write (Write tool calls for FAKE_WRITE_PATHS), question (AskUserQuestion), plain (text only), apikey (bad
apiKeySource), hang (never answers). argv is appended to FAKE_ARGV_LOG."""
import json
import os
import sys
import time

log = os.environ.get("FAKE_ARGV_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps(sys.argv[1:]) + "\n")
sc = os.environ.get("FAKE_SCENARIO", "plain")
sid = sys.argv[sys.argv.index("--session-id") + 1] if "--session-id" in sys.argv else \
    sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else "s0"


def out(o):
    print(json.dumps(o), flush=True)


sys.stdin.readline()
out({"type": "system", "subtype": "init", "session_id": sid,
     "apiKeySource": "ANTHROPIC_API_KEY" if sc == "apikey" else "none"})
if sc == "quota":
    out({"type": "result", "subtype": "error", "is_error": True,
         "result": "Claude AI usage limit reached|1760000000", "session_id": sid})
    sys.exit(0)
if sc == "hang":
    time.sleep(60)
if sc == "write":
    # FAKE_WRITE_PATHS: comma list; the file is written by this script, then reported like Claude Code does
    for n, path in enumerate(filter(None, os.environ.get("FAKE_WRITE_PATHS", "").split(","))):
        if not path.startswith("!"):
            with open(path, "wb") as fh:
                fh.write(b"\x89PNG\r\n" if path.endswith(".png") else b"hello,world\n")
        path = path.lstrip("!")
        out({"type": "assistant", "message": {"id": f"w{n}", "content": [
            {"type": "tool_use", "id": f"w{n}", "name": "Write", "input": {"file_path": path}}]}})
        out({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"w{n}", "content": "ok",
             "is_error": path.endswith(".fail")}]}})
if sc in ("ask", "question"):
    tool = "AskUserQuestion" if sc == "question" else "Bash"
    tin = ({"questions": [{"question": "Which branch?", "header": "Branch", "multiSelect": False,
                           "options": [{"label": "main"}, {"label": "feat/x"}]}]}
           if sc == "question" else {"command": "git push origin feat/x"})
    out({"type": "assistant", "message": {"id": "m1", "content": [
        {"type": "tool_use", "id": "t1", "name": tool, "input": tin}]}})
    out({"type": "control_request", "request_id": "r1",
         "request": {"subtype": "can_use_tool", "tool_name": tool, "input": tin, "tool_use_id": "t1"}})
    resp = json.loads(sys.stdin.readline())["response"]["response"]
    ok = resp.get("behavior") == "allow"
    body = json.dumps(resp.get("updatedInput", {}).get("answers")) if (ok and sc == "question") \
        else ("pushed" if ok else resp.get("message"))
    out({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": body, "is_error": not ok}]}})
out({"type": "assistant", "message": {"id": "m2", "content": [{"type": "text", "text": "all done"}]}})
out({"type": "result", "subtype": "success", "is_error": False, "result": "all done",
     "session_id": sid, "total_cost_usd": 0.01})
