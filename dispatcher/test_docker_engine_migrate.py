"""scripts/docker-engine-migrate.sh (#383): parses, and its `hosts` probe covers the stack.

Nothing here talks to Docker. Run: python3 -m pytest dispatcher/test_docker_engine_migrate.py -q
"""
import os
import re
import shutil
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "docker-engine-migrate.sh")


def _read(rel):
    with open(os.path.join(REPO, rel)) as f:
        return f.read()


@pytest.mark.skipif(not shutil.which("bash"), reason="no bash")
def test_script_parses():
    r = subprocess.run(["bash", "-n", SCRIPT], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(not shutil.which("bash"), reason="no bash")
def test_unknown_subcommand_prints_usage_and_touches_nothing():
    r = subprocess.run(["bash", SCRIPT], capture_output=True, text=True,
                       env={**os.environ, "PATH": "/usr/bin:/bin"})
    assert r.returncode == 2
    assert "export" in r.stdout and "import" in r.stdout


def test_hosts_probe_covers_every_host_docker_internal_port():
    """Each port a container reaches through host.docker.internal is in the probe list."""
    used = set()
    for rel in ("docker-compose.yml", "litellm/config.yaml", "n8n/self-dev-scheduler.workflow.json"):
        used |= {int(p) for p in re.findall(r"host\.docker\.internal:(\d+)", _read(rel))}
    probed = {int(p) for p in re.findall(r'"[^"]+": (\d+)', _read("scripts/docker-engine-migrate.sh"))}
    assert used - probed == set(), f"not probed: {sorted(used - probed)}"
