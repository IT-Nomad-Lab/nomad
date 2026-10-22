"""Tests for autopilot/projects.py: the projects.yaml `autopilot:` schema and loader.

Run: uv run --no-project --with pytest --with pyyaml python -m pytest autopilot -q
"""
import os
import sys
import textwrap

import pytest

NOMAD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, NOMAD)

from autopilot import projects  # noqa: E402
from autopilot.projects import Command, ConfigError  # noqa: E402

REAL = os.path.join(NOMAD, "projects.yaml")


def _registry(tmp_path, body):
    """A throwaway registry at <tmp>/nomad/projects.yaml, so paths resolve under <tmp>."""
    d = tmp_path / "nomad"
    d.mkdir(exist_ok=True)
    f = d / "projects.yaml"
    f.write_text(textwrap.dedent(body))
    return str(f)


def _one(tmp_path, block, pid="p"):
    f = _registry(tmp_path, f"projects:\n  - id: {pid}\n    path: {pid}\n    autopilot:\n"
                  + textwrap.indent(textwrap.dedent(block), "      "))
    return projects.load(f)[0]


# ---- projects without a block still load ----------------------------------------------------

def test_entries_without_autopilot_load_unchanged(tmp_path):
    f = _registry(tmp_path, """\
        projects:
          - {id: a, path: a, aliases: [Old-A]}
          - {id: b, path: grp/b, group: grp, dispatch: false}
          - {path: nameless}
        """)
    a, b = projects.load(f)
    assert (a.id, a.path, a.aliases, a.dispatch, a.autopilot) == \
        ("a", str(tmp_path / "a"), ("old-a",), True, None)
    assert (b.path, b.group, b.dispatch) == (str(tmp_path / "grp" / "b"), "grp", False)


# Projects the autopilot cannot run (#916): not a git repo (no task worktrees), or no board.
NO_BLOCK = {"nomad-shell", "civis-site", "itnomadlab-site", "nomad-public"}


def test_the_real_registry_loads_and_every_runnable_project_has_a_block():
    ps = projects.load(REAL)
    assert len(ps) > 10
    assert {p.id for p in ps if not p.autopilot} == NO_BLOCK
    for p in ps:
        if p.autopilot:
            assert p.autopilot.pre_merge_gate, p.id     # never a block that merges unchecked


def test_duplicate_ids_are_an_error(tmp_path):
    f = _registry(tmp_path, "projects:\n  - {id: a, path: a}\n  - {id: a, path: b}\n")
    with pytest.raises(ConfigError, match="'a'.*listed twice"):
        projects.load(f)


def test_missing_registry_is_reported(tmp_path):
    with pytest.raises(FileNotFoundError):
        projects.load(str(tmp_path / "nope.yaml"))


def test_registry_discovery_uses_the_dispatcher_rule(tmp_path, monkeypatch):
    f = _registry(tmp_path, "projects: []\n")
    monkeypatch.delenv("NOMAD_REGISTRY", raising=False)
    assert projects.registry_file(roots=[str(tmp_path / "x"), str(tmp_path)]) == f
    monkeypatch.setenv("NOMAD_REGISTRY", f)
    assert projects.registry_file(roots=[]) == f


# ---- defaults ---------------------------------------------------------------------------------

def test_an_empty_block_gets_every_default(tmp_path):
    ap = _one(tmp_path, "{}\n").autopilot
    assert ap.test == {} and ap.pre_merge_gate == () and ap.milestone_gate == ()
    assert ap.build is None and ap.post_test == () and ap.locks == ()
    assert ap.flake_set == frozenset() and ap.known_failures == frozenset()
    assert ap.cap == projects.DEFAULT_CAP == 3
    assert ap.working_dirs == () and ap.deny_edit_globs == () and ap.requires_gpu is False
    assert ap.gate_network == "none"
    assert (ap.results, ap.results_files) == ("exit", "") and ap.regenerated == ()
    assert ap.noise == () and ap.lanes == ()
    assert ap.model == "" and ap.max_budget_usd is None and ap.loop_tests == ()
    assert ap.worktree_root == os.path.realpath(tmp_path / "p") + "-wt"


