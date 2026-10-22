"""#1378 · the Standards page's form writes profile YAML in the browser (toYaml in
v2/cockpit_standards.py). The claim: for every profile in the repo, form model -> YAML ->
profiles.validate gives the same Profile as the file itself, so a save from the form never changes
what a type means.

The page's own functions run under node; the test skips when node is missing.

Run: uv run --no-project --with pytest --with pyyaml python -m pytest nomad_projects/test_standards_form.py -q
"""
import glob
import importlib.util
import json
import os
import re
import shutil
import subprocess

import pytest
import yaml

from nomad_projects import profiles

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which("node")


def _page():
    spec = importlib.util.spec_from_file_location("cockpit_standards", os.path.join(HERE, "v2", "cockpit_standards.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.PAGE


def _js():
    """The page's Y, YV, toYaml and norm, as written."""
    page = _page()
    parts = [re.search(r"^const Y=.*$", page, re.M).group(0), re.search(r"^const YV=.*$", page, re.M).group(0)]
    for fn in ("toYaml", "norm"):
        parts.append(re.search(r"^function " + fn + r"\(.*?^}$", page, re.M | re.S).group(0))
    return "\n".join(parts)


def _run_form(raws):
    src = _js() + "\nconst raws=JSON.parse(require('fs').readFileSync(0,'utf8'));" \
                  "process.stdout.write(JSON.stringify(raws.map(r=>toYaml(norm(r)))));"
    out = subprocess.run([NODE, "-e", src], input=json.dumps(raws), capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.fixture(scope="module")
def pols():
    try:
        return profiles.load_policies()
    except Exception as e:                       # no hub checkout
        pytest.skip(f"no policies: {e}")


@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_every_profile_survives_the_form(pols):
    files = sorted(glob.glob(os.path.join(HERE, "profiles", "*.yaml")))
    raws = [yaml.safe_load(open(f)) for f in files]
    assert len(raws) >= 6
    for f, raw, text in zip(files, raws, _run_form(raws)):
        want = profiles.validate(raw, policies=pols, name=raw["id"])
        got = profiles.validate(yaml.safe_load(text), policies=pols, name=raw["id"])
        assert got == want, f"{os.path.relpath(f, HERE)} changed meaning through the form:\n{text}"


@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_awkward_values_are_quoted(pols):
    raw = {"id": "t-quote", "title": 'A: "colon"', "description": "# hash\nand a newline",
           "interview": {"ask": [{"id": "q", "question": "Yes or no?", "kind": "single", "options": ["no", "yes", "on"]}],
                         "skip": ["stack"]},
           "stack": {"language": "python", "versions": [3.12, "3.13"], "nested": {"a": True}},
           "agents": [{"role": "qa-test"}], "gates": {"pre_merge": ["make test: all", "npm run build --if-present"]},
           "files": ["README.md"], "autopilot": {"cap": 2, "model": "sonnet", "max_budget_usd": 1.5, "lanes": ["dev"]}}
    got = yaml.safe_load(_run_form([raw])[0])
    assert got == raw
    profiles.validate(got, policies=pols, name="t-quote")


# ---- #1379: a form save keeps the layout of what it did not change ---------------------------

@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_an_unchanged_form_saves_the_file_as_it_was(pols):
    from nomad_projects import profile_api as pa
    files = sorted(glob.glob(os.path.join(HERE, "profiles", "*.yaml")))
    texts = [open(f).read() for f in files]
    for f, text, form in zip(files, texts, _run_form([yaml.safe_load(t) for t in texts])):
        pid = os.path.basename(f)[:-5]
        assert pa._keep_layout(pid, text, form, pols) == text, os.path.relpath(f, HERE)


@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_a_form_edit_changes_only_its_lines(pols):
    from nomad_projects import profile_api as pa
    text = open(os.path.join(HERE, "profiles", "website.yaml")).read()
    raw = yaml.safe_load(text)
    raw["policies"] = [p for p in raw["policies"] if p != "design-read"]
    raw["title"] = "Web site"
    got = pa._keep_layout("website", text, _run_form([raw])[0], pols)
    changed = [ln for ln in pa._diff(text, got, "x").splitlines()[2:] if ln[:1] in "+-"]
    assert changed == ["-title: Website", '+title: "Web site"',
                       "-policies: [website-default, design-read, docs-audit]",
                       '+policies: ["website-default", "docs-audit"]'], changed
    assert got.startswith("# A website:")                                   # the header comment stays
    assert profiles.validate(yaml.safe_load(got), policies=pols, name="website") == \
        profiles.validate(raw, policies=pols, name="website")


def test_a_layout_that_would_change_the_meaning_falls_back_to_the_form(pols):
    from nomad_projects import profile_api as pa
    old = "id: website\ntitle: T\ndescription: D\npolicies: [design-read]\n"
    new = "id: website\ntitle: T\ndescription: D\npolicies: [docs-audit]\n"
    assert pa._keep_layout("website", old, new, pols) == new
    assert pa._keep_layout("website", None, new, pols) == new
    assert pa._keep_layout("website", "id: [\n", new, pols) == new
