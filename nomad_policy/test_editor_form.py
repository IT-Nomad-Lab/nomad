"""#1397 · the cockpit editor's form writes YAML in the browser (toYaml in v2/cockpit_policies.py).
The claim: for every policy file in the repo, form model -> YAML -> schema.validate gives the same
policy as the file itself, so a save from the form never changes what a policy means.

The page's own functions run under node; the test skips when node is missing.

Run: uv run --no-project --with pytest --with pyyaml python -m pytest nomad_policy/test_editor_form.py -q
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

from nomad_policy import schema

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which("node")


def _page():
    spec = importlib.util.spec_from_file_location("cockpit_policies", os.path.join(HERE, "v2", "cockpit_policies.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.PAGE


def _js():
    """The page's Y, YV, toYaml and norm, as written."""
    page = _page()
    parts = [re.search(r"^const Y=.*$", page, re.M).group(0), re.search(r"^const YV=.*$", page, re.M).group(0)]
    for fn in ("toYaml", "norm"):
        m = re.search(r"^function " + fn + r"\(.*?^}$", page, re.M | re.S)
        parts.append(m.group(0))
    return "\n".join(parts)


def _run_form(raws):
    src = _js() + "\nconst raws=JSON.parse(require('fs').readFileSync(0,'utf8'));" \
                  "process.stdout.write(JSON.stringify(raws.map(r=>toYaml(norm(r)))));"
    out = subprocess.run([NODE, "-e", src], input=json.dumps(raws), capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.fixture(scope="module")
def reg():
    try:
        return schema.Registry.live()
    except Exception as e:                       # no hub checkout
        pytest.skip(f"no registry: {e}")


@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_every_policy_survives_the_form(reg):
    files = sorted(glob.glob(os.path.join(HERE, "policies", "**", "*.yaml"), recursive=True))
    raws = [yaml.safe_load(open(f)) for f in files]
    assert len(raws) >= 10
    for f, raw, text in zip(files, raws, _run_form(raws)):
        want = schema.validate(raw, reg, expect_id=raw["id"])
        got = schema.validate(yaml.safe_load(text), reg, expect_id=raw["id"])
        assert got == want, f"{os.path.relpath(f, HERE)} changed meaning through the form:\n{text}"


@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_awkward_strings_are_quoted(reg):
    raw = {"id": "t-quote", "role": "permission", "description": 'a: "colon", # hash\nand a newline',
           "statements": [{"sid": "s", "effect": "ask", "kind": "command", "match": ["^git (push|reset) .*: x$", "yes"],
                           "when": {"tier": "red", "lane": ["dev", "ops"]}}],
           "settings": {"cap": 2, "pre_merge_gate": [{"run": "make test", "env": {"A": "1"}}]}}
    text = _run_form([raw])[0]
    got = yaml.safe_load(text)
    assert got["description"] == raw["description"]
    assert got["statements"][0]["match"] == raw["statements"][0]["match"]
    assert got["statements"][0]["when"] == {"tier": ["red"], "lane": ["dev", "ops"]}
    assert got["settings"] == raw["settings"]
