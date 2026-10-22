"""Tests for autopilot alerts (PRD 5.7, R8, AC3): one alert per event on each channel, deduplicated.

Every transport is a fake: nothing here reaches Zulip or n8n.
Run: uv run --no-project --with pytest --with pyyaml --with requests python -m pytest autopilot -q
"""
import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import alerts as al  # noqa: E402
import state as st  # noqa: E402


class FakeChannel(al.Channel):
    def __init__(self, name, fail=0):
        self.name = name
        self.sent = []
        self.fail = fail          # fail this many sends, then succeed

    def send(self, project, title, body):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("down")
        self.sent.append((project, title, body))


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {"result": "success", "id": 1}

    def json(self):
        return self._body


class FakeHTTP:
    def __init__(self, resp=None):
        self.calls = []
        self.resp = resp or FakeResp()

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return self.resp


@pytest.fixture
def ledger(tmp_path):
    return al.Ledger(str(tmp_path / "alerts.json"))


@pytest.fixture
def chans():
    return [FakeChannel("cockpit"), FakeChannel("zulip"), FakeChannel("email")]


@pytest.fixture
def alerter(chans, ledger):
    return al.Alerter(chans, ledger)


def counts(chans):
    return [len(c.sent) for c in chans]


# ---- which events alert --------------------------------------------------------------------

def test_needs_operator_and_failed_alert_on_every_channel(alerter, chans):
    assert alerter.task_outcome("rm", 412, "needs_operator", "reserved decision") == \
        {"cockpit": "sent", "zulip": "sent", "email": "sent"}
    alerter.task_outcome("rm", 413, "failed", "tests red")
    assert counts(chans) == [2, 2, 2]
    assert chans[1].sent[0] == ("rm", "task #412 needs operator", "reserved decision")


def test_merge_and_resume_outcomes_do_not_alert(alerter, chans):
    assert alerter.task_outcome("rm", 1, "merge") == {}
    assert alerter.task_outcome("rm", 2, "resume") == {}
    assert counts(chans) == [0, 0, 0]


@pytest.mark.parametrize("state", ["needs_you", "budget_paused", "error"])
def test_alerting_project_states(alerter, chans, state):
    alerter.project_state("rm", state, "why")
    assert counts(chans) == [1, 1, 1]


@pytest.mark.parametrize("state", ["off", "running", "waiting", "idle"])
def test_quiet_project_states(alerter, chans, state):
    assert alerter.project_state("rm", state) == {}
    assert counts(chans) == [0, 0, 0]


def test_feature_completion_alerts(alerter, chans):
    alerter.feature_completed("rm", 800, "Match-3 core")
    assert chans[2].sent == [("rm", "Feature #800 complete", "Match-3 core")]


# ---- dedup -------------------------------------------------------------------------------------

def test_the_same_condition_every_pass_alerts_once(alerter, chans):
    for _ in range(5):
        alerter.project_state("rm", "error", "cannot reach nomad-pm")
    assert counts(chans) == [1, 1, 1]


def test_a_changing_detail_in_the_same_state_is_still_one_alert(alerter, chans):
    alerter.project_state("rm", "error", "timeout")
    alerter.project_state("rm", "error", "connection refused")
    assert counts(chans) == [1, 1, 1]


def test_leaving_and_re_entering_a_state_is_a_new_event(alerter, chans):
    alerter.project_state("rm", "needs_you")
    alerter.project_state("rm", "running", "1 worker(s)")
    alerter.project_state("rm", "needs_you")
    assert counts(chans) == [2, 2, 2]


def test_switching_between_alerting_states_alerts_each(alerter, chans):
    alerter.project_state("rm", "budget_paused")
    alerter.project_state("rm", "error")
    alerter.project_state("rm", "budget_paused")     # left and came back
    assert counts(chans) == [3, 3, 3]


