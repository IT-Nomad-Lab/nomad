"""S4 — the outbox has to say whether a send actually left.

It used to return ok=True whatever happened and stamp `delivered_at` either way, so a refused
webhook and a delivered email were indistinguishable in the audit table and the run still read as
`executed`. Same shape as the n8n gate reporting success for an action that never ran, which is a
mistake this project has already paid for once.

Runs in the container, because `mcp_comms` imports `mcp`:

    docker compose exec nomad-v2-engine sh -c 'python -m pytest test_outbox_status.py -q'
"""
import json
import urllib.error
import urllib.request

import pytest

import mcp_comms


class FakeDB:
    """Just the two methods `send_message_impl` reaches for."""

    def __init__(self, approved=True, reject_columns=()):
        self.rows = []
        self.approved = approved
        self.reject_columns = set(reject_columns)   # simulate a base predating the migration
        self._id = 0

    def find(self, table, col, val):
        assert (table, col) == ("comms", "run_id")
        return {"status": "executing"} if self.approved else None

    def create(self, table, fields):
        assert table == "outbox"
        bad = self.reject_columns & set(fields)
        if bad:
            raise RuntimeError(f"unknown column(s): {sorted(bad)}")
        self._id += 1
        row = {**fields, "Id": self._id}
        self.rows.append(row)
        return row


