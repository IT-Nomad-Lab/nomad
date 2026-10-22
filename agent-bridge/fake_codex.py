#!/usr/bin/env python3
"""A stand-in for `codex exec --json` for the tests."""
import json
import os
import sys

prompt = sys.stdin.read()
log = os.environ.get("FAKE_ARGV_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"argv": sys.argv[1:], "prompt": prompt}) + "\n")


def out(o):
    print(json.dumps(o), flush=True)


out({"type": "thread.started", "thread_id": "th-123"})
out({"type": "turn.started"})
out({"type": "item.started", "item": {"id": "i1", "type": "command_execution",
     "command": "/bin/bash -lc 'echo hi'", "aggregated_output": "", "exit_code": None}})
out({"type": "item.completed", "item": {"id": "i1", "type": "command_execution",
     "command": "/bin/bash -lc 'echo hi'", "aggregated_output": "hi\n", "exit_code": 0, "status": "completed"}})
out({"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "codex did it"}})
out({"type": "turn.completed", "usage": {}})
