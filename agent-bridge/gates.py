"""Pending permission requests and questions, each with a stable ask_id, durable in store.py.

An ask is born when claude sends a `can_use_tool` control request. The run's reader thread blocks
in `wait()` until the operator decides (`decide()`), the run is stopped, or the timeout passes
(then it is denied: no answer is never an approval).

States: pending -> answered | timeout | cancelled (run stopped) | expired.
`expired` is what a restart does to every ask still pending: the claude process that asked died
with the bridge, so there is nothing left to answer. The ask stays listed (honest history) and a
decision on it says so and applies nothing.

First answer wins: the first decision for an ask_id applies; any later one gets
{"ok": False, "error": "already answered"} and changes nothing.

Chat form: `answer <ask_id> <json>` (see parse_answer) with json either
{"decision": "approve"|"deny", "note"?: str} or {"answers": [{"question", "answer"}]}
where answer is a string or a list of strings.
"""
import json
import re
import threading
import time
import uuid

TIMEOUT_S = 1800
ALREADY = {"ok": False, "error": "already answered"}
ANSWER_RE = re.compile(r"^\s*answer\s+(ask-[0-9a-f]+)\s+(.+?)\s*$", re.I | re.S)


def new_ask_id() -> str:
    return "ask-" + uuid.uuid4().hex[:10]


class Pending:
    def __init__(self, run_id, chat_id, request_id, tool, input_, kind, reason=""):
        self.run_id, self.chat_id, self.request_id = run_id, chat_id, request_id
        self.tool, self.input, self.kind = tool, input_, kind     # kind: permission | question
        self.reason = reason
        self.ask_id = new_ask_id()
        self.state = "pending"
        self.created = time.time()
        self.decision = None                                     # {"decision", "answers"?}
        self.event = threading.Event()


def parse_answer(text: str) -> dict:
    """`answer <ask_id> <json>` -> {"ok": True, "ask_id", "decision", "answers", "note"} or
    {"ok": False, "error"}. `answers` is {question: "a, b"} (lists joined); an answers payload means
    approve. Nothing is guessed: bad json or a missing field is an error, never an approval."""
    m = ANSWER_RE.match(text or "")
    if not m:
        return {"ok": False, "error": "expected: answer <ask_id> <json>"}
    ask_id = m.group(1).lower()
    try:
        body = json.loads(m.group(2))
    except ValueError:
        return {"ok": False, "error": "the answer is not valid json"}
    if not isinstance(body, dict):
        return {"ok": False, "error": "the answer must be a json object"}
    if "answers" in body:
        if not isinstance(body["answers"], list) or not body["answers"]:
            return {"ok": False, "error": "answers must be a non-empty list"}
        out = {}
        for a in body["answers"]:
            if not isinstance(a, dict) or not a.get("question") or "answer" not in a:
                return {"ok": False, "error": "each answer needs question and answer"}
            v = a["answer"]
            if isinstance(v, list):
                v = ", ".join(str(x) for x in v)
            elif not isinstance(v, str):
                return {"ok": False, "error": "answer must be a string or a list"}
            out[str(a["question"])] = v
        return {"ok": True, "ask_id": ask_id, "decision": "approve", "answers": out, "note": ""}
    if body.get("decision") in ("approve", "deny"):
        return {"ok": True, "ask_id": ask_id, "decision": body["decision"], "answers": {},
                "note": str(body.get("note") or "")}
    return {"ok": False, "error": 'json needs "decision": "approve"|"deny" or "answers"'}