def test_cost_fields_parse(tmp_path):
    ap = _one(tmp_path, """\
        test: {EditMode: a, PlayMode: b}
        model: opus
        max_budget_usd: 4.5
        loop_tests: [EditMode]
    """).autopilot
    assert (ap.model, ap.max_budget_usd, ap.loop_tests) == ("opus", 4.5, ("EditMode",))


def test_noise_rules_parse(tmp_path):
    ap = _one(tmp_path, """\
        noise:
          - {glob: "Assets/*.meta", rule: unity-default-platforms}
          - {glob: ProjectSettings/ProjectSettings.asset, rule: only-keys, keys: [productName]}
        """).autopilot
    assert ap.noise == (
        projects.Noise("Assets/*.meta", "unity-default-platforms"),
        projects.Noise("ProjectSettings/ProjectSettings.asset", "only-keys", ("productName",)))


def test_lanes_parse_lower_case_without_repeats(tmp_path):
    assert _one(tmp_path, "lanes: [Comms, research, comms]\n").autopilot.lanes == ("comms", "research")


def test_the_cap_default_comes_from_the_global_setting(tmp_path):
    f = _registry(tmp_path, "projects:\n  - {id: p, path: p, autopilot: {}}\n")
    assert projects.load(f, default_cap=5)[0].autopilot.cap == 5


def test_worktree_root_defaults_to_the_real_repo_path(tmp_path):
    real = tmp_path / "Real"
    real.mkdir()
    os.symlink(real, tmp_path / "p")
    p = _one(tmp_path, "{}\n")
    assert p.repo == str(real)
    assert p.autopilot.worktree_root == str(real) + "-wt"


def test_gates_default_to_every_suite_then_the_build(tmp_path):
    ap = _one(tmp_path, """\
        test: {unit: "pytest {filter}", e2e: npm run e2e}
        build: npm run build
        """).autopilot
    assert [c.run for c in ap.pre_merge_gate] == ["pytest {filter}", "npm run e2e"]
    assert [c.run for c in ap.milestone_gate] == ["pytest {filter}", "npm run e2e", "npm run build"]


def test_a_single_test_command_is_the_default_suite(tmp_path):
    ap = _one(tmp_path, "test: pytest -q {filter}\n").autopilot
    assert list(ap.test) == ["default"]
    assert ap.test["default"].render() == "pytest -q"
    assert ap.test["default"].render("tests/test_x.py") == "pytest -q tests/test_x.py"
    assert Command("make test").render("ignored") == "make test"


def test_results_format_short_and_long_form(tmp_path):
    assert _one(tmp_path, "results: nunit\n").autopilot.results == "nunit"
    ap = _one(tmp_path, "results: {format: nunit, files: Logs/test_*.xml}\n").autopilot
    assert (ap.results, ap.results_files) == ("nunit", "Logs/test_*.xml")


def test_relative_paths_resolve_against_the_repo(tmp_path):
    ap = _one(tmp_path, "working_dirs: [../shared, /abs/dir, ~/x]\nworktree_root: ../wt\n").autopilot
    repo = os.path.realpath(tmp_path / "p")
    assert ap.working_dirs == (os.path.normpath(os.path.join(repo, "../shared")), "/abs/dir",
                               os.path.expanduser("~/x"))
    assert ap.worktree_root == os.path.normpath(os.path.join(repo, "../wt"))


# ---- validation errors name the project and the field -----------------------------------------

