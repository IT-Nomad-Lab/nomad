"""SQLite state for the agent bridge: which Claude Code session a chat is on, and what each run did.

chat_id → session_id + cwd is what makes the next turn `--resume` the same conversation.
Runs are an audit trail. Asks (permission requests and questions) are persisted here so they stay
listed across a bridge restart, but the claude process that asked dies with the bridge: gates.py
marks every open ask `expired` at startup and a decision on it applies nothing.
"""
import json
import os
import sqlite3
import threading
import time

DB = os.environ.get("AGENT_BRIDGE_DB") or os.path.expanduser(
    "~/.local/state/nomad-agent-bridge/state.db")

_SCHEMA = """
create table if not exists chats(chat_id text primary key, session_id text not null, cwd text not null,
  project text, updated real);
create table if not exists backend_sessions(chat_id text, backend text, session_id text, cwd text,
  updated real, primary key(chat_id, backend));
create table if not exists runs(run_id text primary key, chat_id text, status text, started real,
  finished real, summary text, background integer default 0, seen integer default 0);
create table if not exists asks(ask_id text primary key, run_id text, chat_id text, kind text, tool text,
  summary text, reason text, questions text, created real, state text, decision text, answered real);
create index if not exists asks_chat on asks(chat_id, created);
"""


class Store:
    def __init__(self, path: str = ""):
        path = path or DB
        if path != ":memory:":
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)

    def _q(self, sql, args=()):
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur.fetchall()

    def chat(self, chat_id: str) -> dict | None:
        rows = self._q("select * from chats where chat_id=?", (chat_id,))
        return dict(rows[0]) if rows else None

    def bind(self, chat_id: str, session_id: str, cwd: str, project: str):
        self._q("insert into chats values(?,?,?,?,?) on conflict(chat_id) do update set "
                "session_id=excluded.session_id, cwd=excluded.cwd, project=excluded.project, "
                "updated=excluded.updated", (chat_id, session_id, cwd, project, time.time()))

    def reset(self, chat_id: str):
        self._q("delete from chats where chat_id=?", (chat_id,))
        self._q("delete from backend_sessions where chat_id=?", (chat_id,))

    def session(self, chat_id: str, backend: str) -> dict | None:
        rows = self._q("select * from backend_sessions where chat_id=? and backend=?", (chat_id, backend))
        return dict(rows[0]) if rows else None

    def set_session(self, chat_id: str, backend: str, session_id: str, cwd: str):
        self._q("insert into backend_sessions values(?,?,?,?,?) on conflict(chat_id, backend) do update "
                "set session_id=excluded.session_id, cwd=excluded.cwd, updated=excluded.updated",
                (chat_id, backend, session_id, cwd, time.time()))

    def run_start(self, run_id: str, chat_id: str, background: bool):
        self._q("insert into runs(run_id,chat_id,status,started,background) values(?,?,?,?,?)",
                (run_id, chat_id, "running", time.time(), int(background)))

    def run_end(self, run_id: str, status: str, summary: str):
        self._q("update runs set status=?, finished=?, summary=? where run_id=?",
                (status, time.time(), summary[:2000], run_id))

    def runs(self, chat_id: str, unseen_background: bool = False) -> list[dict]:
        sql = "select * from runs where chat_id=?"
        if unseen_background:
            sql += " and background=1 and seen=0 and status!='running'"
        rows = [dict(r) for r in self._q(sql + " order by started desc limit 20", (chat_id,))]
        if unseen_background and rows:
            self._q("update runs set seen=1 where chat_id=? and background=1", (chat_id,))
        return rows

    # ---- asks (gates.py is the only writer)
    def ask_add(self, a: dict):
        self._q("insert into asks(ask_id,run_id,chat_id,kind,tool,summary,reason,questions,created,state) "
                "values(?,?,?,?,?,?,?,?,?,?)",
                (a["ask_id"], a["run_id"], a["chat_id"], a["kind"], a["tool"], a["summary"], a["reason"],
                 json.dumps(a["questions"]) if a["questions"] is not None else None, a["created"], a["state"]))

    def ask_set(self, ask_id: str, state: str, decision: dict | None = None):
        self._q("update asks set state=?, decision=?, answered=? where ask_id=?",
                (state, json.dumps(decision) if decision is not None else None, time.time(), ask_id))

    @staticmethod
    def _ask(r) -> dict:
        d = dict(r)
        d["questions"] = json.loads(d["questions"]) if d.get("questions") else None
        d["decision"] = json.loads(d["decision"]) if d.get("decision") else None
        return d

    def ask(self, ask_id: str) -> dict | None:
        rows = self._q("select * from asks where ask_id=?", (ask_id,))
        return self._ask(rows[0]) if rows else None

    def asks(self, chat_id: str = "", limit: int = 50) -> list[dict]:
        sql, args = "select * from asks", ()
        if chat_id:
            sql, args = sql + " where chat_id=?", (chat_id,)
        rows = self._q(sql + " order by created desc limit ?", args + (limit,))
        return [self._ask(r) for r in reversed(rows)]          # oldest first

    def ask_latest_for_run(self, run_id: str) -> dict | None:
        rows = self._q("select * from asks where run_id=? order by created desc limit 1", (run_id,))
        return self._ask(rows[0]) if rows else None

    def expire_open_asks(self) -> int:
        with self._lock:
            cur = self._db.execute("update asks set state='expired', answered=? where state='pending'",
                                   (time.time(),))
            self._db.commit()
            return cur.rowcount