def test_the_same_task_outcome_reported_twice_alerts_once(alerter, chans):
    alerter.task_outcome("rm", 412, "failed")
    alerter.task_outcome("rm", 412, "failed")
    assert counts(chans) == [1, 1, 1]


def test_a_task_restarted_and_failing_again_alerts_again(alerter, chans):
    alerter.task_outcome("rm", 412, "failed")
    alerter.task_started("rm", 412)
    alerter.task_outcome("rm", 412, "failed")
    assert counts(chans) == [2, 2, 2]


def test_restarting_one_task_does_not_rearm_another(alerter, chans):
    alerter.task_outcome("rm", 41, "failed")
    alerter.task_outcome("rm", 412, "failed")
    alerter.task_started("rm", 41)
    alerter.task_outcome("rm", 412, "failed")
    assert counts(chans) == [2, 2, 2]


def test_projects_do_not_share_keys(alerter, chans):
    alerter.project_state("rm", "error")
    alerter.project_state("nomad", "error")
    assert counts(chans) == [2, 2, 2]


def test_a_feature_alerts_once_ever(alerter, chans):
    for _ in range(3):
        alerter.feature_completed("rm", 800)
    assert counts(chans) == [1, 1, 1]


def test_dedup_survives_a_restart(chans, ledger, tmp_path):
    al.Alerter(chans, ledger).project_state("rm", "error")
    fresh = [FakeChannel(c.name) for c in chans]
    al.Alerter(fresh, al.Ledger(ledger.path)).project_state("rm", "error")
    assert counts(fresh) == [0, 0, 0]


def test_digest_style_notify_sends_once_per_key(alerter, chans):
    """The trial's double digest: #879 sends with a per-day key, so a second call is a no-op."""
    assert alerter.notify("rm", "digest:rm:2026-09-28", "digest", "3 done")["zulip"] == "sent"
    assert alerter.notify("rm", "digest:rm:2026-09-28", "digest", "3 done")["zulip"] == "dup"
    alerter.notify("rm", "digest:rm:2026-09-29", "digest", "1 done")
    assert counts(chans) == [2, 2, 2]