@pytest.mark.parametrize("block, field", [
    ("tset: x\n", "autopilot.tset: unknown field"),
    ("cap: 0\n", "autopilot.cap"),
    ("cap: two\n", "autopilot.cap"),
    ("cap: true\n", "autopilot.cap"),
    ("requires_gpu: yes please\n", "autopilot.requires_gpu"),
    ("gate_network: off\n", "autopilot.gate_network"),
    ("flake_set: BLK6\n", "autopilot.flake_set"),
    ("flake_set: [BLK6, '']\n", r"autopilot.flake_set\[1\]"),
    ("test: 3\n", "autopilot.test"),
    ("test: {}\n", "autopilot.test"),
    ("test: {unit: {cmd: pytest}}\n", "autopilot.test.unit"),
    ("test: {unit: {locks: [/tmp/l]}}\n", "autopilot.test.unit: a command mapping needs `run`"),
    ("test: {unit: {run: x, env: [A]}}\n", "autopilot.test.unit.env"),
    ("test: {unit: {run: x, env: {A: [1]}}}\n", "autopilot.test.unit.env.A"),
    ("test: {unit: {run: x, locks: [rel.lock]}}\n", r"autopilot.test.unit.locks\[0\]"),
    ("test: {unit: {run: x, locks: [/tmp/l]}}\n", "autopilot.locks: /tmp/l is held"),
    ("locks: [rel.lock]\n", r"autopilot.locks\[0\]"),
    ("pre_merge_gate: make check\n", "autopilot.pre_merge_gate"),
    ("test: pytest\npre_merge_gate: ['test:unit']\n", r"autopilot.pre_merge_gate\[0\]: no test suite"),
    ("milestone_gate: [build]\n", r"autopilot.milestone_gate\[0\]: `build` step"),
    ("post_test: git checkout .\n", "autopilot.post_test"),
    ("flake_set: [X]\nknown_failures: [X]\n", "autopilot.known_failures"),
    ("worktree_root: ''\n", "autopilot.worktree_root"),
    ("deny_edit_globs: [1]\n", r"autopilot.deny_edit_globs\[0\]"),
    ("regenerated: [/abs/font.asset]\n", r"autopilot.regenerated\[0\]"),
    ("regenerated: [../other/font.asset]\n", r"autopilot.regenerated\[0\]"),
    ("regenerated: [.]\n", r"autopilot.regenerated\[0\]"),
    ("noise: {glob: x, rule: only-keys}\n", "autopilot.noise: expected a list"),
    ("noise: [x.meta]\n", r"autopilot.noise\[0\]: expected a mapping"),
    ("noise: [{glob: x, rule: whatever}]\n", r"autopilot.noise\[0\].rule"),
    ("noise: [{glob: x, rule: only-keys}]\n", r"autopilot.noise\[0\].keys"),
    ("noise: [{glob: x, rule: unity-default-platforms, keys: [a]}]\n", r"autopilot.noise\[0\].keys"),
    ("noise: [{glob: ../x, rule: unity-default-platforms}]\n", r"autopilot.noise\[0\].glob"),
    ("noise: [{rule: unity-default-platforms}]\n", r"autopilot.noise\[0\].glob"),
    ("noise: [{glob: x, rule: only-keys, keys: [a], path: y}]\n", r"autopilot.noise\[0\]: unknown key"),
    ("lanes: dev\n", "autopilot.lanes"),
    ("lanes: []\n", "autopilot.lanes: empty list"),
    ("lanes: [dev, '']\n", r"autopilot.lanes\[1\]"),
    ("results: junit\n", "autopilot.results.format"),
    ("results: [nunit]\n", "autopilot.results"),
    ("results: {format: nunit, glob: x}\n", "autopilot.results: unknown key"),
    ("results: {format: nunit, files: /abs/*.xml}\n", "autopilot.results.files"),
    ("results: {format: nunit, files: ../up/*.xml}\n", "autopilot.results.files"),
    ("model: claude-opus-5-5\n", "autopilot.model: expected one of"),
    ("max_budget_usd: 0\n", "autopilot.max_budget_usd"),
    ("max_budget_usd: lots\n", "autopilot.max_budget_usd"),
    ("max_budget_usd: true\n", "autopilot.max_budget_usd"),
    ("loop_tests: []\n", "autopilot.loop_tests: empty list"),
    ("test: {EditMode: x}\nloop_tests: [PlayMode]\n", r"autopilot.loop_tests\[0\]: no test suite"),
])
def test_invalid_blocks_name_project_and_field(tmp_path, block, field):
    with pytest.raises(ConfigError, match=f"project 'shop': {field}"):
        _one(tmp_path, block, pid="shop")


def test_a_non_mapping_block_is_an_error(tmp_path):
    f = _registry(tmp_path, "projects:\n  - {id: shop, path: shop, autopilot: [x]}\n")
    with pytest.raises(ConfigError, match="project 'shop': autopilot: expected a mapping"):
        projects.load(f)