@pytest.fixture
def db(monkeypatch):
    d = FakeDB()
    monkeypatch.setattr(mcp_comms, "_db", d)
    monkeypatch.setattr(mcp_comms, "OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setattr(mcp_comms, "N8N_SEND_URL", "http://n8n.invalid/webhook/v2-send")
    return d


def _wire(monkeypatch, ok, body=b"", raises=None, seen=None):
    """Point the module's urlopen at a webhook that answers, refuses, or hangs.

    `body` is what the webhook replies with, which is the whole difference S6 turns on: an
    `onReceived` webhook acknowledges with nothing useful, a `lastNode` one hands back the Gmail
    node's output. `raises` covers the case the fix newly made possible, a timeout after the send.
    """
    def fake(req, timeout=None):
        if seen is not None:
            seen.append(timeout)
        if raises is not None:
            raise raises
        if not ok:
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

        class R:
            def read(self):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return R()
    monkeypatch.setattr(mcp_comms.urllib.request, "urlopen", fake)


GMAIL_OK = json.dumps({"id": "18f2c0abcd", "threadId": "18f2c0abcd",
                       "labelIds": ["SENT"]}).encode()


def test_a_reply_with_no_message_id_is_only_accepted(db, monkeypatch):
    """A 200 carrying nothing is what an `onReceived` webhook sends: n8n took the request, and
    that is all. It must not be promoted to `sent` because the workflow was meant to be upgraded."""
    _wire(monkeypatch, ok=True)
    r = mcp_comms.send_message_impl("a@b.c", "body", "run1", "subj")
    assert r["ok"] is True and r["sent"] is True
    row = db.rows[0]
    assert row["status"] == mcp_comms.ACCEPTED
    assert row["delivered_at"]
    assert row["error"] == ""


def test_a_failed_send_is_not_stamped_delivered(db, monkeypatch):
    """A timestamp is a claim that something happened. Nothing happened."""
    _wire(monkeypatch, ok=False)
    r = mcp_comms.send_message_impl("a@b.c", "body", "run1", "subj")
    row = db.rows[0]
    assert row["status"] == mcp_comms.FAILED
    assert row["delivered_at"] is None
    assert "403" in row["error"]


def test_a_failed_send_fails_the_run(db, monkeypatch):
    """ok=False is what makes the engine mark the run `failed` instead of `executed`, which is the
    difference between an operator who knows the mail did not go and one who does not."""
    _wire(monkeypatch, ok=False)
    r = mcp_comms.send_message_impl("a@b.c", "body", "run1", "subj")
    assert r["ok"] is False
    assert r["sent"] is False
    assert r["status"] == mcp_comms.FAILED
    assert "403" in r["error"]


def test_the_attempt_is_still_audited_when_it_fails(db, monkeypatch):
    """A failed attempt is exactly what an audit table is for. Dropping the row would make a send
    that was tried and refused look like one that was never requested."""
    _wire(monkeypatch, ok=False)
    r = mcp_comms.send_message_impl("a@b.c", "body", "run1", "subj")
    assert len(db.rows) == 1
    assert db.rows[0]["Id"] == r["outbox_id"]
    assert db.rows[0]["run_id"] == "run1"


def test_an_un_upgraded_webhook_cannot_produce_a_sent(db, monkeypatch):
    """The two deploys are independent: the engine ships before n8n is restarted onto the new
    workflow. During that window the reply is still a bare acknowledgement, and claiming `sent`
    off it would be exactly the lie this column exists to stop."""
    assert mcp_comms.ACCEPTED == "accepted"
    _wire(monkeypatch, ok=True)
    mcp_comms.send_message_impl("a@b.c", "body", "run1", "subj")
    assert db.rows[0]["status"] != mcp_comms.SENT
    assert db.rows[0]["provider_id"] == ""


def test_an_unapproved_run_still_refuses_and_writes_nothing(db, monkeypatch):
    """The gate interlock comes first; S4 must not have widened it."""
    db.approved = False
    _wire(monkeypatch, ok=True)
    r = mcp_comms.send_message_impl("a@b.c", "body", "run1", "subj")
    assert r["ok"] is False and r["gated"] is True
    assert db.rows == []


def test_an_older_base_without_the_columns_still_records_the_attempt(db, monkeypatch):
    """A missing column must not turn a failed send into a silent one. The row is retried without
    the new fields rather than lost, and the return still carries the outcome."""
    db.reject_columns = {"status", "error"}
    _wire(monkeypatch, ok=False)
    r = mcp_comms.send_message_impl("a@b.c", "body", "run1", "subj")
    assert len(db.rows) == 1
    assert not {"status", "error", "provider_id"} & set(db.rows[0])
    assert r["ok"] is False and r["status"] == mcp_comms.FAILED and "403" in r["error"]


def test_no_address_fails_rather_than_writing_a_delivered_row(db, monkeypatch):
    monkeypatch.setattr(mcp_comms, "OPERATOR_EMAIL", "")
    _wire(monkeypatch, ok=True)
    r = mcp_comms.send_message_impl("not-an-email", "body", "run1", "subj")
    assert r["ok"] is False
    assert db.rows[0]["status"] == mcp_comms.FAILED
    assert db.rows[0]["delivered_at"] is None
    assert db.rows[0]["error"] == "no address"


# ---------------------------------------------------------------- S6: `sent` is now reachable


def test_gmails_own_id_is_what_earns_a_sent(db, monkeypatch):
    """`lastNode` makes the webhook answer with the Gmail node's output. The id in it is the one
    piece of evidence that comes from Gmail rather than from us, so it is what `sent` rests on."""
    _wire(monkeypatch, ok=True, body=GMAIL_OK)
    r = mcp_comms.send_message_impl("a@b.c", "body", "run1", "subj")
    assert r["ok"] is True and r["status"] == mcp_comms.SENT
    assert r["provider_id"] == "18f2c0abcd"
    assert db.rows[0]["status"] == "sent"
    assert db.rows[0]["provider_id"] == "18f2c0abcd"
    assert db.rows[0]["delivered_at"]


def test_the_id_is_kept_so_the_claim_can_be_checked_later(db, monkeypatch):
    """`sent` is this system's word for it. The id is the receipt someone can take to the mailbox,
    which is the only reason the column is worth having."""
    _wire(monkeypatch, ok=True, body=GMAIL_OK)
    mcp_comms.send_message_impl("a@b.c", "body", "run1", "subj")
    assert db.rows[0]["provider_id"] == "18f2c0abcd"


def test_a_reply_that_is_not_json_does_not_become_a_sent(db, monkeypatch):
    """Anything unparseable is no evidence at all, and no evidence degrades to `accepted`."""
    _wire(monkeypatch, ok=True, body=b"<html>Workflow was started</html>")
    r = mcp_comms.send_message_impl("a@b.c", "body", "run1", "subj")
    assert r["status"] == mcp_comms.ACCEPTED
    assert db.rows[0]["provider_id"] == ""


def test_a_reply_shaped_as_a_list_is_still_read(db, monkeypatch):
    """n8n hands back a list of items when more than one is emitted. The id is still Gmail's."""
    _wire(monkeypatch, ok=True, body=b'[{"id": "abc123", "labelIds": ["SENT"]}]')
    r = mcp_comms.send_message_impl("a@b.c", "body", "run1", "subj")
    assert r["status"] == mcp_comms.SENT and r["provider_id"] == "abc123"


def test_an_empty_list_is_not_a_send(db, monkeypatch):
    _wire(monkeypatch, ok=True, body=b"[]")
    assert mcp_comms.send_message_impl("a@b.c", "b", "run1", "s")["status"] == mcp_comms.ACCEPTED


def test_a_reply_with_an_empty_id_is_not_a_send(db, monkeypatch):
    _wire(monkeypatch, ok=True, body=b'{"id": ""}')
    assert mcp_comms.send_message_impl("a@b.c", "b", "run1", "s")["status"] == mcp_comms.ACCEPTED


def test_a_timeout_says_unknown_rather_than_failed(db, monkeypatch):
    """The fix created this case. Waiting for Gmail means the wait can expire after the mail left,
    and `failed` would be a lie in the other direction that gets the operator to send it twice."""
    _wire(monkeypatch, ok=True, raises=TimeoutError("timed out"))
    r = mcp_comms.send_message_impl("a@b.c", "body", "run1", "subj")
    assert r["status"] == mcp_comms.UNKNOWN
    assert db.rows[0]["status"] == "unknown"


def test_an_unknown_send_is_not_stamped_delivered(db, monkeypatch):
    """A timestamp is a claim. We do not have one to make."""
    _wire(monkeypatch, ok=True, raises=TimeoutError("timed out"))
    mcp_comms.send_message_impl("a@b.c", "body", "run1", "subj")
    assert db.rows[0]["delivered_at"] is None


def test_an_unknown_send_still_fails_the_run(db, monkeypatch):
    """Between a run wrongly at `failed` and one wrongly at `executed`, only the first is
    recoverable, and recovering it costs a fresh approval so nothing re-sends unwatched."""
    _wire(monkeypatch, ok=True, raises=TimeoutError("timed out"))
    r = mcp_comms.send_message_impl("a@b.c", "body", "run1", "subj")
    assert r["ok"] is False and r["sent"] is False


def test_the_unknown_error_tells_the_operator_not_to_just_retry(db, monkeypatch):
    """This text is the only warning anyone gets before they approve a duplicate."""
    _wire(monkeypatch, ok=True, raises=TimeoutError("timed out"))
    r = mcp_comms.send_message_impl("a@b.c", "body", "run1", "subj")
    assert "may have gone out" in r["error"] and "before approving a retry" in r["error"]


def test_a_connect_timeout_counts_as_a_timeout_too(db, monkeypatch):
    """A read timeout arrives bare; a connect timeout arrives wrapped in URLError."""
    _wire(monkeypatch, ok=True, raises=urllib.error.URLError(TimeoutError("timed out")))
    assert mcp_comms.send_message_impl("a@b.c", "b", "run1", "s")["status"] == mcp_comms.UNKNOWN


def test_a_refusal_is_still_a_plain_failure(db, monkeypatch):
    """A 403 is n8n answering. There is no ambiguity to be generous about."""
    _wire(monkeypatch, ok=False)
    r = mcp_comms.send_message_impl("a@b.c", "body", "run1", "subj")
    assert r["status"] == mcp_comms.FAILED and "403" in r["error"]


def test_the_send_waits_longer_than_it_used_to(db, monkeypatch):
    """The webhook holds the connection open across Gmail now, so the old 20s was the budget for
    n8n alone and would turn ordinary sends into `unknown`."""
    seen = []
    _wire(monkeypatch, ok=True, body=GMAIL_OK, seen=seen)
    mcp_comms.send_message_impl("a@b.c", "body", "run1", "subj")
    assert seen == [mcp_comms.SEND_TIMEOUT]
    assert mcp_comms.SEND_TIMEOUT >= 45


def test_an_unapproved_run_refuses_before_any_of_this(db, monkeypatch):
    """S6 widened what a send can report. It must not have widened who can ask for one."""
    db.approved = False
    _wire(monkeypatch, ok=True, body=GMAIL_OK)
    r = mcp_comms.send_message_impl("a@b.c", "body", "run1", "subj")
    assert r["ok"] is False and r["gated"] is True and db.rows == []
