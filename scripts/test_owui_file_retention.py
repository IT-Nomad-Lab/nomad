import importlib.util
import os

_spec = importlib.util.spec_from_file_location("ret", os.path.join(os.path.dirname(__file__), "owui-file-retention.py"))
ret = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ret)

NOW = 1_800_000_000
DAY = 86400


def f(fid, age_days, marked, name="a.png", size=10):
    data = {ret.MARKER: True} if marked else {}
    return {"id": fid, "filename": name, "user_id": "u1", "created_at": NOW - age_days * DAY,
            "meta": {"name": name, "size": size, "data": data}}


class FakeApi:
    def __init__(self, files):
        self._files, self.deleted = files, []

    def files(self):
        return self._files

    def delete(self, fid):
        self.deleted.append(fid)


FILES = [f("old-pipe", 40, True), f("new-pipe", 5, True), f("old-own", 90, False, "report.pdf"),
         f("new-own", 1, False), f("edge", 30.5, True)]


def test_selects_only_marked_files_past_the_cutoff():
    cand, unmarked = ret.select(FILES, 30, NOW)
    assert {c["id"] for c in cand} == {"old-pipe", "edge"}
    assert [u["id"] for u in unmarked] == ["old-own"]


def test_operator_uploads_are_never_selected_whatever_the_age():
    assert ret.select([f("x", 3650, False)], 0, NOW)[0] == []


def test_a_marker_that_is_not_true_does_not_count():
    g = f("x", 99, True)
    g["meta"]["data"][ret.MARKER] = "yes"
    assert ret.select([g], 30, NOW)[0] == []
    assert ret.select([{"id": "n", "created_at": 0, "meta": None}], 30, NOW)[0] == []


def test_cutoff_is_configurable():
    assert {c["id"] for c in ret.select(FILES, 3, NOW)[0]} == {"old-pipe", "new-pipe", "edge"}


def test_also_adds_a_named_unmarked_file():
    cand, unmarked = ret.select(FILES, 30, NOW, also=["old-own"])
    assert "old-own" in {c["id"] for c in cand} and unmarked == []


def test_env_sets_the_default_days(monkeypatch):
    monkeypatch.delenv("OWUI_FILE_RETENTION_DAYS", raising=False)
    assert ret.keep_days() == 30
    monkeypatch.setenv("OWUI_FILE_RETENTION_DAYS", "7")
    assert ret.keep_days() == 7


def run(argv, files=FILES, have=None):
    api = FakeApi(list(files))
    rc = ret.main(argv, api=api, now=NOW, have=have or ret.pinned_version())
    return rc, api


def test_default_is_a_dry_run_that_deletes_nothing(capsys, monkeypatch):
    monkeypatch.delenv("OWUI_FILE_RETENTION_DAYS", raising=False)
    rc, api = run([])
    out = capsys.readouterr().out
    assert rc == 0 and api.deleted == []
    assert "dry run" in out and "old-pipe" in out and "total 2 file(s)" in out
    assert "owner=u1" in out and "age=40.0d" in out


def test_apply_deletes_exactly_the_candidates(monkeypatch):
    monkeypatch.delenv("OWUI_FILE_RETENTION_DAYS", raising=False)
    rc, api = run(["--apply"])
    assert rc == 0 and sorted(api.deleted) == ["edge", "old-pipe"]


def test_apply_never_touches_operator_files():
    _, api = run(["--apply", "--days", "0"])
    assert not {"old-own", "new-own"} & set(api.deleted)


def test_refuses_another_open_webui_version():
    rc, api = run(["--apply"], have="0.99.0")
    assert rc == 2 and api.deleted == []


def test_also_with_an_unknown_id_is_refused():
    rc, api = run(["--apply", "--also", "nope"])
    assert rc == 2 and api.deleted == []


def test_pinned_version_is_read_from_the_pipe():
    assert ret.pinned_version() == "0.12.3"