# ---- the project type (#1369) ----------------------------------------------------------------

class _FakeProfiles:
    """Stands in for nomad_projects.profiles, so these tests need only the profile names."""
    class ProfileError(ValueError):
        pass

    def __init__(self, ids):
        self.ids = ids

    def load(self, type_id):
        if type_id not in self.ids:
            raise self.ProfileError(f"profile '{type_id}': no such profile")
        return type_id

    def load_all(self):
        return {i: i for i in self.ids}                  # like the real one: {id: profile}


@pytest.fixture
def fake_profiles(monkeypatch):
    mod = _FakeProfiles(["generic", "website"])
    pkg = type(sys)("nomad_projects")
    pkg.profiles = mod
    monkeypatch.setitem(sys.modules, "nomad_projects", pkg)
    monkeypatch.setitem(sys.modules, "nomad_projects.profiles", mod)
    return mod


def test_a_missing_type_is_generic(tmp_path):
    f = _registry(tmp_path, "projects:\n  - {id: a, path: a}\n  - {id: b, path: b, type: ~}\n")
    assert [p.type for p in projects.load(f)] == ["generic", "generic"]


def test_a_known_type_is_exposed_on_the_project(tmp_path, fake_profiles):
    f = _registry(tmp_path, "projects:\n  - {id: a, path: a, type: website}\n"
                            "  - {id: b, path: b, type: generic}\n")
    assert [p.type for p in projects.load(f)] == ["website", "generic"]
    assert projects.get("a", f).type == "website"


def test_an_unknown_type_names_the_project_and_field(tmp_path, fake_profiles):
    f = _registry(tmp_path, "projects:\n  - {id: a, path: a}\n  - {id: shop, path: s, type: ecommerce}\n")
    with pytest.raises(ConfigError, match=r"project 'shop': type: .*'ecommerce'.*known types: generic, website"):
        projects.load(f)


@pytest.mark.parametrize("bad", ["''", "7", "[website]"])
def test_a_non_string_type_is_an_error(tmp_path, bad):
    f = _registry(tmp_path, f"projects:\n  - {{id: shop, path: s, type: {bad}}}\n")
    with pytest.raises(ConfigError, match=r"project 'shop': type: expected a non-empty string"):
        projects.load(f)


def test_generic_never_needs_a_profile(tmp_path, monkeypatch):
    """A broken or missing profiles package must not break a project that is (or defaults to)
    generic."""
    monkeypatch.setitem(sys.modules, "nomad_projects", None)     # any import raises
    f = _registry(tmp_path, "projects:\n  - {id: a, path: a}\n  - {id: b, path: b, type: generic}\n")
    assert [p.type for p in projects.load(f)] == ["generic", "generic"]


def test_every_current_registry_entry_loads_as_generic_with_other_fields_unchanged():
    """Adding the field changes nothing for the entries listed today: none sets `type:`, all
    load as generic, and the rest of each entry is exactly what the file says."""
    import yaml
    with open(REAL, encoding="utf-8") as fh:
        raw = [e for e in yaml.safe_load(fh)["projects"] if e.get("id") and e.get("path")]
    assert all("type" not in e for e in raw)
    ps = projects.load(REAL)
    assert len(ps) == len(raw) > 10
    for p, e in zip(ps, raw):
        assert p.type == "generic", p.id
        assert (p.id, p.group, p.aliases, p.dispatch) == (
            str(e["id"]), str(e.get("group") or ""),
            tuple(str(a).lower() for a in e.get("aliases") or []), e.get("dispatch", True) is not False)
        assert p.path.endswith(str(e["path"]))
        assert (p.autopilot is None) == ("autopilot" not in e)


def test_the_real_profiles_decide_which_types_exist():
    pytest.importorskip("nomad_projects.profiles")
    assert projects.parse_type("p", "generic") == "generic"
    with pytest.raises(ConfigError, match=r"project 'p': type: "):
        projects.parse_type("p", "no-such-type")


# ---- the RuneMatch block ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def rm():
    return projects.get("runematch", REAL)