class Gates:
    def __init__(self, store=None):
        self._by_ask: dict[str, Pending] = {}
        self._lock = threading.Lock()
        self.store = store
        self.expired_at_start = store.expire_open_asks() if store else 0

    def open(self, p: Pending):
        with self._lock:
            self._by_ask[p.ask_id] = p
        if self.store:
            self.store.ask_add(_row(p))

    def _close(self, p: Pending, state: str, decision: dict):
        """Under self._lock. Settles the ask exactly once."""
        p.state, p.decision = state, decision
        if self.store:
            self.store.ask_set(p.ask_id, state, decision)
        p.event.set()

    def wait(self, p: Pending, timeout: float = TIMEOUT_S) -> dict:
        p.event.wait(timeout)
        with self._lock:
            if p.state == "pending":                         # nobody answered: deny
                self._close(p, "timeout", {"decision": "deny", "note": f"no answer within {int(timeout)}s"})
            self._by_ask.pop(p.ask_id, None)
        return p.decision

    def pending(self, chat_id: str = "") -> list[Pending]:
        with self._lock:
            return [p for p in self._by_ask.values()
                    if p.state == "pending" and (not chat_id or p.chat_id == chat_id)]

    def list_asks(self, chat_id: str = "", state: str = "") -> list[dict]:
        """Every ask, any state, oldest first, in the shape GET /agent/pending returns."""
        if self.store:
            rows = self.store.asks(chat_id)
        else:
            with self._lock:
                rows = [_row(p) for p in self._by_ask.values() if not chat_id or p.chat_id == chat_id]
        out = [_public(r) for r in rows]
        return [r for r in out if not state or r["state"] == state]

    def decide(self, run_id: str, decision: str, answers: dict | None = None, chat_id: str = "",
               ask_id: str = "", via: str = "api") -> dict:
        """{"ok": True, ...} when a real pending ask took the decision, else {"ok": False,
        "error": ...} saying nothing was applied. `via` is where the answer came from: "chat" when
        the chat turn that sent it streams the rest of the run, "api" (the cockpit) when nobody does,
        so the pipe's watcher carries the run into the chat (#1406)."""
        if decision not in ("approve", "deny"):
            return {"ok": False, "error": f"decision must be approve or deny, not {decision!r}"}
        note = ""
        if isinstance(answers, dict) and "_note" in answers:
            answers = dict(answers)
            note = str(answers.pop("_note") or "")
        with self._lock:
            if ask_id:
                p = self._by_ask.get(ask_id)
                if p is None:
                    return self._settled(self.store.ask(ask_id) if self.store else None, ask_id)
            else:
                p = next((x for x in self._by_ask.values() if x.run_id == run_id and x.state == "pending"),
                         None) if run_id else None
                if p is None and not run_id:              # "approve" with no id: only if one is open
                    mine = [x for x in self._by_ask.values() if x.state == "pending"
                            and (not chat_id or x.chat_id == chat_id)]
                    p = mine[0] if len(mine) == 1 else None
                    if len(mine) > 1:
                        ids = ", ".join(x.run_id for x in mine)
                        return {"ok": False, "error": f"{len(mine)} requests are pending ({ids}); name the run id"}
                if p is None and run_id and self.store:    # a repeat or late decide by run id
                    last = self.store.ask_latest_for_run(run_id)
                    if last and last["state"] != "pending":
                        return self._settled(last, last["ask_id"])
            if p is None:
                return {"ok": False, "error": "nothing is pending" + (f" for run {run_id}" if run_id else "")
                        + "; nothing was approved or denied"}
            if p.state != "pending":
                return self._settled({"state": p.state}, p.ask_id)
            if chat_id and p.chat_id != chat_id:
                return {"ok": False, "error": f"run {p.run_id} belongs to another chat"}
            d = {"decision": decision, "answers": answers or {}, "via": via}
            if note:
                d["note"] = note
            self._close(p, "answered", d)
            return {"ok": True, "ask_id": p.ask_id, "run_id": p.run_id, "tool": p.tool, "decision": decision}

    @staticmethod
    def _settled(row, ask_id):
        st = row["state"] if row else ""
        if st == "answered":
            return dict(ALREADY)
        if st == "expired":
            return {"ok": False, "state": "expired", "error": "expired: the bridge restarted and the Claude "
                    "process that asked is gone; nothing was applied. Ask the agent again."}
        if st in ("timeout", "cancelled"):
            why = "timed out and was denied" if st == "timeout" else "was cancelled when the run stopped"
            return {"ok": False, "state": st, "error": f"this ask {why}; nothing was applied"}
        return {"ok": False, "error": f"unknown ask {ask_id}; nothing was applied"}

    def answer_text(self, text: str, chat_id: str = "", via: str = "api") -> dict:
        """Apply a chat-form `answer <ask_id> <json>` message."""
        a = parse_answer(text)
        if not a["ok"]:
            return a
        with self._lock:
            p = self._by_ask.get(a["ask_id"])
        if p is not None and chat_id and p.chat_id != chat_id:
            return {"ok": False, "error": f"ask {a['ask_id']} belongs to another chat"}
        if p is not None and p.kind == "permission" and a["answers"]:
            return {"ok": False, "error": "this ask is an approval, not a question; answer with a decision"}
        ans = dict(a["answers"])
        if a["note"]:
            ans["_note"] = a["note"]
        return self.decide("", a["decision"], ans, chat_id, ask_id=a["ask_id"], via=via)

    def cancel_run(self, run_id: str):
        with self._lock:
            for p in [x for x in self._by_ask.values() if x.run_id == run_id]:
                self._by_ask.pop(p.ask_id, None)
                if p.state == "pending":
                    self._close(p, "cancelled", {"decision": "deny", "note": "run stopped"})


def _row(p) -> dict:
    import events
    return {"ask_id": p.ask_id, "run_id": p.run_id, "chat_id": p.chat_id, "kind": p.kind, "tool": p.tool,
            "summary": events.summarize(p.tool, p.input), "reason": p.reason, "created": p.created,
            "state": p.state, "decision": p.decision,
            "questions": p.input.get("questions") if p.kind == "question" else None}


def _public(r: dict) -> dict:
    out = {k: r[k] for k in ("ask_id", "run_id", "chat_id", "kind", "tool", "summary", "reason", "created",
                              "state")}
    if r["kind"] == "question":
        out["questions"] = r.get("questions") or []
    d = r.get("decision")
    if d and r["state"] == "answered":
        out["decision"] = d.get("decision")
        out["via"] = d.get("via") or ""
        if d.get("answers"):
            out["answers"] = d["answers"]
    return out