def test_two_alerters_on_one_ledger_do_not_double_send(ledger):
    """Two supervisor processes (or threads) racing on the same condition: one send in total."""
    shared = FakeChannel("zulip")
    barrier = threading.Barrier(8)

    def go():
        a = al.Alerter([shared], al.Ledger(ledger.path))
        barrier.wait()
        a.project_state("rm", "error")
    ts = [threading.Thread(target=go) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(shared.sent) == 1


# ---- failures ----------------------------------------------------------------------------------

def test_a_failed_channel_is_retried_without_resending_the_others(ledger):
    cockpit, zulip, email = FakeChannel("cockpit"), FakeChannel("zulip", fail=1), FakeChannel("email")
    a = al.Alerter([cockpit, zulip, email], ledger)
    assert a.project_state("rm", "error") == {"cockpit": "sent", "zulip": "failed", "email": "sent"}
    assert a.project_state("rm", "error") == {"cockpit": "dup", "zulip": "sent", "email": "dup"}
    assert counts([cockpit, zulip, email]) == [1, 1, 1]


def test_a_channel_that_keeps_failing_gives_up(ledger):
    bad = FakeChannel("email", fail=100)
    a = al.Alerter([bad], ledger, max_tries=3)
    results = [a.project_state("rm", "error")["email"] for _ in range(5)]
    assert results == ["failed", "failed", "failed", "gave_up", "gave_up"]


def test_a_broken_ledger_does_not_raise(tmp_path, chans):
    p = tmp_path / "alerts.json"
    p.write_text("{not json")
    a = al.Alerter(chans, al.Ledger(str(p)))
    a.project_state("rm", "error")
    assert counts(chans) == [1, 1, 1]
    assert json.loads(p.read_text())["state:rm:error"]["sent"].keys() == {"cockpit", "zulip", "email"}


def test_a_ledger_that_cannot_be_written_does_not_raise(tmp_path, chans):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    a = al.Alerter(chans, al.Ledger(str(blocker / "sub" / "alerts.json")))
    assert a.project_state("rm", "error") == {}      # logged, not raised into the pass


# ---- on_pass (the supervisor hook) ---------------------------------------------------------------

def test_on_pass_covers_finished_started_and_state(alerter, chans):
    alerter.on_pass("rm", finished=[{"task": 1, "outcome": "failed", "detail": "red"},
                                    {"task": 2, "outcome": "merge"}],
                    started=[], state="running", detail="1 worker(s)")
    assert [t for _, t, _ in chans[0].sent] == ["task #1 failed"]
    alerter.on_pass("rm", started=[{"task": 1}], state="running")
    alerter.on_pass("rm", finished=[{"task": 1, "outcome": "failed"}], state="needs_you", detail="#1")
    assert [t for _, t, _ in chans[0].sent] == ["task #1 failed", "task #1 failed",
                                                "project rm is needs you"]


# ---- the real channels, with fake transports -----------------------------------------------------

def test_cockpit_channel_writes_an_alert_event(tmp_path):
    store = st.StateStore(str(tmp_path / "state.json"))
    a = al.Alerter([al.CockpitChannel(store)], al.Ledger(str(tmp_path / "alerts.json")))
    a.task_outcome("rm", 412, "needs_operator")
    a.task_outcome("rm", 412, "needs_operator")
    ev = [e for e in store.project("rm")["events"] if e["kind"] == "alert"]
    assert [e["msg"] for e in ev] == ["task #412 needs operator"]


def test_zulip_channel_posts_to_the_stream_with_the_project_as_topic(tmp_path):
    rc = tmp_path / "zuliprc"
    rc.write_text("[api]\nemail=bot@z.test\nkey=k123\nsite=https://z.test\n")
    http = FakeHTTP()
    ch = al.ZulipChannel.from_zuliprc(str(rc), "autopilot", http=http)
    ch.send("runematch", "task #1 failed", "tests red")
    url, kw = http.calls[0]
    assert url == "https://z.test/api/v1/messages"
    assert kw["auth"] == ("bot@z.test", "k123")
    assert kw["data"]["to"] == "runematch" and kw["data"]["topic"] == "autopilot"   # #1123
    assert kw["data"]["type"] == "stream" and "task #1 failed" in kw["data"]["content"]


def test_zulip_channel_raises_on_an_api_error(tmp_path):
    ch = al.ZulipChannel("https://z.test", "b", "k",
                         http=FakeHTTP(FakeResp(400, {"result": "error", "msg": "Stream does not exist"})))
    with pytest.raises(RuntimeError, match="Stream does not exist"):
        ch.send("rm", "t", "")


def test_zulip_channel_uses_the_board_and_falls_back_to_autopilot():
    class Seq(FakeHTTP):
        def __init__(self, resps):
            super().__init__()
            self.resps = resps

        def post(self, url, **kw):
            self.calls.append((url, kw))
            return self.resps.pop(0)
    http = Seq([FakeResp(400, {"result": "error", "msg": "Channel 'civis' does not exist"}), FakeResp()])
    al.ZulipChannel("https://z.test", "b", "k", http=http).send("civis-poc", "t", "")
    assert [(c[1]["data"]["to"], c[1]["data"]["topic"]) for c in http.calls] == \
        [("civis", "autopilot"), ("autopilot", "civis-poc")]
    http = FakeHTTP()
    al.ZulipChannel("https://z.test", "b", "k", http=http).send("nomad-public", "t", "")   # no board
    assert [(c[1]["data"]["to"], c[1]["data"]["topic"]) for c in http.calls] == [("autopilot", "nomad-public")]
    assert al.project_stream("nomad") == "nomad-work" and al.project_stream("Bad!") is None


def test_email_channel_posts_to_n8n_with_the_token_and_operator_address():
    http = FakeHTTP()
    ch = al.EmailChannel("http://n8n.invalid/webhook/v2-send", "tok", "op@x.test", http=http)
    ch.send("rm", "project rm is error", "cannot reach repo")
    url, kw = http.calls[0]
    assert url == "http://n8n.invalid/webhook/v2-send"
    assert kw["headers"]["X-Nomad-Token"] == "tok"
    body = json.loads(kw["data"])
    assert body["to"] == "op@x.test" and body["subject"] == "[autopilot] rm: project rm is error"
    assert "cannot reach repo" in body["body"]


def test_email_channel_raises_on_http_error():
    ch = al.EmailChannel("http://n8n.invalid/x", "tok", "op@x.test", http=FakeHTTP(FakeResp(403)))
    with pytest.raises(RuntimeError, match="403"):
        ch.send("rm", "t", "")


# ---- configuration -----------------------------------------------------------------------------

def _env(tmp_path, **kw):
    rc = tmp_path / "zuliprc"
    rc.write_text("[api]\nemail=bot@z.test\nkey=k\nsite=z.test\n")
    env = {"NOMAD_ZULIPRC": str(rc), "OPERATOR_EMAIL": "op@x.test", "N8N_WEBHOOK_TOKEN": "tok"}
    env.update(kw)
    return env


def test_all_three_channels_when_configured(tmp_path):
    store = st.StateStore(str(tmp_path / "state.json"))
    chs = al.build_channels(store, _env(tmp_path), http=FakeHTTP())
    assert [c.name for c in chs] == ["cockpit", "zulip", "email"]
    assert isinstance(chs[1], al.ZulipChannel) and chs[1].site == "https://z.test"
    assert isinstance(chs[2], al.EmailChannel)


def test_unconfigured_channels_are_skipped_with_a_warning(tmp_path, caplog):
    store = st.StateStore(str(tmp_path / "state.json"))
    env = {"NOMAD_ZULIPRC": str(tmp_path / "missing")}
    with caplog.at_level("WARNING", logger="autopilot.alerts"):
        chs = al.build_channels(store, env)
    assert [c.name for c in chs] == ["cockpit"]
    assert "Zulip channel skipped" in caplog.text and "email channel skipped" in caplog.text


def test_relay_token_name_is_accepted(tmp_path):
    env = _env(tmp_path, N8N_WEBHOOK_TOKEN="", NOMAD_N8N_WEBHOOK_TOKEN="tok2")
    chs = al.build_channels(None, env)
    assert [c.name for c in chs] == ["zulip", "email"] and chs[1].token == "tok2"


def test_channel_subset_and_dry_run(tmp_path):
    env = _env(tmp_path, NOMAD_AUTOPILOT_ALERT_CHANNELS="zulip,email",
               NOMAD_AUTOPILOT_ALERTS_DRY_RUN="1")
    chs = al.build_channels(None, env)
    assert [c.name for c in chs] == ["zulip", "email"]
    assert all(isinstance(c, al.DryRunChannel) for c in chs)
    chs[0].send("rm", "t", "b")       # logs only


def test_from_env_uses_the_given_ledger(tmp_path):
    http = FakeHTTP()
    a = al.from_env(None, ledger_path=str(tmp_path / "l.json"), env=_env(tmp_path), http=http)
    a.project_state("rm", "error", "x")
    a.project_state("rm", "error", "x")
    assert len(http.calls) == 2         # zulip + email, once each
    assert os.path.exists(tmp_path / "l.json")


def test_load_env_fills_unset_keys_from_a_dotenv(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("# c\nOPERATOR_EMAIL=op@x.test\nN8N_WEBHOOK_TOKEN='tok'\n")
    monkeypatch.delenv("OPERATOR_EMAIL", raising=False)
    monkeypatch.setenv("N8N_WEBHOOK_TOKEN", "from-env")
    env = al.load_env(str(f))
    assert env["OPERATOR_EMAIL"] == "op@x.test" and env["N8N_WEBHOOK_TOKEN"] == "from-env"