def test_runematch_tests_and_gate(rm):
    ap = rm.autopilot
    assert set(ap.test) == {"EditMode", "PlayMode"}
    assert ap.test["EditMode"].render("RuneMerge.Tests.X") == \
        "scripts/unity.sh test EditMode RuneMerge.Tests.X"
    assert ap.test["PlayMode"].render() == "scripts/unity.sh test PlayMode"
    assert [c.render() for c in ap.pre_merge_gate] == [
        "scripts/unity.sh compile",
        "scripts/unity.sh test EditMode",
        "scripts/unity.sh test PlayMode"]
    assert ap.milestone_gate == ap.pre_merge_gate and ap.build is None


def test_runematch_playmode_holds_the_lock_with_a_long_timeout(rm):
    ap = rm.autopilot
    play = ap.test["PlayMode"]
    assert play.locks == ("/tmp/runematch-playmode.lock",) and play.env == {"TIMEOUT": "2400"}
    assert ap.locks == ("/tmp/runematch-playmode.lock",)
    assert ap.test["EditMode"].locks == () and ap.pre_merge_gate[0].locks == ()


def test_runematch_flakes_known_reds_and_cleanup(rm):
    ap = rm.autopilot
    assert ap.flake_set == {"PWR3", "PWR2_2", "PWR2_5", "PWR2_7", "PWR2_6a", "AB5", "CH4",
                            "BLK6", "BLK2_6", "BLK2_8"}
    assert ap.is_flake("RuneMerge.Tests.BlockerTests.BLK6_OneHitPerResolution")
    assert ap.is_flake("BLK2_6")
    assert not ap.is_flake("RuneMerge.Tests.BlockerTests.BLK61_Other")
    assert not ap.is_flake("RuneMerge.Tests.PowerUpTests.PWR2_6b_Something")
    assert ap.known_failures == {"CV5a", "CV5b"}
    assert ap.is_known_failure("LevelCurveTests.CV5a_BossFractionsSitInTheSameBandAsTheRealm")
    assert [c.run for c in ap.post_test] == ["git checkout -- Assets/Tests/Screenshots/"]


def test_runematch_paths_and_denies(rm):
    ap = rm.autopilot
    assert rm.path.endswith("/runematch")
    assert ap.worktree_root == "/mnt/c/Users/josia/Projects/RuneMatch-wt"
    assert ap.deny_edit_globs == ("**/*.unity", "**/*.prefab", "**/AGENTS.md")
    assert ap.cap == 3 and ap.requires_gpu is False
    assert (ap.results, ap.results_files) == ("nunit", "Logs/test_*.xml")
    assert ap.regenerated == (
        "Assets/TextMesh Pro/Resources/Fonts & Materials/LiberationSans SDF - Fallback.asset",)
    assert ap.regenerated == (
        "Assets/TextMesh Pro/Resources/Fonts & Materials/LiberationSans SDF - Fallback.asset",)


# ── policies: attached to one project (#1373, PRD 6.5 layer 3) ──────────────────────────

def test_no_policies_is_none(tmp_path):
    f = _registry(tmp_path, "projects:\n  - {id: a, path: a}\n")
    assert projects.load(f)[0].policies == ()


def test_attached_policies_are_exposed_in_order(tmp_path):
    f = _registry(tmp_path, "projects:\n  - {id: g, path: g, policies: [engine-unity, docs-audit]}\n")
    assert projects.get("g", f).policies == ("engine-unity", "docs-audit")


@pytest.mark.parametrize("v,want", [
    ("engine-unity", r"policies: expected a list"),
    ("[no-such-policy]", r"policies\[0\]: no policy 'no-such-policy'"),
    ("['../x']", r"policies\[0\]: not a policy id"),
    ("[engine-unity, engine-unity]", r"policies\[1\]: 'engine-unity' is listed twice"),
])
def test_bad_policies_name_the_project_and_field(tmp_path, v, want):
    f = _registry(tmp_path, f"projects:\n  - {{id: g, path: g, policies: {v}}}\n")
    with pytest.raises(ConfigError, match=rf"project 'g': {want}"):
        projects.load(f)


def test_gate_network_none_parses(tmp_path):
    assert _one(tmp_path, "gate_network: none\n").autopilot.gate_network == "none"
